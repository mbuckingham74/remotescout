import sqlite3
from pathlib import Path

import pytest

from remotescout import db
from remotescout.app import create_app
from remotescout.resume import extract_resume_text

BASE_DIR = Path(__file__).resolve().parent.parent
RESUME_PATH = BASE_DIR / "docs" / "Michael-Buckingham-Resume-Infrastructure-Delivery-Director.pdf"


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE_PATH": str(tmp_path / "test.db")})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def no_engine(monkeypatch):
    def build(connection, recommendation_date=None, **kwargs):
        return []

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", build)


def test_application_starts(client):
    response = client.get("/")
    assert response.status_code == 200


def test_database_schema_initializes(app):
    with app.app_context():
        connection = sqlite3.connect(app.config["DATABASE_PATH"])
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            connection.close()
    assert {"jobs", "recommendations", "applications", "application_events"} <= tables


def test_resume_extracts_nonempty_text():
    text = extract_resume_text(str(RESUME_PATH))
    assert text
    assert len(text.strip()) > 500


def test_recommendations_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Recommendations" in response.get_data(as_text=True)
    assert "No strong matches today." in response.get_data(as_text=True)


def test_tracker_page_renders(client):
    response = client.get("/tracker")
    assert response.status_code == 200
    assert "Application Tracker" in response.get_data(as_text=True)


def test_healthz_returns_200(client):
    response = client.get("/healthz")
    assert response.status_code == 200


def test_healthz_does_not_invoke_recommendation_engine(client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("healthz must not invoke the recommendation engine")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    response = client.get("/healthz")
    assert response.status_code == 200


def test_healthz_does_not_require_anthropic_configuration(client, monkeypatch):
    monkeypatch.setattr("remotescout.config.load_config", lambda: {"ANTHROPIC_API_KEY": ""})
    response = client.get("/healthz")
    assert response.status_code == 200


def test_healthz_does_no_external_network_work(client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("healthz must not open network connections")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    monkeypatch.setattr("remotescout.discovery.weworkremotely.fetch_jobs", explode)
    monkeypatch.setattr("remotescout.resolution.resolve_job", explode)
    response = client.get("/healthz")
    assert response.status_code == 200


def test_application_and_history_event_roundtrip(app):
    with app.app_context():
        connection = db.get_db()
        job_id = db.create_job(
            connection,
            title="Senior Product Manager",
            employer="Example Co.",
            employer_url="https://example.com/jobs/123",
        )
        application_id = db.create_application(
            connection, job_id, applied_at="2026-08-11", status="Applied"
        )
        db.add_application_event(
            connection, application_id, event_date="2026-08-15",
            status="Screen", note="Recruiter screen"
        )
        connection.commit()

        applications = db.get_applications(connection)
        assert len(applications) == 1
        row = applications[0]
        assert row["employer"] == "Example Co."
        assert row["title"] == "Senior Product Manager"
        assert row["applied_at"] == "2026-08-11"
        assert row["status"] == "Applied"

        events = db.get_application_events(connection, application_id)
        assert len(events) == 1
        assert events[0]["status"] == "Screen"
        assert events[0]["note"] == "Recruiter screen"
