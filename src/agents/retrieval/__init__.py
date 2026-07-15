"""检索 Agent：retrieval seam 真实适配器（PRD §8 / §Q1/Q3/B1 · Slice 2）。

ADR-0014 子包拆分：``contract.py`` 放注入 seam ``RetrievalRuntime`` Protocol + 映射纯函数
+ payload 构造纯函数 + 离线 Fake 桩；``agent.py`` 放真实适配器编排 :func:`real_retrieval`
（实现 :class:`agents.assembly.RetrievalFn`）+ daemon worker loop + 延迟单例 proxy。本
``__init__`` re-export 两者的公开符号，保持
``from agents.retrieval import real_retrieval, map_citations, ...`` 的外部 import 路径不变。

Slice 2：把 vendored SearchAgent V12 作真实检索 provider 接入 retrieval seam——填 manifest
的 ``real=None`` 空位、与 judgment 同形管理（``real=`` 工厂 + ``RealDeps.retrieval_runtime``
注入）。``with_llm=False`` 跑、丢弃 ``verdict``、judgment 重判（无双倍 LLM 成本）；loop-affine
httpx client 由 daemon worker loop 承载、同步 ``NodeFn`` 经 ``run_coroutine_threadsafe`` 桥接
（签名不动）。
"""

from agents.retrieval.agent import (
    build_real_retrieval,
    lazy_search_agent_runtime,
    real_retrieval,
)
from agents.retrieval.contract import (
    FakeSearchAgentRuntime,
    RetrievalRuntime,
    build_search_agent_payload,
    map_citations,
)

__all__ = [
    "RetrievalRuntime",
    "FakeSearchAgentRuntime",
    "map_citations",
    "build_search_agent_payload",
    "real_retrieval",
    "build_real_retrieval",
    "lazy_search_agent_runtime",
]
