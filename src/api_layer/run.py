"""``POST /api/agent/run`` 的请求 / 响应模型与驱动逻辑（T-04·ADR-0022 / ADR-0024）。

:class:`RunService` 把 T-03 的 ``interrupt + AsyncPostgresSaver`` 图从 CLI resume 循环换成
HTTP resume 驱动：一个请求 ``ainvoke`` 一步，阻塞到终止态（``NEED_HUMAN_INPUT`` /
``SUCCESS`` / ``FAILED``）或 120s 超时（``GRAPH_TIMEOUT``）即返回。

fresh（``query`` 非空、无活跃 ``pause_meta``）→ mint 新 ``trace_id``、获 ``session_locks`` 行、
喂 ``original_doc`` + ``session_context`` 至首个 interrupt 或终态。
resume（``human_response`` 非空、有活跃 ``pause_meta``）→ 复用 ``pause_meta.trace_id``、
据 ``aget_state`` 判定 interrupt 节点构造 ``Hitl*Reply``、``Command(resume=reply)`` 续跑。

T-05：fresh / resume 的图驱动经 ``astream_events(version="v2")``（非 ``ainvoke``），挂
:class:`api_layer.translator.EventTranslator` 把事件映射为 §6.4 类型、mint ``event_seq``、
非阻塞写 ``trace_events``（ADR-0023 不变量：显示侧落库不反压图、不杀图）。

关键不变量（ADR-0022 / ADR-0023）：

- ``NEED_HUMAN_INPUT`` 由 ``aget_state`` 判定（``state.next`` 含 hitl 节点且 ``tasks`` 带
  interrupt），与 WS ``human_pause`` 同源——杜绝「WS 说暂停、HTTP 说成功」竞态。
- ``human_question`` / ``hint`` 从 checkpoint interrupt payload（``Hitl*Question``）读，不另存。
- HITL 暂停期锁**不释放**（行留存、续跑复用、不再 INSERT 故不误触 ``LOCK_EXIST``）；终态 /
  abort 删锁行。
- 业务纯函数（``confirm`` / ``confirm_partition`` / ``resolve_rewrites`` /
  ``assemble_final_document``）零改动——本模块只驱动图 + 管 side metadata。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from langgraph.types import Command
from pydantic import BaseModel

from agents.assembly import MANIFEST
from agents.hitl1 import Hitl1Action, Hitl1Question, Hitl1Reply
from agents.hitl2 import Hitl2Action, Hitl2Question, Hitl2Reply
from api_layer.errors import PAUSE_TTL_SECONDS, ApiError, ErrorCode
from api_layer.graph_view import VisibilityConfig, compute_hidden_names
from api_layer.session_cache import PauseMeta, SessionCacheBase
from api_layer.trace_store import InMemoryTraceEventStore, TraceEventStoreBase
from api_layer.translator import EventTranslator
from domain import SessionContext
from runtime.orchestrator import Orchestrator

__all__ = [
    "RunStatus",
    "HumanResponse",
    "RunRequest",
    "RunResponse",
    "RunServiceConfig",
    "RunService",
    "DEFAULT_SESSION_LIMIT",
    "DEFAULT_GRAPH_TIMEOUT_SECONDS",
]


DEFAULT_SESSION_LIMIT: int = 100
"""活跃会话数上限（PRD §9.7）。``session_owner.last_seen`` 近 30min 计数达此值且无法淘汰
→ ``SESSION_LIMIT``。"""

DEFAULT_GRAPH_TIMEOUT_SECONDS: float = 120.0
"""单请求全局超时（PRD §9.2）。``asyncio.wait_for`` 兜底 → ``GRAPH_TIMEOUT``。"""


# --------------------------------------------------------------------------- #
# 请求 / 响应模型
# --------------------------------------------------------------------------- #


class RunStatus(StrEnum):
    """``/api/agent/run`` 单请求返回的终止态。``NEED_HUMAN_INPUT`` = 暂停等回填（请求结束、
    client 下轮 resume）；``SUCCESS`` / ``FAILED`` = 图终态。"""

    NEED_HUMAN_INPUT = "NEED_HUMAN_INPUT"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class HumanResponse(BaseModel):
    """resume 回填（ADR-0022 一期：``action`` + 自由文本，结构化 ops 推二期）。

    ``action`` 为 ``skip/accept/edit/replay``（hitl1）或 ``pass/decide``（hitl2）的字符串；
    由 :class:`RunService` 据当前 interrupt 节点校验 + 铸成 ``Hitl*Reply``。
    """

    action: str
    text: str = ""


class RunRequest(BaseModel):
    """``/api/agent/run`` 入参。

    ``session_id`` 必填（外部生成，Python 仅登记校验，CONTEXT「会话」）；``query`` 与
    ``human_response`` 互斥（违例 → ``PARAM_ERROR``）；``document`` 为 fresh-run 文档本体
    （ADR-0024：orchestrator 要求 ``original_doc``，故 fresh 路径必填、resume 忽略）；
    ``biz_trace_id`` 透传供外部链路关联。
    """

    session_id: str
    query: str | None = None
    human_response: HumanResponse | None = None
    document: str | None = None
    biz_trace_id: str | None = None


class RunResponse(BaseModel):
    """/api/agent/run 的统一响应。``NEED_HUMAN_INPUT`` 时载 ``node_id`` / ``human_question``
    / ``hint`` / ``detail``（interrupt payload 序列化）；``SUCCESS`` 载 ``final_document``；
    ``FAILED`` 载 ``errors``（+ 可能有 ``final_document`` 兜底）。"""

    status: RunStatus
    session_id: str
    trace_id: str
    node_id: str | None = None
    human_question: str | None = None
    hint: str | None = None
    detail: dict[str, Any] | None = None
    final_document: str | None = None
    errors: list[str] = []
    biz_trace_id: str | None = None


@dataclass(frozen=True)
class RunServiceConfig:
    """RunService 可调参数（PRD §9.2 / §9.7）。"""

    session_limit: int = DEFAULT_SESSION_LIMIT
    graph_timeout_seconds: float = DEFAULT_GRAPH_TIMEOUT_SECONDS


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


# --------------------------------------------------------------------------- #
# interrupt payload 取回 + human_question / hint 抽取
# --------------------------------------------------------------------------- #


def _interrupt_payload(state: Any) -> Any:
    """从 ``aget_state`` 结果取首个 interrupt 的 value（``Hitl*Question``）；无则 ``None``。"""

    for task in getattr(state, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            return getattr(intr, "value", None)
    return None


def _render_question(payload: Any) -> tuple[str, str]:
    """据 interrupt payload（``Hitl1Question`` / ``Hitl2Question``）产 (human_question, hint)。

    两段从 checkpoint payload 读、不另存（ADR-0023）。``detail`` 由调用方用 ``model_dump``
    序列化整 payload 供前端。
    """

    if isinstance(payload, Hitl1Question):
        return (
            "请确认段落切分是否合理。",
            "可回复 action：skip（跳过）/ accept（接受）/ edit（编辑，一期 ops 推后）/ replay（按 prompt 重跑）",
        )
    if isinstance(payload, Hitl2Question):
        if not payload.review.has_pending:
            return (
                "终稿确认：无提议重写，可一键通过。",
                "可回复 action：pass（一键通过）",
            )
        return (
            "请逐段确认终稿重写（原文 × 提议）。",
            "可回复 action：decide（一期 = 全驳回、原文逐字节保留；逐段 confirm/edit/reject 推后）",
        )
    # 未知 payload：节点名已校验为 hitl*，到此处为 invariant 破裂。
    return ("等待人工确认。", "")


# --------------------------------------------------------------------------- #
# RunService
# --------------------------------------------------------------------------- #


class RunService:
    """``/api/agent/run`` 的驱动者：side metadata + 图驱动 + 终态分类。

    注入一个已 ``compile(checkpointer=AsyncPostgresSaver)`` + ``InterruptHitl*Gate`` 的
    :class:`Orchestrator`（PRD §10.3 单例：一个服务一个 orch + 一个 saver）、一个
    :class:`SessionCacheBase`（side metadata，内存 / Postgres 两 adapter）与 :class:`RunServiceConfig`。
    ``clock`` 可注入以测过期分支。
    """

    def __init__(
        self,
        orch: Orchestrator,
        session_cache: SessionCacheBase,
        *,
        trace_store: TraceEventStoreBase | None = None,
        langfuse_handler: Any | None = None,
        visibility: VisibilityConfig | None = None,
        config: RunServiceConfig | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._orch = orch
        self._cache = session_cache
        self._trace_store: TraceEventStoreBase = trace_store or InMemoryTraceEventStore()
        # Langfuse LangChain callback handler（可选外部 sink）：注入则进 ``RunnableConfig.callbacks``，
        # 与 ``astream_events`` 消费端共存（ADR-0023）。``None`` = 未配置 / 离线态、不注入。
        self._langfuse_handler = langfuse_handler
        vis = visibility if visibility is not None else VisibilityConfig()
        self._visibility = vis
        # 翻译层单源：节点展示元数据 + 隐藏集（与 graph_view 同源 compute_hidden_names）。
        self._manifest_index: dict[str, Any] = {e.name: e for e in MANIFEST}
        self._hidden: frozenset[str] = compute_hidden_names(MANIFEST, vis)
        self._config = config or RunServiceConfig()
        self._clock = clock

    # fresh / resume 二态的对外入口。 ----------------------------------- #

    async def run(self, request: RunRequest, *, user_id: str) -> RunResponse:
        """驱动一次 ``/api/agent/run``：校验 → 所有权 → fresh/resume → 图驱动 → 分类返回。

        所有控制面错误经 :class:`ApiError` 抛（路由层映射 HTTP）。``user_id`` 来自
        ``X-User-Id``（一期信任 Nginx 注入，ADR-0024）；缺即 ``FORBIDDEN``。
        """

        if not user_id:
            raise ApiError(ErrorCode.FORBIDDEN, "缺失 X-User-Id（一期信任 Nginx 注入）")
        sid = request.session_id
        if not sid:
            raise ApiError(ErrorCode.PARAM_ERROR, "session_id 必填")

        # query XOR human_response（互斥）。
        has_query = request.query is not None and request.query != ""
        has_reply = request.human_response is not None
        if has_query == has_reply:  # 同真（都给）或同假（都不给）→ PARAM_ERROR
            raise ApiError(
                ErrorCode.PARAM_ERROR,
                "query 与 human_response 必须恰一（互斥）：fresh 用 query、resume 用 human_response",
            )

        await self._enforce_ownership(sid, user_id)

        if has_query:
            return await self._run_fresh(request, user_id=user_id)
        return await self._run_resume(request, user_id=user_id)

    # ------------------------------------------------------------------ #
    # 会话所有权（PRD §3.2）
    # ------------------------------------------------------------------ #

    async def _enforce_ownership(self, session_id: str, user_id: str) -> None:
        """``session_id`` 首见登记 + 绑定 ``X-User-Id``；已登记不匹配 → ``FORBIDDEN``；
        新登记触达活跃上限且无法淘汰 → ``SESSION_LIMIT``。"""

        existing = await self._cache.get_session_owner(session_id)
        if existing is None:
            # 新会话：先看活跃数是否已满（idle 已排除在 count 外，无以淘汰）。
            count = await self._cache.get_active_count(now=self._clock())
            if count >= self._config.session_limit:
                raise ApiError(
                    ErrorCode.SESSION_LIMIT,
                    f"活跃会话数达上限 {self._config.session_limit} 且无法淘汰",
                )
            await self._cache.set_session_owner(session_id, user_id)
            return
        if existing != user_id:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                f"会话 {session_id} 已登记给用户 {existing!r}，跨用户访问禁止",
            )
        await self._cache.touch_session_owner(session_id, now=self._clock())

    # ------------------------------------------------------------------ #
    # fresh run
    # ------------------------------------------------------------------ #

    async def _run_fresh(self, request: RunRequest, *, user_id: str) -> RunResponse:
        sid = request.session_id
        assert request.query is not None  # 由 run() 保证
        if not request.document:
            raise ApiError(
                ErrorCode.PARAM_ERROR,
                "fresh run（query）需提供 document（待修订文档本体）",
            )

        # 活跃 pause_meta 存在 → 未处理断点 / 过期。
        pause = await self._cache.get_pause_meta(sid)
        if pause is not None:
            if self._is_pause_expired(pause):
                await self._cleanup_terminal(sid)
                raise ApiError(ErrorCode.PAUSE_EXPIRED, f"断点已过期（session={sid}）")
            raise ApiError(
                ErrorCode.LOCK_EXIST,
                f"会话 {sid} 有未处理断点（node={pause.node_id}），请先 resume 而非 fresh",
            )

        trace_id = str(uuid.uuid4())
        # 获执行锁：INSERT OCND；冲突未过期 → LOCK_EXIST（重复提交）。
        acquired = await self._cache.lock_session(sid, trace_id, now=self._clock())
        if not acquired:
            raise ApiError(
                ErrorCode.LOCK_EXIST,
                f"会话 {sid} 已有进行中 run（重复提交）",
            )

        ctx = SessionContext(
            session_id=sid,
            user_id=user_id,
            current_time=self._clock(),
            user_prompt=request.query,
        )
        translator = await self._new_translator(sid, trace_id)
        drive_config = self._drive_config(sid, trace_id, user_id)
        try:
            await asyncio.wait_for(
                self._drive(
                    translator,
                    {"original_doc": request.document.encode(), "session_context": ctx},
                    drive_config,
                ),
                timeout=self._config.graph_timeout_seconds,
            )
        except TimeoutError as exc:
            await translator.emit_stream_abort("GRAPH_TIMEOUT")
            await translator.flush()
            await self._cache.unlock_session(sid)
            raise ApiError(ErrorCode.GRAPH_TIMEOUT, f"图执行超时：{exc}") from exc

        return await self._classify(sid, trace_id, request, is_resume=False, translator=translator)

    # ------------------------------------------------------------------ #
    # resume run
    # ------------------------------------------------------------------ #

    async def _run_resume(self, request: RunRequest, *, user_id: str) -> RunResponse:
        sid = request.session_id
        assert request.human_response is not None
        pause = await self._cache.get_pause_meta(sid)
        if pause is None:
            raise ApiError(
                ErrorCode.PARAM_ERROR,
                f"无活跃 pause_meta，无法 resume（session={sid}）；fresh run 请用 query",
            )
        if self._is_pause_expired(pause):
            await self._cleanup_terminal(sid)
            raise ApiError(ErrorCode.PAUSE_EXPIRED, f"断点已过期（session={sid}）")

        trace_id = pause.trace_id
        state = await self._orch.graph.aget_state(self._config_dict(sid))
        node = state.next[0] if state.next else None
        translator = await self._new_translator(sid, trace_id)
        if node is None:
            # pause_meta 在但图已终态：stale，按终态返回并清理。
            return await self._classify(
                sid, trace_id, request, is_resume=True, stale=True, translator=translator
            )

        payload = _interrupt_payload(state)
        reply = self._build_reply(node, request.human_response, payload)
        # HITL 暂停期锁不释放、行留存、续跑复用（touch heartbeat 保 TTL、不重 INSERT）。
        await self._cache.heartbeat_lock(sid, trace_id, now=self._clock())
        drive_config = self._drive_config(sid, trace_id, user_id)

        try:
            await asyncio.wait_for(
                self._drive(translator, Command(resume=reply), drive_config),
                timeout=self._config.graph_timeout_seconds,
            )
        except TimeoutError as exc:
            # abort：删锁行（stream_finish/abort 语义）；pause_meta 留存（interrupt checkpoint
            # 仍为最后已知良态，client 可重试 resume）。
            await translator.emit_stream_abort("GRAPH_TIMEOUT")
            await translator.flush()
            await self._cache.unlock_session(sid)
            raise ApiError(ErrorCode.GRAPH_TIMEOUT, f"图执行超时：{exc}") from exc

        return await self._classify(sid, trace_id, request, is_resume=True, translator=translator)

    # ------------------------------------------------------------------ #
    # 终态分类（fresh / resume 共用）
    # ------------------------------------------------------------------ #

    async def _classify(
        self,
        session_id: str,
        trace_id: str,
        request: RunRequest,
        *,
        is_resume: bool,
        stale: bool = False,
        translator: EventTranslator | None = None,
    ) -> RunResponse:
        """``aget_state`` 判定 NEED_HUMAN_INPUT vs 终态。"""

        config = self._config_dict(session_id)
        state = await self._orch.graph.aget_state(config)
        node = state.next[0] if state.next else None
        payload = _interrupt_payload(state) if state.next else None

        if node is not None and payload is not None:
            # interrupt 暂停 → NEED_HUMAN_INPUT。set pause_meta（复用 trace_id）+ heartbeat。
            await self._cache.set_pause_meta(
                session_id, trace_id, node, now=self._clock()
            )
            await self._cache.heartbeat_lock(session_id, trace_id, now=self._clock())
            question, hint = _render_question(payload)
            # human_pause 与 HTTP NEED_HUMAN_INPUT 同源：同一 aget_state payload，
            # question/hint/detail 三者一致（ADR-0023 杜绝竞态）。
            if translator is not None:
                detail = payload.model_dump(mode="json")
                await translator.emit_human_pause(node, question, hint, detail)
                await translator.flush()
            return RunResponse(
                status=RunStatus.NEED_HUMAN_INPUT,
                session_id=session_id,
                trace_id=trace_id,
                node_id=node,
                human_question=question,
                hint=hint,
                detail=payload.model_dump(mode="json"),
                biz_trace_id=request.biz_trace_id,
            )

        # 终态：final_document + errors。清理 side metadata。
        final: bytes = state.values.get("final_document", b"")
        errors: list[str] = list(state.values.get("errors", []))
        await self._cleanup_terminal(session_id)
        if translator is not None:
            await translator.emit_stream_finish()
            await translator.flush()
        if errors or not final:
            return RunResponse(
                status=RunStatus.FAILED,
                session_id=session_id,
                trace_id=trace_id,
                final_document=final.decode("utf-8", errors="surrogateescape") or None,
                errors=errors,
                biz_trace_id=request.biz_trace_id,
            )
        return RunResponse(
            status=RunStatus.SUCCESS,
            session_id=session_id,
            trace_id=trace_id,
            final_document=final.decode("utf-8", errors="surrogateescape"),
            errors=[],
            biz_trace_id=request.biz_trace_id,
        )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _is_pause_expired(self, pause: PauseMeta) -> bool:
        return pause.pause_time + timedelta(seconds=PAUSE_TTL_SECONDS) < self._clock()

    def _config_dict(self, session_id: str) -> dict[str, Any]:
        """``aget_state`` 用的最小 config（thread_id + recursion_limit；无 callbacks）。"""

        return {
            "configurable": {"thread_id": session_id},
            "recursion_limit": self._orch._recursion_limit,
        }

    def _drive_config(
        self, session_id: str, trace_id: str, user_id: str
    ) -> dict[str, Any]:
        """``astream_events`` 驱动 config：``_config_dict`` + Langfuse callback + trace metadata。

        Langfuse handler（可选外部 sink）与 ``astream_events`` 消费端共存于同一 config
        （ADR-0023）：handler 经 ``callbacks`` 线程贯穿整条 Agent 链路、``trace_id`` 作
        ``langfuse_tags`` 关联同 trace 多 invoke（PRD §10.3 约束 3）。``None`` 时不注入、
        行为与离线态一致。
        """

        config = self._config_dict(session_id)
        if self._langfuse_handler is not None:
            config["callbacks"] = [self._langfuse_handler]
            config["metadata"] = {
                "langfuse_session_id": session_id,
                "langfuse_user_id": user_id,
                "langfuse_tags": [trace_id],
            }
        return config

    async def _new_translator(
        self, session_id: str, trace_id: str
    ) -> EventTranslator:
        """构造本段翻译层；``start_seq = store.max_seq(trace_id) + 1``（fresh=0、resume 顺延）、
        ``node_instance`` seed 自 store 既有 node_start 计数（跨段连续，区分回放环）。"""

        start_seq = await self._trace_store.max_seq(trace_id) + 1
        prior_instances = await self._trace_store.node_instance_counts(trace_id)
        return EventTranslator(
            self._trace_store,
            session_id=session_id,
            trace_id=trace_id,
            start_seq=start_seq,
            manifest_index=self._manifest_index,
            hidden=self._hidden,
            clock=self._clock,
            prior_node_instances=prior_instances,
        )

    async def _drive(
        self,
        translator: EventTranslator,
        graph_input: Any,
        config: dict[str, Any],
    ) -> None:
        """消费 ``astream_events(version="v2")``、喂翻译层（非阻塞落库）。

        astream_events 既是图驱动（消费 async generator 即推进图）又是事件源；翻译层 ``feed``
        仅入队、不 await 落库 → 图推进不被慢写反压（ADR-0023 不变量）。generator 在 interrupt
        暂停或终态时完成；其后由 :meth:`_classify` 据 ``aget_state`` 产 human_pause / stream_finish。
        """

        async for ev in self._orch.graph.astream_events(
            graph_input, config=config, version="v2"
        ):
            await translator.feed(ev)

    async def _cleanup_terminal(self, session_id: str) -> None:
        """终态 / abort / 过期清理：删 pause_meta + 锁行。"""

        await self._cache.delete_pause_meta(session_id)
        await self._cache.unlock_session(session_id)

    def _build_reply(
        self, node: str, human: HumanResponse, payload: Any
    ) -> Hitl1Reply | Hitl2Reply:
        """据 interrupt 节点名 + payload 类型校验 ``action`` 并铸 ``Hitl*Reply``。

        ``action`` 非法（不在该节点合法动作集）→ ``PARAM_ERROR``。``text`` 自由文本透传
        （一期 ``parse_reply`` 产 action-only 决策，text 不影响纯函数）。
        """

        action = human.action.strip().lower()
        if node == "hitl1" and isinstance(payload, Hitl1Question):
            try:
                return Hitl1Reply(action=Hitl1Action(action), text=human.text)
            except ValueError as exc:
                raise ApiError(
                    ErrorCode.PARAM_ERROR,
                    f"hitl1 非法 action {action!r}（合法：skip/accept/edit/replay）",
                ) from exc
        if node == "hitl2" and isinstance(payload, Hitl2Question):
            try:
                return Hitl2Reply(action=Hitl2Action(action), text=human.text)
            except ValueError as exc:
                raise ApiError(
                    ErrorCode.PARAM_ERROR,
                    f"hitl2 非法 action {action!r}（合法：pass/decide）",
                ) from exc
        raise ApiError(
            ErrorCode.PARAM_ERROR,
            f"当前 interrupt 节点 {node!r}（payload={type(payload).__name__}）与 human_response 不匹配",
        )
