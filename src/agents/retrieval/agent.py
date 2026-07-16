"""检索 Agent 编排：真实适配器 ``real_retrieval`` + daemon worker loop + 延迟单例。

PRD §Q4（loop-affine 纠偏）：vendored V12 的 ``VolcanoWebSearchClient`` /
``BishengRetrieveClient`` 首次请求即绑当时 loop（loop-affine）。故「singleton runtime + 每次节点
调用 ``asyncio.run(runtime.ainvoke(...))``」会坏——spine 里 ``create_real_agents`` 跑在
uvicorn loop、client 绑之；retrieval 节点在 LangGraph threadpool worker 无 loop、``asyncio.run``
开新 loop → ``attached to a different loop``。

决议（(i) 零框架改动）：适配器自持一条**专用长驻 daemon worker event loop**（独立线程
``loop.run_forever()``），runtime 在 worker loop 上建单例（httpx client 首请求即绑 worker loop）；
retrieval 节点（同步 ``NodeFn``、threadpool、无 loop）经
``asyncio.run_coroutine_threadsafe(runtime.ainvoke(payload), worker_loop).result(timeout)``
同步桥接——非-loop 线程向长驻 loop 投递协程的正典，``NodeFn`` 类型不动。

spine（长驻 uvicorn）只建一次 agents → runtime 进程级单例、跨所有请求复用（无 per-request
泄漏、无 loop-bind 炸点）。关闭：worker 线程作 daemon、进程退出即死、OS 回收 socket
（与既有 ``build_qwen_chat_model()`` 不 ``aclose`` 先例同形）；可选 ``atexit`` 尽力 ``aclose()``
（**住在适配器、不碰框架**）。
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from functools import partial
from typing import Any, cast

from search_agent.evidence_retrieval.public_contracts import SearchAgentOutputState

from agents.retrieval.contract import (
    FakeSearchAgentRuntime,
    RetrievalRuntime,
    build_search_agent_payload,
    map_citations,
)
from domain import (
    Argument,
    Hypothesis,
    ParagraphRecord,
    SessionContext,
    TimeRange,
)
from infra.retrieval import Source

__all__ = [
    "real_retrieval",
    "build_real_retrieval",
    "lazy_search_agent_runtime",
    "RetrievalRuntime",
    "FakeSearchAgentRuntime",
    "map_citations",
    "build_search_agent_payload",
]


# --------------------------------------------------------------------------- #
# daemon worker event loop（进程级单例、独立线程）
# --------------------------------------------------------------------------- #

_WORKER_LOOP: asyncio.AbstractEventLoop | None = None
_WORKER_THREAD: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()


def _get_worker_loop() -> asyncio.AbstractEventLoop:
    """返回专用长驻 daemon worker event loop（首调惰性起独立线程 ``run_forever``）。

    非-loop 线程（LangGraph threadpool worker / 测试主线程）经
    :func:`asyncio.run_coroutine_threadsafe` 向本 loop 投递协程、同步 ``.result(timeout)`` 桥接。
    线程作 daemon、进程退出即死、OS 回收 socket（不碰框架 shutdown 钩子）。
    """

    global _WORKER_LOOP, _WORKER_THREAD
    if _WORKER_LOOP is not None and not _WORKER_LOOP.is_closed():
        return _WORKER_LOOP
    with _WORKER_LOCK:
        if _WORKER_LOOP is not None and not _WORKER_LOOP.is_closed():
            return _WORKER_LOOP
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_forever,
            name="hypoargus-retrieval-worker",
            daemon=True,
        )
        thread.start()
        _WORKER_LOOP = loop
        _WORKER_THREAD = thread
        return loop


# --------------------------------------------------------------------------- #
# 延迟单例 proxy：在 worker loop 上建 vendored SearchAgentRuntime（生产 runtime）
# --------------------------------------------------------------------------- #


class _LazySearchAgentRuntime:
    """生产 :class:`RetrievalRuntime`：首 ``ainvoke`` 在 worker loop 上延迟建
    ``SearchAgentRuntime.from_env(with_llm=False)`` 单例、跨请求复用。

    ``from_env(with_llm=False)`` 走 ``EvidenceRetrievalDependencies.defaults``：构造四个
    httpx client（首请求绑 worker loop）+ 确定性 ``DeterministicEvidenceJudge``、不调 LLM、
    不需 VOLCANO/BISHENG 凭证即可构造（凭据校验在请求期才触发）。本 proxy 的 ``ainvoke``
    经 :func:`run_coroutine_threadsafe` 在 worker loop 上执行——runtime 构造与首请求都在
    worker loop、httpx client 绑之，无 loop-bind 炸点。

    丢弃 ``TaskDecision.verdict``（Q1：judgment 重判、无双倍 LLM 成本）——``map_citations``
    不读 verdict。
    """

    def __init__(self) -> None:
        self._runtime: Any = None
        self._lock = threading.Lock()

    async def ainvoke(self, payload: Any) -> dict[str, Any]:
        from search_agent.api import SearchAgentRuntime

        runtime = self._runtime
        if runtime is None:
            with self._lock:
                runtime = self._runtime
                if runtime is None:
                    # 构造在 worker loop（本 ainvoke 即跑在 worker loop 上）。
                    runtime = SearchAgentRuntime.from_env(with_llm=False)
                    self._runtime = runtime
        return cast(dict[str, Any], await runtime.ainvoke(payload))

    async def aclose(self) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        await runtime.aclose()
        self._runtime = None


_LAZY_RUNTIME: _LazySearchAgentRuntime | None = None


def lazy_search_agent_runtime() -> RetrievalRuntime:
    """返回进程级单例延迟 proxy（生产 runtime holder、PRD §Q4 进程级单例）。

    供 :func:`create_real_agents` 的 ``retrieval_runtime`` 注入（spine / CLI 同源）；多次调用
    返回同一实例→ runtime 跨所有请求复用、无 per-request 泄漏。
    """

    global _LAZY_RUNTIME
    if _LAZY_RUNTIME is None:
        _LAZY_RUNTIME = _LazySearchAgentRuntime()
    return _LAZY_RUNTIME


# --------------------------------------------------------------------------- #
# 同步桥接：async runtime.ainvoke ← 经 worker loop → 同步结果
# --------------------------------------------------------------------------- #


def _invoke_sync(runtime: RetrievalRuntime, payload: Any, *, timeout: float = 120.0) -> Any:
    """在调用方无运行 loop 的前提下，经 worker loop 同步拿到 ``runtime.ainvoke`` 结果。

    正典 ``run_coroutine_threadsafe``：把 async ``ainvoke`` 投递到长驻 worker loop、本线程
    阻塞 ``.result(timeout)``。调用方（LangGraph threadpool worker / 测试主线程）无运行 loop
    亦不炸——不取 ``asyncio.run``（会开新 loop、与 loop-affine client 冲突）。
    """

    loop = _get_worker_loop()
    future = asyncio.run_coroutine_threadsafe(runtime.ainvoke(payload), loop)
    return future.result(timeout=timeout)


# --------------------------------------------------------------------------- #
# 真实 RetrievalFn 适配器（实现 agents.assembly.RetrievalFn 5 输入 Protocol）
# --------------------------------------------------------------------------- #


def real_retrieval(
    argument_tree: list[Argument],
    hypotheses: dict[str, list[Hypothesis]],
    query_time_range: TimeRange,
    session_context: SessionContext,
    paragraph_list: list[ParagraphRecord],
    *,
    runtime: RetrievalRuntime,
) -> dict[str, list[Source]]:
    """真实检索 ``RetrievalFn``（5 输入、实现 :class:`agents.assembly.RetrievalFn`）。

    流程：per-段构造 ``SearchAgentInputState``（``build_search_agent_payload``、含脱敏 + id 映射）
    → 经 worker loop 同步桥接 ``runtime.ainvoke`` → ``map_citations`` 汇总为
    ``dict[str, list[Source]]``（key=item_id=argument_id/hypothesis_id）。``query_time_range``
    被 V12 自有 scope_guard 承载（语义 scope、非域名白名单），本适配器不另传。

    异常由 :func:`agents.assembly._guarded` 兜底为空 citations + 日志向前（retrieval 节点 build
    闭包）；真实后端未配置 / 未触达任何段时无 citations → tracer-bullet 字节级承诺继续成立。
    """

    _ = query_time_range  # 贯穿背景；V12 scope_guard 自承、不另传。
    payloads = build_search_agent_payload(
        argument_tree, hypotheses, session_context, paragraph_list
    )
    citations: dict[str, list[Source]] = {}
    for payload in payloads:
        output_dict = _invoke_sync(runtime, payload)
        output = SearchAgentOutputState.model_validate(output_dict)
        for item_id, sources in map_citations(output).items():
            citations.setdefault(item_id, []).extend(sources)
    return citations


def build_real_retrieval(runtime: RetrievalRuntime) -> Any:
    """返回绑定 runtime 的 ``RetrievalFn``（``partial(real_retrieval, runtime=...)`` 形）。

    供 MANIFEST retrieval ``real`` 工厂（``partial(_real_retrieval, runtime=d.retrieval_runtime)``
    同 judgment ``partial(judge_and_adjudicate, llm=d.judgment_llm)`` 形）与测试 ``replace`` 注入。
    """

    return partial(real_retrieval, runtime=runtime)


# --------------------------------------------------------------------------- #
# atexit 尽力 aclose（住在适配器、不碰框架）
# --------------------------------------------------------------------------- #


def _atexit_aclose() -> None:
    """进程退出尽力 ``aclose`` runtime + stop worker loop（best-effort、不抛）。"""

    global _LAZY_RUNTIME
    lazy = _LAZY_RUNTIME
    loop = _WORKER_LOOP
    if lazy is not None and loop is not None and not loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(lazy.aclose(), loop).result(timeout=5.0)
        except Exception:  # noqa: BLE001 — atexit best-effort、绝不抛
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:  # noqa: BLE001
            pass


atexit.register(_atexit_aclose)
