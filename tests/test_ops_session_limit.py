"""SESSION_LIMIT 单测（T-08·PRD §9.7 验收）。

``session_owner`` 近 30min 计数达上限且无法淘汰 → 新会话 ``SESSION_LIMIT``（429）。
逻辑属 T-04 惰性路径（:meth:`RunService._enforce_ownership`）；本测试覆盖 T-08 验收清单。
"""

from __future__ import annotations

from typing import Any

import httpx

from api_layer.app import create_app
from api_layer.run import RunService, RunServiceConfig
from api_layer.session_cache import InMemorySessionCache
from runtime.orchestrator import Orchestrator


class _FastGraph:
    """伪图：``astream_events`` 空、``aget_state`` 返回终态。"""

    next: tuple[str, ...] = ()
    values: dict[str, Any] = {"final_document": b"done", "errors": []}
    tasks: tuple[Any, ...] = ()

    async def astream_events(self, *_a: Any, **_k: Any) -> Any:
        if False:  # pragma: no cover  # noqa: RET503
            yield

    async def aget_state(self, *_a: Any, **_k: Any) -> Any:
        return self


async def test_session_limit_rejects_new_session() -> None:
    """活跃会话数达上限（=1）→ 新会话 fresh → 429 SESSION_LIMIT。"""

    orch = Orchestrator()
    orch.graph = _FastGraph()
    cache = InMemorySessionCache()
    service = RunService(
        orch,
        cache,
        config=RunServiceConfig(session_limit=1),
    )
    # 先占满唯一名额。
    await cache.set_session_owner("s0", "u0")
    app = create_app(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/agent/run",
            json={"session_id": "s1", "query": "改", "document": "doc"},
            headers={"X-User-Id": "u1"},
        )
        assert r.status_code == 429
        assert r.json()["error"] == "SESSION_LIMIT"
