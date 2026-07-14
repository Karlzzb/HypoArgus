"""Shared pytest fixtures."""

from __future__ import annotations

import pytest
from dotenv import load_dotenv

# 加载仓库根 .env（DashScope / Langfuse / HYPOARGUS_PG_DSN 等）——仅本地、
# .gitignore 忽略。让 checkpointer 集成测试与 CLI 都能读 PG 连接串而不必手动 export。
load_dotenv()

# 一组覆盖各类边界形态的样例文档（bytes），用于分区不变式与字节级回写断言。
SAMPLE_DOCS: dict[str, bytes] = {
    "simple": b"First paragraph.\n\nSecond paragraph.\n",
    "blank_lines": b"\n\nLeading blanks.\n\n\nBetween.\n\nTrailing.\n\n\n",
    "indent": b"    indented para\n\n      deeper indent\n\nnormal\n",
    "list": b"- item one\n- item two\n\n- item three\n\nafter list\n",
    "code_fence": b"intro\n\n```python\nx = 1\n\ny = 2\n```\n\nafter code\n",
    "tilde_fence": b"intro\n\n~~~\nblank line inside\n\n~~~\n\ndone\n",
    "no_trailing_newline": b"para one\n\npara two",
    "trailing_spaces": b"line one.   \n\nline two.\n",
    "mixed": b"# Title\n\nintro paragraph.\n\n- bullet\n\n```python\ncode\n```\n\nFinal.\n",
    "only_blanks": b"\n\n\n",
    "single_line": b"only one paragraph no newline",
}


@pytest.fixture(params=list(SAMPLE_DOCS.items()), ids=list(SAMPLE_DOCS.keys()))
def sample_doc(request):
    name, doc = request.param
    return name, doc


# --------------------------------------------------------------------------- #
# Postgres checkpointer 集成测试夹具（T-03·ADR-0022）
#
# 共享 Postgres（ADR-0022：一期无需 Redis、持久化与跨进程续跑均由 Postgres 承担）。
# 读 HYPOARGUS_PG_DSN（.env 注入）；连接不可达即 skip——不阻塞离线纯函数测试。
# 每个 test 独占一个 saver / 连接、共用同一 PG 实例；各 test 用唯一 thread_id 避免碰撞。
# --------------------------------------------------------------------------- #


@pytest.fixture
async def pg_checkpointer():
    """产一个已 setup 的 :class:`AsyncPostgresSaver`（装配 ``HypoArgusSerializer``）。

    PG 不可达时 skip（不令集成测试在无 PG 环境失败）。
    """

    from runtime.checkpoint import CheckpointConfigError, build_async_checkpointer

    try:
        cm = build_async_checkpointer()
    except CheckpointConfigError as exc:
        pytest.skip(f"Postgres checkpointer 未配置：{exc}")
        raise  # pragma: no cover  # noqa: RET504 — mypy: pytest.skip 不返回
    try:
        async with cm as saver:
            try:
                await saver.setup()
            except Exception as exc:  # psycopg.OperationalError 等
                pytest.skip(f"Postgres 不可达：{exc}")
                return  # pragma: no cover
            yield saver
    finally:
        pass


# --------------------------------------------------------------------------- #
# Postgres SessionCache 集成测试夹具（T-04·ADR-0024）
#
# side-metadata 三表（pause_meta / session_owner / session_locks）落同一 Postgres
# （ADR-0022）。读 HYPOARGUS_PG_DSN（.env 注入）；不可达即 skip——不阻塞离线单测。
# 与 pg_checkpointer 共用同一 PG 实例；各 test 用唯一 session_id 避免碰撞。
# --------------------------------------------------------------------------- #


@pytest.fixture
async def pg_session_cache():
    """产一个已 setup 的 :class:`PostgresSessionCache`（side-meta 三表已建）。

    PG 不可达时 skip。
    """

    from api_layer.session_cache import PostgresSessionCache
    from runtime.checkpoint import CheckpointConfigError, resolve_pg_dsn

    try:
        dsn = resolve_pg_dsn()
    except CheckpointConfigError as exc:
        pytest.skip(f"Postgres SessionCache 未配置：{exc}")
        raise  # pragma: no cover  # noqa: RET504 — mypy: pytest.skip 不返回
    try:
        async with PostgresSessionCache(dsn) as cache:
            try:
                await cache.setup()
            except Exception as exc:  # psycopg.OperationalError 等
                pytest.skip(f"Postgres 不可达：{exc}")
                return  # pragma: no cover
            yield cache
    finally:
        pass


# --------------------------------------------------------------------------- #
# Postgres TraceEventStore 集成测试夹具（T-05·ADR-0023）
#
# trace_events 表落同一 Postgres（ADR-0022「一期无需 Redis」）。读 HYPOARGUS_PG_DSN
# （.env 注入）；不可达即 skip——不阻塞离线单测。与 pg_checkpointer / pg_session_cache
# 共用同一 PG 实例；各 test 用唯一 trace_id 避免碰撞（trace_events 跨 test 持久、不清理）。
# --------------------------------------------------------------------------- #


@pytest.fixture
async def pg_trace_store():
    """产一个已 setup 的 :class:`PostgresTraceEventStore`（trace_events 表已建）。

    PG 不可达时 skip。
    """

    from api_layer.trace_store import PostgresTraceEventStore
    from runtime.checkpoint import CheckpointConfigError, resolve_pg_dsn

    try:
        dsn = resolve_pg_dsn()
    except CheckpointConfigError as exc:
        pytest.skip(f"Postgres trace store 未配置：{exc}")
        raise  # pragma: no cover  # noqa: RET504 — mypy: pytest.skip 不返回
    try:
        async with PostgresTraceEventStore(dsn) as store:
            try:
                await store.setup()
            except Exception as exc:  # psycopg.OperationalError 等
                pytest.skip(f"Postgres 不可达：{exc}")
                return  # pragma: no cover
            yield store
    finally:
        pass
