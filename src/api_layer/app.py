"""FastAPI 控制面应用（T-04·ADR-0022 / ADR-0024）。

``create_app`` 装配一个 :class:`api_layer.run.RunService`（图驱动 + side metadata）为
FastAPI 应用，挂两个路由：

- ``POST /api/agent/run``：fresh（``query``）与 resume（``human_response``）二态驱动；
- ``GET /api/agent/graph``：复用 T-02 :func:`api_layer.graph_view.build_graph_view`，
  单一源拓扑，HTTP 层不另写。

鉴权 / 所有权（PRD §3.2）：一期信任 Nginx 注入的 ``X-User-Id`` 头；user_id 抽取集中在
依赖 :func:`_user_id`，所有权校验集中在 :meth:`RunService._enforce_ownership`
（控制面层、非散落路由）。``session_id`` 首见登记绑定；已登记不匹配 → ``403 FORBIDDEN``。

错误响应统一：:class:`api_layer.errors.ApiError` → JSON ``{error, message}`` + 映射 HTTP 状态
（:data:`api_layer.errors.ERROR_HTTP_STATUS`）。非 ``ApiError`` 异常 → ``500``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Header, Request, WebSocket, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agents.assembly import MANIFEST
from api_layer.errors import ERROR_HTTP_STATUS, ApiError
from api_layer.graph_view import (
    GraphView,
    VisibilityConfig,
    build_graph_view,
    load_visibility,
)
from api_layer.run import RunRequest, RunResponse, RunService
from api_layer.ws import WSSenderService

__all__ = ["GraphResponse", "create_app", "default_visibility_path"]


def default_visibility_path() -> Path:
    """``config/visibility.yaml`` 的仓根相对路径（``src/api_layer`` 上溯两级）。"""

    return Path(__file__).resolve().parents[2] / "config" / "visibility.yaml"


# --------------------------------------------------------------------------- #
# 响应模型
# --------------------------------------------------------------------------- #


class GraphNodeOut(BaseModel):
    id: str
    label: str
    type: str
    color: str | None = None
    visible: bool
    interrupt: bool


class GraphEdgeOut(BaseModel):
    source: str
    target: str
    cond: str | None = None
    max: int | None = None


class GraphResponse(BaseModel):
    """``GET /api/agent/graph`` 输出（PRD §5.4 形状）：节点 / 边 / 可见性告警。"""

    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    warnings: list[str]


@dataclass(frozen=True)
class _AppDeps:
    """装配应用所需依赖（图驱动服务 + WS-sender + 可见性配置路径）。"""

    run_service: RunService
    ws_service: WSSenderService | None
    visibility_path: Path | None


def _graph_view_to_response(gv: GraphView) -> GraphResponse:
    return GraphResponse(
        nodes=[
            GraphNodeOut(
                id=n.id,
                label=n.label,
                type=n.type,
                color=n.color,
                visible=n.visible,
                interrupt=n.interrupt,
            )
            for n in gv.nodes
        ],
        edges=[
            GraphEdgeOut(source=e.source, target=e.target, cond=e.cond, max=e.max)
            for e in gv.edges
        ],
        warnings=list(gv.warnings),
    )


# --------------------------------------------------------------------------- #
# 应用工厂
# --------------------------------------------------------------------------- #


def create_app(
    run_service: RunService,
    *,
    ws_service: WSSenderService | None = None,
    visibility_path: Path | None = None,
) -> FastAPI:
    """装配 FastAPI 应用。

    ``run_service`` 已注入 ``Orchestrator``（checkpointer + InterruptHitl*Gate）+
    :class:`SessionCacheBase`；本函数只挂路由 + 错误处理 + user_id 抽取依赖。
    ``ws_service`` 注入则挂 ``/ws/agent/stream`` WS-sender 路由（T-06·ADR-0023：只读尾随
    ``trace_events``，WS 断开不中止 run）；缺省 ``None`` 不挂 WS 路由（T-06 前的离线态）。
    ``visibility_path`` 缺省 ``None`` → 全可见（``VisibilityConfig()``），故 ``hitl1→parse+partition``
    回放边出现（PRD §5.4）；传入路径则按部署 override 隐藏节点（文件缺失 → 全可见，不抛）。
    """

    deps = _AppDeps(
        run_service=run_service,
        ws_service=ws_service,
        visibility_path=visibility_path,
    )
    app = FastAPI(title="HypoArgus 控制面", version="0.1.0")
    app.state.deps = deps

    @app.exception_handler(ApiError)
    async def _api_error_handler(_req: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=ERROR_HTTP_STATUS.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR),
            content={"error": exc.code.value, "message": exc.message},
        )

    @app.get("/api/agent/graph", response_model=GraphResponse)
    async def get_graph() -> GraphResponse:
        """返回图结构（PRD §5.4）：来自 ``build_graph_view``，含 ``hitl1→parse+partition`` 回放边。"""

        vis = (
            load_visibility(deps.visibility_path)
            if deps.visibility_path is not None
            else VisibilityConfig()
        )
        gv = build_graph_view(MANIFEST, vis)
        return _graph_view_to_response(gv)

    @app.post("/api/agent/run", response_model=RunResponse)
    async def post_run(
        request: RunRequest,
        x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    ) -> RunResponse:
        """fresh（``query``）/ resume（``human_response``）二态驱动至终止态或暂停。"""

        return await deps.run_service.run(request, user_id=x_user_id or "")

    @app.websocket("/ws/agent/stream")
    async def ws_stream(websocket: WebSocket) -> None:
        """WS-sender（T-06·ADR-0023）：``?session_id=`` + ``X-User-Id`` 头 → 只读尾随 ``trace_events``。

        ``ws_service`` 未注入（T-06 前离线态）→ 1008 关闭；注入则委托 :meth:`WSSenderService.serve`。
        """

        if deps.ws_service is None:
            await websocket.accept()
            await websocket.close(code=1008)
            return
        session_id = websocket.query_params.get("session_id") or ""
        user_id = websocket.headers.get("x-user-id") or websocket.headers.get("X-User-Id") or ""
        await deps.ws_service.serve(websocket, session_id, user_id)

    return app
