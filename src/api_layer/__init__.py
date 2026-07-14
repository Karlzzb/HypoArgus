"""``api_layer`` 子包：可视化服务一期控制面（ADR-0014 扁平 src 子包 · ADR-0022 / ADR-0024）。

T-02 落地纯数据 + 纯函数的图结构内省（:func:`graph_view.build_graph_view`）；
T-04 在其上加 HTTP 控制面：side-metadata 缓存 seam（:mod:`api_layer.session_cache`）、
错误码（:mod:`api_layer.errors`）、运行驱动（:mod:`api_layer.run`）、FastAPI 应用
（:func:`app.create_app`）。HTTP 与 WS（T-06）都调本子包的 seam，单一源不漂移。
"""

from __future__ import annotations

from api_layer.errors import (
    ERROR_HTTP_STATUS,
    PAUSE_TTL_SECONDS,
    ApiError,
    ErrorCode,
)
from api_layer.graph_view import (
    GraphEdge,
    GraphNode,
    GraphView,
    VisibilityConfig,
    build_graph_view,
    load_visibility,
)
from api_layer.session_cache import (
    DEFAULT_LOCK_TTL_SECONDS,
    OWNER_ACTIVE_WINDOW_SECONDS,
    InMemorySessionCache,
    PauseMeta,
    PostgresSessionCache,
    SessionCacheBase,
)

__all__ = [
    # errors
    "ErrorCode",
    "ApiError",
    "ERROR_HTTP_STATUS",
    "PAUSE_TTL_SECONDS",
    # graph_view
    "GraphNode",
    "GraphEdge",
    "GraphView",
    "VisibilityConfig",
    "build_graph_view",
    "load_visibility",
    # session_cache
    "SessionCacheBase",
    "PauseMeta",
    "InMemorySessionCache",
    "PostgresSessionCache",
    "DEFAULT_LOCK_TTL_SECONDS",
    "OWNER_ACTIVE_WINDOW_SECONDS",
]
