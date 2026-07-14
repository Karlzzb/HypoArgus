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
    ConfirmRewriteOp,
    EditRewriteOp,
    Hitl2Action,
    Hitl2Decision,
    Hitl2Op,
    Hitl2Review,
    ParagraphRewriteReview,
    RejectRewriteOp,
)
from domain import Argument, ArgumentType

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

    def review(self, argument_tree: list[Argument]) -> Hitl1Decision:
        self._print_tree(argument_tree)
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
                    action=Hitl1Action.EDIT, ops=self._collect_ops(argument_tree)
                )
            self._out("未知选项，请输入 s/a/e。")

    def _print_tree(self, argument_tree: list[Argument]) -> None:
        self._out("=== HITL-1 结构确认：解析树 ===")
        if not argument_tree:
            self._out("（空树）")
            return
        for n in argument_tree:
            self._out(
                f"{n.argument_id}\ttype={n.argument_type.value}\tweight={n.argument_weight}"
                f"\tpara={n.paragraph_id}\tparent={n.parent_id}\tstatus={n.status.value}"
            )
        self._out(
            "编辑命令：reparent <id> <parent_id|root> | set_type <id> <type>"
            " | mark_no_op <para_id> | split <id> | merge <id> <id>... | done"
        )

    def _collect_ops(self, argument_tree: list[Argument]) -> list[Hitl1Op]:
        ids = {n.argument_id for n in argument_tree}
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
                    argument_id=args[0],
                    new_parent_id=None if args[1] == "root" else args[1],
                )
            if cmd == "set_type":
                if len(args) != 2 or args[0] not in ids:
                    return None
                return SetTypeOp(argument_id=args[0], new_type=ArgumentType(args[1]))
            if cmd == "mark_no_op":
                if len(args) != 1:
                    return None
                return MarkNoOpOp(paragraph_id=args[0])
            if cmd == "split":
                if len(args) != 1 or args[0] not in ids:
                    return None
                return SplitOp(argument_id=args[0])
            if cmd == "merge":
                if len(args) < 2 or any(a not in ids for a in args):
                    return None
                return MergeOp(argument_ids=args)
            if cmd == "?":
                return None
        except (ValueError, KeyError):
            return None
        return None


# --------------------------------------------------------------------------- #
# HITL-2：修订确认（硬闸门）
# --------------------------------------------------------------------------- #


class CliHitl2Gate:
    """交互式 HITL-2 硬闸门：逐被触达段呈现原文 × 提议重写 → 确认 / 编辑 / 驳回。

    Slice 6（ADR-0017）重定位 hitl2 为终稿文本确认闸门后，本闸门呈现的是
    ``proposed_rewrites`` 中的被触达段（``ParagraphRewriteReview``：原文 × 提议重写
    文本），逐段产一个段级三态 op：

    - ``[c]onfirm`` → :class:`ConfirmRewriteOp`（终稿用提议文本）。
    - ``edit <text...>`` → :class:`EditRewriteOp`（终稿用编辑文本、覆盖提议）。
    - ``[r]eject``（默认） → :class:`RejectRewriteOp`（该段回退原文 bytes）。

    gate 自身只做**输入解析 + 软校验**：呈现给闸门的段即 ``proposed_rewrites`` 的合法 pid，
    故任何经此闸门产出的 op 其 ``paragraph_id`` 必在 ``proposed_rewrites`` 内——绝不会
    产出触发 :class:`Hitl2GateError` 的越权决策（笔误重 prompt，不产 op）。决策合法性最终
    仍由 :func:`agents.hitl2.confirm` 在应用 ops 时兜底校验。
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
            self._out("=== HITL-2：无提议重写，一键通过。 ===")
            return Hitl2Decision(action=Hitl2Action.PASS)
        if not _is_interactive(self._interactive):
            self._out("[非交互] HITL-2 有提议重写但无人拍板 → 全驳回、原文逐字节保留。")
            return Hitl2Decision(action=Hitl2Action.DECIDE, ops=[])
        ops: list[Hitl2Op] = []
        for paragraph in review.paragraphs:
            ops.append(self._prompt_paragraph_decision(paragraph))
        return Hitl2Decision(action=Hitl2Action.DECIDE, ops=ops)

    def _prompt_paragraph_decision(self, paragraph: ParagraphRewriteReview) -> Hitl2Op:
        self._out(f"\n--- {paragraph.paragraph_id} ---")
        self._out(f"原文：{paragraph.original_text}")
        self._out(f"提议：{paragraph.proposed_text}")
        self._out("命令：[c]onfirm | edit <text...> | [r]eject（默认 reject）")
        while True:
            raw = self._input("hitl2> ").strip()
            if not raw:
                continue
            low = raw.lower()
            if low in ("c", "confirm"):
                op: Hitl2Op = ConfirmRewriteOp(paragraph_id=paragraph.paragraph_id)
                self._out(f"  + {op!r}")
                return op
            if low in ("r", "reject"):
                op = RejectRewriteOp(paragraph_id=paragraph.paragraph_id)
                self._out(f"  + {op!r}")
                return op
            if low.startswith("edit "):
                text = raw[len("edit ") :].strip()
                if not text:
                    self._out("edit 需提供文本。")
                    continue
                op = EditRewriteOp(paragraph_id=paragraph.paragraph_id, text=text)
                self._out(f"  + {op!r}")
                return op
            self._out("未知选项，请输入 c/edit <text>/r。")
