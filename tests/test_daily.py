import pytest

from remotescout import daily, db
from remotescout.app import create_app
from remotescout.business_time import business_today
from remotescout.scoring import MissingApiKeyError

DAY = business_today().isoformat()


@pytest.fixture
def config(tmp_path, monkeypatch):
    values = {
        "DATABASE_PATH": str(tmp_path / "daily.db"),
        "RESUME_PATH": str(tmp_path / "resume.pdf"),
        "ANTHROPIC_API_KEY": "test-key",
        "ANTHROPIC_MODEL": "test-model",
        "RECOMMENDATION_THRESHOLD": 70.0,
    }
    monkeypatch.setattr("remotescout.daily.load_config", lambda: values)
    return values


def fake_engine(rows):
    def build(connection, recommendation_date=None, **kwargs):
        for rank, row in enumerate(rows, start=1):
            connection.execute(
                "INSERT INTO recommendations (date, rank, job_id, score, explanation) "
                "VALUES (?, ?, ?, ?, ?)",
                (recommendation_date, rank, row["job_id"], row["score"], row["explanation"]),
            )
        db.mark_recommendation_day_complete(connection, recommendation_date)
        connection.commit()
        return db.get_recommendations(connection, recommendation_date)

    return build


def open_connection(db_path):
    db.init_db(db_path)
    return db.connect(db_path)


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


def seed_job_rows(connection):
    return [
        {"job_id": create_job(connection, title="Role A"), "score": 92.0, "explanation": "A."},
        {"job_id": create_job(connection, title="Role B"), "score": 88.0, "explanation": "B."},
        {"job_id": create_job(connection, title="Role C"), "score": 84.0, "explanation": "C."},
    ]


def test_command_invokes_engine_for_today(config):
    called = []

    def recording_build(connection, recommendation_date=None, **kwargs):
        called.append(recommendation_date)
        return []

    exit_code, message = daily.run_daily(build=recording_build)
    assert called == [DAY]
    assert exit_code == 0


def test_successful_three_result_run_exits_zero(config, monkeypatch, capsys):
    rows = seed_job_rows(open_connection(config["DATABASE_PATH"]))
    monkeypatch.setattr(
        "remotescout.engine.build_daily_recommendations", fake_engine(rows)
    )
    exit_code = daily.main()
    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"Remote Scout daily recommendations complete: 3 recommendations for {DAY}" in out


def test_successful_zero_result_run_exits_zero(config, capsys):
    exit_code, message = daily.run_daily(build=lambda connection, recommendation_date=None, **kwargs: [])
    assert exit_code == 0
    assert "0 recommendations" in message


def test_success_output_includes_recommendation_count(config, monkeypatch, capsys):
    rows = seed_job_rows(open_connection(config["DATABASE_PATH"]))
    monkeypatch.setattr(
        "remotescout.engine.build_daily_recommendations", fake_engine(rows)
    )
    daily.main()
    out = capsys.readouterr().out
    assert "3 recommendations" in out
    assert str(DAY) in out


def test_already_completed_day_exits_zero_without_engine_work(config):
    connection = open_connection(config["DATABASE_PATH"])
    rows = seed_job_rows(connection)
    for rank, row in enumerate(rows, start=1):
        connection.execute(
            "INSERT INTO recommendations (date, rank, job_id, score, explanation) "
            "VALUES (?, ?, ?, ?, ?)",
            (DAY, rank, row["job_id"], row["score"], row["explanation"]),
        )
    db.mark_recommendation_day_complete(connection, DAY)
    connection.commit()
    connection.close()

    def explode(*args, **kwargs):
        raise AssertionError("engine must not run for an already-completed day")

    exit_code, message = daily.run_daily(build=explode)
    assert exit_code == 0
    assert "already complete" in message
    assert "3 recommendations" in message


def test_zero_result_completed_day_does_not_rebuild(config):
    connection = open_connection(config["DATABASE_PATH"])
    db.mark_recommendation_day_complete(connection, DAY)
    connection.commit()
    connection.close()

    def explode(*args, **kwargs):
        raise AssertionError("engine must not run for an already-completed zero-result day")

    exit_code, message = daily.run_daily(build=explode)
    assert exit_code == 0
    assert "already complete" in message
    assert "0 recommendations" in message


def test_pipeline_exception_exits_nonzero(config, monkeypatch, capsys):
    def explode(connection, recommendation_date=None, **kwargs):
        raise RuntimeError("discovery failure")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    exit_code = daily.main()
    assert exit_code == 1


def test_failure_writes_concise_error_to_stderr(config, monkeypatch, capsys):
    def explode(connection, recommendation_date=None, **kwargs):
        raise RuntimeError("discovery failure")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    exit_code = daily.main()
    captured = capsys.readouterr()
    assert "Remote Scout daily recommendations failed: discovery failure" in captured.err
    assert "complete" not in captured.out


def test_failed_run_does_not_create_completed_marker(config, monkeypatch):
    def explode(connection, recommendation_date=None, **kwargs):
        raise RuntimeError("discovery failure")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    daily.main()
    connection = open_connection(config["DATABASE_PATH"])
    assert not db.is_recommendation_day_complete(connection, DAY)
    assert db.get_recommendations(connection, DAY) == []
    connection.close()


def test_missing_api_key_failure_exits_nonzero(config, monkeypatch, capsys):
    def explode(connection, recommendation_date=None, **kwargs):
        raise MissingApiKeyError(
            "ANTHROPIC_API_KEY is not set; cannot score jobs without it"
        )

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    exit_code = daily.main()
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ANTHROPIC_API_KEY is not set" in captured.err
    assert "0 recommendations" not in captured.out


def test_command_uses_configured_database(config):
    rows = seed_job_rows(open_connection(config["DATABASE_PATH"]))
    exit_code, _ = daily.run_daily(
        build=fake_engine([{"job_id": rows[0]["job_id"], "score": 92.0, "explanation": "A."}])
    )
    assert exit_code == 0
    connection = open_connection(config["DATABASE_PATH"])
    assert db.is_recommendation_day_complete(connection, DAY)
    assert [row["job_id"] for row in db.get_recommendations(connection, DAY)] == [
        rows[0]["job_id"]
    ]
    connection.close()


def test_fresh_temp_database_is_initialized(config):
    rows = seed_job_rows(open_connection(config["DATABASE_PATH"]))
    exit_code, _ = daily.run_daily(
        build=fake_engine([{"job_id": rows[0]["job_id"], "score": 92.0, "explanation": "A."}])
    )
    assert exit_code == 0
    connection = open_connection(config["DATABASE_PATH"])
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    connection.close()
    assert {"jobs", "recommendations", "recommendation_days"} <= tables


def test_module_entry_runs_pipeline(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "remotescout.daily.load_config",
        lambda: {
            "DATABASE_PATH": str(tmp_path / "entry.db"),
            "RESUME_PATH": str(tmp_path / "resume.pdf"),
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_MODEL": "test-model",
            "RECOMMENDATION_THRESHOLD": 70.0,
        },
    )

    def build(connection, recommendation_date=None, **kwargs):
        return []

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", build)
    exit_code = daily.main()
    assert exit_code == 0
    assert f"0 recommendations for {DAY}" in capsys.readouterr().out


def test_open_does_not_rerun_after_daily_command(config, monkeypatch):
    rows = seed_job_rows(open_connection(config["DATABASE_PATH"]))
    exit_code, _ = daily.run_daily(
        build=fake_engine([{"job_id": rows[0]["job_id"], "score": 92.0, "explanation": "A."}])
    )
    assert exit_code == 0

    def explode(*args, **kwargs):
        raise AssertionError("engine must not rerun after the daily command completed the day")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    app = create_app({"DATABASE_PATH": config["DATABASE_PATH"]})
    response = app.test_client().get("/")
    assert response.status_code == 200
    assert "Role A" in response.get_data(as_text=True)
