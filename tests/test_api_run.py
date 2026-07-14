"""``api_layer.run.RunService`` 逻辑分支单测（T-04·ADR-0024）。

验 fresh/resume 二态判定、参数互斥、会话所有权、``LOCK_EXIST`` / ``PAUSE_EXPIRED`` /
``SESSION_LIMIT`` / ``FORBIDDEN`` / ``PARAM_ERROR`` 的**判定逻辑**——这些分支在驱动图之前
发生，故用 :class:`InMemorySessionCache` + 默认 ``Orchestrator``（图不被触及）即可，无需 Postgres。
真正驱动图至 interrupt / 终态的集成测试见 :mod:`tests.test_api_http_integration`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from api_layer.errors import ApiError, ErrorCode
from api_layer.run import HumanResponse, RunRequest, RunService, RunServiceConfig
from api_layer.session_cache import InMemorySessionCache
from runtime.orchestrator import Orchestrator

_DOC = "主论点。\n\n分论点。\n\n论据。\n"


def _fixed() -> datetime:
    return datetime(2026, 7, 14, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def service() -> RunService:
    clock = _fixed
    # 共享 clock：cache 与 service 同源时间，过期判定一致。
    return RunService(
        Orchestrator(),
        InMemorySessionCache(clock=clock),
        config=RunServiceConfig(session_limit=2),
        clock=clock,
    )


# --------------------------------------------------------------------------- #
# 参数互斥 / 必填
# --------------------------------------------------------------------------- #


async def test_missing_user_id_forbidden(service: RunService) -> None:
    with pytest.raises(ApiError) as exc:
        await service.run(
            RunRequest(session_id="s1", query="改一改", document=_DOC), user_id=""
        )
    assert exc.value.code is ErrorCode.FORBIDDEN


async def test_missing_session_id_param_error(service: RunService) -> None:
    with pytest.raises(ApiError) as exc:
        await service.run(
            RunRequest(session_id="", query="改一改", document=_DOC), user_id="u1"
        )
    assert exc.value.code is ErrorCode.PARAM_ERROR


async def test_query_and_human_response_mutually_exclusive(service: RunService) -> None:
    with pytest.raises(ApiError) as exc:
        await service.run(
            RunRequest(
                session_id="s1",
                query="改一改",
                human_response=HumanResponse(action="skip"),
                document=_DOC,
            ),
            user_id="u1",
        )
    assert exc.value.code is ErrorCode.PARAM_ERROR


async def test_neither_query_nor_human_response_param_error(service: RunService) -> None:
    with pytest.raises(ApiError) as exc:
        await service.run(RunRequest(session_id="s1"), user_id="u1")
    assert exc.value.code is ErrorCode.PARAM_ERROR


async def test_fresh_without_document_param_error(service: RunService) -> None:
    with pytest.raises(ApiError) as exc:
        await service.run(
            RunRequest(session_id="s1", query="改一改"), user_id="u1"
        )
    assert exc.value.code is ErrorCode.PARAM_ERROR


# --------------------------------------------------------------------------- #
# 会话所有权（PRD §3.2）
# --------------------------------------------------------------------------- #


async def test_cross_user_access_forbidden(service: RunService) -> None:
    # u1 登记 s1（fresh 会先过所有权；这里直接预热登记）。
    await service._cache.set_session_owner("s1", "u1")
    with pytest.raises(ApiError) as exc:
        await service.run(
            RunRequest(
                session_id="s1",
                human_response=HumanResponse(action="skip"),
            ),
            user_id="u2",
        )
    assert exc.value.code is ErrorCode.FORBIDDEN


async def test_session_limit_reached(service: RunService) -> None:
    # session_limit=2：登记 2 个活跃会话后，第 3 个新会话 → SESSION_LIMIT。
    await service._cache.set_session_owner("s1", "u1")
    await service._cache.set_session_owner("s2", "u2")
    with pytest.raises(ApiError) as exc:
        await service.run(
            RunRequest(session_id="s3", query="改一改", document=_DOC),
            user_id="u3",
        )
    assert exc.value.code is ErrorCode.SESSION_LIMIT


# --------------------------------------------------------------------------- #
# fresh / resume 判定 + LOCK_EXIST / PAUSE_EXPIRED
# --------------------------------------------------------------------------- #


async def test_fresh_with_active_pause_is_lock_exist(service: RunService) -> None:
    await service._cache.set_pause_meta("s1", "t-old", "hitl1")
    with pytest.raises(ApiError) as exc:
        await service.run(
            RunRequest(session_id="s1", query="改一改", document=_DOC),
            user_id="u1",
        )
    assert exc.value.code is ErrorCode.LOCK_EXIST


async def test_fresh_with_expired_pause_is_pause_expired(service: RunService) -> None:
    await service._cache.set_pause_meta("s1", "t-old", "hitl1")
    later = _fixed() + timedelta(minutes=31)
    service._clock = lambda: later  # type: ignore[assignment]
    with pytest.raises(ApiError) as exc:
        await service.run(
            RunRequest(session_id="s1", query="改一改", document=_DOC),
            user_id="u1",
        )
    assert exc.value.code is ErrorCode.PAUSE_EXPIRED
    # 过期清理：pause_meta + lock 应被清。
    assert await service._cache.get_pause_meta("s1") is None


async def test_resume_without_pause_is_param_error(service: RunService) -> None:
    with pytest.raises(ApiError) as exc:
        await service.run(
            RunRequest(
                session_id="s1",
                human_response=HumanResponse(action="skip"),
            ),
            user_id="u1",
        )
    assert exc.value.code is ErrorCode.PARAM_ERROR


async def test_resume_with_expired_pause_is_pause_expired(service: RunService) -> None:
    await service._cache.set_pause_meta("s1", "t-old", "hitl1")
    later = _fixed() + timedelta(minutes=31)
    service._clock = lambda: later  # type: ignore[assignment]
    with pytest.raises(ApiError) as exc:
        await service.run(
            RunRequest(
                session_id="s1",
                human_response=HumanResponse(action="skip"),
            ),
            user_id="u1",
        )
    assert exc.value.code is ErrorCode.PAUSE_EXPIRED
