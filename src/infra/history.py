"""历史对话 seam（ADR-0016）：ReAct 步骤间的检索观察记忆 + 压缩。

载体为 ``list[Source]``（非 ``BaseMessage``）——HypoArgus 当前 ReAct「历史」是检索观察
而非聊天消息（``LlmClient`` Protocol 冻结，ADR-0015；真实 provider 落地后再评估升级为
消息轮次形态并全量融合 ``docs/graph_utils.py`` 的 ``trim_messages``）。

``CompressionConfig`` 形状部分移植自 ``docs/graph_utils.py``（``max_items`` / ``max_tokens``
/ ``strategy`` / ``char//4`` token 近似计数）——只移植对 ``Source`` 有意义的部分；
``trim_messages``、``BaseMessage`` 机制、``start_on`` / ``include_system``（消息类型专属、
对 ``Source`` 无意义）、抗漂移摘要、三层记忆均**不**移植（属 #3/#4 切片）。

作用域 = 每 ReAct 循环（每节点 / 每假设），与既有 ``observations`` 作用域一致；跨 run
持久化属 #4 checkpointer 切片——``session_id`` 线程现为预备（``Orchestrator.run`` 透传
``config=``），本 seam 内存态、不消费 ``session_id``。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from infra.retrieval import Source

__all__ = [
    "CompressionStrategy",
    "CompressionConfig",
    "DEFAULT_COMPRESSION",
    "HistoryStore",
]

TokenCounter = Callable[[list[Source]], int]


class CompressionStrategy(StrEnum):
    """裁剪方向。"""

    LAST = "last"  # 保留最近
    FIRST = "first"  # 保留最早


@dataclass(frozen=True)
class CompressionConfig:
    """历史压缩策略（部分移植自 ``docs/graph_utils.py`` 的 ``CompressionConfig``）。

    ``max_items`` / ``max_tokens`` 可同时生效（先按条数、再按 token，取更严者）；
    ``None`` 表示不按该维裁剪。``token_counter`` 缺省时用 ``char//4`` 近似（移植自
    graph_utils 的 ``_approx_token_count``，适配 ``Source`` 的 ``snippet``/``title`` 字段）。
    ``trim_messages`` 的 ``start_on`` / ``include_system`` 是 ``BaseMessage`` 专属、对
    ``Source`` 无意义，未移植。
    """

    max_items: int | None = 20
    max_tokens: int | None = None
    strategy: CompressionStrategy = CompressionStrategy.LAST
    token_counter: TokenCounter | None = None


DEFAULT_COMPRESSION = CompressionConfig()
"""省略 ``compression`` 时用的默认策略：保留最近 20 条、不限 token。"""


def _approx_token_count(messages: list[Source]) -> int:
    """粗略 token 估算：约 4 字符 ≈ 1 token（中英文混排保守值，移植自 graph_utils）。"""

    chars = sum(len(m.snippet) + len(m.title or "") for m in messages)
    return chars // 4 + len(messages)


def _trim(items: list[Source], config: CompressionConfig) -> list[Source]:
    """按条数 / token 裁剪 ``Source`` 列表。"""

    if not items:
        return items
    counter = config.token_counter or _approx_token_count
    # 先按条数裁剪
    if config.max_items is not None and len(items) > config.max_items:
        if config.strategy is CompressionStrategy.LAST:
            items = items[-config.max_items:]
        else:
            items = items[:config.max_items]
    # 再按 token 裁剪（贪心反向累积直到预算耗尽）
    if config.max_tokens is not None and counter(items) > config.max_tokens:
        kept: list[Source] = []
        cost = 0
        seq = reversed(items) if config.strategy is CompressionStrategy.LAST else iter(items)
        for m in seq:
            c = counter([m])
            if cost + c > config.max_tokens:
                break
            kept.append(m)
            cost += c
        items = list(reversed(kept)) if config.strategy is CompressionStrategy.LAST else kept
    return items


class HistoryStore:
    """ReAct 循环内的检索观察历史 + 压缩（ADR-0016）。

    Agent 经 :meth:`append` / :meth:`extend` 累积观察，经 :meth:`compressed_view` 取
    「压至预算内」的列表回喂 LLM（dev-guide §4 源压缩铁律）。压缩策略集中在 seam 之后
    （locality：未来升级为真实摘要 / 抗漂移只改此模块）。全量 :meth:`all` 供审计 / 断言。

    作用域每循环一个（per argument / per hypothesis）——与既有 ``observations`` 作用域一致。
    """

    def __init__(self, config: CompressionConfig | None = None) -> None:
        self._config = config or DEFAULT_COMPRESSION
        self._items: list[Source] = []

    def append(self, source: Source) -> None:
        """累积一条检索素材。"""

        self._items.append(source)

    def extend(self, sources: list[Source]) -> None:
        """累积多条检索素材（``ToolResult.sources`` 流入此处）。"""

        self._items.extend(sources)

    def all(self) -> list[Source]:
        """全量（未压缩）观察——供审计 / 断言。"""

        return list(self._items)

    def compressed_view(self) -> list[Source]:
        """压至 :class:`CompressionConfig` 预算内的视图，回喂 LLM。"""

        return _trim(self._items, self._config)

    def __len__(self) -> int:
        return len(self._items)
