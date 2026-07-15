"""Shared dependencies and context builder for evidence retrieval."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import EvidenceRetrievalConfig
from .evidence_judge import DeterministicEvidenceJudge, EvidenceJudge, SingleJudgeBatchAdapter
from .providers.bisheng_retrieve import BishengRetrieveClient
from .providers.structured_query import StructuredQueryClient
from .providers.volcano_web import VolcanoWebSearchClient
from .providers.web_content_fetcher import WebContentFetcher
from .schemas import PreparedContext, RetrievalTask


def build_prepared_context(task: RetrievalTask) -> PreparedContext:
    combined = " ".join(filter(None, [task.target_text, task.boundary or "", task.paragraph_text]))
    years = list(dict.fromkeys(re.findall(r"(?:19|20)\d{2}(?:年)?|近[一二三四五六七八九十\d]+年", combined)))
    regions = list(dict.fromkeys(re.findall(r"[\u4e00-\u9fff]{2,12}(?:省|市|区|县|州|自治区)", combined)))
    subject_terms = [token for token in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,24}", task.target_text)[:8]]
    metric_terms = [slot for slot in task.required_slots if any(key in slot for key in ("指标", "数量", "比例", "金额", "增速", "变化"))]
    parent = " > ".join(item.text for item in task.argument_path)
    existing = (task.existing_evidence_text or "")[:800]
    query_context = "；".join(filter(None, [
        f"目标：{task.target_text}", f"上位论证：{parent}" if parent else "",
        f"限定：{task.boundary}" if task.boundary else "", f"时间：{'、'.join(years)}" if years else "",
        f"地域：{'、'.join(regions)}" if regions else "", f"原有论据：{existing}" if existing else "",
    ]))
    return PreparedContext(
        target_text=task.target_text, paragraph_text=task.paragraph_text,
        argument_path=task.argument_path, boundary=task.boundary,
        required_slots=task.required_slots, subject_terms=subject_terms,
        time_scope=years, region_scope=regions, metric_terms=metric_terms,
        parent_argument_summary=parent, existing_evidence_summary=existing,
        source_refs=task.source_refs, query_context=query_context,
    )


@dataclass(slots=True)
class EvidenceRetrievalDependencies:
    web_search: Any | None = None
    web_fetcher: Any | None = None
    kb_client: Any | None = None
    structured_client: Any | None = None
    judge: EvidenceJudge | None = None
    batch_judge: Any | None = None
    structured_intent_model: Any | None = None

    @classmethod
    def defaults(cls, config: EvidenceRetrievalConfig) -> "EvidenceRetrievalDependencies":
        return cls(
            web_search=VolcanoWebSearchClient(config), web_fetcher=WebContentFetcher(config),
            kb_client=BishengRetrieveClient(config), structured_client=StructuredQueryClient(config),
            judge=DeterministicEvidenceJudge(),
        )

    @classmethod
    def with_llm(cls, config: EvidenceRetrievalConfig, llm, **overrides) -> "EvidenceRetrievalDependencies":
        from .evidence_judge import StructuredLLMEvidenceJudge, StructuredLLMBatchEvidenceJudge
        values = dict(
            # The parallel V7 flow uses batch_judge.  Keep the single-item
            # seam available for richer LangChain models without requiring a
            # lightweight OpenAI-compatible client to emulate structured
            # output APIs it never calls.
            judge=(
                StructuredLLMEvidenceJudge(llm)
                if hasattr(llm, "with_structured_output")
                else DeterministicEvidenceJudge()
            ),
            batch_judge=StructuredLLMBatchEvidenceJudge(llm, config),
            structured_intent_model=llm,
        )
        values.update(overrides)
        return cls(**values).complete(config)

    def complete(self, config: EvidenceRetrievalConfig) -> "EvidenceRetrievalDependencies":
        if self.web_search is None:
            self.web_search = VolcanoWebSearchClient(config)
        if self.web_fetcher is None:
            self.web_fetcher = WebContentFetcher(config)
        if self.kb_client is None:
            self.kb_client = BishengRetrieveClient(config)
        if self.structured_client is None:
            self.structured_client = StructuredQueryClient(config)
        if self.judge is None:
            self.judge = DeterministicEvidenceJudge()
        if self.batch_judge is None:
            self.batch_judge = SingleJudgeBatchAdapter(self.judge)
        return self

    async def aclose(self) -> None:
        import inspect
        seen: set[int] = set()
        for dependency in (
            self.web_search, self.web_fetcher, self.kb_client,
            self.structured_client, self.batch_judge,
        ):
            if dependency is None or id(dependency) in seen:
                continue
            seen.add(id(dependency))
            close = getattr(dependency, "aclose", None) or getattr(dependency, "close", None)
            if close is None:
                continue
            result = close()
            if inspect.isawaitable(result):
                await result
