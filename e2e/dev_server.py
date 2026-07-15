"""E2E 确定性后端（T-07）——镜像 ``api_layer.server.serve()``，但：

- 四条 LLM seam 换成 ``e2e/_fakes.py`` 的 :class:`StreamingFakeChat`（逐字符流式吐 schema
  默认 JSON → ``astream_events`` 产 ``llm_thinking``，前端实时 CoT 数据源）；
- side metadata（session_cache / trace_store）用 InMemory（E2E 单进程即可，断连重放由
  InMemory ``trace_events`` 承载）；
- checkpointer 用真实 Postgres（``interrupt`` 续跑所需，ADR-0022；``.env`` 已配 PG）；
- 可见性全可见（``VisibilityConfig()``），令 ``parse+partition`` 节点可见、其 LLM 流式产
  ``llm_thinking``；生产用仓根 ``config/visibility.yaml``（CoT 来自可见下游节点）。

无网络 / 无 token 消耗、确定性。供 Playwright E2E 经 Vite 代理访问（默认端口 8001）。
启动：``PYTHONPATH=src conda run -n HypoArgus python e2e/dev_server.py``
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import uvicorn
from _fakes import StreamingFakeChat
from dotenv import load_dotenv

from agents.assembly import MANIFEST, create_real_agents
from api_layer.app import create_app
from api_layer.graph_view import VisibilityConfig
from api_layer.run import RunService, RunServiceConfig
from api_layer.session_cache import InMemorySessionCache
from api_layer.trace_store import InMemoryTraceEventStore
from api_layer.ws import WSSenderService
from infra.llm_adapters import (
    QwenHypothesisLlmClient,
    QwenJudgmentLlmClient,
    QwenParseLlmClient,
    QwenRewriteLlmClient,
)
from runtime.checkpoint import build_async_checkpointer
from runtime.gates import InterruptHitl1Gate, InterruptHitl2Gate
from runtime.orchestrator import Orchestrator

__all__ = ["serve", "main"]


def _fake_agents() -> Any:
    """四 LLM seam 全换 StreamingFakeChat + InterruptHitl*Gate（与生产同构）。"""

    fake = StreamingFakeChat()
    return create_real_agents(
        llm=QwenParseLlmClient(fake),
        hitl1_gate=InterruptHitl1Gate(),
        hypothesis_llm=QwenHypothesisLlmClient(fake),
        judgment_llm=QwenJudgmentLlmClient(fake),
        rewrite_llm=QwenRewriteLlmClient(fake),
        hitl2_gate=InterruptHitl2Gate(),
    )


async def serve(host: str = "127.0.0.1", port: int = 8001) -> None:
    """长持所有依赖并跑 ``uvicorn``（E2E 入口）。"""

    # 仓根 .env（HYPOARGUS_PG_DSN 等）——非 pytest 运行时也加载，使 checkpointer 可连 PG。
    load_dotenv()
    async with build_async_checkpointer() as saver:
        await saver.setup()
        session_cache = InMemorySessionCache()
        trace_store = InMemoryTraceEventStore()
        await session_cache.setup()
        await trace_store.setup()
        orch = Orchestrator(agents=_fake_agents(), checkpointer=saver)
        visibility = VisibilityConfig()
        run_service = RunService(
            orch,
            session_cache,
            trace_store=trace_store,
            visibility=visibility,
            config=RunServiceConfig(),
        )
        ws_service = WSSenderService(
            session_cache,
            trace_store,
            manifest=MANIFEST,
            visibility=visibility,
        )
        # visibility_path=None → GET /api/agent/graph 与 WSSenderService graph_static 均全可见。
        app = create_app(run_service, ws_service=ws_service, visibility_path=None)
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, loop="asyncio"))
        await server.serve()


def main() -> None:
    port = int(os.environ.get("HYPOARGUS_E2E_PORT", "8001"))
    host = os.environ.get("HYPOARGUS_E2E_HOST", "127.0.0.1")
    asyncio.run(serve(host=host, port=port))


if __name__ == "__main__":
    main()
