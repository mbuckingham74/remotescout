#!/usr/bin/env python3
"""Bounded live smoke test for the daily recommendation pipeline.

Uses live WWR discovery, real filtering, real Anthropic scoring (at most
SCORE_LIMIT plausible candidates), and real resolution, against a temporary
SQLite database. Nothing touches the normal development database.
"""
import datetime
import os
import tempfile

from remotescout import db, filtering, resolution, scoring
from remotescout.config import load_config
from remotescout.discovery import weworkremotely
from remotescout.engine import build_daily_recommendations
from remotescout.resume import extract_resume_text

SCORE_LIMIT = 5


def main():
    config = load_config()
    if not config["ANTHROPIC_API_KEY"]:
        print("Skipping smoke test: ANTHROPIC_API_KEY is not set.")
        return

    print("Fetching We Work Remotely feed ...")
    try:
        discovered = weworkremotely.fetch_jobs()
    except Exception as error:
        print(f"Skipping smoke test: live WWR discovery failed: {error}")
        return
    if not discovered:
        print("Skipping smoke test: live WWR feed returned no jobs.")
        return

    passed, rejected = filtering.filter_jobs(discovered)
    sample = passed[:SCORE_LIMIT]
    print(f"Candidates considered: {len(discovered)}")
    print(f"Candidates filtered before scoring: {len(rejected)}")
    print(f"Plausible candidates (bounded scoring sample, max {SCORE_LIMIT}): {len(sample)}")

    temp_dir = tempfile.mkdtemp(prefix="remotescout-smoke-")
    db_path = os.path.join(temp_dir, "smoke.db")
    db.init_db(db_path)
    connection = db.connect(db_path)

    resume_text = extract_resume_text(config["RESUME_PATH"])
    scoring_attempts = 0
    scoring_errors = 0
    scores = []

    def recording_scorer(job, text):
        nonlocal scoring_attempts, scoring_errors
        scoring_attempts += 1
        try:
            result = scoring.score_job(job, text)
        except scoring.ScoringError:
            scoring_errors += 1
            raise
        scores.append((job.title, job.employer, result.score, result.fit_explanation))
        return result

    resolutions = []

    def recording_resolver(job):
        result = resolution.resolve_job(job)
        resolutions.append((job.title, job.employer, result))
        return result

    day = datetime.date.today().isoformat()
    threshold = config["RECOMMENDATION_THRESHOLD"]
    try:
        recommendations = build_daily_recommendations(
            connection,
            recommendation_date=day,
            discover=lambda: list(sample),
            score=recording_scorer,
            resolve=recording_resolver,
            resume_text=resume_text,
            threshold=threshold,
        )
    finally:
        connection.close()

    print(f"Threshold: {threshold}")
    print(f"Scoring attempts this run: {scoring_attempts} "
          f"(successful: {len(scores)}, skipped after ScoringError: {scoring_errors})")
    below_threshold = []
    print(f"Scores for the bounded scored sample (this run only, max {SCORE_LIMIT}):")
    for title, employer, score, explanation in scores:
        status = "below threshold" if score < threshold else "eligible"
        if score < threshold:
            below_threshold.append(title)
        print(f"  {score:>3}  {title} ({employer}) [{status}]")
    print(f"Below threshold: {below_threshold or 'none'}")
    print("Resolution attempts (eligible candidates, descending score order):")
    resolved_count = 0
    for title, employer, result in resolutions:
        status = "resolved" if result.resolved else "failed"
        if result.resolved:
            resolved_count += 1
        print(f"  {title} ({employer}): {status} url={result.employer_url} req={result.requisition_id}")
    print(f"Resolved successfully: {resolved_count}")
    print(f"Final recommendations for {day}: {len(recommendations)}")
    for row in recommendations:
        print(
            f"  rank {row['rank']}  score {row['score']}  "
            f"{row['employer']} - {row['title']}  {row['employer_url']}"
        )
    print("Skipped as already applied: none (temporary empty database)")
    print("Note: all counts and scores above are for this single run only; each run is an "
          "independent live fetch and scoring pass and results are never aggregated across runs.")


if __name__ == "__main__":
    main()
