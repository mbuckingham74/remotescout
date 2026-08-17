"""Zero-Anthropic selector replay for Package 8 cost containment.

This script demonstrates the new Package 8 selector's behavior on a
fresh WWR feed (or a local production database, when available) without
ever invoking the scoring client. It exercises:

  * deterministic hard-reject filter
  * already-processed suppression (against ``pipeline_run_jobs``)
  * positive deterministic target-role gate
  * deterministic pre-scoring ranking
  * hard scoring budget

Use ``--database`` to point at the live production database (read-only).
Without ``--database``, the script fetches a live WWR feed without
scoring anything.

The script never modifies the database. It opens a temporary copy if
``--database`` is passed, then runs the selector over that copy.
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile

from remotescout import db, filtering, ranking, targeting
from remotescout.config import load_config
from remotescout.discovery import weworkremotely


SCORING_BUDGET = 15


def already_processed(connection):
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "pipeline_run_jobs" not in tables:
        return set()
    rows = connection.execute(
        "SELECT DISTINCT job_id FROM pipeline_run_jobs "
        "WHERE scoring_succeeded = 1"
    ).fetchall()
    return {row[0] for row in rows}


def run_selector(db_path, jobs, budget):
    connection = db.connect(db_path)
    try:
        processed = already_processed(connection)
    finally:
        connection.close()

    discovered = len(jobs)
    hard_rejected = []
    candidates = []
    for job in jobs:
        result = filtering.filter_job(job)
        if not result.passed:
            hard_rejected.append((job, result))
            continue
        candidates.append(job)

    surviving = candidates
    after_already = surviving

    passed_gate = []
    rejected_gate = []
    for job in after_already:
        gate = targeting.evaluate(job)
        if gate.passed:
            passed_gate.append((job, gate))
        else:
            rejected_gate.append((job, gate))

    ranked = ranking.rank_candidates(
        [(index + 1, job) for index, (job, _gate) in enumerate(passed_gate)]
    )
    would_score = ranked[:budget]
    deferred = ranked[budget:]

    print(f"Discovered: {discovered}")
    print(f"Hard-filter rejected: {len(hard_rejected)}")
    print(f"Already processed: {len(surviving) - len(after_already)}")
    print(f"Positive-gate rejected: {len(rejected_gate)}")
    print(f"Eligible: {len(ranked)}")
    print(f"Would be scored under budget={budget}: {len(would_score)}")
    print(f"Budget deferred: {len(deferred)}")
    print()
    print("Candidates that WOULD have reached Claude (by preselection rank):")
    for index, candidate in enumerate(would_score, start=1):
        print(f"  {index:>2}. {candidate.job.title} - {candidate.job.employer}")
    print()
    if deferred:
        print("First few budget-deferred candidates (would not reach Claude):")
        for index, candidate in enumerate(deferred[:5], start=1):
            print(f"  {index:>2}. {candidate.job.title} - {candidate.job.employer}")

    near_target_keywords = (
        "program",
        "project",
        "delivery",
        "portfolio",
        "pmo",
        "epmo",
        "tpm",
        "product",
        "transformation",
        "technology",
        "technical",
        "infrastructure",
        "platform",
        "cloud",
    )
    near_target_rejections = []
    for job, gate in rejected_gate:
        text = f"{job.title} {job.description or ''}".lower()
        if any(keyword in text for keyword in near_target_keywords):
            near_target_rejections.append((job, gate))
    print()
    print(
        f"Near-target titles still rejected by the positive gate: "
        f"{len(near_target_rejections)}"
    )
    if near_target_rejections:
        print(
            "These titles contain at least one near-target keyword but the "
            "gate still rejected them. Treat as a false-negative diagnostic."
        )
        for index, (job, gate) in enumerate(near_target_rejections, start=1):
            print(
                f"  {index:>3}. {job.title} - {job.employer}  "
                f"[reason: {gate.reason}]"
            )


def replay_against_local_database(db_path):
    print(f"Reading WWR feed for local database replay: {db_path}")
    jobs = weworkremotely.fetch_jobs()
    if not jobs:
        print("Live WWR feed returned no jobs.")
        return
    work_dir = tempfile.mkdtemp(prefix="remotescout-replay-")
    try:
        replay_db = os.path.join(work_dir, "replay.db")
        shutil.copy(db_path, replay_db)
        run_selector(replay_db, jobs, SCORING_BUDGET)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def replay_against_live_feed():
    print("Reading WWR feed for selector-only replay (no database):")
    jobs = weworkremotely.fetch_jobs()
    if not jobs:
        print("Live WWR feed returned no jobs.")
        return
    work_dir = tempfile.mkdtemp(prefix="remotescout-selector-")
    try:
        replay_db = os.path.join(work_dir, "selector.db")
        db.init_db(replay_db)
        run_selector(replay_db, jobs, SCORING_BUDGET)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default=None,
        help="Optional path to a local SQLite database to read for already-processed evidence.",
    )
    args = parser.parse_args()

    if args.database:
        if not os.path.exists(args.database):
            print(f"Database not found: {args.database}", file=sys.stderr)
            return 1
        replay_against_local_database(args.database)
    else:
        replay_against_live_feed()
    return 0


if __name__ == "__main__":
    sys.exit(main())