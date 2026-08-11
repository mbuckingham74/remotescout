import pytest

from remotescout import db
from remotescout.app import create_app
from remotescout.discovery import DiscoveredJob


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE_PATH": str(tmp_path / "test.db")})


@pytest.fixture
def connection(app):
    with app.app_context():
        yield db.get_db()


def complete_job(**overrides):
    fields = {
        "source": "weworkremotely",
        "source_job_id": "job-123",
        "source_url": "https://weworkremotely.com/remote-jobs/job-123",
        "title": "Senior Product Manager",
        "employer": "Example Co.",
        "location": "Remote (US)",
        "description": "Lead product delivery for a platform team.",
        "compensation": "$140k - $170k",
        "posted_at": "2026-08-10",
    }
    fields.update(overrides)
    return DiscoveredJob(**fields)


def test_model_accepts_complete_discovery_result():
    job = complete_job()
    assert job.source == "weworkremotely"
    assert job.source_job_id == "job-123"
    assert job.source_url == "https://weworkremotely.com/remote-jobs/job-123"
    assert job.title == "Senior Product Manager"
    assert job.employer == "Example Co."
    assert job.location == "Remote (US)"
    assert job.description == "Lead product delivery for a platform team."
    assert job.compensation == "$140k - $170k"
    assert job.posted_at == "2026-08-10"


def test_model_optional_fields_can_be_absent():
    job = DiscoveredJob(
        source="remoteok",
        source_url="https://remoteok.com/remote-jobs/42",
        title="SRE",
        employer="Acme",
        description="Keep systems running.",
    )
    assert job.source_job_id is None
    assert job.location is None
    assert job.compensation is None
    assert job.posted_at is None


def test_discovered_job_can_be_persisted(connection):
    job_id = db.upsert_job(connection, complete_job())
    connection.commit()

    row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["source"] == "weworkremotely"
    assert row["source_job_id"] == "job-123"
    assert row["source_url"] == "https://weworkremotely.com/remote-jobs/job-123"
    assert row["title"] == "Senior Product Manager"
    assert row["employer"] == "Example Co."
    assert row["location"] == "Remote (US)"
    assert row["description"] == "Lead product delivery for a platform team."
    assert row["compensation"] == "$140k - $170k"
    assert row["posted_at"] == "2026-08-10"
    assert row["identity_key"]


def test_same_source_job_id_does_not_create_two_jobs(connection):
    db.upsert_job(connection, complete_job())
    db.upsert_job(connection, complete_job())
    connection.commit()

    count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count == 1


def test_same_source_url_without_source_id_does_not_create_two_jobs(connection):
    db.upsert_job(connection, complete_job(source_job_id=None))
    db.upsert_job(
        connection,
        complete_job(source_job_id=None, source_url="https://weworkremotely.com/remote-jobs/job-123/"),
    )
    connection.commit()

    count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count == 1


def test_reingestion_updates_last_seen_at(connection):
    job_id = db.upsert_job(connection, complete_job())
    connection.execute("UPDATE jobs SET last_seen_at = '2020-01-01 00:00:00' WHERE id = ?", (job_id,))
    connection.commit()

    db.upsert_job(connection, complete_job())
    connection.commit()

    row = connection.execute("SELECT last_seen_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["last_seen_at"] != "2020-01-01 00:00:00"


def test_reingestion_refreshes_source_fields_and_preserves_enrichment(connection):
    job_id = db.upsert_job(connection, complete_job())
    connection.execute(
        "UPDATE jobs SET employer_url = ?, score = ? WHERE id = ?",
        ("https://example.com/jobs/123", 92.0, job_id),
    )
    connection.commit()

    db.upsert_job(
        connection,
        complete_job(title="Senior Product Manager II", compensation="$150k - $180k"),
    )
    connection.commit()

    row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["title"] == "Senior Product Manager II"
    assert row["compensation"] == "$150k - $180k"
    assert row["employer_url"] == "https://example.com/jobs/123"
    assert row["score"] == 92.0
