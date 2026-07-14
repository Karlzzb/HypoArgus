"""PostgresSaver checkpointer 装配 + ``OriginalParagraphs`` 序列化编解码器
（T-03·ADR-0022）。

ADR-0022 把图从「同步注入、无 checkpointer」改造为 ``interrupt()`` + ``PostgresSaver``
持久化 + ``ainvoke`` resume 驱动。本模块承载侵入面 #2（orchestrator 装配层）的两件
存储层事务：

1. **DSN 解析**（:func:`resolve_pg_dsn`）：从 ``HYPOARGUS_PG_DSN``（或
   ``HYPOARGUS_PG_HOST/PORT/USER/PASSWORD/DB`` 分项）解析 Postgres 连接串；未配置即
   抛 :class:`CheckpointConfigError`，绝不硬编码连接信息。
2. **``OriginalParagraphs`` 编解码器**（:class:`HypoArgusSerializer`）：默认
   :class:`langgraph.checkpoint.serde.jsonplus.JsonPlusSerializer` 的 msgpack 编码器
   ``_msgpack_default`` 不认 ``OriginalParagraphs``（slots + ``MappingProxyType`` +
   ``bytes`` value），末尾抛 ``TypeError``。本子类在 ``dumps_typed`` 顶层把
   ``OriginalParagraphs`` 摊成带哨兵键的纯数据信封（``order`` + ``entries``），
   其余值原样委托父类；``loads_typed`` 据哨兵键还原。``OriginalParagraphs`` 在
   ``PipelineState`` 中仅作为顶层 ``original_paragraphs`` channel 值出现（不嵌套于
   任何 pydantic 模型 / dict），故顶层变换充分。

:func:`build_async_checkpointer` 据 DSN 产 :class:`AsyncPostgresSaver`（async 上下文
管理器；``ainvoke`` / ``aget_state`` 需 async checkpointer——同步 ``PostgresSaver``
的 ``aget_tuple`` 抛 ``NotImplementedError``），并装配 :class:`HypoArgusSerializer`。
调用方在 ``async with`` 作用域内持有 saver 期间驱动图（PRD §10.3 全局单例：一个
驱动者一个 saver，禁止每请求重建）。
"""

from __future__ import annotations

import os
from contextlib import AbstractAsyncContextManager
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from original_paragraphs import OriginalParagraphs
from partition import Paragraph

__all__ = [
    "CheckpointConfigError",
    "HypoArgusSerializer",
    "resolve_pg_dsn",
    "build_async_checkpointer",
]

#: 信封哨兵键：``OriginalParagraphs`` 经 ``dumps_typed`` 摊成以此键为唯一键的 dict，
#: 读回时据此键还原。键名带版本号 ``_v1`` 以便未来演进（旧 checkpoint 可识别）。
_DECODE_TAG = "__hypoargus_original_paragraphs_v1__"


class CheckpointConfigError(RuntimeError):
    """Postgres checkpointer 连接配置缺失 / 非法。"""


class HypoArgusSerializer(JsonPlusSerializer):
    """为 ``OriginalParagraphs`` 注册自定义编解码的 ``JsonPlusSerializer`` 子类。

    仅顶层处理 ``OriginalParagraphs``（``original_paragraphs`` channel 值，不嵌套）：
    encode 摊成 ``{_DECODE_TAG: {"order": [pid...], "entries": {pid: bytes}}}`` 纯数据
    信封、委托父类 msgpack 编码；decode 据信封还原为 ``OriginalParagraphs``（经公共
    ``OriginalParagraphs([Paragraph(...)])`` 构造器，不改 ``OriginalParagraphs`` 自身）。
    其余 state 值（pydantic 模型 / bytes / dict / 原生）原样委托父类——
    :class:`JsonPlusSerializer` 的 ``_msgpack_default`` 已覆盖 ``Argument`` /
    ``Hypothesis`` / ``SessionContext`` / ``TimeRange`` / ``Source``（pydantic v2 ext）。
    """

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        return super().dumps_typed(_encode_original_paragraphs(obj))

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        return _decode_original_paragraphs(super().loads_typed(data))


def _encode_original_paragraphs(obj: Any) -> Any:
    """``OriginalParagraphs`` → 纯数据信封；其余原样返回（委托父类）。"""

    if isinstance(obj, OriginalParagraphs):
        order = list(obj.paragraph_ids())
        return {
            _DECODE_TAG: {
                "order": order,
                "entries": {pid: op_get(obj, pid) for pid in order},
            }
        }
    return obj


def op_get(op: OriginalParagraphs, pid: str) -> bytes:
    """``OriginalParagraphs.get`` 的模块级别名（供 dict 推导调用、避免闭包）。"""

    return op.get(pid)


def _decode_original_paragraphs(obj: Any) -> Any:
    """信封 dict → ``OriginalParagraphs``；形状不符则原样返回（不误吞普通 dict）。"""

    if not isinstance(obj, dict) or len(obj) != 1 or _DECODE_TAG not in obj:
        return obj
    payload = obj[_DECODE_TAG]
    # 形状自检：order 为非空 list、entries 为 dict 且 key 全覆盖 order——否则视为
    # 偶然同形的普通 dict，原样返回（绝不因形状不符而抛、污染读回路径）。
    if not isinstance(payload, dict):
        return obj
    order = payload.get("order")
    entries = payload.get("entries")
    if not isinstance(order, list) or not isinstance(entries, dict):
        return obj
    if not order or not all(pid in entries for pid in order):
        return obj
    paragraphs = [
        Paragraph(paragraph_id=pid, content=entries[pid])
        for pid in order
    ]
    return OriginalParagraphs(paragraphs)


def resolve_pg_dsn(conn_string: str | None = None) -> str:
    """解析 Postgres 连接串：显式入参 > ``HYPOARGUS_PG_DSN`` > 分项组合 > 抛错。

    分项组合读 ``HYPOARGUS_PG_HOST`` / ``_PORT`` / ``_USER`` / ``_PASSWORD`` / ``_DB``
    （DB 缺省 ``postgres``）。任一配置缺失即抛 :class:`CheckpointConfigError`——
    连接信息绝不硬编码进仓库。
    """

    if conn_string:
        return conn_string
    dsn = os.environ.get("HYPOARGUS_PG_DSN")
    if dsn:
        return dsn
    host = os.environ.get("HYPOARGUS_PG_HOST")
    if host:
        port = os.environ.get("HYPOARGUS_PG_PORT", "5432")
        user = os.environ.get("HYPOARGUS_PG_USER", "postgres")
        password = os.environ.get("HYPOARGUS_PG_PASSWORD", "")
        db = os.environ.get("HYPOARGUS_PG_DB", "postgres")
        if password:
            return f"postgresql://{user}:{password}@{host}:{port}/{db}?sslmode=prefer"
        return f"postgresql://{user}@{host}:{port}/{db}?sslmode=prefer"
    raise CheckpointConfigError(
        "Postgres checkpointer 未配置：设 HYPOARGUS_PG_DSN"
        "（或 HYPOARGUS_PG_HOST/PORT/USER/PASSWORD/DB）。见 .env。"
    )


def build_async_checkpointer(
    conn_string: str | None = None,
    *,
    serde: SerializerProtocol | None = None,
) -> AbstractAsyncContextManager[AsyncPostgresSaver]:
    """据 DSN 产 :class:`AsyncPostgresSaver`（async 上下文管理器），装配
    :class:`HypoArgusSerializer`。

    返回的 saver 是 async 上下文管理器：``async with build_async_checkpointer() as
    saver: await saver.setup(); ...``——连接在 ``__aexit__`` 关闭。``setup`` 建表幂等。
    ``serde`` 缺省 :class:`HypoArgusSerializer`；调用方可注入裸 ``JsonPlusSerializer``
    做对照测试。
    """

    dsn = resolve_pg_dsn(conn_string)
    return AsyncPostgresSaver.from_conn_string(
        dsn, serde=serde or HypoArgusSerializer()
    )
