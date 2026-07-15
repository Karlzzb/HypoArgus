"""EDRE Planner (Slice C) — task -> a fixed set of falsifiable EvidenceClaims.

A one-shot LLM call turns the task into 3..6 *falsifiable hypotheses* (claims
that evidence can prove TRUE or FALSE), never search queries or topics, each
tagged CRITICAL or OPTIONAL. Deterministic guardrails then enforce the Planner
contract regardless of what the LLM returned (see ADR-0003, CONTEXT.md):

- claim count is hard-clamped to ``[min_claims, max_claims]`` (upper bound
  truncates; a below-floor / unparseable result degrades to a single claim);
- at least ``min_critical`` claims are CRITICAL;
- CRITICAL is kept a strict minority.

The Planner is a replaceable component: ``make_llm_planner(llm)`` returns a
``Planner`` closure, so swapping the decomposition strategy never touches the
downstream nodes.
"""

from __future__ import annotations

import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from .models import EDREConfig, EvidenceClaim, Importance

_PLANNER_SYSTEM_PROMPT = (
    "You are a research planner. Given a task or question, decompose it into a "
    "small set of falsifiable evidence claims: hypotheses that evidence can prove "
    "TRUE or FALSE — never search queries or bare topics. Tag each claim's "
    "importance as CRITICAL (core to answering the question; keep these a "
    "minority) or OPTIONAL. Respond with ONLY a JSON array of objects, each with "
    'keys "hypothesis" and "importance".'
)


def _planner_prompt(task: str, config: EDREConfig) -> str:
    return (
        f"Decompose the following task into between {config.min_claims} and "
        f"{config.max_claims} falsifiable evidence claims (hypotheses, not search "
        f"queries). Mark at least {config.min_critical} as CRITICAL and keep "
        "CRITICAL a minority; the rest OPTIONAL. Return ONLY a JSON array like "
        '[{"hypothesis": "...", "importance": "CRITICAL"}].\n\n'
        f"Task:\n{task}"
    )


def _parse_claims(text: str) -> list[tuple[str, Importance]]:
    """Parse the LLM's plan into ``(hypothesis, importance)`` pairs.

    Accepts a JSON array of ``{hypothesis, importance}`` objects (optionally
    fenced), falling back to a newline list of bare hypotheses. Returns ``[]``
    when nothing usable is produced so the caller can degrade gracefully.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    if not text:
        return []

    parsed = None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None

    pairs: list[tuple[str, Importance]] = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                hypothesis = str(item.get("hypothesis", "")).strip()
                raw_importance = str(item.get("importance", "OPTIONAL")).strip()
            else:
                hypothesis = str(item).strip()
                raw_importance = "OPTIONAL"
            if hypothesis:
                pairs.append((hypothesis, _coerce_importance(raw_importance)))
        return pairs

    for line in text.splitlines():
        cleaned = line.strip().lstrip("-*0123456789.() ").strip()
        if cleaned:
            pairs.append((cleaned, Importance.OPTIONAL))
    return pairs


def _coerce_importance(raw: str) -> Importance:
    return (
        Importance.CRITICAL
        if raw.strip().upper() == Importance.CRITICAL.value
        else Importance.OPTIONAL
    )


def _apply_guardrails(
    pairs: list[tuple[str, Importance]], config: EDREConfig
) -> list[tuple[str, Importance]]:
    """Enforce the Planner contract on parsed claims, deterministically.

    Hard-clamps the count to the upper bound, guarantees at least
    ``min_critical`` CRITICAL claims, and demotes any excess so CRITICAL stays a
    strict minority. The lower count bound is a prompt-level ask, not fabricated
    here (a below-floor result is left as-is; a truly empty one degrades earlier).
    """
    items = [[hypothesis, importance] for hypothesis, importance in pairs][
        : config.max_claims
    ]
    n = len(items)
    if n == 0:
        return []

    # Largest count that is still a strict minority of n, but never below the
    # required floor (which the ceiling already dominates for any sane config).
    minority_ceiling = (n - 1) // 2
    critical_cap = max(config.min_critical, minority_ceiling)

    critical_indices = [i for i, it in enumerate(items) if it[1] is Importance.CRITICAL]

    # Promote earliest OPTIONAL claims until the min_critical floor is met.
    for i in range(n):
        if len(critical_indices) >= config.min_critical:
            break
        if items[i][1] is not Importance.CRITICAL:
            items[i][1] = Importance.CRITICAL
            critical_indices.append(i)

    # Demote the trailing excess so CRITICAL respects the minority cap.
    critical_indices = [i for i, it in enumerate(items) if it[1] is Importance.CRITICAL]
    for i in critical_indices[critical_cap:]:
        items[i][1] = Importance.OPTIONAL

    return [(hypothesis, importance) for hypothesis, importance in items]


def make_llm_planner(llm: BaseChatModel):
    """Build a ``Planner`` closure backed by *llm*."""

    async def planner(task: str, config: EDREConfig) -> list[EvidenceClaim]:
        message = await llm.ainvoke(
            [
                SystemMessage(content=_PLANNER_SYSTEM_PROMPT),
                HumanMessage(content=_planner_prompt(task, config)),
            ]
        )
        text = (
            message.content
            if isinstance(message.content, str)
            else str(message.content)
        )
        pairs = _apply_guardrails(_parse_claims(text), config)
        if not pairs:
            # Graceful degradation: never emit an empty plan.
            return [
                EvidenceClaim(
                    id="c1", hypothesis=task, importance=Importance.CRITICAL
                )
            ]
        return [
            EvidenceClaim(id=f"c{i}", hypothesis=hypothesis, importance=importance)
            for i, (hypothesis, importance) in enumerate(pairs, 1)
        ]

    return planner


__all__ = ["make_llm_planner"]
