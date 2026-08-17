"""Integration tests for routes and the daily command at controlled instants.

These tests freeze "now" at the test surface (not the wall clock) by
monkeypatching :func:`remotescout.business_time.business_today` where each
production call site imported it. Each test pins a specific Pacific
business date and proves every user-facing surface selects that date and
the prior/next calendar dates are not.
"""
import datetime
from zoneinfo import ZoneInfo

import pytest

from remotescout import daily, db
from remotescout.app import create_app

INDEPENDENT_PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = datetime.timezone.utc


def utc(year, month, day, hour, minute):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=UTC)


def pacific_date(instant_utc):
    return instant_utc.astimezone(INDEPENDENT_PACIFIC).date()


# Two controlled instants: a UTC-next-day / Pacific-prior-day window and a
# fully-past-midnight Pacific window. The expected dates are computed
# independently from the standard library.
SUMMER_ROLLOVER_INSTANT = utc(2026, 8, 17, 3, 30)  # 20:30 PDT prior day
WINTER_ROLLOVER_INSTANT = utc(2026, 1, 2, 7, 30)   # 23:30 PST prior day
SUMMER_MORNING_INSTANT = utc(2026, 8, 17, 15, 30)  # 08:30 PDT same day

assert pacific_date(SUMMER_ROLLOVER_INSTANT) == datetime.date(2026, 8, 16)
assert pacific_date(WINTER_ROLLOVER_INSTANT) == datetime.date(2026, 1, 1)
assert pacific_date(SUMMER_MORNING_INSTANT) == datetime.date(2026, 8, 17)


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE_PATH": str(tmp_path / "test.db")})


@pytest.fixture
def client(app):
    return app.test_client()


def create_job(connection, **overrides):
    fields = {
        "title": "Senior Product Manager",
        "employer": "Acme Inc.",
        "employer_url": "https://boards.greenhouse.io/acme/jobs/1234",
    }
    fields.update(overrides)
    job_id = db.create_job(connection, **fields)
    connection.commit()
    return job_id


def html(response):
    return response.get_data(as_text=True)


@pytest.fixture
def freeze_app_business_date(app, request):
    """Bind ``remotescout.app.business_today`` to a fixed date.

    Production code imports ``business_today`` at module load time, so the
    route-level reference is what the test must freeze.
    """
    pinned_date = request.param
    return pinned_date.isoformat()


def seed_persisted_recommendations(connection, target_date, jobs):
    for rank, job_id in enumerate(jobs, start=1):
        connection.execute(
            "INSERT INTO recommendations (date, rank, job_id, score, explanation) "
            "VALUES (?, ?, ?, ?, ?)",
            (target_date, rank, job_id, 90 - rank, f"Explanation for rank {rank}."),
        )
    db.mark_recommendation_day_complete(connection, target_date)
    connection.commit()


class TestRecommendationsPageAtRollover:
    """GET / during the UTC-next-day / Pacific-prior-day window must treat
    the Pacific prior date as the current recommendation day."""

    def test_render_selects_pacific_business_date_not_utc_date(
        self, app, client, monkeypatch
    ):
        business_date = pacific_date(SUMMER_ROLLOVER_INSTANT)
        utc_date = SUMMER_ROLLOVER_INSTANT.date()
        assert business_date != utc_date
        assert business_date == datetime.date(2026, 8, 16)
        assert utc_date == datetime.date(2026, 8, 17)

        monkeypatch.setattr(
            "remotescout.app.business_today", lambda: business_date
        )

        with app.app_context():
            connection = db.get_db()
            job = create_job(connection)
            seed_persisted_recommendations(connection, business_date.isoformat(), [job])

        response = client.get("/")
        body = html(response)
        assert response.status_code == 200
        assert f"Recommendations for {business_date.isoformat()}" in body
        assert f"Recommendations for {utc_date.isoformat()}" not in body
        assert "Acme Inc." in body

    def test_render_ignores_utc_date_state_when_pacific_rolls(
        self, app, client, monkeypatch
    ):
        """Persisted state for the UTC date must not be selected while the
        Pacific business date is the prior calendar day."""
        business_date = pacific_date(WINTER_ROLLOVER_INSTANT)
        utc_date = WINTER_ROLLOVER_INSTANT.date()
        assert business_date != utc_date

        monkeypatch.setattr(
            "remotescout.app.business_today", lambda: business_date
        )

        with app.app_context():
            connection = db.get_db()
            utc_job = create_job(connection, title="UTC Day Role", employer="UTC Co.")
            db.mark_recommendation_day_complete(connection, utc_date.isoformat())
            connection.commit()

        response = client.get("/")
        body = html(response)
        assert response.status_code == 200
        assert "UTC Day Role" not in body
        assert "hasn't completed yet" in body

    def test_pending_state_does_not_invoke_engine_on_rollover(
        self, app, client, monkeypatch
    ):
        business_date = pacific_date(SUMMER_ROLLOVER_INSTANT)
        monkeypatch.setattr(
            "remotescout.app.business_today", lambda: business_date
        )

        def explode(*args, **kwargs):
            raise AssertionError("GET / must not invoke the recommendation engine")

        monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)

        response = client.get("/")
        assert response.status_code == 200
        assert "hasn't completed yet" in html(response)


class TestDailyCommandAtRollover:
    """``python -m remotescout.daily`` must compute the same Pacific
    recommendation date that the recommendations page uses."""

    def _stub_config(self, tmp_path, monkeypatch, anthropic_key="test-key"):
        values = {
            "DATABASE_PATH": str(tmp_path / "daily.db"),
            "RESUME_PATH": str(tmp_path / "resume.pdf"),
            "ANTHROPIC_API_KEY": anthropic_key,
            "ANTHROPIC_MODEL": "test-model",
            "RECOMMENDATION_THRESHOLD": 70.0,
        }
        monkeypatch.setattr("remotescout.daily.load_config", lambda: values)
        return values

    def test_command_passes_pacific_business_date_to_engine(
        self, tmp_path, monkeypatch
    ):
        business_date = pacific_date(SUMMER_ROLLOVER_INSTANT)
        utc_date = SUMMER_ROLLOVER_INSTANT.date()
        assert business_date != utc_date

        self._stub_config(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "remotescout.daily.business_today", lambda: business_date
        )

        called_dates = []

        def recording_build(connection, recommendation_date=None, **kwargs):
            called_dates.append(recommendation_date)
            return db.get_recommendations(connection, recommendation_date)

        daily.run_daily(build=recording_build)
        assert called_dates == [business_date.isoformat()]

    def test_command_matches_route_date_winter_rollover(
        self, tmp_path, app, client, monkeypatch
    ):
        business_date = pacific_date(WINTER_ROLLOVER_INSTANT)
        utc_date = WINTER_ROLLOVER_INSTANT.date()
        assert business_date != utc_date

        self._stub_config(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "remotescout.daily.business_today", lambda: business_date
        )
        monkeypatch.setattr(
            "remotescout.app.business_today", lambda: business_date
        )

        called_dates = []

        def recording_build(connection, recommendation_date=None, **kwargs):
            called_dates.append(recommendation_date)
            return []

        daily.run_daily(build=recording_build)
        assert called_dates == [business_date.isoformat()]

        shared_app = create_app({"DATABASE_PATH": str(tmp_path / "daily.db")})
        with shared_app.app_context():
            connection = db.get_db()
            db.mark_recommendation_day_complete(connection, business_date.isoformat())
            connection.commit()
        shared_client = shared_app.test_client()
        page = html(shared_client.get("/"))
        assert f"Recommendations for {business_date.isoformat()}" in page
        assert f"Recommendations for {utc_date.isoformat()}" not in page


class TestMarkAppliedAtRollover:
    """The initial application/event date recorded on Mark Applied must be
    the Pacific business date, not the UTC date."""

    def test_persisted_application_uses_pacific_business_date(
        self, app, client, monkeypatch
    ):
        business_date = pacific_date(SUMMER_MORNING_INSTANT)
        utc_date = SUMMER_MORNING_INSTANT.date()
        assert business_date == datetime.date(2026, 8, 17)
        assert business_date == utc_date

        monkeypatch.setattr(
            "remotescout.app.business_today", lambda: business_date
        )

        with app.app_context():
            connection = db.get_db()
            job_id = create_job(connection)
            seed_persisted_recommendations(connection, business_date.isoformat(), [job_id])

        response = client.post(f"/recommendations/{job_id}/applied")
        assert response.status_code == 302

        with app.app_context():
            connection = db.get_db()
            application = connection.execute(
                "SELECT applied_at FROM applications WHERE job_id = ?", (job_id,)
            ).fetchone()
            event = connection.execute(
                "SELECT event_date FROM application_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert application["applied_at"] == business_date.isoformat()
        assert event["event_date"] == business_date.isoformat()

    def test_persisted_application_uses_pacific_business_date_on_utc_rollover(
        self, app, client, monkeypatch
    ):
        business_date = pacific_date(SUMMER_ROLLOVER_INSTANT)
        utc_date = SUMMER_ROLLOVER_INSTANT.date()
        assert business_date != utc_date

        monkeypatch.setattr(
            "remotescout.app.business_today", lambda: business_date
        )

        with app.app_context():
            connection = db.get_db()
            job_id = create_job(connection)
            seed_persisted_recommendations(connection, business_date.isoformat(), [job_id])

        client.post(f"/recommendations/{job_id}/applied")

        with app.app_context():
            connection = db.get_db()
            application = connection.execute(
                "SELECT applied_at FROM applications WHERE job_id = ?", (job_id,)
            ).fetchone()
            event = connection.execute(
                "SELECT event_date FROM application_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert application["applied_at"] == business_date.isoformat()
        assert event["event_date"] == business_date.isoformat()
        assert application["applied_at"] != utc_date.isoformat()


class TestStatusUpdateAtRollover:
    """The status-history event date appended by ``/applications/<id>/status``
    must use the Pacific business date."""

    def _seed_application(self, app, applied_at):
        with app.app_context():
            connection = db.get_db()
            job_id = create_job(connection, title=f"Role {applied_at}")
            application_id = db.mark_job_applied(connection, job_id, applied_at)
            connection.commit()
            return application_id

    def test_status_event_uses_pacific_business_date(
        self, app, client, monkeypatch
    ):
        business_date = pacific_date(SUMMER_ROLLOVER_INSTANT)
        utc_date = SUMMER_ROLLOVER_INSTANT.date()

        monkeypatch.setattr(
            "remotescout.app.business_today", lambda: business_date
        )

        application_id = self._seed_application(app, business_date.isoformat())
        response = client.post(
            f"/applications/{application_id}/status", data={"status": "Screen"}
        )
        assert response.status_code == 302

        with app.app_context():
            connection = db.get_db()
            events = connection.execute(
                "SELECT event_date, status FROM application_events "
                "WHERE application_id = ? ORDER BY id",
                (application_id,),
            ).fetchall()
        assert [row["status"] for row in events] == ["Applied", "Screen"]
        assert events[1]["event_date"] == business_date.isoformat()
        assert events[1]["event_date"] != utc_date.isoformat()

    def test_status_event_uses_pacific_business_date_winter(
        self, app, client, monkeypatch
    ):
        business_date = pacific_date(WINTER_ROLLOVER_INSTANT)
        utc_date = WINTER_ROLLOVER_INSTANT.date()

        monkeypatch.setattr(
            "remotescout.app.business_today", lambda: business_date
        )

        application_id = self._seed_application(app, business_date.isoformat())
        response = client.post(
            f"/applications/{application_id}/status", data={"status": "Interview"}
        )
        assert response.status_code == 302

        with app.app_context():
            connection = db.get_db()
            last_event = connection.execute(
                "SELECT event_date FROM application_events "
                "WHERE application_id = ? ORDER BY id DESC LIMIT 1",
                (application_id,),
            ).fetchone()
        assert last_event["event_date"] == business_date.isoformat()
        assert last_event["event_date"] != utc_date.isoformat()
