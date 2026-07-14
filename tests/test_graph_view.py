"""``api_layer.graph_view`` 测试（PRD §5.4 / §10.1 / §7.3 · T-02）。

验证 ``build_graph_view`` 为纯函数、从 ``MANIFEST`` 单一源推导拓扑（含 START/END +
受控回放边）、可见性旋钮只影响展示（隐藏中间节点补直连边、回放边自环丢弃）、
HITL 节点强制可见并告警、``config/visibility.yaml`` 经 :func:`load_visibility` 生效。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from agents.assembly import MANIFEST, AgentEntry
from api_layer.graph_view import (
    GraphEdge,
    GraphNode,
    GraphView,
    VisibilityConfig,
    build_graph_view,
    load_visibility,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VISIBILITY_YAML = _REPO_ROOT / "config" / "visibility.yaml"


def _edge(gv: GraphView, source: str, target: str) -> GraphEdge | None:
    for e in gv.edges:
        if e.source == source and e.target == target:
            return e
    return None


def _node(gv: GraphView, node_id: str) -> GraphNode | None:
    for n in gv.nodes:
        if n.id == node_id:
            return n
    return None


# --------------------------------------------------------------------------- #
# 默认全可见：节点 / 展示元数据 / interrupt 标注
# --------------------------------------------------------------------------- #


def test_default_all_visible_exposes_all_nodes_with_metadata():
    """无 override：7 个 manifest 节点 + __start__ / __end__，label 默认从 name 推导、
    interrupt 仅 hitl1 / hitl2、无告警。"""

    gv = build_graph_view(MANIFEST, VisibilityConfig())

    ids = [n.id for n in gv.nodes]
    assert ids == [
        "__start__",
        "parse+partition",
        "hitl1",
        "hypothesis_propose",
        "retrieval",
        "judgment",
        "rewrite_loop",
        "hitl2",
        "__end__",
    ]
    # 展示元数据：label / node_type / color / desc 落入节点（label 非 name 即显式标注）。
    parse = _node(gv, "parse+partition")
    assert parse is not None
    assert parse.label == "解析+切分"
    assert parse.type == "parse"
    assert parse.color == "#4A90D9"
    assert parse.visible is True
    # start / end 为 system 节点。
    assert _node(gv, "__start__").type == "system"
    assert _node(gv, "__end__").type == "system"
    # interrupt 仅 hitl1 / hitl2。
    assert {n.id for n in gv.nodes if n.interrupt} == {"hitl1", "hitl2"}
    assert gv.warnings == ()


def test_node_id_is_opaque_string_with_plus():
    """``parse+partition`` 含 ``+``、作为不透明字符串整名出现、不拆分。"""

    gv = build_graph_view(MANIFEST, VisibilityConfig())
    assert _node(gv, "parse+partition") is not None
    assert "parse" not in {n.id for n in gv.nodes}
    assert "partition" not in {n.id for n in gv.nodes}


# --------------------------------------------------------------------------- #
# 线性链 + START/END 边
# --------------------------------------------------------------------------- #


def test_linear_chain_edges_from_manifest_deps():
    """从 manifest deps 推导线性链 + 起止边（与 orchestrator START/END 单一源对齐）。"""

    gv = build_graph_view(MANIFEST, VisibilityConfig())
    for src, tgt in [
        ("__start__", "parse+partition"),
        ("parse+partition", "hitl1"),
        ("hitl1", "hypothesis_propose"),
        ("hypothesis_propose", "retrieval"),
        ("retrieval", "judgment"),
        ("judgment", "rewrite_loop"),
        ("rewrite_loop", "hitl2"),
        ("hitl2", "__end__"),
    ]:
        assert _edge(gv, src, tgt) is not None, f"missing edge {src} -> {tgt}"


# --------------------------------------------------------------------------- #
# 受控回放边（ADR-0018）
# --------------------------------------------------------------------------- #


def test_replay_edge_hitl1_to_parse_partition():
    """``hitl1 → parse+partition`` 条件回放边出现：cond=replay、max=3。"""

    gv = build_graph_view(MANIFEST, VisibilityConfig())
    replay = _edge(gv, "hitl1", "parse+partition")
    assert replay is not None
    assert replay.cond == "replay"
    assert replay.max == 3
    # 正常 dep 边（反方向）仍在。
    assert _edge(gv, "parse+partition", "hitl1") is not None


# --------------------------------------------------------------------------- #
# 可见性：隐藏中间节点补直连边
# --------------------------------------------------------------------------- #


def test_hide_middle_node_bridges_predecessor_to_successor():
    """隐藏 retrieval：不出现在 nodes，hypothesis_propose→judgment 直连边补出，
    触 retrieval 的两条边移除。"""

    gv = build_graph_view(
        MANIFEST, VisibilityConfig(hidden=frozenset({"retrieval"}))
    )
    assert _node(gv, "retrieval") is None
    assert _edge(gv, "hypothesis_propose", "retrieval") is None
    assert _edge(gv, "retrieval", "judgment") is None
    # 补直连边。
    assert _edge(gv, "hypothesis_propose", "judgment") is not None
    # 其余链路不受影响。
    assert _edge(gv, "judgment", "rewrite_loop") is not None
    assert _edge(gv, "__start__", "parse+partition") is not None


def test_hide_parse_partition_drops_replay_self_loop_and_bridges_start():
    """隐藏 parse+partition：回放边 hitl1→parse+partition 经 parse+partition→hitl1 桥成
    hitl1→hitl1 自环 → 丢弃；START 改接 hitl1；parse+partition 不出现在 nodes。"""

    gv = build_graph_view(
        MANIFEST, VisibilityConfig(hidden=frozenset({"parse+partition"}))
    )
    assert _node(gv, "parse+partition") is None
    # 回放边被移除（自环丢弃）。
    assert _edge(gv, "hitl1", "parse+partition") is None
    assert _edge(gv, "hitl1", "hitl1") is None  # 不留自环
    # START 桥接到首个可见节点 hitl1。
    assert _edge(gv, "__start__", "hitl1") is not None
    # hitl1 下游链路不受影响。
    assert _edge(gv, "hitl1", "hypothesis_propose") is not None
    # 执行照跑：流水线拓扑不变（仅展示层）。
    assert _node(gv, "hitl1").visible is True


# --------------------------------------------------------------------------- #
# HITL 强制可见 + 告警
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("hidden_node", ["hitl1", "hitl2"])
def test_interrupt_node_forced_visible_with_warning(hidden_node: str) -> None:
    """配置 override 隐藏 hitl1 / hitl2：被强制 visible=True、忽略 override、记告警。"""

    gv = build_graph_view(
        MANIFEST, VisibilityConfig(hidden=frozenset({hidden_node}))
    )
    node = _node(gv, hidden_node)
    assert node is not None
    assert node.visible is True
    assert node.interrupt is True
    assert any(hidden_node in w for w in gv.warnings), gv.warnings


def test_interrupt_override_does_not_silently_drop_visibility():
    """告警信息可读、点名节点 + 说明强制可见（不静默吞）。"""

    gv = build_graph_view(
        MANIFEST, VisibilityConfig(hidden=frozenset({"hitl1", "hitl2"}))
    )
    assert len(gv.warnings) == 2
    for w in gv.warnings:
        assert "强制 visible=True" in w


# --------------------------------------------------------------------------- #
# 纯函数性
# --------------------------------------------------------------------------- #


def test_build_graph_view_is_pure_and_does_not_mutate_inputs():
    """两次调用结果相等、不修改入参（manifest / visibility 不可变）。"""

    vis = VisibilityConfig(hidden=frozenset({"retrieval"}))
    manifest_copy = deepcopy(MANIFEST)
    gv1 = build_graph_view(MANIFEST, vis)
    gv2 = build_graph_view(MANIFEST, vis)
    assert gv1 == gv2
    # manifest 未被改动（条目数、deps、name 不变）。
    assert MANIFEST == manifest_copy
    assert all(isinstance(e, AgentEntry) for e in MANIFEST)


# --------------------------------------------------------------------------- #
# load_visibility：config/visibility.yaml 生效
# --------------------------------------------------------------------------- #


def test_load_visibility_reads_repo_config_yaml():
    """仓库 ``config/visibility.yaml`` 载入：hidden == {parse+partition}。"""

    vis = load_visibility(_VISIBILITY_YAML)
    assert vis.hidden == frozenset({"parse+partition"})


def test_load_visibility_missing_file_returns_empty_config():
    """缺文件视为无 override（全可见）。"""

    vis = load_visibility(_REPO_ROOT / "config" / "__does_not_exist__.yaml")
    assert vis == VisibilityConfig()


def test_load_visibility_invalid_hidden_not_a_list_raises():
    """``hidden`` 非 list 时硬暴露 ValueError（配置错误不静默吞）。"""

    bad = _REPO_ROOT / "config" / "__bad_visibility.yaml"
    bad.write_text("hidden: not-a-list\n", encoding="utf-8")
    try:
        with pytest.raises(ValueError):
            load_visibility(bad)
    finally:
        bad.unlink()


def test_repo_visibility_yaml_hides_parse_partition_end_to_end():
    """端到端：load_visibility(repo yaml) → build_graph_view 隐藏 parse+partition
    （回放边自环丢弃、START 桥接 hitl1）。"""

    gv = build_graph_view(MANIFEST, load_visibility(_VISIBILITY_YAML))
    assert _node(gv, "parse+partition") is None
    assert _edge(gv, "hitl1", "parse+partition") is None
    assert _edge(gv, "__start__", "hitl1") is not None
    assert _node(gv, "hitl1").visible is True
