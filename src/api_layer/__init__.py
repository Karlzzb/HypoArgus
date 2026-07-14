"""``api_layer`` 子包：可视化服务一期共享 seam（ADR-0014 扁平 src 子包）。

本切片只落**纯数据 + 纯函数**的图结构内省（:func:`graph_view.build_graph_view`），
不引入 web（HTTP / WS 属 T-04 / T-06）。二者都调本 seam，单一源不漂移。
"""

from __future__ import annotations

from api_layer.graph_view import (
    GraphEdge,
    GraphNode,
    GraphView,
    VisibilityConfig,
    build_graph_view,
    load_visibility,
)

__all__ = [
    "GraphEdge",
    "GraphNode",
    "GraphView",
    "VisibilityConfig",
    "build_graph_view",
    "load_visibility",
]
