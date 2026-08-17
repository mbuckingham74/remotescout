"""Read-only scoring inspector presentation helpers.

Package 7 turns Package 5's durable ``pipeline_run_jobs`` evidence into
human-inspection surfaces. This module owns the small presentation
logic those surfaces need: defensive JSON parsing, threshold
categorization, narrow near-miss selection, and run-scoped summary
counts.

It never calls scoring, discovery, resolution, or any external network.
Every helper here is a pure function over already-loaded Package 5
rows.
"""
import json

NEAR_MISS_WINDOW = 10
MAX_NEAR_MISSES = 10


def _parse_json_list(value):
    """Parse a Package-5-persisted JSON list defensively.

    Package 5 stored ``strengths`` and ``gaps`` as JSON text. Older rows
    may be missing the field, contain malformed JSON, or contain
    non-list payloads. This helper returns a list of strings, or an
    empty list when the payload is unusable. It never raises and never
    falls back to ``eval``.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str)]
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(v) for v in parsed if isinstance(v, str)]


def parse_strengths(value):
    return _parse_json_list(value)


def parse_gaps(value):
    return _parse_json_list(value)


def derive_threshold_result(row):
    """Return ``(label, css_class)`` describing the scoring threshold outcome.

    Distinct from the Package 6 downstream outcome: this is purely about
    what scoring produced versus the configured threshold.
    """
    if not row["scoring_attempted"]:
        return ("Pending", "threshold-pending")
    if row["scoring_succeeded"]:
        if row["meets_threshold"]:
            return ("Passed", "threshold-passed")
        return ("Below", "threshold-below")
    return ("Error", "threshold-error")


def derive_scoring_outcome_label(row):
    """Run-scoped downstream outcome label.

    Reuses Package 6 ordering so the scoring inspector does not invent
    a second incompatible outcome vocabulary. The label describes what
    happened to the job after scoring within this run only.
    """
    if row["accepted_rank"]:
        return f"Recommended #{row['accepted_rank']}"
    if row["suppressed_canonical_duplicate"]:
        return "Canonical duplicate"
    if row["suppressed_post_resolution"]:
        return "Already applied — after resolution"
    if row["resolution_attempted"] and not row["resolution_succeeded"]:
        return "Unresolved employer posting"
    if row["meets_threshold"] and not row["resolution_attempted"]:
        return "Resolution not reached"
    if row["scoring_succeeded"] and not row["meets_threshold"]:
        if row["score"] is None:
            return "Below threshold"
        return f"Below threshold — {int(row['score'])}"
    if row["scoring_attempted"] and not row["scoring_succeeded"]:
        return "Scoring error"
    return "Scoring incomplete"


def compute_scoring_summary(rows):
    """Derive scoring-only counters from Package 5 run/job evidence.

    No new aggregates are persisted; this function walks the per-job
    rows the route already loaded and counts in-memory.
    """
    summary = {
        "scoring_attempted": 0,
        "scoring_succeeded": 0,
        "scoring_errors": 0,
        "meets_threshold": 0,
        "below_threshold": 0,
    }
    for row in rows:
        summary["scoring_attempted"] += 1
        if row["scoring_succeeded"]:
            summary["scoring_succeeded"] += 1
        else:
            summary["scoring_errors"] += 1
        if row["meets_threshold"]:
            summary["meets_threshold"] += 1
        elif row["scoring_succeeded"]:
            summary["below_threshold"] += 1
    return summary


def compute_near_misses(rows, threshold, *, limit=MAX_NEAR_MISSES):
    """Apply the fixed narrow near-miss rule.

    A near miss is a successful scoring result whose score satisfies:

        threshold - 10 <= score < threshold

    The window is intentionally narrow. It is **not** broadened when no
    results land inside it; instead, the caller renders an explicit
    "no scores landed within 10 points of the threshold" message.

    Returns:

    - ``None`` when ``threshold`` is missing or non-numeric, so the
      caller can render Near Misses as unavailable.
    - A list of at most ``limit`` matching rows ordered by score
      descending, then by the stable ``(run_job_id, job_id)`` tuple as
      deterministic tie-breaker.
    """
    if threshold is None:
        return None
    try:
        threshold_value = float(threshold)
    except (TypeError, ValueError):
        return None
    lower = threshold_value - NEAR_MISS_WINDOW
    near = []
    for row in rows:
        if not row["scoring_succeeded"]:
            continue
        if row["meets_threshold"]:
            continue
        if row["score"] is None:
            continue
        if row["score"] < lower:
            continue
        if row["score"] >= threshold_value:
            continue
        near.append(row)
    near.sort(
        key=lambda r: (
            -int(r["score"]),
            r["run_job_id"],
            r["job_id"],
        )
    )
    return near[:limit]


def format_below_distance(score, threshold):
    """Return ``"N below"`` for a successful below-threshold score."""
    if score is None or threshold is None:
        return ""
    try:
        delta = int(float(threshold) - float(score))
    except (TypeError, ValueError):
        return ""
    if delta <= 0:
        return "At threshold"
    return f"{delta} below"