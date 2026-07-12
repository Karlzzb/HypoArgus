"""交互式 CLI HITL 闸门——HITL-1 / HITL-2 闸门 seam 的第二 adapter。

contract 层（``Hitl1Gate`` / ``Hitl2Gate`` Protocol）是 provider-free 的同步注入闸门；
真实 ``interrupt`` + ``Command(resume)`` + checkpointer 属后续切片（dev-guide §7/§8）。
本模块是「第二个 adapter」：在终端把树 / 呈现交给**人**、用 ``input()`` 收回**纯数据决策**，
交给 ``confirm`` 校验应用。决策合法性最终仍由各 ``confirm`` 在深拷贝上兜底（HITL-1
逐 op ``validate_tree``、HITL-2 状态机）——gate 自身只做**输入解析 + 软校验**，避免
用户笔误触发 ``Hitl2GateError`` 硬停卡死整条流水线。

非交互环境（无 tty，如 CI / 管道）退化为**保守**决策：HITL-1 ``SKIP``（不改结构、
原文一字不动）、HITL-2 有待决则 ``DECIDE``+空 ops（全驳回、原文逐字节保留）——
守住「绝不替人拍板自动采纳」底线（ADR-0010）。可注入 ``input_fn`` / ``out_fn`` 供单测。
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from agents.hitl1 import (
    Hitl1Action,
    Hitl1Decision,
    Hitl1Op,
    MarkNoOpOp,
    MergeOp,
    ReparentOp,
    SetTypeOp,
    SplitOp,
)
from agents.hitl2 import (
    AdoptOp,
    EditContentOp,
    Hitl2Action,
    Hitl2Decision,
    Hitl2Op,
    Hitl2Review,
    NodeReview,
    RejectOp,
)
from domain import ArgumentationNode, NodeType

__all__ = ["CliHitl1Gate", "CliHitl2Gate"]


_INPUT_FN = Callable[[str], str]
_OUT_FN = Callable[..., None]


def _is_interactive(flag: bool | None) -> bool:
    """``flag`` 显式指定优先；否则据 stdin 是否 tty 判定。"""

    if flag is not None:
        return flag
    return sys.stdin.isatty()


# --------------------------------------------------------------------------- #
# HITL-1：结构确认
# --------------------------------------------------------------------------- #


class CliHitl1Gate:
    """交互式 HITL-1 闸门：打印树 → [s]kip/[a]ccept/[e]dit → 收 op 序列。"""

    def __init__(
        self,
        *,
        interactive: bool | None = None,
        input_fn: _INPUT_FN = input,
        out_fn: _OUT_FN = print,
    ) -> None:
        self._interactive = interactive
        self._input = input_fn
        self._out = out_fn

    def review(self, tree: list[ArgumentationNode]) -> Hitl1Decision:
        self._print_tree(tree)
        if not _is_interactive(self._interactive):
            self._out("[非交互] HITL-1 保守 SKIP（不改结构、原文不动）。")
            return Hitl1Decision(action=Hitl1Action.SKIP)
        while True:
            raw = self._input("[HITL-1] 结构确认 [s]kip/[a]ccept/[e]dit: ").strip().lower()
            if raw in ("s", "skip"):
                return Hitl1Decision(action=Hitl1Action.SKIP)
            if raw in ("a", "accept"):
                return Hitl1Decision(action=Hitl1Action.ACCEPT)
            if raw in ("e", "edit"):
                return Hitl1Decision(
                    action=Hitl1Action.EDIT, ops=self._collect_ops(tree)
                )
            self._out("未知选项，请输入 s/a/e。")

    def _print_tree(self, tree: list[ArgumentationNode]) -> None:
        self._out("=== HITL-1 结构确认：解析树 ===")
        if not tree:
            self._out("（空树）")
            return
        for n in tree:
            self._out(
                f"{n.node_id}\ttype={n.node_type.value}\tweight={n.argument_weight}"
                f"\tpara={n.paragraph_id}\tparent={n.parent_id}\tstatus={n.status.value}"
            )
        self._out(
            "编辑命令：reparent <id> <parent_id|root> | set_type <id> <type>"
            " | mark_no_op <para_id> | split <id> | merge <id> <id>... | done"
        )

    def _collect_ops(self, tree: list[ArgumentationNode]) -> list[Hitl1Op]:
        ids = {n.node_id for n in tree}
        ops: list[Hitl1Op] = []
        while True:
            raw = self._input("edit> ").strip()
            if not raw:
                continue
            if raw.lower() in ("done", "d"):
                break
            op = self._parse_op(raw, ids)
            if op is None:
                self._out(f"无法解析或非法：{raw!r}（? 查命令）")
                continue
            ops.append(op)
            self._out(f"  + {op!r}")
        return ops

    def _parse_op(
        self, raw: str, ids: set[str]
    ) -> Hitl1Op | None:
        parts = raw.split()
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd == "reparent":
                if len(args) != 2 or args[0] not in ids:
                    return None
                return ReparentOp(
                    node_id=args[0],
                    new_parent_id=None if args[1] == "root" else args[1],
                )
            if cmd == "set_type":
                if len(args) != 2 or args[0] not in ids:
                    return None
                return SetTypeOp(node_id=args[0], new_type=NodeType(args[1]))
            if cmd == "mark_no_op":
                if len(args) != 1:
                    return None
                return MarkNoOpOp(paragraph_id=args[0])
            if cmd == "split":
                if len(args) != 1 or args[0] not in ids:
                    return None
                return SplitOp(node_id=args[0])
            if cmd == "merge":
                if len(args) < 2 or any(a not in ids for a in args):
                    return None
                return MergeOp(node_ids=args)
            if cmd == "?":
                return None
        except (ValueError, KeyError):
            return None
        return None


# --------------------------------------------------------------------------- #
# HITL-2：修订确认（硬闸门）
# --------------------------------------------------------------------------- #


class CliHitl2Gate:
    """交互式 HITL-2 硬闸门：逐待决节点呈现 → 采纳/驳回/手改。

    gate 自身软校验：采纳只能勾选 ``activated_hypothesis_ids`` 内的假设；驳回限于
    候选集；手改限于待决节点。笔误重 prompt，绝不产出会让 ``confirm`` 抛
    ``Hitl2GateError`` 的决策。
    """

    def __init__(
        self,
        *,
        interactive: bool | None = None,
        input_fn: _INPUT_FN = input,
        out_fn: _OUT_FN = print,
    ) -> None:
        self._interactive = interactive
        self._input = input_fn
        self._out = out_fn

    def review(self, review: Hitl2Review) -> Hitl2Decision:
        if not review.has_pending:
            self._out("=== HITL-2：无待决内容，一键通过。 ===")
            return Hitl2Decision(action=Hitl2Action.PASS)
        if not _is_interactive(self._interactive):
            self._out("[非交互] HITL-2 有待决但无人拍板 → 全驳回、原文逐字节保留。")
            return Hitl2Decision(action=Hitl2Action.DECIDE, ops=[])
        ops: list[Hitl2Op] = []
        for node in review.nodes:
            ops.extend(self._prompt_node(node))
        return Hitl2Decision(action=Hitl2Action.DECIDE, ops=ops)

    def _prompt_node(self, node: NodeReview) -> list[Hitl2Op]:
        self._out(
            f"\n--- {node.node_id} [type={node.node_type.value} "
            f"status={node.status.value} para={node.paragraph_id}] ---"
        )
        self._out(f"原文：{node.original_text}")
        if node.issue_tags:
            self._out(f"批注：{', '.join(node.issue_tags)}")
        cand_ids = {c.hypothesis_id for c in node.candidates}
        activated = set(node.activated_hypothesis_ids)
        for c in node.candidates:
            mark = "★可采纳" if c.hypothesis_id in activated else "弱呈现"
            self._out(
                f"  [{mark}] {c.hypothesis_id} rel={c.relation.value}"
                f" status={c.status.value} conf={c.confidence:.2f}"
                f"\n    {c.text}"
            )
        self._out(
            "命令：adopt <hid>[ :: <edited_text>] | reject <hid> | "
            "edit-content <text...> | skip"
        )
        ops: list[Hitl2Op] = []
        while True:
            raw = self._input("hitl2> ").strip()
            if not raw:
                continue
            low = raw.lower()
            if low in ("skip", "s"):
                break
            if low in ("done", "d", "next", "n"):
                break
            op = self._parse_op(raw, node, activated, cand_ids)
            if op is None:
                self._out(f"无法解析或非法：{raw!r}")
                continue
            ops.append(op)
            self._out(f"  + {op!r}")
        return ops

    def _parse_op(
        self,
        raw: str,
        node: NodeReview,
        activated: set[str],
        cand_ids: set[str],
    ) -> Hitl2Op | None:
        text = raw.strip()
        low = text.lower()
        if low.startswith("adopt "):
            rest = text[len("adopt ") :].strip()
            if "::" in rest:
                hid, edited = rest.split("::", 1)
                hid, edited = hid.strip(), edited.strip()
            else:
                hid, edited = rest, None
            if hid not in activated:
                self._out(f"  {hid} 不在可采纳集（仅 ★ 标记可采纳）。")
                return None
            return AdoptOp(
                node_id=node.node_id, hypothesis_id=hid, edited_text=edited
            )
        if low.startswith("reject "):
            hid = text[len("reject ") :].strip()
            if hid not in cand_ids:
                self._out(f"  {hid} 不在候选集。")
                return None
            return RejectOp(node_id=node.node_id, hypothesis_id=hid)
        if low.startswith("edit-content "):
            content = text[len("edit-content ") :].strip()
            if not content:
                return None
            return EditContentOp(node_id=node.node_id, content=content)
        return None
