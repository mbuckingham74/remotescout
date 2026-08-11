import datetime

import pytest

from remotescout import db
from remotescout.app import create_app

DAY = datetime.date.today().isoformat()


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


def make_fake_engine(day, recommendations):
    def build(connection, recommendation_date=None, **kwargs):
        build.calls.append(recommendation_date)
        for row in recommendations:
            connection.execute(
                "INSERT INTO recommendations (date, rank, job_id, score, explanation) "
                "VALUES (?, ?, ?, ?, ?)",
                (day, row["rank"], row["job_id"], row["score"], row["explanation"]),
            )
        db.mark_recommendation_day_complete(connection, day)
        connection.commit()
        return db.get_recommendations(connection, day)

    build.calls = []
    return build


def seed_recommendations(connection, jobs):
    for rank, job_id in enumerate(jobs, start=1):
        connection.execute(
            "INSERT INTO recommendations (date, rank, job_id, score, explanation) "
            "VALUES (?, ?, ?, ?, ?)",
            (DAY, rank, job_id, 90 - rank, f"Explanation for rank {rank}."),
        )
    connection.commit()


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE_PATH": str(tmp_path / "test.db")})


@pytest.fixture
def client(app):
    return app.test_client()


def html(response):
    return response.get_data(as_text=True)


def test_pinned_recommendations_do_not_invoke_engine(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
        seed_recommendations(connection, [job_id])

    def fail_engine(*args, **kwargs):
        raise AssertionError("engine must not run when recommendations are pinned")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fail_engine)
    response = client.get("/")
    assert response.status_code == 200
    assert "Senior Product Manager" in html(response)


def test_no_pinned_recommendations_invokes_engine_once(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
    fake = make_fake_engine(
        DAY, [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}]
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    response = client.get("/")
    assert fake.calls == [DAY]
    assert "Senior Product Manager" in html(response)


def test_up_to_three_recommendations_render(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        jobs = [
            create_job(connection, title=f"Role {letter}")
            for letter in ("A", "B", "C")
        ]
    fake = make_fake_engine(
        DAY,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    page = html(client.get("/"))
    for title in ("Role A", "Role B", "Role C"):
        assert title in page


def test_employer_url_used_for_apply_link(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
    fake = make_fake_engine(
        DAY, [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}]
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    page = html(client.get("/"))
    assert 'href="https://boards.greenhouse.io/acme/jobs/1234"' in page


def test_source_url_not_used_as_apply_destination(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
    fake = make_fake_engine(
        DAY, [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}]
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    page = html(client.get("/"))
    assert "weworkremotely.com/remote-jobs/acme-senior-product-manager" not in page


def test_score_and_fit_explanation_render(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
    fake = make_fake_engine(
        DAY,
        [
            {
                "rank": 1,
                "job_id": job_id,
                "score": 91,
                "explanation": "Excellent direct delivery leadership match.",
            }
        ],
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    page = html(client.get("/"))
    assert "91" in page
    assert "Excellent direct delivery leadership match." in page


def test_zero_recommendations_shows_empty_state(app, client, monkeypatch):
    def build(connection, recommendation_date=None, **kwargs):
        return []

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", build)
    page = html(client.get("/"))
    assert "No strong matches today." in page


def test_one_or_two_recommendations_render_without_filler(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        jobs = [create_job(connection, title=f"Role {letter}") for letter in ("A", "B")]
    fake = make_fake_engine(
        DAY,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
        ],
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    page = html(client.get("/"))
    assert "Role A" in page
    assert "Role B" in page
    assert "No strong matches today." not in page


def test_applied_post_creates_application_and_event(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
    fake = make_fake_engine(
        DAY, [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}]
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
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
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
    fake = make_fake_engine(
        DAY, [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}]
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
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
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
    fake = make_fake_engine(
        DAY, [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}]
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")

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
    with app.app_context():
        connection = db.get_db()
        jobs = [create_job(connection, title=f"Role {letter}") for letter in ("A", "B", "C")]
    fake = make_fake_engine(
        DAY,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
    client.post(f"/recommendations/{jobs[0]}/applied")
    page = html(client.get("/"))
    assert "Role A" not in page
    assert "Role B" in page
    assert "Role C" in page


def test_persisted_recommendation_row_remains_after_applied(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        jobs = [create_job(connection, title=f"Role {letter}") for letter in ("A", "B", "C")]
    fake = make_fake_engine(
        DAY,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
    client.post(f"/recommendations/{jobs[0]}/applied")
    with app.app_context():
        connection = db.get_db()
        rows = db.get_recommendations(connection, DAY)
    assert len(rows) == 3
    assert [row["job_id"] for row in rows] == jobs


def test_applied_does_not_invoke_engine_to_refill(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        jobs = [create_job(connection, title=f"Role {letter}") for letter in ("A", "B", "C")]
    fake = make_fake_engine(
        DAY,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
    client.post(f"/recommendations/{jobs[0]}/applied")
    client.get("/")
    assert fake.calls == [DAY]


def test_remaining_recommendations_stay_unchanged(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        jobs = [create_job(connection, title=f"Role {letter}") for letter in ("A", "B", "C")]
    fake = make_fake_engine(
        DAY,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
    client.post(f"/recommendations/{jobs[0]}/applied")
    page = html(client.get("/"))
    assert page.index("Role B") < page.index("Role C")
    assert "Role B" in page and "Role C" in page
    assert "Role D" not in page


def test_applied_job_appears_on_tracker(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
    fake = make_fake_engine(
        DAY, [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}]
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
    client.post(f"/recommendations/{job_id}/applied")
    page = html(client.get("/tracker"))
    assert "Senior Product Manager" in page
    assert "Acme Inc." in page
    assert "Applied" in page
    assert DAY in page


def test_tracker_includes_employer_posting_link(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
    fake = make_fake_engine(
        DAY, [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}]
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
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
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
    fake = make_fake_engine(
        DAY, [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}]
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
    client.post(f"/recommendations/{job_id}/applied")
    with app.app_context():
        connection = db.get_db()
        applied = db.get_applied_jobs(connection)
    assert [row["job_id"] for row in applied] == [job_id]


def test_invalid_job_id_does_not_create_application(app, client, monkeypatch):
    response = client.post("/recommendations/99999/applied")
    assert response.status_code == 404
    with app.app_context():
        connection = db.get_db()
        count = connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    assert count == 0


def test_unrecommended_job_id_rejected(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        job_id = create_job(connection)
    fake = make_fake_engine(
        DAY, [{"rank": 1, "job_id": job_id, "score": 88, "explanation": "Strong match."}]
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
    with app.app_context():
        connection = db.get_db()
        unrelated = create_job(connection, title="Unrelated Role")
    response = client.post(f"/recommendations/{unrelated}/applied")
    assert response.status_code == 404
    with app.app_context():
        connection = db.get_db()
        count = connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    assert count == 0


def test_all_applied_shows_completed_state(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        jobs = [create_job(connection, title=f"Role {letter}") for letter in ("A", "B", "C")]
    fake = make_fake_engine(
        DAY,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
    for job_id in jobs:
        client.post(f"/recommendations/{job_id}/applied")
    page = html(client.get("/"))
    assert "You've handled today's recommendations." in page


def test_completed_zero_result_day_does_not_invoke_engine_on_refresh(app, client, monkeypatch):
    fake = make_fake_engine(DAY, [])
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    first = client.get("/")
    assert "No strong matches today." in html(first)
    assert fake.calls == [DAY]
    second = client.get("/")
    assert "No strong matches today." in html(second)
    assert fake.calls == [DAY]


def test_applied_does_not_alter_completion_marker(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        jobs = [create_job(connection, title=f"Role {letter}") for letter in ("A", "B", "C")]
    fake = make_fake_engine(
        DAY,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
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


def test_all_applied_completed_state_does_not_rerun(app, client, monkeypatch):
    with app.app_context():
        connection = db.get_db()
        jobs = [create_job(connection, title=f"Role {letter}") for letter in ("A", "B", "C")]
    fake = make_fake_engine(
        DAY,
        [
            {"rank": 1, "job_id": jobs[0], "score": 92, "explanation": "One."},
            {"rank": 2, "job_id": jobs[1], "score": 88, "explanation": "Two."},
            {"rank": 3, "job_id": jobs[2], "score": 84, "explanation": "Three."},
        ],
    )
    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", fake)
    client.get("/")
    for job_id in jobs:
        client.post(f"/recommendations/{job_id}/applied")
    page = html(client.get("/"))
    assert "You've handled today's recommendations." in page
    assert fake.calls == [DAY]


def test_failed_generation_shows_error_state_not_empty_state(app, client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("pipeline failure")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", boom)
    page = html(client.get("/"))
    assert "couldn't be generated right now" in page
    assert "No strong matches today." not in page
    with app.app_context():
        connection = db.get_db()
        marker = connection.execute(
            "SELECT 1 FROM recommendation_days WHERE recommendation_date = ?", (DAY,)
        ).fetchone()
        rows = connection.execute(
            "SELECT COUNT(*) FROM recommendations WHERE date = ?", (DAY,)
        ).fetchone()[0]
    assert marker is None
    assert rows == 0
