"""HypoArgus 控制面 + WS-sender 装配入口（T-06·ADR-0022 / ADR-0023）。

把 T-03 checkpointer、T-04 控制面（:class:`RunService`）、T-05 翻译层 + ``trace_events``、
T-06 WS-sender（:class:`WSSenderService`）装进一个 FastAPI 应用，经 ``uvicorn`` 跑起：

- ``POST /api/agent/run``（fresh / resume）
- ``GET /api/agent/graph``
- ``WS /ws/agent/stream?session_id=…``（只读尾随 ``trace_events``，WS 断开不中止 run）

复用 :func:`runtime.run_real.create_real_agents` 的真实 LLM adapter + ``InterruptHitl*Gate``
（一期 retrieval 桩、judgment 空裁决 → 无触达段 → 终稿逐字节原文；真实后端 Out of Scope）。

用法（``websockets`` / ``wscat`` 客户端即可演示 WS 尾随）::

    export DASHSCOPE_API_KEY=...
    conda run -n HypoArgus python -m api_layer.server
    # 另一终端：发起 run + 尾随 WS
    curl -X POST http://127.0.0.1:8000/api/agent/run -H 'X-User-Id: u1' \\
        -H 'Content-Type: application/json' \\
        -d '{"session_id":"s1","query":"改","document":"..."}'
    wscat -c 'ws://127.0.0.1:8000/ws/agent/stream?session_id=s1' -H 'X-User-Id: u1'

所有控制面 / 显示层依赖落同一 Postgres（``HYPOARGUS_PG_DSN``，ADR-0022「一期无需 Redis」）。
T-08 运维加固（``/health`` / ``/metrics`` / 扫孤儿 / 超时 / 上限 / 脱敏）在后续切片扩展本入口。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import uvicorn

from agents.assembly import MANIFEST, create_real_agents
from agents.retrieval import lazy_search_agent_runtime
from api_layer.app import create_app, default_visibility_path
from api_layer.graph_view import load_visibility
from api_layer.langfuse_wrap import wrap_langfuse_handler
from api_layer.logging_setup import configure_logging
from api_layer.metrics import OpsMetrics
from api_layer.ops import OpsService
from api_layer.redaction import Redactor, default_redaction_config
from api_layer.run import RunService, RunServiceConfig
from api_layer.session_cache import PostgresSessionCache
from api_layer.trace_store import PostgresTraceEventStore
from api_layer.ws import WSSenderService
from infra.llm_adapters import (
    QwenHypothesisLlmClient,
    QwenJudgmentLlmClient,
    QwenParseLlmClient,
    QwenRewriteLlmClient,
)
from infra.llm_provider import build_qwen_chat_model
from infra.observability import build_langfuse_callback
from runtime.checkpoint import build_async_checkpointer, resolve_pg_dsn
from runtime.gates import InterruptHitl1Gate, InterruptHitl2Gate
from runtime.orchestrator import Orchestrator

__all__ = ["serve", "main"]

_logger = logging.getLogger(__name__)


def _real_agents() -> Any:
    """真实四 LLM seam + ``InterruptHitl*Gate`` + 真实检索后端（与
    :func:`runtime.run_real.arun_real_pipeline` 同源；retrieval runtime 进程级单例、Slice 2）。"""

    chat = build_qwen_chat_model()
    return create_real_agents(
        llm=QwenParseLlmClient(chat),
        hitl1_gate=InterruptHitl1Gate(),
        hypothesis_llm=QwenHypothesisLlmClient(chat),
        judgment_llm=QwenJudgmentLlmClient(chat),
        rewrite_llm=QwenRewriteLlmClient(chat),
        hitl2_gate=InterruptHitl2Gate(),
        retrieval_runtime=lazy_search_agent_runtime(),  # Slice 2：spine 进程级单例、daemon worker loop 承载、跨所有请求复用。
    )


async def _sweep_loop(ops: OpsService, interval: float) -> None:
    """后台 sweep 循环（PRD §9 / §11）：周期清扫孤儿锁 / 过期 pause / 80% 告警。

    单实例一期（PRD §4.4 跨实例扇出属二期）。异常不退出循环——单次 sweep 失败降级记错、
    下个周期继续，保证后台常驻。
    """

    while True:
        await asyncio.sleep(interval)
        try:
            await ops.sweep()
        except Exception:  # noqa: BLE001 — 后台循环须常驻、不因单次失败退出
            _logger.exception("sweep 单次失败——降级、下周期继续")


async def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """长持所有依赖并跑 ``uvicorn``（生产入口）。

    checkpointer / session_cache / trace_store 各自 ``async with`` 持连接池作用域，在作用域内
    构造 :class:`RunService` + :class:`WSSenderService` + :class:`OpsService` + app 并 serve 请求
    （PRD §10.3 单例）。T-08：装配结构化 JSON 日志、Langfuse 计数代理、脱敏钩子、后台 sweep 循环、
    ``/health`` + ``/metrics`` 端点。
    """

    configure_logging()  # T-08：结构化 JSON 日志（默认脱敏关，PRD §3.3）
    metrics = OpsMetrics()
    redactor = Redactor(default_redaction_config())
    langfuse_handler = wrap_langfuse_handler(build_langfuse_callback(), metrics)

    async with (
        build_async_checkpointer() as saver,
        PostgresSessionCache(resolve_pg_dsn()) as session_cache,
        PostgresTraceEventStore(resolve_pg_dsn()) as trace_store,
    ):
        await saver.setup()
        orch = Orchestrator(agents=_real_agents(), checkpointer=saver)
        vis_path = default_visibility_path()
        visibility = load_visibility(vis_path)  # 缺文件 → 全可见（VisibilityConfig()）
        run_service = RunService(
            orch,
            session_cache,
            trace_store=trace_store,
            langfuse_handler=langfuse_handler,
            visibility=visibility,
            config=RunServiceConfig(),
            metrics=metrics,
            redactor=redactor,
        )
        ws_service = WSSenderService(
            session_cache,
            trace_store,
            manifest=MANIFEST,
            visibility=visibility,
        )
        ops = OpsService(
            session_cache,
            trace_store,
            metrics=metrics,
            ws_service=ws_service,
            run_service=run_service,
        )
        sweep_task = asyncio.create_task(
            _sweep_loop(ops, ops.config.sweep_interval_seconds)
        )
        try:
            app = create_app(
                run_service,
                ws_service=ws_service,
                visibility_path=vis_path,
                ops_service=ops,
            )
            server = uvicorn.Server(
                uvicorn.Config(app, host=host, port=port, loop="asyncio")
            )
            await server.serve()
        finally:
            sweep_task.cancel()
            try:
                await sweep_task
            except (asyncio.CancelledError, Exception):
                pass


def main() -> None:
    """CLI 入口（``python -m api_layer.server``）。"""

    import asyncio

    asyncio.run(serve())


if __name__ == "__main__":
    main()
