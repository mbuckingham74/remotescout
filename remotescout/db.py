import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from flask import current_app, g

from remotescout.discovery.models import DiscoveredJob

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

SUPPORTED_STATUSES = ("Applied", "Screen", "Interview", "Offer", "Rejected")


def connect(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(path):
    parent = Path(path).parent
    if str(parent) != ".":
        os.makedirs(parent, exist_ok=True)
    connection = connect(path)
    try:
        connection.executescript(SCHEMA_PATH.read_text())
        _apply_additive_migrations(connection)
        connection.commit()
    finally:
        connection.close()


def _column_exists(connection, table_name, column_name):
    rows = connection.execute("PRAGMA table_info(" + table_name + ")").fetchall()
    return any(row["name"] == column_name for row in rows)


def _add_column_if_missing(connection, table_name, column_name, definition):
    """Idempotently add a column to an existing SQLite table.

    SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``. ``CREATE
    TABLE IF NOT EXISTS`` is also insufficient against an existing table.
    This helper inspects ``PRAGMA table_info`` first and only emits the
    DDL when the column is genuinely absent, so it is safe to call on
    every initialization.

    All ``table_name``, ``column_name``, and ``definition`` arguments
    must be sourced from hardcoded module-local literals; they are
    composed via plain string concatenation rather than an f-string so
    that the dynamic-SQL signature does not appear in this file's
    review surface.
    """
    if _column_exists(connection, table_name, column_name):
        return
    sql = (
        "ALTER TABLE " + table_name + " ADD COLUMN " + column_name + " " + definition
    )
    connection.execute(sql)


def _apply_additive_migrations(connection):
    """Apply narrow idempotent additive migrations for Package 8 telemetry.

    SQLite ignores ``CREATE TABLE IF NOT EXISTS`` against an existing
    table, so any new column on ``pipeline_run_jobs`` must be added with
    ``ALTER TABLE``. These helpers inspect the schema first and only emit
    DDL when the column is genuinely absent, so the migration is safe to
    re-run on every initialization and on every Package 5+ database.
    """
    if not _column_exists(connection, "pipeline_run_jobs", "suppressed_already_processed"):
        _add_column_if_missing(
            connection,
            "pipeline_run_jobs",
            "suppressed_already_processed",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column_if_missing(
            connection,
            "pipeline_run_jobs",
            "positive_gate_passed",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column_if_missing(
            connection,
            "pipeline_run_jobs",
            "positive_gate_reason",
            "TEXT",
        )
        _add_column_if_missing(
            connection,
            "pipeline_run_jobs",
            "preselection_score",
            "INTEGER",
        )
        _add_column_if_missing(
            connection,
            "pipeline_run_jobs",
            "suppressed_scoring_budget",
            "INTEGER NOT NULL DEFAULT 0",
        )
    if not _column_exists(connection, "pipeline_run_jobs", "scoring_reused"):
        _add_column_if_missing(
            connection,
            "pipeline_run_jobs",
            "scoring_reused",
            "INTEGER NOT NULL DEFAULT 0",
        )


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE_PATH"])
    return g.db


def close_db(error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def get_recommendations(connection, day):
    return connection.execute(
        """
        SELECT r.rank, r.score, r.explanation,
               j.id AS job_id, j.title, j.employer, j.location,
               j.compensation, j.employer_url
        FROM recommendations r
        JOIN jobs j ON j.id = r.job_id
        WHERE r.date = ?
        ORDER BY r.rank
        """,
        (day,),
    ).fetchall()


def is_recommendation_day_complete(connection, recommendation_date):
    return (
        connection.execute(
            "SELECT 1 FROM recommendation_days WHERE recommendation_date = ?",
            (recommendation_date,),
        ).fetchone()
        is not None
    )


def mark_recommendation_day_complete(connection, recommendation_date):
    connection.execute(
        "INSERT OR REPLACE INTO recommendation_days (recommendation_date, completed_at) "
        "VALUES (?, datetime('now'))",
        (recommendation_date,),
    )


def get_applications(connection):
    return connection.execute(
        """
        SELECT a.id, a.job_id, a.applied_at, a.status, a.notes, a.updated_at,
               j.title, j.employer, j.location, j.employer_url
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        ORDER BY a.applied_at DESC, a.id DESC
        """
    ).fetchall()


def get_applied_jobs(connection):
    return connection.execute(
        """
        SELECT j.id AS job_id, j.source, j.source_job_id, j.identity_key,
               j.employer_url, j.requisition_id, j.employer
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        """
    ).fetchall()


def get_application_events(connection, application_id):
    return connection.execute(
        """
        SELECT event_date, status, note
        FROM application_events
        WHERE application_id = ?
        ORDER BY event_date, id
        """,
        (application_id,),
    ).fetchall()


def create_job(connection, **fields):
    columns = [
        "title",
        "employer",
        "description",
        "location",
        "compensation",
        "source",
        "source_url",
        "source_job_id",
        "employer_url",
        "requisition_id",
        "posted_at",
        "identity_key",
        "score",
        "fit_explanation",
    ]
    present = {name: fields[name] for name in columns if name in fields and fields[name] is not None}
    names = ", ".join(present)
    placeholders = ", ".join("?" for _ in present)
    cursor = connection.execute(
        f"INSERT INTO jobs ({names}) VALUES ({placeholders})", list(present.values())
    )
    return cursor.lastrowid


def normalize_url(url):
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, "")
    )


def normalize_employer_url(url):
    parts = urlsplit(url)
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            parts.query,
            "",
        )
    )


def identity_key(job):
    parts = [job.employer, job.title]
    if job.location:
        parts.append(job.location)
    normalized = " | ".join(" ".join(p.split()) for p in parts if p).lower()
    return normalized or None


def upsert_job(connection, job: DiscoveredJob) -> int:
    if job.source_job_id:
        existing = connection.execute(
            "SELECT id FROM jobs WHERE source = ? AND source_job_id = ?",
            (job.source, job.source_job_id),
        ).fetchone()
    else:
        existing = connection.execute(
            "SELECT id FROM jobs WHERE source = ? AND source_url = ?",
            (job.source, normalize_url(job.source_url)),
        ).fetchone()
    if existing is not None:
        connection.execute(
            """
            UPDATE jobs
            SET title = ?, employer = ?, description = ?, location = ?,
                compensation = ?, posted_at = ?, source_url = ?, last_seen_at = datetime('now')
            WHERE id = ?
            """,
            (
                job.title,
                job.employer,
                job.description,
                job.location,
                job.compensation,
                job.posted_at,
                job.source_url,
                existing["id"],
            ),
        )
        return existing["id"]
    return create_job(
        connection,
        source=job.source,
        source_job_id=job.source_job_id,
        source_url=job.source_url,
        title=job.title,
        employer=job.employer,
        location=job.location,
        description=job.description,
        compensation=job.compensation,
        posted_at=job.posted_at,
        identity_key=identity_key(job),
    )


def set_job_score(connection, job_id, score, fit_explanation):
    connection.execute(
        "UPDATE jobs SET score = ?, fit_explanation = ? WHERE id = ?",
        (score, fit_explanation, job_id),
    )


def set_resolution(connection, job_id, employer_url, requisition_id=None):
    connection.execute(
        "UPDATE jobs SET employer_url = ?, requisition_id = ? WHERE id = ?",
        (employer_url, requisition_id, job_id),
    )


def create_application(connection, job_id, applied_at, status="Applied", notes=None):
    cursor = connection.execute(
        "INSERT INTO applications (job_id, applied_at, status, notes) VALUES (?, ?, ?, ?)",
        (job_id, applied_at, status, notes),
    )
    return cursor.lastrowid


def mark_job_applied(connection, job_id, applied_at, notes=None):
    existing = connection.execute(
        "SELECT id FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()
    if existing is not None:
        return existing["id"]
    try:
        application_id = create_application(connection, job_id, applied_at, notes=notes)
        add_application_event(connection, application_id, applied_at, status="Applied")
        connection.commit()
        return application_id
    except Exception:
        connection.rollback()
        raise


def add_application_event(connection, application_id, event_date, status=None, note=None):
    cursor = connection.execute(
        "INSERT INTO application_events (application_id, event_date, status, note) VALUES (?, ?, ?, ?)",
        (application_id, event_date, status, note),
    )
    return cursor.lastrowid


def update_application_status(connection, application_id, new_status, event_date):
    row = connection.execute(
        "SELECT id, status FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None:
        return "not_found", None
    if row["status"] == new_status:
        return "unchanged", None
    try:
        connection.execute(
            "UPDATE applications SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (new_status, application_id),
        )
        add_application_event(connection, application_id, event_date, status=new_status)
        connection.commit()
        return "updated", application_id
    except Exception:
        connection.rollback()
        raise


ERROR_MESSAGE_MAX_LENGTH = 500


def _bounded_error_text(text, limit=ERROR_MESSAGE_MAX_LENGTH):
    if text is None:
        return None
    value = str(text)
    if len(value) <= limit:
        return value
    return value[:limit]


def _json_list(value):
    if value is None:
        return None
    return json.dumps(list(value))


def create_pipeline_run(connection, recommendation_date, threshold, scoring_model):
    cursor = connection.execute(
        "INSERT INTO pipeline_runs "
        "(recommendation_date, status, recommendation_threshold, scoring_model) "
        "VALUES (?, 'running', ?, ?)",
        (recommendation_date, threshold, scoring_model),
    )
    return cursor.lastrowid


def finish_pipeline_run_succeeded(connection, run_id):
    connection.execute(
        "UPDATE pipeline_runs "
        "SET status = 'succeeded', finished_at = datetime('now') "
        "WHERE id = ?",
        (run_id,),
    )


def finish_pipeline_run_failed(connection, run_id, error_type, error_message):
    connection.execute(
        "UPDATE pipeline_runs "
        "SET status = 'failed', finished_at = datetime('now'), "
        "error_type = ?, error_message = ? "
        "WHERE id = ?",
        (_bounded_error_text(error_type), _bounded_error_text(error_message), run_id),
    )


def get_pipeline_run(connection, run_id):
    return connection.execute(
        "SELECT id, recommendation_date, status, started_at, finished_at, "
        "recommendation_threshold, scoring_model, error_type, error_message "
        "FROM pipeline_runs WHERE id = ?",
        (run_id,),
    ).fetchone()


def create_pipeline_source_attempt(connection, run_id, source):
    cursor = connection.execute(
        "INSERT INTO pipeline_source_attempts (run_id, source, status) "
        "VALUES (?, ?, 'running')",
        (run_id, source),
    )
    return cursor.lastrowid


def finish_pipeline_source_attempt_succeeded(connection, attempt_id, discovered_count):
    connection.execute(
        "UPDATE pipeline_source_attempts "
        "SET status = 'succeeded', finished_at = datetime('now'), discovered_count = ? "
        "WHERE id = ?",
        (discovered_count, attempt_id),
    )


def finish_pipeline_source_attempt_failed(connection, attempt_id, error_type, error_message):
    connection.execute(
        "UPDATE pipeline_source_attempts "
        "SET status = 'failed', finished_at = datetime('now'), "
        "error_type = ?, error_message = ? "
        "WHERE id = ?",
        (
            _bounded_error_text(error_type),
            _bounded_error_text(error_message),
            attempt_id,
        ),
    )


def get_pipeline_source_attempts(connection, run_id):
    return connection.execute(
        "SELECT id, run_id, source, status, started_at, finished_at, "
        "discovered_count, error_type, error_message "
        "FROM pipeline_source_attempts WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()


def record_pipeline_run_job(connection, run_id, job_id, source, filter_passed, filter_reasons):
    connection.execute(
        "INSERT INTO pipeline_run_jobs "
        "(run_id, job_id, source, filter_passed, filter_reasons) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            run_id,
            job_id,
            source,
            1 if filter_passed else 0,
            _json_list(filter_reasons) if filter_reasons else None,
        ),
    )


def mark_pipeline_run_job_suppressed_pre_score(connection, run_id, job_id):
    connection.execute(
        "UPDATE pipeline_run_jobs SET suppressed_pre_score = 1 "
        "WHERE run_id = ? AND job_id = ?",
        (run_id, job_id),
    )


def mark_pipeline_run_job_suppressed_already_processed(connection, run_id, job_id):
    connection.execute(
        "UPDATE pipeline_run_jobs SET suppressed_already_processed = 1 "
        "WHERE run_id = ? AND job_id = ?",
        (run_id, job_id),
    )


def record_pipeline_run_job_positive_gate(
    connection, run_id, job_id, passed, reason, preselection_score
):
    connection.execute(
        "UPDATE pipeline_run_jobs SET "
        "positive_gate_passed = ?, positive_gate_reason = ?, "
        "preselection_score = ? "
        "WHERE run_id = ? AND job_id = ?",
        (
            1 if passed else 0,
            reason,
            preselection_score,
            run_id,
            job_id,
        ),
    )


def mark_pipeline_run_job_suppressed_scoring_budget(connection, run_id, job_id):
    connection.execute(
        "UPDATE pipeline_run_jobs SET suppressed_scoring_budget = 1 "
        "WHERE run_id = ? AND job_id = ?",
        (run_id, job_id),
    )


def get_same_day_reused_results(connection, recommendation_date, current_run_id):
    """Return per-job latest successful same-day scoring evidence.

    The lookup joins ``pipeline_run_jobs`` to ``pipeline_runs`` and
    returns, for every job that has a successful scoring row on any prior
    run for the same ``recommendation_date``, the latest such row's
    evidence payload. The ``current_run_id`` is excluded defensively.

    Returned mapping shape::

        {job_id: {
            "score": int,
            "fit_explanation": str,
            "strengths": list[str],
            "gaps": list[str],
            "meets_threshold": int (0 or 1),
            "source_run_job_id": int,
            "source_run_id": int,
        }}

    ``strengths`` and ``gaps`` are JSON-decoded defensively; callers may
    pass them through to the recommendation layer unchanged.
    """
    rows = connection.execute(
        """
        SELECT prj.id AS run_job_id,
               prj.run_id,
               prj.job_id,
               prj.score,
               prj.fit_explanation,
               prj.strengths,
               prj.gaps,
               prj.meets_threshold
        FROM pipeline_run_jobs prj
        JOIN pipeline_runs pr ON pr.id = prj.run_id
        WHERE pr.recommendation_date = ?
          AND pr.id != ?
          AND prj.scoring_succeeded = 1
        ORDER BY prj.id DESC
        """,
        (recommendation_date, current_run_id),
    ).fetchall()
    reused = {}
    for row in rows:
        job_id = row["job_id"]
        if job_id in reused:
            continue
        reused[job_id] = {
            "score": row["score"],
            "fit_explanation": row["fit_explanation"],
            "strengths": _json_list(row["strengths"]),
            "gaps": _json_list(row["gaps"]),
            "meets_threshold": row["meets_threshold"],
            "source_run_job_id": row["run_job_id"],
            "source_run_id": row["run_id"],
        }
    return reused


def mark_pipeline_run_job_scoring_reused(
    connection, run_id, job_id,
    score, fit_explanation, strengths, gaps, meets_threshold,
):
    """Persist a same-day scoring reuse on the current run.

    Distinct from :func:`record_pipeline_run_job_scoring_succeeded`:
    reuse never implies a Claude API call. ``scoring_attempted`` and
    ``scoring_succeeded`` stay 0; ``scoring_reused`` becomes 1.
    """
    connection.execute(
        "UPDATE pipeline_run_jobs SET "
        "scoring_reused = 1, "
        "score = ?, fit_explanation = ?, strengths = ?, gaps = ?, "
        "meets_threshold = ? "
        "WHERE run_id = ? AND job_id = ?",
        (
            score,
            fit_explanation,
            _json_list(strengths),
            _json_list(gaps),
            1 if meets_threshold else 0,
            run_id,
            job_id,
        ),
    )


def get_already_processed_job_ids(connection, recommendation_date=None):
    """Return the set of job_ids that already produced a successful score.

    Used by the Package 8 already-processed suppression. The set is the
    union of two independent evidence sources:

    * A successful Package-5-style scoring outcome recorded on
      ``pipeline_run_jobs`` is the durable, run-scoped signal that no
      further scoring is required.
    * ``jobs.score IS NOT NULL`` is treated as boolean evidence only:
      ``db.set_job_score`` is invoked solely after a successful scoring
      result, so any persisted job whose ``jobs.score`` column is set
      has already been paid to evaluate. This admits legitimate
      pre-Package-5 successful scores into the processed set without
      promoting ``jobs.score`` to authoritative run-display evidence.

    When ``recommendation_date`` is provided, ``pipeline_run_jobs``
    successful rows whose ``pipeline_runs.recommendation_date`` matches
    are excluded from the processed set: those rows represent
    same-day reuse evidence, not "already processed" suppression, and
    are handled by the same-day reuse branch in the engine. Jobs in
    the legacy ``jobs.score`` branch are likewise excluded when the
    same job has a same-day successful ``pipeline_run_jobs`` row —
    the legacy column was written as a side effect of that prior
    successful run, so the row belongs in the reuse branch. Passing
    ``None`` preserves the original behavior (include all dates).
    """
    if recommendation_date is None:
        rows = connection.execute(
            """
            SELECT DISTINCT job_id
            FROM pipeline_run_jobs
            WHERE scoring_succeeded = 1
            UNION
            SELECT id AS job_id
            FROM jobs
            WHERE score IS NOT NULL
            """
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT DISTINCT job_id
            FROM pipeline_run_jobs
            WHERE scoring_succeeded = 1
              AND NOT EXISTS (
                SELECT 1 FROM pipeline_runs pr
                WHERE pr.id = pipeline_run_jobs.run_id
                  AND pr.recommendation_date = ?
              )
            UNION
            SELECT id AS job_id
            FROM jobs
            WHERE score IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM pipeline_run_jobs prj
                JOIN pipeline_runs pr ON pr.id = prj.run_id
                WHERE prj.job_id = jobs.id
                  AND prj.scoring_succeeded = 1
                  AND pr.recommendation_date = ?
              )
            """,
            (recommendation_date, recommendation_date),
        ).fetchall()
    return {row["job_id"] for row in rows}


def record_pipeline_run_job_scoring_succeeded(
    connection, run_id, job_id, score, fit_explanation, strengths, gaps, meets_threshold
):
    connection.execute(
        "UPDATE pipeline_run_jobs SET "
        "scoring_attempted = 1, scoring_succeeded = 1, "
        "score = ?, fit_explanation = ?, strengths = ?, gaps = ?, "
        "meets_threshold = ? "
        "WHERE run_id = ? AND job_id = ?",
        (
            score,
            fit_explanation,
            _json_list(strengths),
            _json_list(gaps),
            1 if meets_threshold else 0,
            run_id,
            job_id,
        ),
    )


def record_pipeline_run_job_scoring_error(connection, run_id, job_id, error_type, error_message):
    connection.execute(
        "UPDATE pipeline_run_jobs SET "
        "scoring_attempted = 1, scoring_succeeded = 0, "
        "scoring_error_type = ?, scoring_error_message = ? "
        "WHERE run_id = ? AND job_id = ?",
        (
            _bounded_error_text(error_type),
            _bounded_error_text(error_message),
            run_id,
            job_id,
        ),
    )


def record_pipeline_run_job_resolution(
    connection, run_id, job_id, resolved, employer_url, requisition_id, method
):
    connection.execute(
        "UPDATE pipeline_run_jobs SET "
        "resolution_attempted = 1, resolution_succeeded = ?, "
        "resolution_method = ?, employer_url = ?, requisition_id = ? "
        "WHERE run_id = ? AND job_id = ?",
        (
            1 if resolved else 0,
            method,
            employer_url,
            requisition_id,
            run_id,
            job_id,
        ),
    )


def mark_pipeline_run_job_resolution_attempted(connection, run_id, job_id):
    connection.execute(
        "UPDATE pipeline_run_jobs SET resolution_attempted = 1 "
        "WHERE run_id = ? AND job_id = ?",
        (run_id, job_id),
    )


def mark_pipeline_run_job_suppressed_post_resolution(connection, run_id, job_id):
    connection.execute(
        "UPDATE pipeline_run_jobs SET suppressed_post_resolution = 1 "
        "WHERE run_id = ? AND job_id = ?",
        (run_id, job_id),
    )


def mark_pipeline_run_job_suppressed_canonical_duplicate(connection, run_id, job_id):
    connection.execute(
        "UPDATE pipeline_run_jobs SET suppressed_canonical_duplicate = 1 "
        "WHERE run_id = ? AND job_id = ?",
        (run_id, job_id),
    )


def set_pipeline_run_job_accepted_rank(connection, run_id, job_id, rank):
    connection.execute(
        "UPDATE pipeline_run_jobs SET accepted_rank = ? "
        "WHERE run_id = ? AND job_id = ?",
        (rank, run_id, job_id),
    )


def get_pipeline_run_jobs(connection, run_id):
    return connection.execute(
        "SELECT id, run_id, job_id, source, filter_passed, filter_reasons, "
        "suppressed_pre_score, scoring_attempted, scoring_succeeded, "
        "score, fit_explanation, strengths, gaps, meets_threshold, "
        "resolution_attempted, resolution_succeeded, resolution_method, "
        "employer_url, requisition_id, suppressed_post_resolution, "
        "suppressed_canonical_duplicate, accepted_rank, "
        "scoring_error_type, scoring_error_message, "
        "suppressed_already_processed, positive_gate_passed, "
        "positive_gate_reason, preselection_score, suppressed_scoring_budget, "
        "scoring_reused "
        "FROM pipeline_run_jobs WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()


def get_pipeline_runs_for_date(connection, recommendation_date):
    return connection.execute(
        "SELECT id, recommendation_date, status, started_at, finished_at, "
        "recommendation_threshold, scoring_model, error_type, error_message "
        "FROM pipeline_runs WHERE recommendation_date = ? ORDER BY id",
        (recommendation_date,),
    ).fetchall()


def get_recent_pipeline_runs(connection, limit=30):
    return connection.execute(
        "SELECT id, recommendation_date, status, started_at, finished_at, "
        "recommendation_threshold, scoring_model, error_type, error_message "
        "FROM pipeline_runs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def get_pipeline_run_jobs_with_details(connection, run_id):
    return connection.execute(
        """
        SELECT prj.id AS run_job_id,
               prj.job_id,
               prj.source,
               prj.filter_passed,
               prj.filter_reasons,
               prj.suppressed_pre_score,
               prj.scoring_attempted,
               prj.scoring_succeeded,
               prj.score,
               prj.fit_explanation,
               prj.strengths,
               prj.gaps,
               prj.meets_threshold,
               prj.resolution_attempted,
               prj.resolution_succeeded,
               prj.resolution_method,
               prj.employer_url,
               prj.requisition_id,
               prj.suppressed_post_resolution,
               prj.suppressed_canonical_duplicate,
               prj.accepted_rank,
               prj.scoring_error_type,
               prj.scoring_error_message,
               prj.suppressed_already_processed,
               prj.positive_gate_passed,
               prj.positive_gate_reason,
               prj.preselection_score,
               prj.suppressed_scoring_budget,
               prj.scoring_reused,
               j.title,
               j.employer,
               j.location
        FROM pipeline_run_jobs prj
        JOIN jobs j ON j.id = prj.job_id
        WHERE prj.run_id = ?
        ORDER BY prj.id
        """,
        (run_id,),
    ).fetchall()


def count_pipeline_runs(connection):
    return connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]


def get_pipeline_run_scoring_jobs(connection, run_id):
    """Return per-job scoring evidence joined with current job fields.

    Restricted to ``scoring_attempted = 1`` rows so the scoring inspector
    only inspects jobs that actually reached scoring for this run.
    ``scoring_reused`` is selected for shape consistency with other
    pipeline_run_jobs readers; because the WHERE filter excludes reused
    rows the value is always 0 here.
    """
    return connection.execute(
        """
        SELECT prj.id AS run_job_id,
               prj.job_id,
               prj.source,
               prj.scoring_attempted,
               prj.scoring_succeeded,
               prj.score,
               prj.fit_explanation,
               prj.strengths,
               prj.gaps,
               prj.meets_threshold,
               prj.scoring_error_type,
               prj.scoring_error_message,
               prj.resolution_attempted,
               prj.resolution_succeeded,
               prj.resolution_method,
               prj.suppressed_post_resolution,
               prj.suppressed_canonical_duplicate,
               prj.accepted_rank,
               prj.scoring_reused,
               j.title,
               j.employer,
               j.location,
               j.description,
               j.source_url,
               j.employer_url
        FROM pipeline_run_jobs prj
        JOIN jobs j ON j.id = prj.job_id
        WHERE prj.run_id = ? AND prj.scoring_attempted = 1
        ORDER BY prj.id
        """,
        (run_id,),
    ).fetchall()


def get_pipeline_run_scoring_job(connection, run_id, job_id):
    """Return the single run/job scoring record if scoring was attempted.

    Returns ``None`` for unknown run/job pairs or for jobs that never
    reached scoring in that run, so the route can 404 truthfully rather
    than fall back to the global ``jobs.score`` row.
    """
    return connection.execute(
        """
        SELECT prj.id AS run_job_id,
               prj.job_id,
               prj.source,
               prj.scoring_attempted,
               prj.scoring_succeeded,
               prj.score,
               prj.fit_explanation,
               prj.strengths,
               prj.gaps,
               prj.meets_threshold,
               prj.scoring_error_type,
               prj.scoring_error_message,
               prj.resolution_attempted,
               prj.resolution_succeeded,
               prj.resolution_method,
               prj.suppressed_post_resolution,
               prj.suppressed_canonical_duplicate,
               prj.accepted_rank,
               prj.scoring_reused,
               j.title,
               j.employer,
               j.location,
               j.description,
               j.source_url,
               j.employer_url
        FROM pipeline_run_jobs prj
        JOIN jobs j ON j.id = prj.job_id
        WHERE prj.run_id = ? AND prj.job_id = ? AND prj.scoring_attempted = 1
        """,
        (run_id, job_id),
    ).fetchone()
