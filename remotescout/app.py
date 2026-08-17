import datetime
import json

from flask import (
    Flask,
    abort,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from remotescout import db
from remotescout.business_time import BUSINESS_TIMEZONE, business_today
from remotescout.config import load_config
from remotescout.scoring_view import (
    compute_near_misses,
    compute_scoring_summary,
    derive_scoring_outcome_label,
    parse_gaps,
    parse_strengths,
)


def _format_date_label(value):
    try:
        return datetime.date.fromisoformat(value).strftime("%b %d, %Y")
    except (TypeError, ValueError):
        return value


SOURCE_LABELS = {
    "weworkremotely": "We Work Remotely",
}

FILTER_REASON_LABELS = {
    "unrelated_occupation": "unrelated occupation",
    "wrong_job_family": "wrong job family",
    "seniority_too_low": "seniority too low",
    "not_remote": "not remote",
    "geography_excluded": "geography excluded",
}

RUN_STATUS_LABELS = {
    "running": "Running",
    "succeeded": "Succeeded",
    "failed": "Failed",
}

SOURCE_STATUS_LABELS = {
    "running": "Running",
    "succeeded": "Succeeded",
    "failed": "Failed",
}

RECENT_RUNS_LIMIT = 30
RECENT_SCORING_RUNS_LIMIT = 10


def _scoring_rubric_excerpt():
    """Return a short, current application snippet of the scoring rubric.

    Used on the scoring detail page as a small "current scoring rubric"
    section. It is intentionally a current application excerpt, never a
    claim of historical prompt provenance.
    """
    from remotescout import scoring

    text = (scoring.SYSTEM_PROMPT or "").strip()
    if len(text) <= 600:
        return text
    return text[:600].rstrip() + "…"


def _format_pacific_time(value):
    """Convert SQLite UTC datetime to a Pacific business time display string.

    SQLite stores ``datetime('now')`` values as naive UTC strings; we surface
    them to the operator in the same America/Los_Angeles calendar the rest
    of the application already uses for the recommendation date.
    """
    if value is None:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(BUSINESS_TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")


def format_source(source_id):
    if not source_id:
        return ""
    if source_id in SOURCE_LABELS:
        return SOURCE_LABELS[source_id]
    return source_id.replace("-", " ").replace("_", " ").title()


def format_run_status(status):
    return RUN_STATUS_LABELS.get(status, status or "")


def format_source_status(status):
    return SOURCE_STATUS_LABELS.get(status, status or "")


def _load_json_list(value):
    if value is None:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [str(v) for v in parsed]
    return []


def format_filter_reasons(value):
    reasons = _load_json_list(value)
    if not reasons:
        return ""
    return ", ".join(FILTER_REASON_LABELS.get(r, r.replace("_", " ")) for r in reasons)


def derive_job_outcome(row):
    """Return (label, css_class) describing the terminal/intermediate outcome.

    Precedence reflects actual pipeline execution: an accepted recommendation
    is the most terminal state, followed by suppression types, then resolved
    attempts that did not qualify, then threshold outcomes, then scoring
    errors, then pre-score applied suppression, then filter rejections.
    """
    if row["accepted_rank"]:
        return (f"Recommended #{row['accepted_rank']}", "outcome-recommended")
    if row["suppressed_canonical_duplicate"]:
        return ("Canonical duplicate", "outcome-suppressed")
    if row["suppressed_post_resolution"]:
        return ("Already applied — after resolution", "outcome-suppressed")
    if row["resolution_attempted"] and not row["resolution_succeeded"]:
        return ("Unresolved employer posting", "outcome-unresolved")
    if row["meets_threshold"] and not row["resolution_attempted"]:
        return ("Resolution not reached", "outcome-not-reached")
    if row["scoring_succeeded"] and not row["meets_threshold"]:
        score = row["score"]
        if score is None:
            return ("Below threshold", "outcome-below")
        return (f"Below threshold — {int(score)}", "outcome-below")
    if row["scoring_attempted"] and not row["scoring_succeeded"]:
        return ("Scoring error", "outcome-error")
    if row["suppressed_pre_score"]:
        return ("Already applied — before scoring", "outcome-suppressed")
    if not row["filter_passed"]:
        reasons = format_filter_reasons(row["filter_reasons"])
        if reasons:
            return (f"Filtered — {reasons}", "outcome-filtered")
        return ("Filtered", "outcome-filtered")
    return ("Filter passed (incomplete)", "outcome-incomplete")


def compute_funnel(run_jobs):
    """Derive visible funnel counts from per-job pipeline evidence.

    Package 5 deliberately made per-job evidence authoritative; this helper
    derives non-overlapping aggregate counts without persisting new state.
    """
    counts = {
        "discovered": len(run_jobs),
        "filter_passed": 0,
        "filter_rejected": 0,
        "pre_score_applied": 0,
        "scoring_attempted": 0,
        "scoring_succeeded": 0,
        "scoring_errors": 0,
        "meets_threshold": 0,
        "below_threshold": 0,
        "resolution_attempted": 0,
        "resolved": 0,
        "unresolved": 0,
        "post_resolution_applied": 0,
        "canonical_duplicates": 0,
        "recommended": 0,
    }
    for row in run_jobs:
        if row["filter_passed"]:
            counts["filter_passed"] += 1
        else:
            counts["filter_rejected"] += 1
        if row["suppressed_pre_score"]:
            counts["pre_score_applied"] += 1
        if row["scoring_attempted"]:
            counts["scoring_attempted"] += 1
            if row["scoring_succeeded"]:
                counts["scoring_succeeded"] += 1
            else:
                counts["scoring_errors"] += 1
        if row["scoring_succeeded"] and not row["meets_threshold"]:
            counts["below_threshold"] += 1
        if row["meets_threshold"]:
            counts["meets_threshold"] += 1
        if row["resolution_attempted"]:
            counts["resolution_attempted"] += 1
            if row["resolution_succeeded"]:
                counts["resolved"] += 1
            else:
                counts["unresolved"] += 1
        if row["suppressed_post_resolution"]:
            counts["post_resolution_applied"] += 1
        if row["suppressed_canonical_duplicate"]:
            counts["canonical_duplicates"] += 1
        if row["accepted_rank"]:
            counts["recommended"] += 1
    return counts


def compute_run_summary(run_jobs):
    """Smaller aggregate shown on the runs list."""
    summary = {
        "discovered": len(run_jobs),
        "scoring_succeeded": 0,
        "meets_threshold": 0,
        "recommended": 0,
    }
    for row in run_jobs:
        if row["scoring_succeeded"]:
            summary["scoring_succeeded"] += 1
        if row["meets_threshold"]:
            summary["meets_threshold"] += 1
        if row["accepted_rank"]:
            summary["recommended"] += 1
    return summary


def _recent_run_summaries(connection, recent_runs):
    """Return per-run aggregate counters and source attempt summaries.

    Fetches both aggregations in two SQL queries rather than per-run, so the
    runs list view stays at a bounded query count regardless of how many
    attempts are listed.
    """
    if not recent_runs:
        return {}, {}
    run_ids = [run["id"] for run in recent_runs]
    placeholders = ", ".join("?" for _ in run_ids)
    rows = connection.execute(
        f"""
        SELECT run_id,
               COUNT(*) AS discovered,
               SUM(CASE WHEN scoring_succeeded = 1 THEN 1 ELSE 0 END) AS scoring_succeeded,
               SUM(CASE WHEN meets_threshold = 1 THEN 1 ELSE 0 END) AS meets_threshold,
               SUM(CASE WHEN accepted_rank IS NOT NULL THEN 1 ELSE 0 END) AS recommended
        FROM pipeline_run_jobs
        WHERE run_id IN ({placeholders})
        GROUP BY run_id
        """,
        run_ids,
    ).fetchall()
    summaries = {
        row["run_id"]: {
            "discovered": row["discovered"] or 0,
            "scoring_succeeded": row["scoring_succeeded"] or 0,
            "meets_threshold": row["meets_threshold"] or 0,
            "recommended": row["recommended"] or 0,
        }
        for row in rows
    }
    source_rows = connection.execute(
        f"""
        SELECT run_id, source, status, started_at, finished_at,
               discovered_count, error_type, error_message
        FROM pipeline_source_attempts
        WHERE run_id IN ({placeholders})
        ORDER BY run_id, id
        """,
        run_ids,
    ).fetchall()
    sources = {}
    for row in source_rows:
        sources.setdefault(row["run_id"], []).append(row)
    return summaries, sources


def create_app(config_overrides=None):
    app = Flask(__name__)
    app.config.update(load_config())
    if config_overrides:
        app.config.update(config_overrides)

    db.init_db(app.config["DATABASE_PATH"])
    app.teardown_appcontext(db.close_db)

    app.jinja_env.filters["date_label"] = _format_date_label
    app.jinja_env.filters["format_pacific_time"] = _format_pacific_time
    app.jinja_env.filters["format_source"] = format_source
    app.jinja_env.filters["format_run_status"] = format_run_status
    app.jinja_env.filters["format_source_status"] = format_source_status
    app.jinja_env.filters["format_filter_reasons"] = format_filter_reasons
    app.jinja_env.filters["scoring_outcome"] = derive_scoring_outcome_label
    app.jinja_env.filters["parse_strengths"] = parse_strengths
    app.jinja_env.filters["parse_gaps"] = parse_gaps

    @app.route("/")
    def recommendations():
        today = business_today().isoformat()
        connection = db.get_db()
        pinned = db.get_recommendations(connection, today)
        if not db.is_recommendation_day_complete(connection, today):
            return render_template(
                "recommendations.html",
                day=today,
                recommendations=[],
                state="pending",
            )
        applied_job_ids = {row["job_id"] for row in db.get_applied_jobs(connection)}
        active = [row for row in pinned if row["job_id"] not in applied_job_ids]
        if not pinned:
            state = "empty"
        elif not active:
            state = "all_applied"
        else:
            state = "active"
        return render_template(
            "recommendations.html",
            day=today,
            recommendations=active,
            state=state,
        )

    @app.route("/recommendations/<int:job_id>/applied", methods=["POST"])
    def mark_applied(job_id):
        today = business_today().isoformat()
        connection = db.get_db()
        recommended = {row["job_id"] for row in db.get_recommendations(connection, today)}
        if job_id not in recommended:
            abort(404)
        db.mark_job_applied(connection, job_id, today)
        return redirect(url_for("recommendations"))

    @app.route("/healthz")
    def healthz():
        return "ok"

    @app.route("/applications/<int:application_id>/status", methods=["POST"])
    def update_status(application_id):
        new_status = request.form.get("status", "")
        if new_status not in db.SUPPORTED_STATUSES:
            abort(400)
        result, _ = db.update_application_status(
            db.get_db(),
            application_id,
            new_status,
            business_today().isoformat(),
        )
        if result == "not_found":
            abort(404)
        return redirect(url_for("tracker"))

    @app.route("/tracker")
    def tracker():
        connection = db.get_db()
        applications = db.get_applications(connection)
        history = {
            row["id"]: db.get_application_events(connection, row["id"])
            for row in applications
        }
        return render_template(
            "tracker.html",
            applications=applications,
            history=history,
            supported_statuses=db.SUPPORTED_STATUSES,
        )

    @app.route("/runs")
    def runs():
        connection = db.get_db()
        recent_runs = db.get_recent_pipeline_runs(connection, RECENT_RUNS_LIMIT)
        summaries, sources = _recent_run_summaries(connection, recent_runs)
        decorated = []
        for run in recent_runs:
            decorated.append(
                {
                    "run": run,
                    "summary": summaries.get(
                        run["id"],
                        {"discovered": 0, "scoring_succeeded": 0, "meets_threshold": 0, "recommended": 0},
                    ),
                    "sources": sources.get(run["id"], []),
                }
            )
        return render_template(
            "runs.html",
            runs=decorated,
            has_runs=bool(recent_runs),
        )

    @app.route("/runs/<int:run_id>")
    def run_detail(run_id):
        connection = db.get_db()
        run = db.get_pipeline_run(connection, run_id)
        if run is None:
            abort(404)
        source_attempts = db.get_pipeline_source_attempts(connection, run_id)
        run_jobs = db.get_pipeline_run_jobs_with_details(connection, run_id)
        funnel = compute_funnel(run_jobs)
        job_outcomes = [(row, *derive_job_outcome(row)) for row in run_jobs]
        return render_template(
            "run_detail.html",
            run=run,
            source_attempts=source_attempts,
            funnel=funnel,
            job_outcomes=job_outcomes,
        )

    def _scoring_view_data(connection, run_id):
        run = db.get_pipeline_run(connection, run_id)
        if run is None:
            return None
        jobs = db.get_pipeline_run_scoring_jobs(connection, run_id)
        summary = compute_scoring_summary(jobs)
        near_misses = compute_near_misses(jobs, run["recommendation_threshold"])
        return {
            "run": run,
            "jobs": jobs,
            "summary": summary,
            "near_misses": near_misses,
        }

    @app.route("/scoring")
    def scoring():
        connection = db.get_db()
        recent_runs = db.get_recent_pipeline_runs(
            connection, RECENT_SCORING_RUNS_LIMIT
        )
        if not recent_runs:
            return render_template("scoring.html", has_runs=False, recent_runs=[])
        view = _scoring_view_data(connection, recent_runs[0]["id"])
        return render_template(
            "scoring.html",
            has_runs=True,
            recent_runs=recent_runs,
            view=view,
        )

    @app.route("/runs/<int:run_id>/scoring")
    def scoring_run(run_id):
        connection = db.get_db()
        view = _scoring_view_data(connection, run_id)
        if view is None:
            abort(404)
        recent_runs = db.get_recent_pipeline_runs(
            connection, RECENT_SCORING_RUNS_LIMIT
        )
        return render_template(
            "scoring_run.html",
            recent_runs=recent_runs,
            view=view,
        )

    @app.route("/runs/<int:run_id>/scoring/<int:job_id>")
    def scoring_detail(run_id, job_id):
        connection = db.get_db()
        run = db.get_pipeline_run(connection, run_id)
        if run is None:
            abort(404)
        row = db.get_pipeline_run_scoring_job(connection, run_id, job_id)
        if row is None:
            abort(404)
        return render_template(
            "scoring_detail.html",
            run=run,
            row=row,
            rubric_excerpt=_scoring_rubric_excerpt(),
        )

    return app
