"""Paragraph-level LLM intent agent using required parallel Tool Calling."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .registry import StructuredToolDefinition, tools_from_registry


@dataclass(slots=True)
class RequestedToolCall:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]


class StructuredIntentAgent:
    def __init__(self, model: Any, registry: dict[str, StructuredToolDefinition], config: Any = None):
        self.model = model
        self.registry = registry
        self.by_name = {definition.tool.name: definition for definition in registry.values()}
        self.min_tool_calls = getattr(config, "structured_min_tool_calls", 1) if config else 1
        self.max_tool_calls = getattr(config, "structured_max_tool_calls", 5) if config else 5
        self.repair_count = getattr(config, "structured_repair_count", 1) if config else 1

    def _fallback(self, task_ids: list[str], reason: str) -> list[RequestedToolCall]:
        return [RequestedToolCall(
            tool_call_id=f"no-structured-{uuid.uuid4().hex}",
            tool_name="no_structured_query",
            arguments={"reason": reason, "evaluated_task_ids": task_ids},
        )]

    @staticmethod
    def build_prompt(paragraph_text: str, tasks: list[Any], organization_context: dict[str, Any] | None = None, min_tool_calls: int = 1, max_tool_calls: int = 5) -> str:
        task_payload = [{
            "task_id": task.task_id,
            "line_type": task.line_type.value,
            "target_text": task.target_text,
            "required_slots": task.required_slots,
            "atomic_claims": [claim.model_dump(mode="json") for claim in task.atomic_claims],
            "source_refs": task.source_refs,
        } for task in tasks]
        min_hint = f"你必须至少调用 {min_tool_calls} 个真实场景工具。" if min_tool_calls > 0 else "你可以调用 0 个真实场景工具。"
        return (
            "你是 SearchAgent 的结构化数据意图节点。你必须调用至少一个工具。"
            f"{min_hint}最多调用 {max_tool_calls} 个工具。"
            "仅当段落明确属于某个已注册业务场景时调用对应查询工具；公开市场、白皮书、"
            "通用 Web 事实必须调用 no_structured_query。可一次并行调用多个场景工具。"
            "每个真实查询工具都必须提供 target_task_ids，且只能来自输入 task_id。"
            "不得生成 SQL、表名、API 地址或额外参数。\n"
            f"段落：{paragraph_text}\n"
            f"任务：{json.dumps(task_payload, ensure_ascii=False, separators=(',', ':'))}"
        )

    async def select(
        self,
        paragraph_text: str,
        tasks: list[Any],
        *,
        feedback: str | None = None,
        organization_context: dict[str, Any] | None = None,
    ) -> tuple[list[RequestedToolCall], list[str]]:
        task_ids = [task.task_id for task in tasks]
        if self.model is None or not hasattr(self.model, "bind_tools"):
            return self._fallback(task_ids, "Structured Tool Calling model is unavailable"), ["STRUCTURED_INTENT_MODEL_UNAVAILABLE"]
        bound = self.model.bind_tools(
            tools_from_registry(self.registry),
            parallel_tool_calls=True,
            tool_choice="required",
        )
        prompt = self.build_prompt(paragraph_text, tasks, organization_context, self.min_tool_calls, self.max_tool_calls)
        prompt += "\norganization_context: " + json.dumps(
            organization_context or {}, ensure_ascii=False, separators=(",", ":")
        )
        if feedback:
            prompt += (
                "\n上轮工具执行返回 INVALID_ARGUMENT。仅允许本次修复；请根据错误重新提取参数，"
                f"不要重复无效调用：{feedback}"
            )
        warnings: list[str] = []
        for attempt in range(self.repair_count + 1):
            response = await bound.ainvoke(prompt)
            raw_calls = list(getattr(response, "tool_calls", []) or [])
            calls: list[RequestedToolCall] = []
            invalid: list[str] = []
            for index, raw in enumerate(raw_calls):
                name = str(raw.get("name") or "")
                arguments = dict(raw.get("args")) if isinstance(raw.get("args"), dict) else {}
                arguments.pop("tool_call_id", None)
                call_id = str(raw.get("id") or f"structured-call-{attempt}-{index}")
                definition = self.by_name.get(name)
                if definition is None:
                    invalid.append(f"unknown tool {name}")
                    continue
                try:
                    organization = organization_context or {}
                    fields = definition.args_schema.model_fields
                    if "school_id" in fields and not arguments.get("school_id") and organization.get("school_id"):
                        arguments["school_id"] = organization["school_id"]
                    if "my_school_id" in fields and not arguments.get("my_school_id") and organization.get("school_id"):
                        arguments["my_school_id"] = organization["school_id"]
                    validated = definition.args_schema.model_validate(arguments).model_dump(exclude_none=True)
                except ValidationError as exc:
                    invalid.append(f"{name}: {exc.errors(include_url=False)}")
                    continue
                targets = validated.get("target_task_ids") or validated.get("evaluated_task_ids") or []
                if any(task_id not in task_ids for task_id in targets):
                    invalid.append(f"{name}: target_task_ids outside paragraph")
                    continue
                calls.append(RequestedToolCall(call_id, name, validated))
            if calls and not invalid:
                return calls, warnings
            if attempt == 0:
                warnings.append("STRUCTURED_ARGUMENT_REPAIR")
                prompt += "\n上次 Tool Call 无效，请仅修正一次：" + "; ".join(invalid or ["模型未返回 Tool Call"])
        warnings.append("STRUCTURED_INVALID_ARGUMENT")
        return self._fallback(task_ids, "No valid structured tool call after one repair"), warnings


__all__ = ["RequestedToolCall", "StructuredIntentAgent"]
