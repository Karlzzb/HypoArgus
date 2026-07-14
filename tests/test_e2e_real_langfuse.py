"""真实端到端测试：DashScope 真 LLM + Langfuse trace 落地验证。

默认 **skip**——需 ``DASHSCOPE_API_KEY`` + Langfuse 三变量 + 网络 + token。手动跑：

.. code-block:: bash

    conda run -n HypoArgus pytest -rsv tests/test_e2e_real_langfuse.py

验证两件事：
1. 真 LLM 装配（``build_qwen_chat_model`` 读 ``.env`` 的 ``qwen-max``）驱动整条流水线到终稿，
   ``run_real_pipeline`` 返回 :class:`RunResult`、``final_document`` 非空。
2. Langfuse handler 经 ``session_config["callbacks"]`` 注入后，全链 LLM 调用 / 图节点在
   Langfuse 服务端落 trace——直接 ``GET {base}/api/public/traces?userId=...`` 断言命中本会话 trace
   （``flush()`` 强制批冲后轮询，规避 5s 默认批冲延迟）。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

from agents.hitl1 import FakeHitl1Gate, Hitl1Action, Hitl1Decision
from agents.hitl2 import ConservativeHitl2Gate
from infra.observability import build_langfuse_callback, langfuse_env_present
from runtime.orchestrator import RunResult
from runtime.run_real import run_real_pipeline

# 一段真实论证文本：含主论点 / 分论点 / 论据，触发 parse + hypothesis + judgment LLM 调用。
_DOC = (
    "远程办公显著提升了研发团队的交付效率。\n\n"
    "多家科技公司的内部数据显示，远程办公期间 PR 合并速度提升约 20%。\n\n"
    "因此管理层应将远程办公作为长期可选工作模式。\n"
).encode()


def _langfuse_ready() -> bool:
    try:
        import langfuse  # noqa: F401
    except ImportError:
        return False
    import os
    from pathlib import Path

    # 与 infra.observability._load_env_file 同义：仅 setdefault 未设置的 env 变量，
    # 让 skipif 守卫据 .env 而非裸 os.environ 判定（pytest 不自动加载 .env）。
    env_path = Path.cwd() / ".env"
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    return bool(
        os.environ.get("DASHSCOPE_API_KEY")
        and langfuse_env_present()
    )


pytestmark = pytest.mark.skipif(
    not _langfuse_ready(),
    reason="needs DASHSCOPE_API_KEY + LANGFUSE_* env + langfuse SDK + network",
)


def _basic_auth_header() -> dict[str, str]:
    import os

    pub = os.environ["LANGFUSE_PUBLIC_KEY"]
    sec = os.environ["LANGFUSE_SECRET_KEY"]
    token = base64.b64encode(f"{pub}:{sec}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _fetch_traces(user_id: str, timeout: float = 30.0) -> list[dict]:
    """轮询 Langfuse ``GET /api/public/traces?userId=``，直到命中本会话 trace 或超时。"""

    import os

    base = os.environ["LANGFUSE_BASE_URL"].rstrip("/")
    url = f"{base}/api/public/traces?userId={urllib.parse.quote(user_id)}&limit=20"
    headers = _basic_auth_header()
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            last_err = exc
            time.sleep(1.0)
            continue
        data = payload.get("data", []) if isinstance(payload, dict) else []
        if data:
            return data
        time.sleep(1.0)
    if last_err is not None:
        raise AssertionError(f"Langfuse traces 查询失败：{last_err!r}")
    return []


def test_real_pipeline_with_langfuse_trace_lands():
    """真 LLM 跑流水线 → RunResult 非空终稿；Langfuse 服务端命中本会话 trace。"""

    handler = build_langfuse_callback()
    assert handler is not None, "Langfuse handler 未构造（env 未配置？）"

    session_id = f"e2e-{uuid.uuid4()}"
    user_id = f"hypoargus-e2e-{uuid.uuid4()}"
    session_config: dict[str, object] = {
        "callbacks": [handler],
        "metadata": {
            "langfuse_session_id": session_id,
            "langfuse_user_id": user_id,
            "langfuse_tags": ["e2e-real"],
        },
    }

    report = run_real_pipeline(
        _DOC,
        hitl1_gate=FakeHitl1Gate(Hitl1Decision(action=Hitl1Action.SKIP)),
        hitl2_gate=ConservativeHitl2Gate(),
        session_config=session_config,
    )

    # 1) 真 LLM 流水线跑通至终稿。
    assert isinstance(report, RunResult)
    assert report.final_document, "终稿为空"

    # 2) 强制批冲后轮询 Langfuse，断言本会话 trace 落地。
    client = getattr(handler, "_langfuse_client", None)
    if client is not None and hasattr(client, "flush"):
        client.flush()  # 强制批冲，规避 5s 默认 flush_interval

    traces = _fetch_traces(user_id, timeout=30.0)
    assert traces, f"Langfuse 未收到 user_id={user_id} 的 trace"
    own = [t for t in traces if t.get("sessionId") == session_id]
    assert own, f"Langfuse trace 未命中 session_id={session_id}（收到 {len(traces)} 条他者 trace）"
