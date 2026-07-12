"""工具框架 seam（骨架移植自 DeepTutor ``core/tool_protocol.py``，ADR-0015）。

ReAct Agent（体检 #4 / 开药 #5）经 :class:`ToolRegistry` 的 :meth:`ToolRegistry.dispatch`
调用工具，产出 :class:`ToolResult`。骨架只移植协议与注册/调度——**不**移植 OpenAI-schema
发射、并行调度、重复调用去重、``pause_for_user``、deferred 渐进披露、``ToolEventSink``：
HypoArgus 的 ``LlmClient`` Protocol 冻结（无原生函数调用）、ReAct 单步单迭代、当前单工具
（检索），上述机器属假设性开销（ADR-0015 权衡）。

未来工具实现 :class:`BaseTool` 后 :meth:`ToolRegistry.register` 即可挂入——Agent 经
``dispatch`` 调用，与具体工具解耦（leverage：一处注册、N 调用点）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from infra.retrieval import Source

__all__ = [
    "ToolResult",
    "BaseTool",
    "ToolRegistry",
    "UnknownToolError",
]


@dataclass(frozen=True)
class ToolResult:
    """工具调用结果。

    ``sources`` 为检索素材（当前唯一工具形态产出 ``Source`` 列表，流入
    :class:`infra.history.HistoryStore`）；``metadata`` 携带可审计附加信息（通道、脱敏后
    查询）；``success`` 标记调用是否成功。``sources``-specific 是诚实的——当前仅检索工具，
    未来非检索工具形态出现时再泛化（「一个 adapter 是假设 seam，两个才是真 seam」）。
    """

    sources: list[Source] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    success: bool = True


class UnknownToolError(KeyError):
    """``dispatch`` 到未注册工具名。抛出而非静默——保留 ReAct 循环的异常→兜底路径
    （节点落 ``error`` / 假设落 ``doubtful``，与 #4/#5 既有行为一致）。"""


class BaseTool(ABC):
    """工具 seam：经 ``ToolRegistry.dispatch`` 调用，产出 ``ToolResult``。

    ``execute`` 取 ``**kwargs``——具体工具入参形状由工具自身定义（见
    :class:`infra.retrieval_tool.RetrievalTool`，入参为 ``step=``）。未移植 OpenAI-schema
    发射：HypoArgus 的 ``LlmClient`` Protocol 冻结（无原生函数调用），工具由 Agent
    代码侧据 ``SearchStep`` 显式 dispatch，而非 LLM 自动函数调用。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具注册名（``ToolRegistry`` 据此路由）。"""

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult:
        """执行工具，返回 :class:`ToolResult`。具体入参由子类定义。"""


class ToolRegistry:
    """name → tool 路由。Agent 经 :meth:`dispatch` 调用工具，与具体工具解耦。

    骨架：单 :class:`infra.retrieval_tool.RetrievalTool` 注册于 ``"retrieve"``；未来工具
    实现 :class:`BaseTool` 后 :meth:`register` 即可挂入，Agent 调用代码不动。
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """注册工具；重名覆盖（后注册者胜）。"""

        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        """按名取工具；未注册返回 ``None``。"""

        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """已注册工具名（注册序）。"""

        return list(self._tools)

    def dispatch(self, name: str, /, **kwargs: Any) -> ToolResult:
        """按名调度工具。``name`` 为位置参数（positional-only），防工具入参恰名 ``name`` 冲突。

        未注册名抛 :class:`UnknownToolError`——保留 ReAct 循环的异常→兜底路径。
        """

        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError(name)
        return tool.execute(**kwargs)
