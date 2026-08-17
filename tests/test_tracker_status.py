import datetime

import pytest

from remotescout import db
from remotescout.app import create_app
from remotescout.business_time import business_today

DAY = business_today().isoformat()


def create_job(connection, **overrides):
    fields = {
        "title": "Senior Product Manager",
        "employer": "Acme Inc.",
        "location": "Remote (US)",
        "compensation": "$150k - $180k",
        "source": "weworkremotely",
        "source_url": "https://weworkremotely.com/remote-jobs/acme-senior-product-manager",
        "source_job_id": "acme-spm",
        "employer_url": "https://boards.greenhouse.io/acme/jobs/1234",
        "score": 88,
        "fit_explanation": "Strong delivery leadership match.",
    }
    fields.update(overrides)
    job_id = db.create_job(connection, **fields)
    connection.commit()
    return job_id


def seed_recommendations(connection, jobs):
    for rank, job_id in enumerate(jobs, start=1):
        connection.execute(
            "INSERT INTO recommendations (date, rank, job_id, score, explanation) "
            "VALUES (?, ?, ?, ?, ?)",
            (DAY, rank, job_id, 90 - rank, f"Explanation for rank {rank}."),
        )
    db.mark_recommendation_day_complete(connection, DAY)
    connection.commit()


def create_application(connection, status="Applied", applied_at="2026-08-11"):
    job_id = create_job(connection)
    application_id = db.mark_job_applied(connection, job_id, applied_at)
    if status != "Applied":
        db.update_application_status(connection, application_id, status, applied_at)
    return application_id


def application_status(connection, application_id):
    return connection.execute(
        "SELECT status FROM applications WHERE id = ?", (application_id,)
    ).fetchone()["status"]


def application_updated_at(connection, application_id):
    return connection.execute(
        "SELECT updated_at FROM applications WHERE id = ?", (application_id,)
    ).fetchone()["updated_at"]


def events(connection, application_id):
    return connection.execute(
        "SELECT event_date, status, note FROM application_events "
        "WHERE application_id = ? ORDER BY event_date, id",
        (application_id,),
    ).fetchall()


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE_PATH": str(tmp_path / "test.db")})


@pytest.fixture
def client(app):
    return app.test_client()


def html(response):
    return response.get_data(as_text=True)


class TestStatusUpdates:
    def test_applied_to_screen_updates_current_status(self, app, client):
        with app.app_context():
            application_id = create_application(db.get_db())
        response = client.post(f"/applications/{application_id}/status", data={"status": "Screen"})
        assert response.status_code == 302
        with app.app_context():
            assert application_status(db.get_db(), application_id) == "Screen"

    def test_screen_event_is_appended(self, app, client):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection)
        client.post(f"/applications/{application_id}/status", data={"status": "Screen"})
        with app.app_context():
            rows = events(db.get_db(), application_id)
        assert [row["status"] for row in rows] == ["Applied", "Screen"]
        assert rows[1]["event_date"] == DAY

    def test_screen_to_interview_updates_and_appends(self, app, client):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection, status="Screen")
        response = client.post(
            f"/applications/{application_id}/status", data={"status": "Interview"}
        )
        assert response.status_code == 302
        with app.app_context():
            connection = db.get_db()
            assert application_status(connection, application_id) == "Interview"
            assert [row["status"] for row in events(connection, application_id)] == [
                "Applied",
                "Screen",
                "Interview",
            ]

    def test_interview_to_offer_works(self, app, client):
        with app.app_context():
            application_id = create_application(db.get_db(), status="Interview")
        response = client.post(
            f"/applications/{application_id}/status", data={"status": "Offer"}
        )
        assert response.status_code == 302
        with app.app_context():
            assert application_status(db.get_db(), application_id) == "Offer"

    def test_interview_to_rejected_works(self, app, client):
        with app.app_context():
            application_id = create_application(db.get_db(), status="Interview")
        response = client.post(
            f"/applications/{application_id}/status", data={"status": "Rejected"}
        )
        assert response.status_code == 302
        with app.app_context():
            assert application_status(db.get_db(), application_id) == "Rejected"

    def test_rejected_to_interview_is_allowed(self, app, client):
        with app.app_context():
            application_id = create_application(db.get_db(), status="Rejected")
        response = client.post(
            f"/applications/{application_id}/status", data={"status": "Interview"}
        )
        assert response.status_code == 302
        with app.app_context():
            assert application_status(db.get_db(), application_id) == "Interview"

    @pytest.mark.parametrize("status", db.SUPPORTED_STATUSES)
    def test_any_supported_status_selectable_without_transition_graph(self, app, client, status):
        with app.app_context():
            application_id = create_application(db.get_db(), status="Offer")
        response = client.post(f"/applications/{application_id}/status", data={"status": status})
        assert response.status_code == 302
        with app.app_context():
            assert application_status(db.get_db(), application_id) == status


class TestAtomicity:
    def test_status_update_and_event_commit_together(self, app):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection)
            result, _ = db.update_application_status(connection, application_id, "Screen", DAY)
            assert result == "updated"
            connection.rollback()
            assert application_status(connection, application_id) == "Screen"
            assert [row["status"] for row in events(connection, application_id)] == [
                "Applied",
                "Screen",
            ]

    def test_event_write_failure_rolls_back_status_change(self, app, monkeypatch):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection)
            before = application_updated_at(connection, application_id)

            def boom(*args, **kwargs):
                raise RuntimeError("event insert failed")

            monkeypatch.setattr("remotescout.db.add_application_event", boom)
            with pytest.raises(RuntimeError):
                db.update_application_status(connection, application_id, "Screen", DAY)
            assert application_status(connection, application_id) == "Applied"
            assert [row["status"] for row in events(connection, application_id)] == ["Applied"]
            assert application_updated_at(connection, application_id) == before

    def test_route_event_failure_returns_500_and_changes_nothing(self, app, client, monkeypatch):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection)
            before = application_updated_at(connection, application_id)

            def boom(*args, **kwargs):
                raise RuntimeError("event insert failed")

            monkeypatch.setattr("remotescout.db.add_application_event", boom)
            response = client.post(
                f"/applications/{application_id}/status", data={"status": "Screen"}
            )
            assert response.status_code == 500
            assert application_status(connection, application_id) == "Applied"
            assert [row["status"] for row in events(connection, application_id)] == ["Applied"]
            assert application_updated_at(connection, application_id) == before


class TestIdempotency:
    def test_submitting_current_status_creates_no_duplicate_event(self, app, client):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection, status="Interview")
            before = application_updated_at(connection, application_id)
        response = client.post(
            f"/applications/{application_id}/status", data={"status": "Interview"}
        )
        assert response.status_code == 302
        with app.app_context():
            connection = db.get_db()
            assert application_status(connection, application_id) == "Interview"
            assert [row["status"] for row in events(connection, application_id)] == [
                "Applied",
                "Interview",
            ]
            assert application_updated_at(connection, application_id) == before

    def test_repeated_same_status_post_redirects_normally(self, app, client):
        with app.app_context():
            application_id = create_application(db.get_db(), status="Screen")
        first = client.post(
            f"/applications/{application_id}/status", data={"status": "Screen"}
        )
        second = client.post(
            f"/applications/{application_id}/status", data={"status": "Screen"}
        )
        assert first.status_code == 302
        assert second.status_code == 302
        with app.app_context():
            connection = db.get_db()
            assert [row["status"] for row in events(connection, application_id)] == [
                "Applied",
                "Screen",
            ]

    def test_helper_returns_unchanged_for_same_status(self, app):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection, status="Offer")
            result, _ = db.update_application_status(connection, application_id, "Offer", DAY)
            assert result == "unchanged"
            assert [row["status"] for row in events(connection, application_id)] == [
                "Applied",
                "Offer",
            ]


class TestValidation:
    def test_nonexistent_application_id_returns_404(self, app, client):
        response = client.post("/applications/99999/status", data={"status": "Screen"})
        assert response.status_code == 404
        with app.app_context():
            connection = db.get_db()
            assert connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM application_events").fetchone()[0] == 0

    def test_unsupported_status_returns_400(self, app, client):
        with app.app_context():
            application_id = create_application(db.get_db(), status="Screen")
        response = client.post(
            f"/applications/{application_id}/status", data={"status": "Ghosted"}
        )
        assert response.status_code == 400
        with app.app_context():
            connection = db.get_db()
            assert application_status(connection, application_id) == "Screen"
            assert [row["status"] for row in events(connection, application_id)] == [
                "Applied",
                "Screen",
            ]

    def test_missing_status_field_returns_400(self, app, client):
        with app.app_context():
            application_id = create_application(db.get_db(), status="Screen")
        response = client.post(f"/applications/{application_id}/status", data={})
        assert response.status_code == 400
        with app.app_context():
            assert application_status(db.get_db(), application_id) == "Screen"

    def test_invalid_request_changes_nothing(self, app, client):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection, status="Interview")
        client.post(f"/applications/{application_id}/status", data={"status": "Withdrawn"})
        client.post("/applications/99999/status", data={"status": "Interview"})
        with app.app_context():
            connection = db.get_db()
            assert application_status(connection, application_id) == "Interview"
            assert [row["status"] for row in events(connection, application_id)] == [
                "Applied",
                "Interview",
            ]
            assert connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1

    def test_form_title_employer_url_fields_are_ignored(self, app, client):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection)
            job_id = connection.execute(
                "SELECT job_id FROM applications WHERE id = ?", (application_id,)
            ).fetchone()["job_id"]
            original = connection.execute(
                "SELECT title, employer, employer_url FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        response = client.post(
            f"/applications/{application_id}/status",
            data={
                "status": "Screen",
                "title": "Fake Title",
                "employer": "Fake Employer",
                "employer_url": "https://evil.example.com",
                "applied_at": "2000-01-01",
            },
        )
        assert response.status_code == 302
        with app.app_context():
            connection = db.get_db()
            current = connection.execute(
                "SELECT title, employer, employer_url FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            assert application_status(connection, application_id) == "Screen"
            assert current["title"] == original["title"]
            assert current["employer"] == original["employer"]
            assert current["employer_url"] == original["employer_url"]


class TestHistory:
    def test_tracker_displays_initial_applied_event(self, app, client):
        with app.app_context():
            connection = db.get_db()
            job_id = create_job(connection)
            seed_recommendations(connection, [job_id])
        client.post(f"/recommendations/{job_id}/applied")
        with app.app_context():
            connection = db.get_db()
            application_id = connection.execute(
                "SELECT id FROM applications WHERE job_id = ?", (job_id,)
            ).fetchone()["id"]
            event_date = events(connection, application_id)[0]["event_date"]
            expected = (
                f"Applied &mdash; "
                f"{datetime.date.fromisoformat(event_date).strftime('%b %d, %Y')}"
            )
        page = html(client.get("/tracker"))
        assert "History" in page
        assert expected in page

    def test_tracker_displays_subsequent_status_events(self, app, client):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection, applied_at="2026-08-11")
            db.update_application_status(connection, application_id, "Screen", "2026-08-14")
            db.update_application_status(connection, application_id, "Interview", "2026-08-20")
        page = html(client.get("/tracker"))
        assert "Applied &mdash; Aug 11, 2026" in page
        assert "Screen &mdash; Aug 14, 2026" in page
        assert "Interview &mdash; Aug 20, 2026" in page

    def test_history_ordering_is_deterministic_oldest_first(self, app, client):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection, applied_at="2026-08-11")
            db.update_application_status(connection, application_id, "Screen", "2026-08-14")
            db.update_application_status(connection, application_id, "Interview", "2026-08-20")
        page = html(client.get("/tracker"))
        assert (
            page.index("Applied &mdash; Aug 11, 2026")
            < page.index("Screen &mdash; Aug 14, 2026")
            < page.index("Interview &mdash; Aug 20, 2026")
        )

    def test_same_day_events_ordered_by_insertion(self, app, client):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection, applied_at="2026-08-11")
            db.update_application_status(connection, application_id, "Screen", "2026-08-20")
            db.update_application_status(connection, application_id, "Interview", "2026-08-20")
            db.update_application_status(connection, application_id, "Offer", "2026-08-20")
        page = html(client.get("/tracker"))
        assert (
            page.index("Applied &mdash; Aug 11, 2026")
            < page.index("Screen &mdash; Aug 20, 2026")
            < page.index("Interview &mdash; Aug 20, 2026")
            < page.index("Offer &mdash; Aug 20, 2026")
        )

    def test_current_status_matches_latest_successful_update(self, app, client):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection)
        for status in ("Screen", "Interview", "Offer", "Rejected", "Interview"):
            response = client.post(
                f"/applications/{application_id}/status", data={"status": status}
            )
            assert response.status_code == 302
        with app.app_context():
            assert application_status(db.get_db(), application_id) == "Interview"

    def test_history_survives_page_refresh(self, app, client):
        with app.app_context():
            connection = db.get_db()
            application_id = create_application(connection)
            db.update_application_status(connection, application_id, "Screen", "2026-08-14")
        first = html(client.get("/tracker"))
        second = html(client.get("/tracker"))
        assert first == second
        assert "Screen &mdash; Aug 14, 2026" in second


class TestRegression:
    def test_recommendation_to_applied_creates_exactly_one_initial_event(self, app, client):
        with app.app_context():
            connection = db.get_db()
            job_id = create_job(connection)
            seed_recommendations(connection, [job_id])
        client.post(f"/recommendations/{job_id}/applied")
        client.get("/tracker")
        with app.app_context():
            connection = db.get_db()
            application_id = connection.execute(
                "SELECT id FROM applications WHERE job_id = ?", (job_id,)
            ).fetchone()["id"]
            assert [row["status"] for row in events(connection, application_id)] == ["Applied"]
        page = html(client.get("/tracker"))
        assert page.count("Applied &mdash;") == 1

    def test_marking_applied_removes_card_without_refill(self, app, client, monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

        monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
        with app.app_context():
            connection = db.get_db()
            self_job_a = create_job(connection, title="Role A")
            self_job_b = create_job(connection, title="Role B")
            seed_recommendations(connection, [self_job_a, self_job_b])
        client.get("/")
        client.post(f"/recommendations/{self_job_a}/applied")
        page = html(client.get("/"))
        assert "Role A" not in page
        assert "Role B" in page
