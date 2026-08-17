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


def seed_persisted_recommendations(app, recommendations):
    """Persist recommendations directly and mark the day complete.

    Each recommendation is a dict with rank, job_id, score, explanation.
    The job referenced by job_id must already exist in the database.
    """
    with app.app_context():
        connection = db.get_db()
        for row in recommendations:
            connection.execute(
                "INSERT INTO recommendations (date, rank, job_id, score, explanation) "
                "VALUES (?, ?, ?, ?, ?)",
                (DAY, row["rank"], row["job_id"], row["score"], row["explanation"]),
            )
        db.mark_recommendation_day_complete(connection, DAY)
        connection.commit()


def mark_day_complete(app):
    with app.app_context():
        connection = db.get_db()
        db.mark_recommendation_day_complete(connection, DAY)
        connection.commit()


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE_PATH": str(tmp_path / "test.db")})


@pytest.fixture
def client(app):
    return app.test_client()


def html(response):
    return response.get_data(as_text=True)


def test_pending_state_when_day_incomplete(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on an incomplete day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    response = client.get("/")
    assert response.status_code == 200
    assert "hasn't completed yet" in html(response)
    assert "No strong matches today." not in html(response)


def test_completed_empty_day_renders_empty_state(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    mark_day_complete(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "No strong matches today." in html(response)
    assert "hasn't completed yet" not in html(response)


def test_completed_zero_result_day_does_not_rerun_engine(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed empty day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    mark_day_complete(app)
    first = client.get("/")
    second = client.get("/")
    assert "No strong matches today." in html(first)
    assert "No strong matches today." in html(second)


def test_completed_day_with_recommendations_renders_them(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        job_id = create_job(db.get_db())
    seed_persisted_recommendations(
        app,
        [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}],
    )
    response = client.get("/")
    assert response.status_code == 200
    assert "Senior Product Manager" in html(response)
    assert "Acme Inc." in html(response)
    assert "Strong match." in html(response)


def test_all_applied_state_preserved(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        jobs = [create_job(db.get_db(), title=f"Role {letter}") for letter in ("A", "B", "C")]
    seed_persisted_recommendations(
        app,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    for job_id in jobs:
        with app.app_context():
            db.mark_job_applied(db.get_db(), job_id, DAY)
    page = html(client.get("/"))
    assert "You've handled today's recommendations." in page


def test_pending_state_does_not_show_recommendations(app, client):
    with app.app_context():
        job_id = create_job(db.get_db())
        create_job(db.get_db(), title="Another Role")
        connection = db.get_db()
        connection.execute(
            "INSERT INTO recommendations (date, rank, job_id, score, explanation) "
            "VALUES (?, 1, ?, 92, 'Match.')",
            (DAY, job_id),
        )
        connection.commit()
    response = client.get("/")
    assert response.status_code == 200
    body = html(response)
    assert "hasn't completed yet" in body
    assert "Senior Product Manager" not in body


def test_up_to_three_recommendations_render(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        jobs = [
            create_job(db.get_db(), title=f"Role {letter}")
            for letter in ("A", "B", "C")
        ]
    seed_persisted_recommendations(
        app,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    page = html(client.get("/"))
    for title in ("Role A", "Role B", "Role C"):
        assert title in page


def test_employer_url_used_for_apply_link(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        job_id = create_job(db.get_db())
    seed_persisted_recommendations(
        app,
        [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}],
    )
    page = html(client.get("/"))
    assert 'href="https://boards.greenhouse.io/acme/jobs/1234"' in page


def test_source_url_not_used_as_apply_destination(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        job_id = create_job(db.get_db())
    seed_persisted_recommendations(
        app,
        [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}],
    )
    page = html(client.get("/"))
    assert "weworkremotely.com/remote-jobs/acme-senior-product-manager" not in page


def test_score_and_fit_explanation_render(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        job_id = create_job(db.get_db())
    seed_persisted_recommendations(
        app,
        [
            {
                "rank": 1,
                "job_id": job_id,
                "score": 91,
                "explanation": "Excellent direct delivery leadership match.",
            }
        ],
    )
    page = html(client.get("/"))
    assert "91" in page
    assert "Excellent direct delivery leadership match." in page


def test_one_or_two_recommendations_render_without_filler(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        jobs = [create_job(db.get_db(), title=f"Role {letter}") for letter in ("A", "B")]
    seed_persisted_recommendations(
        app,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
        ],
    )
    page = html(client.get("/"))
    assert "Role A" in page
    assert "Role B" in page
    assert "No strong matches today." not in page


def test_applied_post_creates_application_and_event(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        job_id = create_job(db.get_db())
    seed_persisted_recommendations(
        app,
        [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}],
    )
    response = client.post(f"/recommendations/{job_id}/applied")
    assert response.status_code == 302
    with app.app_context():
        connection = db.get_db()
        applications = connection.execute("SELECT * FROM applications").fetchall()
        events = connection.execute("SELECT * FROM application_events").fetchall()
    assert len(applications) == 1
    assert applications[0]["job_id"] == job_id
    assert applications[0]["applied_at"] == DAY
    assert applications[0]["status"] == "Applied"
    assert len(events) == 1
    assert events[0]["application_id"] == applications[0]["id"]
    assert events[0]["event_date"] == DAY
    assert events[0]["status"] == "Applied"


def test_repeated_applied_post_is_idempotent(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        job_id = create_job(db.get_db())
    seed_persisted_recommendations(
        app,
        [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}],
    )
    client.post(f"/recommendations/{job_id}/applied")
    response = client.post(f"/recommendations/{job_id}/applied")
    assert response.status_code == 302
    with app.app_context():
        connection = db.get_db()
        applications = connection.execute("SELECT * FROM applications").fetchall()
        events = connection.execute("SELECT * FROM application_events").fetchall()
    assert len(applications) == 1
    assert len(events) == 1


def test_application_and_event_are_atomic(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        job_id = create_job(db.get_db())
    seed_persisted_recommendations(
        app,
        [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}],
    )

    def boom(*args, **kwargs):
        raise RuntimeError("event insert failed")

    monkeypatch.setattr("remotescout.db.add_application_event", boom)
    response = client.post(f"/recommendations/{job_id}/applied")
    assert response.status_code == 500
    with app.app_context():
        connection = db.get_db()
        applications = connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        events = connection.execute("SELECT COUNT(*) FROM application_events").fetchone()[0]
    assert applications == 0
    assert events == 0


def test_applied_job_disappears_from_active_recommendations(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        jobs = [create_job(db.get_db(), title=f"Role {letter}") for letter in ("A", "B", "C")]
    seed_persisted_recommendations(
        app,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    client.post(f"/recommendations/{jobs[0]}/applied")
    page = html(client.get("/"))
    assert "Role A" not in page
    assert "Role B" in page
    assert "Role C" in page


def test_persisted_recommendation_row_remains_after_applied(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        jobs = [create_job(db.get_db(), title=f"Role {letter}") for letter in ("A", "B", "C")]
    seed_persisted_recommendations(
        app,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    client.post(f"/recommendations/{jobs[0]}/applied")
    with app.app_context():
        rows = db.get_recommendations(db.get_db(), DAY)
    assert len(rows) == 3
    assert [row["job_id"] for row in rows] == jobs


def test_remaining_recommendations_stay_unchanged(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        jobs = [create_job(db.get_db(), title=f"Role {letter}") for letter in ("A", "B", "C")]
    seed_persisted_recommendations(
        app,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    client.post(f"/recommendations/{jobs[0]}/applied")
    page = html(client.get("/"))
    assert page.index("Role B") < page.index("Role C")
    assert "Role B" in page and "Role C" in page
    assert "Role D" not in page


def test_applied_job_appears_on_tracker(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        job_id = create_job(db.get_db())
    seed_persisted_recommendations(
        app,
        [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}],
    )
    client.post(f"/recommendations/{job_id}/applied")
    page = html(client.get("/tracker"))
    assert "Senior Product Manager" in page
    assert "Acme Inc." in page
    assert "Applied" in page
    assert DAY in page


def test_tracker_includes_employer_posting_link(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        job_id = create_job(db.get_db())
    seed_persisted_recommendations(
        app,
        [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}],
    )
    client.post(f"/recommendations/{job_id}/applied")
    page = html(client.get("/tracker"))
    assert 'href="https://boards.greenhouse.io/acme/jobs/1234"' in page


def test_tracker_orders_applications_newest_first(app, client):
    with app.app_context():
        connection = db.get_db()
        older = create_job(connection, title="Older Role")
        newer = create_job(connection, title="Newer Role")
        db.mark_job_applied(connection, older, "2026-08-01")
        db.mark_job_applied(connection, newer, "2026-08-10")
    page = html(client.get("/tracker"))
    assert page.index("Newer Role") < page.index("Older Role")


def test_ui_applied_job_visible_to_suppression_query(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        job_id = create_job(db.get_db())
    seed_persisted_recommendations(
        app,
        [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}],
    )
    client.post(f"/recommendations/{job_id}/applied")
    with app.app_context():
        applied = db.get_applied_jobs(db.get_db())
    assert [row["job_id"] for row in applied] == [job_id]


def test_invalid_job_id_does_not_create_application(app, client, monkeypatch):
    monkeypatch.setattr(
        "remotescout.engine.build_daily_recommendations",
        lambda *args, **kwargs: [],
    )
    response = client.post("/recommendations/99999/applied")
    assert response.status_code == 404
    with app.app_context():
        connection = db.get_db()
        count = connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    assert count == 0


def test_unrecommended_job_id_rejected(app, client, monkeypatch):
    monkeypatch.setattr(
        "remotescout.engine.build_daily_recommendations",
        lambda *args, **kwargs: [],
    )
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
        seed_persisted_recommendations(
            app,
            [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}],
        )
        unrelated = create_job(connection, title="Unrelated Role")
    response = client.post(f"/recommendations/{unrelated}/applied")
    assert response.status_code == 404
    with app.app_context():
        connection = db.get_db()
        count = connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    assert count == 0


def test_all_applied_shows_completed_state(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        jobs = [create_job(db.get_db(), title=f"Role {letter}") for letter in ("A", "B", "C")]
    seed_persisted_recommendations(
        app,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    for job_id in jobs:
        client.post(f"/recommendations/{job_id}/applied")
    page = html(client.get("/"))
    assert "You've handled today's recommendations." in page


def test_applied_does_not_alter_completion_marker(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET / must not invoke the recommendation engine on a completed day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    with app.app_context():
        jobs = [create_job(db.get_db(), title=f"Role {letter}") for letter in ("A", "B", "C")]
    seed_persisted_recommendations(
        app,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    client.post(f"/recommendations/{jobs[0]}/applied")
    with app.app_context():
        connection = db.get_db()
        marker = connection.execute(
            "SELECT 1 FROM recommendation_days WHERE recommendation_date = ?", (DAY,)
        ).fetchone()
        rows = connection.execute(
            "SELECT COUNT(*) FROM recommendations WHERE date = ?", (DAY,)
        ).fetchone()[0]
    assert marker is not None
    assert rows == 3