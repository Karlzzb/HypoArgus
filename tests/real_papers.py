"""加载 ``markdown/`` 下真实论文为 bytes，供真实数据测试。

仅加载 ``*.md``；``markdown/`` 缺失则 ``REAL_PAPERS`` 为空——依赖它的参数化
测试自然不产出用例（而非报错），保持离线纯函数测试可跑。模块级具体全局（非
PEP 562 惰性 ``__getattr__``）——mypy 可见精确类型，导入即读盘（9 个小文件、
约 300KB，测试夹具可接受）。
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["REAL_PAPERS", "REAL_PAPER_CASES"]

# 仓库根：tests/ 上溯一层。
_REPO_ROOT = Path(__file__).resolve().parent.parent
_MARKDOWN_DIR = _REPO_ROOT / "markdown"


def _slug(filename: str) -> str:
    """由文件名生成稳定短 slug：去扩展名、去前导「N.」编号、取前导 CJK 连续段。"""

    stem = Path(filename).stem
    # 去前导「1.」「2.」等编号与空白。
    stem = re.sub(r"^\s*\d+\.\s*", "", stem)
    # 取前导连续 CJK 段作为可读 slug；无 CJK 则用整 stem。
    m = re.match(r"[一-鿿]+", stem)
    return m.group(0) if m is not None else stem


def _load() -> dict[str, bytes]:
    """读取 ``markdown/*.md``，返回 ``{稳定 id: 文件 bytes}``。

    目录缺失返回空 dict——调用方据此自然跳过参数化，不阻塞离线单测。
    """

    papers: dict[str, bytes] = {}
    if _MARKDOWN_DIR.is_dir():
        # 按文件名稳定排序后编号，保证跨机器/跨运行 id 不变。
        md_files = sorted(_MARKDOWN_DIR.glob("*.md"), key=lambda p: p.name)
        for idx, path in enumerate(md_files, start=1):
            key = f"paper_{idx:02d}_{_slug(path.name)}"
            papers[key] = path.read_bytes()
    return papers


REAL_PAPERS: dict[str, bytes] = _load()
REAL_PAPER_CASES: list[tuple[str, bytes]] = list(REAL_PAPERS.items())
