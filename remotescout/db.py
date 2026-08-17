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
        connection.commit()
    finally:
        connection.close()


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
        "scoring_error_type, scoring_error_message "
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
