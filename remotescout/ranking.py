"""Deterministic pre-scoring candidate ranking for Package 8.

Package 8 narrows the field before Claude is invoked. The narrowed
field still needs to be ordered deterministically so the scoring
budget is spent on the most plausible candidates first.

This module is intentionally narrow:

- No Claude scores are available yet.
- No embeddings, no randomness, no network calls.
- The point model rewards target-role alignment and senior-level
  leadership context that has historically produced the strongest
  resume fits.
- Stable tie-break is ``(relevance_score desc, job_id asc)`` so two
  fixtures with identical scores produce identical ordering.
"""
from dataclasses import dataclass

from remotescout.discovery.models import DiscoveredJob

from remotescout.targeting import (
    CONTEXT_TITLE_REASON,
    STRONG_TITLE_REASON,
    evaluate as evaluate_gate,
)


LEADERSHIP_TOKENS = (
    "director",
    "principal",
    "lead",
    "senior",
    "sr.",
    "sr ",
    "head",
    "vp",
    "vice president",
    "chief",
)


PROGRAM_FAMILY_TOKENS = (
    "program",
    "programme",
    "delivery",
    "pmo",
    "epmo",
    "portfolio",
)


REMOTE_CONTEXT_TOKENS = (
    "remote",
    "anywhere",
    "us",
    "usa",
    "united states",
    "north america",
)


@dataclass(frozen=True)
class RankedCandidate:
    job_id: int
    job: DiscoveredJob
    relevance_score: int
    gate_reason: str


def _has_any_token(text, tokens):
    for token in tokens:
        if token in text:
            return True
    return False


def _gate_bonus(reason):
    if reason == STRONG_TITLE_REASON:
        return 5
    if reason == CONTEXT_TITLE_REASON:
        return 2
    return 0


def _title_text(job):
    return (job.title or "").lower()


def _location_text(job):
    return (job.location or "").lower()


def _description_text(job):
    return (job.description or "").lower()


def rank_candidates(candidates):
    """Return candidates ordered by deterministic relevance.

    ``candidates`` is an iterable of ``(job_id, job)`` pairs. The
    returned list is ordered by descending ``relevance_score`` and then
    by ascending ``job_id`` for stable ties. The caller is expected to
    have already filtered out hard-rejected and applied jobs; this
    ranker assumes positive-gate survivors.
    """
    ranked = []
    for job_id, job in candidates:
        gate = evaluate_gate(job)
        if not gate.passed:
            continue
        title = _title_text(job)
        location = _location_text(job)
        description = _description_text(job)
        score = 0
        score += _gate_bonus(gate.reason)
        if _has_any_token(title, LEADERSHIP_TOKENS):
            score += 4
        if _has_any_token(title, PROGRAM_FAMILY_TOKENS) or _has_any_token(
            description, PROGRAM_FAMILY_TOKENS
        ):
            score += 2
        if _has_any_token(title, REMOTE_CONTEXT_TOKENS) or _has_any_token(
            location, REMOTE_CONTEXT_TOKENS
        ):
            score += 1
        ranked.append(
            RankedCandidate(
                job_id=job_id,
                job=job,
                relevance_score=score,
                gate_reason=gate.reason,
            )
        )
    ranked.sort(key=lambda candidate: (-candidate.relevance_score, candidate.job_id))
    return ranked