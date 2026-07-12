"""``ToolRegistry`` seam 单测（ADR-0015）：注册 / 路由 / 未注册兜底。"""

from __future__ import annotations

import pytest

from infra.tool_protocol import BaseTool, ToolRegistry, ToolResult, UnknownToolError


class _EchoTool(BaseTool):
    """回显入参为 metadata 的测试工具。"""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def execute(self, **kwargs: object) -> ToolResult:
        return ToolResult(metadata={k: str(v) for k, v in kwargs.items()})


def test_register_get_list() -> None:
    reg = ToolRegistry()
    assert reg.list_tools() == []
    tool = _EchoTool("a")
    reg.register(tool)
    assert reg.get("a") is tool
    assert reg.list_tools() == ["a"]
    assert reg.get("missing") is None


def test_dispatch_routes_and_passes_kwargs() -> None:
    reg = ToolRegistry()
    reg.register(_EchoTool("a"))
    result = reg.dispatch("a", foo="bar")
    assert result.success is True
    assert result.metadata == {"foo": "bar"}
    assert result.sources == []


def test_unknown_tool_raises_keyerror() -> None:
    reg = ToolRegistry()
    with pytest.raises(UnknownToolError):
        reg.dispatch("nope", x=1)


def test_dispatch_name_is_positional_only() -> None:
    """``name`` 为 positional-only——工具入参恰名 ``name`` 不可与调度键冲突。"""

    reg = ToolRegistry()
    reg.register(_EchoTool("a"))
    result = reg.dispatch("a", name="payload")
    assert result.metadata == {"name": "payload"}


def test_reregister_overrides() -> None:
    reg = ToolRegistry()
    first = _EchoTool("a")
    second = _EchoTool("a")
    reg.register(first)
    reg.register(second)
    assert reg.get("a") is second
