import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from flask import current_app, g

from remotescout.discovery.models import DiscoveredJob

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


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


def add_application_event(connection, application_id, event_date, status=None, note=None):
    cursor = connection.execute(
        "INSERT INTO application_events (application_id, event_date, status, note) VALUES (?, ?, ?, ?)",
        (application_id, event_date, status, note),
    )
    return cursor.lastrowid
