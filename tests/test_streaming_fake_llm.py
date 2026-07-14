"""E2E 确定性 streaming fake 验证（T-07）——确认流式 fake 经 astream_events 产 llm_thinking。

复用 T-04 的 InterruptHitl*Gate + AsyncPostgresSaver 图，把四条 LLM seam 换成
``e2e/_fakes.py`` 的 :class:`StreamingFakeChat`（逐字符流式吐 schema 默认 JSON）。
可见性用全可见（``VisibilityConfig()``），令 ``parse+partition`` 节点可见、其 LLM 调用
经 ``astream_events`` 产 ``on_chat_model_stream`` → 翻译层产 ``llm_thinking``
（前端实时 CoT 数据源）。断言一次 fresh run 后 trace_events 含 ``LLM_THINKING`` 且
首次暂停在 ``hitl1``。
"""

from __future__ import annotations

import pathlib
import sys
import uuid
from typing import Any

# e2e/ 不在 src 路径，按需挂载。
_E2E = pathlib.Path(__file__).resolve().parents[1] / "e2e"
if str(_E2E) not in sys.path:
    sys.path.insert(0, str(_E2E))

from _fakes import StreamingFakeChat  # noqa: E402

from agents.assembly import create_real_agents  # noqa: E402
from api_layer.graph_view import VisibilityConfig  # noqa: E402
from api_layer.run import RunRequest, RunService, RunServiceConfig  # noqa: E402
from api_layer.session_cache import InMemorySessionCache  # noqa: E402
from api_layer.trace_store import EventType, InMemoryTraceEventStore  # noqa: E402
from infra.llm_adapters import (  # noqa: E402
    QwenHypothesisLlmClient,
    QwenJudgmentLlmClient,
    QwenParseLlmClient,
    QwenRewriteLlmClient,
)
from runtime.gates import InterruptHitl1Gate, InterruptHitl2Gate  # noqa: E402
from runtime.orchestrator import Orchestrator  # noqa: E402

_DOC = "主论点。\n\n分论点。\n\n论据。\n".encode()


def _streaming_agents() -> Any:
    fake = StreamingFakeChat()
    return create_real_agents(
        llm=QwenParseLlmClient(fake),
        hitl1_gate=InterruptHitl1Gate(),
        hypothesis_llm=QwenHypothesisLlmClient(fake),
        judgment_llm=QwenJudgmentLlmClient(fake),
        rewrite_llm=QwenRewriteLlmClient(fake),
        hitl2_gate=InterruptHitl2Gate(),
    )


async def test_streaming_fake_emits_llm_thinking(pg_checkpointer: Any) -> None:
    orch = Orchestrator(agents=_streaming_agents(), checkpointer=pg_checkpointer)
    session_cache = InMemorySessionCache()
    trace_store = InMemoryTraceEventStore()
    await session_cache.setup()
    await trace_store.setup()
    service = RunService(
        orch,
        session_cache,
        trace_store=trace_store,
        visibility=VisibilityConfig(),
        config=RunServiceConfig(),
    )

    sid = f"e2e-{uuid.uuid4()}"
    res = await service.run(
        RunRequest(session_id=sid, query="修订", document=_DOC.decode()),
        user_id="u1",
    )
    # 空 proposals → 全 background → hitl1 仍首次暂停（partition 确认闸门）。
    assert res.status == "NEED_HUMAN_INPUT"
    assert res.node_id == "hitl1"

    events = await trace_store.events_for_session(sid)
    types = [e.event_type for e in events]
    assert EventType.TRACE_START in types
    assert EventType.LLM_THINKING in types, f"no llm_thinking in {types}"
    assert EventType.HUMAN_PAUSE in types
