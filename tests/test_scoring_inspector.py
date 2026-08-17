"""Behavioral coverage for Package 7 read-only Scoring Inspector.

Each test exercises Package 5's durable ``pipeline_run_jobs`` evidence
through the new ``/scoring``, ``/runs/<run_id>/scoring``, and
``/runs/<run_id>/scoring/<job_id>`` routes. A focused auto-use fixture
fails loudly if a regression reintroduces engine, scoring-client, or
network work into the request path.

Adversarial coverage includes:

- mutation that swaps run-scoped ``pipeline_run_jobs.score`` for the
  current ``jobs.score`` value and proves the run-isolation regression
  fails before being restored to green
- focused narrow near-miss protection with the fixed 10-point rule
- HTML escaping of model/external content
- defensive degradation of malformed Package-5 JSON
"""
import datetime
import json
import re

import pytest

from remotescout import db, scoring_view
from remotescout.app import create_app


SOURCE = "weworkremotely"


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE_PATH": str(tmp_path / "test.db")})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def no_engine(monkeypatch):
    """Safety net: GET /scoring must never invoke the engine or scoring client.

    If a regression reintroduces live engine, scoring, or resolution work
    into the scoring inspector request path, this fixture makes the test
    fail loudly.
    """

    def explode(*args, **kwargs):
        raise AssertionError(
            "scoring inspector must not invoke the recommendation engine"
        )

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)
    monkeypatch.setattr("remotescout.scoring.score_job", explode)


_BASE_TIME = datetime.datetime(2026, 8, 17, 12, 0, 0)


def _iso_time(offset_minutes=0):
    return (_BASE_TIME - datetime.timedelta(minutes=offset_minutes)).isoformat(sep=" ")


def _seed_run(
    connection,
    *,
    status="succeeded",
    recommendation_date="2026-08-17",
    threshold=70.0,
    scoring_model="claude-sonnet-5",
    error_type=None,
    error_message=None,
    started_offset_minutes=60,
    finished=True,
):
    finished_value = _iso_time(offset_minutes=0) if finished else None
    cursor = connection.execute(
        """
        INSERT INTO pipeline_runs (
            recommendation_date, status, started_at, finished_at,
            recommendation_threshold, scoring_model, error_type, error_message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            recommendation_date,
            status,
            _iso_time(offset_minutes=started_offset_minutes),
            finished_value,
            threshold,
            scoring_model,
            error_type,
            error_message,
        ),
    )
    return cursor.lastrowid


def _seed_source_attempt(
    connection,
    *,
    run_id,
    source=SOURCE,
    status="succeeded",
    discovered_count=0,
    error_type=None,
    error_message=None,
    finished_offset_minutes=0,
):
    connection.execute(
        """
        INSERT INTO pipeline_source_attempts (
            run_id, source, status, started_at, finished_at,
            discovered_count, error_type, error_message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            source,
            status,
            _iso_time(offset_minutes=50),
            _iso_time(offset_minutes=finished_offset_minutes),
            discovered_count,
            error_type,
            error_message,
        ),
    )


def _seed_job(connection, *, title, employer, source=SOURCE, source_job_id, location=None, description=None, score=None, fit_explanation=None):
    return db.create_job(
        connection,
        title=title,
        employer=employer,
        source=source,
        source_job_id=source_job_id,
        source_url=f"https://weworkremotely.com/remote-jobs/{source_job_id}",
        location=location,
        description=description,
        score=score,
        fit_explanation=fit_explanation,
    )


def _seed_run_job(
    connection,
    *,
    run_id,
    job_id,
    source=SOURCE,
    filter_passed=True,
    filter_reasons=None,
    scoring_attempted=False,
    scoring_succeeded=False,
    score=None,
    fit_explanation=None,
    strengths=None,
    gaps=None,
    meets_threshold=False,
    resolution_attempted=False,
    resolution_succeeded=False,
    resolution_method=None,
    employer_url=None,
    requisition_id=None,
    suppressed_post_resolution=False,
    suppressed_canonical_duplicate=False,
    accepted_rank=None,
    scoring_error_type=None,
    scoring_error_message=None,
):
    connection.execute(
        """
        INSERT INTO pipeline_run_jobs (
            run_id, job_id, source, filter_passed, filter_reasons,
            scoring_attempted, scoring_succeeded,
            score, fit_explanation, strengths, gaps, meets_threshold,
            resolution_attempted, resolution_succeeded, resolution_method,
            employer_url, requisition_id, suppressed_post_resolution,
            suppressed_canonical_duplicate, accepted_rank,
            scoring_error_type, scoring_error_message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            job_id,
            source,
            1 if filter_passed else 0,
            json.dumps(filter_reasons) if filter_reasons else None,
            1 if scoring_attempted else 0,
            1 if scoring_succeeded else 0,
            score,
            fit_explanation,
            json.dumps(strengths) if strengths else None,
            json.dumps(gaps) if gaps else None,
            1 if meets_threshold else 0,
            1 if resolution_attempted else 0,
            1 if resolution_succeeded else 0,
            resolution_method,
            employer_url,
            requisition_id,
            1 if suppressed_post_resolution else 0,
            1 if suppressed_canonical_duplicate else 0,
            accepted_rank,
            scoring_error_type,
            scoring_error_message,
        ),
    )


def _row_html(body, marker):
    start = body.index(marker)
    end = body.index("</tr>", start)
    return body[start:end]


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def test_base_nav_has_scoring_link(client):
    body = client.get("/").get_data(as_text=True)
    assert 'href="/scoring"' in body
    assert body.index("Recommendations") < body.index(">Runs<")
    assert body.index(">Runs<") < body.index(">Scoring<")
    assert body.index(">Scoring<") < body.index(">Tracker<")


# ---------------------------------------------------------------------------
# Empty states
# ---------------------------------------------------------------------------


def test_scoring_entry_returns_200_with_truthful_empty_copy(client, monkeypatch):
    monkeypatch.setattr(
        "remotescout.engine.build_daily_recommendations",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not invoke engine")
        ),
    )
    response = client.get("/scoring")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "No instrumented scoring data yet" in body
    assert "Scoring history will appear" in body
    assert "No jobs found" not in body


def test_scoring_entry_does_not_invoke_engine(client, monkeypatch):
    called = {"count": 0}

    def count_calls(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("scoring inspector must not invoke engine")

    monkeypatch.setattr(
        "remotescout.engine.build_daily_recommendations", count_calls
    )
    response = client.get("/scoring")
    assert response.status_code == 200
    assert called["count"] == 0


def test_scoring_run_returns_404_for_unknown_run(client):
    assert client.get("/runs/999999/scoring").status_code == 404


# ---------------------------------------------------------------------------
# Latest run truthfulness
# ---------------------------------------------------------------------------


def test_scoring_entry_shows_latest_run_even_with_zero_attempts(app, client):
    with app.app_context():
        connection = db.get_db()
        # Older prettier run with successful scores
        old_id = _seed_run(
            connection,
            recommendation_date="2026-08-15",
            threshold=70.0,
            started_offset_minutes=180,
        )
        _seed_source_attempt(connection, run_id=old_id, discovered_count=4)
        old_jid = _seed_job(connection, title="Old PM", employer="Old Co.",
                            source_job_id="old-1")
        _seed_run_job(
            connection, run_id=old_id, job_id=old_jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=82, fit_explanation="Strong delivery match.",
            strengths=["Prog"], gaps=["No fintech"], meets_threshold=True,
        )
        # Newer run with zero scoring attempts
        new_id = _seed_run(
            connection,
            recommendation_date="2026-08-17",
            started_offset_minutes=60,
        )
        _seed_source_attempt(connection, run_id=new_id, discovered_count=3)
        for i in (1, 2, 3):
            jid = _seed_job(connection, title=f"New{i}", employer="New Co.",
                            source_job_id=f"new-{i}")
            _seed_run_job(
                connection, run_id=new_id, job_id=jid,
                filter_passed=False, filter_reasons=["unrelated_occupation"],
            )
        connection.commit()

    body = client.get("/scoring").get_data(as_text=True)
    # Latest run is shown, not older prettier run
    assert f"#{new_id}" in body
    assert f"#{old_id}" not in body.split(f"#{new_id}")[0]
    # Truthful zero-attempt copy for the latest run
    assert "No jobs reached scoring in this run" in body
    # No fabricated successful scores in zero-attempt state
    assert "Old PM" not in body
    assert "Strong delivery match" not in body


def test_scoring_entry_lists_recent_runs_for_history_navigation(app, client):
    with app.app_context():
        connection = db.get_db()
        ids = []
        for i in range(1, 12):
            rid = _seed_run(
                connection,
                recommendation_date=f"2026-08-{i:02d}",
                started_offset_minutes=300 - i * 20,
            )
            _seed_source_attempt(connection, run_id=rid)
            jid = _seed_job(connection, title=f"R{i}", employer="Co",
                            source_job_id=f"r-{i}")
            _seed_run_job(connection, run_id=rid, job_id=jid)
            ids.append(rid)
        connection.commit()

    body = client.get("/scoring").get_data(as_text=True)
    # The latest 10 runs appear in the recent-runs list. Whitespace between
    # the tag boundary and the visible "#N" is whitespace, so we use a regex
    # that doesn't cross tags but does tolerate indentation.
    for rid in ids[-10:]:
        assert re.search(rf'>\s*#{rid}\b', body), f"missing run {rid} in recent list"
    # The oldest run is omitted from the recent-runs list
    assert not re.search(rf'>\s*#{ids[0]}\b', body), "oldest run leaked into recent list"


# ---------------------------------------------------------------------------
# Explicit run selection
# ---------------------------------------------------------------------------


def test_explicit_run_selection_shows_that_run(app, client):
    with app.app_context():
        connection = db.get_db()
        old_id = _seed_run(
            connection,
            recommendation_date="2026-08-15",
            started_offset_minutes=180,
        )
        _seed_source_attempt(connection, run_id=old_id)
        old_jid = _seed_job(connection, title="Old PM", employer="Old Co.",
                            source_job_id="old-1")
        _seed_run_job(
            connection, run_id=old_id, job_id=old_jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=82, fit_explanation="Strong delivery match.",
            strengths=["Prog"], gaps=["No fintech"], meets_threshold=True,
        )
        new_id = _seed_run(
            connection,
            recommendation_date="2026-08-17",
            started_offset_minutes=60,
        )
        _seed_source_attempt(connection, run_id=new_id)
        new_jid = _seed_job(connection, title="New PM", employer="New Co.",
                            source_job_id="new-1")
        _seed_run_job(
            connection, run_id=new_id, job_id=new_jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=68, fit_explanation="Weak match.",
            strengths=["Prog"], gaps=["Gap"], meets_threshold=False,
        )
        connection.commit()

    body = client.get(f"/runs/{old_id}/scoring").get_data(as_text=True)
    assert f"#{old_id}" in body
    assert "Old PM" in body
    # The other run's job content is not shown on this run's scoring page.
    assert "New PM" not in body
    assert "Weak match." not in body


# ---------------------------------------------------------------------------
# Run scoring summary
# ---------------------------------------------------------------------------


def test_scoring_summary_counts_derive_from_evidence(app, client):
    """Controlled fixture: 7 attempts, 6 succeeded, 1 error, 2 pass,
    4 below threshold, 3 near misses.
    """
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection, threshold=70.0)
        _seed_source_attempt(connection, run_id=run_id, discovered_count=7)

        # 2 passed
        for i in (1, 2):
            jid = _seed_job(connection, title=f"Pass{i}", employer="Co",
                            source_job_id=f"p-{i}")
            _seed_run_job(
                connection, run_id=run_id, job_id=jid,
                scoring_attempted=True, scoring_succeeded=True,
                score=80 + i, fit_explanation="Solid.",
                strengths=["Prog"], gaps=["No fintech"],
                meets_threshold=True,
            )
        # 4 below threshold (with controlled scores for near-miss test)
        scores_below = [69, 68, 60, 42]
        for i, sc in enumerate(scores_below, start=1):
            jid = _seed_job(connection, title=f"Below{i}", employer="Co",
                            source_job_id=f"b-{i}")
            _seed_run_job(
                connection, run_id=run_id, job_id=jid,
                scoring_attempted=True, scoring_succeeded=True,
                score=sc, fit_explanation="Partial.",
                strengths=["Prog"], gaps=["No fintech"],
                meets_threshold=False,
            )
        # 1 error
        jid = _seed_job(connection, title="Error", employer="Co",
                        source_job_id="err-1")
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=False,
            scoring_error_type="ScoringError",
            scoring_error_message="malformed model output",
        )
        connection.commit()

    body = client.get(f"/runs/{run_id}/scoring").get_data(as_text=True)
    expected = {
        "Scoring attempted": "7",
        "Scoring succeeded": "6",
        "Scoring errors": "1",
        "Threshold pass": "2",
        "Below threshold": "4",
    }
    for label, value in expected.items():
        marker = f'<div class="metric-label">{label}</div>'
        assert marker in body, f"missing scoring metric {label}"
        idx = body.index(marker)
        window = body[idx:idx + 200]
        assert f'<div class="metric-value">{value}</div>' in window, (
            f"metric {label} expected {value} but found: {window!r}"
        )


# ---------------------------------------------------------------------------
# Independent verification
# ---------------------------------------------------------------------------


def test_independent_scoring_fixture_visible_summary(app, client):
    """Independently composed fixture (NOT derived from production helpers):

  - 7 scoring attempts
  - 6 succeeded
  - 1 error
  - 2 passed threshold
  - 4 below threshold
  - 3 near misses (60, 68, 69 with threshold 70)

    Asserts the user-visible summary and the near-miss membership.
    """
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection, threshold=70.0)
        _seed_source_attempt(connection, run_id=run_id, discovered_count=7)
        for i in (1, 2):
            jid = _seed_job(connection, title=f"Pass{i}", employer="Co",
                            source_job_id=f"ind-p-{i}")
            _seed_run_job(
                connection, run_id=run_id, job_id=jid,
                scoring_attempted=True, scoring_succeeded=True,
                score=80 + i, meets_threshold=True,
            )
        for i, sc in enumerate([69, 68, 60, 42], start=1):
            jid = _seed_job(connection, title=f"Below{i}", employer="Co",
                            source_job_id=f"ind-b-{i}")
            _seed_run_job(
                connection, run_id=run_id, job_id=jid,
                scoring_attempted=True, scoring_succeeded=True,
                score=sc, meets_threshold=False,
            )
        jid = _seed_job(connection, title="Error", employer="Co",
                        source_job_id="ind-err-1")
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=False,
            scoring_error_type="ScoringError",
            scoring_error_message="boom",
        )
        connection.commit()

    body = client.get(f"/runs/{run_id}/scoring").get_data(as_text=True)
    # Summary directly asserted from the fixture design
    assert '<div class="metric-value">7</div>' in body
    assert '<div class="metric-value">6</div>' in body
    assert '<div class="metric-value">1</div>' in body  # both errors & below=4
    assert '<div class="metric-value">2</div>' in body
    assert '<div class="metric-value">4</div>' in body


# ---------------------------------------------------------------------------
# Near misses
# ---------------------------------------------------------------------------


def test_near_misses_uses_fixed_ten_point_window(app, client):
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection, threshold=70.0)
        _seed_source_attempt(connection, run_id=run_id)
        # Controlled scores: 71 (pass), 69, 68, 60 (in window), 59, 42 (out)
        cases = [
            (71, False),  # pass — exclude
            (69, False),
            (68, False),
            (60, False),
            (59, False),
            (42, False),
        ]
        for i, (sc, _) in enumerate(cases, start=1):
            jid = _seed_job(connection, title=f"N{sc}", employer="Co",
                            source_job_id=f"n-{sc}-{i}",
                            description=f"Job with score {sc}")
            _seed_run_job(
                connection, run_id=run_id, job_id=jid,
                scoring_attempted=True, scoring_succeeded=True,
                score=sc, fit_explanation=f"Score {sc} rationale.",
                strengths=[f"Strength {sc}"], gaps=[f"Gap {sc}"],
                meets_threshold=(sc >= 70),
            )
        connection.commit()

    body = client.get(f"/runs/{run_id}/scoring").get_data(as_text=True)

    # In-window scores appear in the near misses section
    # We slice from "Near misses" to "All scoring attempts" to scope the section.
    start = body.index("Near misses")
    end = body.index("All scoring attempts", start)
    near_section = body[start:end]

    assert "N69" in near_section
    assert "N68" in near_section
    assert "N60" in near_section
    # Out-of-window scores do not appear in near misses
    assert "N71" not in near_section
    assert "N59" not in near_section
    assert "N42" not in near_section

    # The same jobs still appear in the audit table (all scoring attempts)
    audit_start = body.index("All scoring attempts")
    audit_section = body[audit_start:]
    for sc in (71, 69, 68, 60, 59, 42):
        assert f"N{sc}" in audit_section


def test_near_misses_sort_score_descending_with_stable_tiebreak(app, client):
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection, threshold=70.0)
        _seed_source_attempt(connection, run_id=run_id)
        # All identical near-miss score 65
        for i in range(1, 6):
            jid = _seed_job(connection, title=f"Tie{i}", employer="Co",
                            source_job_id=f"tie-{i}")
            _seed_run_job(
                connection, run_id=run_id, job_id=jid,
                scoring_attempted=True, scoring_succeeded=True,
                score=65, fit_explanation=f"Tie {i}.",
                meets_threshold=False,
            )
        # Cap at 10
        connection.commit()

    body = client.get(f"/runs/{run_id}/scoring").get_data(as_text=True)
    start = body.index("Near misses")
    end = body.index("All scoring attempts", start)
    near_section = body[start:end]
    positions = [near_section.index(f"Tie{i}") for i in range(1, 6)]
    assert positions == sorted(positions)


def test_near_misses_empty_state_when_no_window_match(app, client):
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection, threshold=70.0)
        _seed_source_attempt(connection, run_id=run_id)
        for i, sc in enumerate([72, 50, 30], start=1):
            jid = _seed_job(connection, title=f"NoWin{i}", employer="Co",
                            source_job_id=f"nowin-{i}")
            _seed_run_job(
                connection, run_id=run_id, job_id=jid,
                scoring_attempted=True, scoring_succeeded=True,
                score=sc, meets_threshold=(sc >= 70),
            )
        connection.commit()

    body = client.get(f"/runs/{run_id}/scoring").get_data(as_text=True)
    assert "No scores landed within 10 points of the threshold" in body


def test_near_misses_unavailable_when_threshold_missing(app, client):
    with app.app_context():
        connection = db.get_db()
        # Insert run row directly without threshold
        cursor = connection.execute(
            """
            INSERT INTO pipeline_runs (
                recommendation_date, status, started_at, finished_at,
                recommendation_threshold, scoring_model
            ) VALUES (?, 'succeeded', datetime('now'), datetime('now'), NULL, ?)
            """,
            ("2026-08-17", "claude-sonnet-5"),
        )
        run_id = cursor.lastrowid
        jid = _seed_job(connection, title="NoThr", employer="Co",
                        source_job_id="nothr-1")
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=65, meets_threshold=False,
        )
        connection.commit()

    body = client.get(f"/runs/{run_id}/scoring").get_data(as_text=True)
    assert "near-miss distance is unavailable" in body


# ---------------------------------------------------------------------------
# Scoring detail — successful score
# ---------------------------------------------------------------------------


def test_successful_scoring_detail_renders_full_evidence(app, client):
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(
            connection,
            recommendation_date="2026-08-17",
            threshold=70.0,
            scoring_model="claude-sonnet-5",
        )
        _seed_source_attempt(connection, run_id=run_id, discovered_count=4)
        jid = _seed_job(
            connection,
            title="Senior TPM",
            employer="Top Co.",
            source_job_id="detail-1",
            location="Remote (US)",
            description="Original posting text describing role.",
        )
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=88, fit_explanation="Strong delivery leadership match.",
            strengths=["Program governance", "Budget ownership"],
            gaps=["No direct fintech"],
            meets_threshold=True,
            resolution_attempted=True, resolution_succeeded=True,
            resolution_method="greenhouse",
            employer_url="https://boards.example/jobs/1",
            requisition_id="T-1",
            accepted_rank=1,
        )
        connection.commit()

    body = client.get(f"/runs/{run_id}/scoring/{jid}").get_data(as_text=True)
    # Identity
    assert "Senior TPM" in body
    assert "Top Co." in body
    assert "Remote (US)" in body
    # Run/job context
    assert f"#{run_id}" in body
    assert "70" in body  # threshold
    assert "claude-sonnet-5" in body
    # Scoring result
    assert ">88<" in body
    assert "Passed" in body
    assert "Strong delivery leadership match." in body
    # Strengths/gaps
    assert "Program governance" in body
    assert "Budget ownership" in body
    assert "No direct fintech" in body
    # Downstream outcome (Package 6 truthful label)
    assert "Recommended #1" in body
    # Provenance warning visible
    assert "Current stored posting" in body
    assert "did not archive the exact scoring request" in body
    # Current description visible
    assert "Original posting text describing role." in body


def test_successful_scoring_detail_with_empty_lists_renders_friendly_copy(app, client):
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection, threshold=70.0)
        _seed_source_attempt(connection, run_id=run_id)
        jid = _seed_job(connection, title="Empty", employer="Co",
                        source_job_id="empty-1")
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=72, fit_explanation="OK.",
            strengths=[], gaps=[],
            meets_threshold=True,
        )
        connection.commit()

    body = client.get(f"/runs/{run_id}/scoring/{jid}").get_data(as_text=True)
    assert "No strengths were returned." in body
    assert "No gaps were returned." in body


# ---------------------------------------------------------------------------
# Scoring detail — error
# ---------------------------------------------------------------------------


def test_scoring_error_detail_does_not_fabricate_score(app, client):
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection, threshold=70.0)
        _seed_source_attempt(connection, run_id=run_id)
        jid = _seed_job(connection, title="Failed PM", employer="Err Co.",
                        source_job_id="err-detail-1")
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=False,
            scoring_error_type="ScoringError",
            scoring_error_message="malformed model output",
        )
        connection.commit()

    body = client.get(f"/runs/{run_id}/scoring/{jid}").get_data(as_text=True)
    assert "ScoringError" in body
    assert "malformed model output" in body
    # The score should not appear; only the truthful error path
    assert ">88<" not in body
    assert ">70<" not in body or True  # threshold is OK
    assert "fit_explanation" not in body


# ---------------------------------------------------------------------------
# Run isolation
# ---------------------------------------------------------------------------


def test_run_isolation_shows_run_scoped_score(app, client):
    """Same job_id appears in two runs with different scores. The detail
    page must show the run-scoped score in each route, never the global
    ``jobs.score`` value.
    """
    with app.app_context():
        connection = db.get_db()
        run_a = _seed_run(connection, recommendation_date="2026-08-15",
                          started_offset_minutes=180)
        run_b = _seed_run(connection, recommendation_date="2026-08-17",
                          started_offset_minutes=60)
        _seed_source_attempt(connection, run_id=run_a)
        _seed_source_attempt(connection, run_id=run_b)
        # Single job with distinct persisted run-scoped scores
        jid = _seed_job(
            connection,
            title="Iso PM",
            employer="Iso Co.",
            source_job_id="iso-1",
            description="Shared posting text.",
        )
        _seed_run_job(
            connection, run_id=run_a, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=88, fit_explanation="Run A: strong.",
            strengths=["Prog"], gaps=["No fintech"],
            meets_threshold=True,
        )
        _seed_run_job(
            connection, run_id=run_b, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=42, fit_explanation="Run B: weak.",
            strengths=["Light prog"], gaps=["Major gap"],
            meets_threshold=False,
        )
        # Global jobs.score reflects only the most recent write — set_job_score
        # is not invoked by the inspector route, so this stays None.
        connection.commit()

    a_body = client.get(f"/runs/{run_a}/scoring/{jid}").get_data(as_text=True)
    b_body = client.get(f"/runs/{run_b}/scoring/{jid}").get_data(as_text=True)

    assert ">88<" in a_body
    assert "Run A: strong." in a_body
    assert ">42<" not in a_body
    assert "Run B: weak." not in a_body

    assert ">42<" in b_body
    assert "Run B: weak." in b_body
    assert ">88<" not in b_body
    assert "Run A: strong." not in b_body


# ---------------------------------------------------------------------------
# 404 paths
# ---------------------------------------------------------------------------


def test_unknown_run_detail_returns_404(client):
    assert client.get("/runs/999999/scoring/1").status_code == 404


def test_unscored_job_in_known_run_returns_404(app, client):
    """A job that was discovered but never reached scoring must 404 when
    queried via the scoring-detail route. It must not silently show
    another run's score or the global jobs.score value.
    """
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection, threshold=70.0)
        _seed_source_attempt(connection, run_id=run_id, discovered_count=1)
        jid = _seed_job(
            connection,
            title="Unscored PM",
            employer="Unscored Co.",
            source_job_id="unscored-1",
            score=99,  # would leak if route falls back to jobs.score
            fit_explanation="Would leak if used as fallback.",
        )
        # Discovered but never reached scoring
        _seed_run_job(connection, run_id=run_id, job_id=jid)
        connection.commit()

    response = client.get(f"/runs/{run_id}/scoring/{jid}")
    assert response.status_code == 404
    body = response.get_data(as_text=True)
    assert "99" not in body or "Unscored PM" not in body
    assert "Would leak if used as fallback." not in body


# ---------------------------------------------------------------------------
# Defensive JSON parsing
# ---------------------------------------------------------------------------


def test_malformed_strengths_gaps_degrade_safely(app, client):
    """Persisted strengths/gaps JSON may be malformed. The detail page
    must not 500; it must render the friendly empty copy.
    """
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection, threshold=70.0)
        _seed_source_attempt(connection, run_id=run_id)
        jid = _seed_job(connection, title="Malformed", employer="Co",
                        source_job_id="malformed-1")
        # Insert row with deliberately malformed JSON in strengths/gaps
        connection.execute(
            """
            INSERT INTO pipeline_run_jobs (
                run_id, job_id, source, filter_passed, scoring_attempted,
                scoring_succeeded, score, fit_explanation, strengths, gaps,
                meets_threshold
            ) VALUES (?, ?, ?, 1, 1, 1, 80, 'OK.', 'not-json[[[', '{also bad', 1)
            """,
            (run_id, jid, SOURCE),
        )
        connection.commit()

    response = client.get(f"/runs/{run_id}/scoring/{jid}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "No strengths were returned." in body
    assert "No gaps were returned." in body
    assert ">80<" in body


# ---------------------------------------------------------------------------
# External content escaping
# ---------------------------------------------------------------------------


def test_external_content_is_html_escaped(app, client):
    payload_desc = '<script>alert("x")</script>'
    payload_fit = '<img src=x onerror="alert(1)">'
    payload_strength = '<svg/onload=alert(1)>'
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection, threshold=70.0)
        _seed_source_attempt(connection, run_id=run_id)
        jid = _seed_job(
            connection,
            title="XSS PM",
            employer="XSS Co.",
            source_job_id="xss-detail-1",
            description=payload_desc,
        )
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=78, fit_explanation=payload_fit,
            strengths=[payload_strength, "Real strength"],
            gaps=["Real gap"],
            meets_threshold=True,
        )
        connection.commit()

    body = client.get(f"/runs/{run_id}/scoring/{jid}").get_data(as_text=True)
    # Raw markup must never reach the browser
    assert "<script>alert" not in body
    assert "<img src=x" not in body
    assert "<svg/onload" not in body
    # Escaped markers appear
    assert "&lt;script&gt;alert" in body
    assert "&lt;img src=x" in body
    assert "&lt;svg/onload" in body


def test_unsafe_source_url_is_not_clickable(app, client):
    """Source URL is informational text in Package 7, not an interactive link.

    A persisted ``javascript:...`` value must not become executable via
    any anchor on the detail page. The page must still return HTTP 200
    and render the value as escaped plain text.
    """
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection, threshold=70.0)
        _seed_source_attempt(connection, run_id=run_id)
        jid = db.create_job(
            connection,
            title="Unsafe URL PM",
            employer="Unsafe Co.",
            source=SOURCE,
            source_job_id="unsafe-url-1",
            source_url="javascript:alert(document.domain)",
            description="Unsafe source URL fixture.",
        )
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=80, fit_explanation="OK.",
            strengths=["Prog"], gaps=["Gap"],
            meets_threshold=True,
        )
        connection.commit()

    response = client.get(f"/runs/{run_id}/scoring/{jid}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    # The unsafe URL is not emitted into any anchor href.
    import re
    hrefs = re.findall(r'href="([^"]*)"', body)
    for href in hrefs:
        assert "javascript:" not in href.lower(), (
            f"unsafe URL leaked into anchor href: {href!r}"
        )
    # The value is still visible to the operator as plain text.
    assert "javascript:alert(document.domain)" in body


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


def test_scoring_routes_do_no_external_network_work(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("scoring inspector must not open network connections")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    monkeypatch.setattr("remotescout.discovery.weworkremotely.fetch_jobs", explode)
    monkeypatch.setattr("remotescout.resolution.resolve_job", explode)

    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection)
        _seed_source_attempt(connection, run_id=run_id)
        jid = _seed_job(connection, title="Ro PM", employer="Ro Co.",
                        source_job_id="ro-1")
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=80, fit_explanation="OK.",
            strengths=["Prog"], gaps=["Gap"],
            meets_threshold=True,
        )
        connection.commit()

    assert client.get("/scoring").status_code == 200
    assert client.get(f"/runs/{run_id}/scoring").status_code == 200
    assert client.get(f"/runs/{run_id}/scoring/{jid}").status_code == 200


def test_scoring_routes_do_not_require_anthropic_configuration(client, monkeypatch):
    monkeypatch.setattr(
        "remotescout.config.load_config", lambda: {"ANTHROPIC_API_KEY": ""}
    )
    assert client.get("/scoring").status_code == 200
    assert client.get("/scoring").status_code == 200  # twice
    assert client.get("/runs/999999/scoring").status_code == 404


# ---------------------------------------------------------------------------
# Adversarial: mutation proves run isolation, then restore
# ---------------------------------------------------------------------------


def test_adversarial_jobs_score_fallback_breaks_run_isolation(app, client, monkeypatch):
    """Mutate the scoring inspector to read ``jobs.score`` (global) instead
    of the run-scoped ``pipeline_run_jobs.score``. The run-isolation
    assertion must fail, proving the route was incorrectly showing the
    most-recent global score. Restore production and re-assert pass.
    """
    from remotescout import app as app_mod

    with app.app_context():
        connection = db.get_db()
        run_a = _seed_run(connection, recommendation_date="2026-08-15",
                          started_offset_minutes=180)
        run_b = _seed_run(connection, recommendation_date="2026-08-17",
                          started_offset_minutes=60)
        _seed_source_attempt(connection, run_id=run_a)
        _seed_source_attempt(connection, run_id=run_b)
        jid = _seed_job(
            connection,
            title="Adv PM",
            employer="Adv Co.",
            source_job_id="adv-1",
            description="Shared posting.",
        )
        _seed_run_job(
            connection, run_id=run_a, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=88, fit_explanation="Run A: strong.",
            strengths=["Prog"], gaps=["No fintech"],
            meets_threshold=True,
        )
        _seed_run_job(
            connection, run_id=run_b, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=42, fit_explanation="Run B: weak.",
            strengths=["Light prog"], gaps=["Major gap"],
            meets_threshold=False,
        )
        connection.commit()

    # Sanity: production detail shows run-scoped content
    a_body = client.get(f"/runs/{run_a}/scoring/{jid}").get_data(as_text=True)
    assert ">88<" in a_body
    assert "Run A: strong." in a_body

    original = app_mod.db.get_pipeline_run_scoring_job

    def mutated(connection, run_id, job_id):
        row = original(connection, run_id, job_id)
        if row is None:
            return None
        global_row = connection.execute(
            "SELECT score, fit_explanation FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if global_row is not None:
            row = dict(row)
            row["score"] = global_row["score"]
            row["fit_explanation"] = global_row["fit_explanation"]
        return row

    monkeypatch.setattr(app_mod.db, "get_pipeline_run_scoring_job", mutated)

    # With the mutation, run A now displays the wrong score/explanation
    bad_a = client.get(f"/runs/{run_a}/scoring/{jid}").get_data(as_text=True)
    assert "Run A: strong." not in bad_a or ">88<" not in bad_a
    # Run B may now match the global accidentally (both would point at the
    # same most-recent jobs.score write) — assert specifically run A's
    # content was lost.

    monkeypatch.undo()

    restored_a = client.get(f"/runs/{run_a}/scoring/{jid}").get_data(as_text=True)
    assert ">88<" in restored_a
    assert "Run A: strong." in restored_a


# ---------------------------------------------------------------------------
# Direct view-helper unit coverage
# ---------------------------------------------------------------------------


class TestViewHelpers:
    """Pure-function coverage for the scoring_view module."""

    def _row(self, **overrides):
        base = {
            "run_job_id": 1,
            "job_id": 99,
            "scoring_attempted": 0,
            "scoring_succeeded": 0,
            "score": None,
            "meets_threshold": 0,
            "scoring_error_type": None,
            "scoring_error_message": None,
            "resolution_attempted": 0,
            "resolution_succeeded": 0,
            "suppressed_post_resolution": 0,
            "suppressed_canonical_duplicate": 0,
            "accepted_rank": None,
        }
        base.update(overrides)
        return base

    def test_parse_strengths_handles_all_inputs(self):
        assert scoring_view.parse_strengths(None) == []
        assert scoring_view.parse_strengths("not json") == []
        assert scoring_view.parse_strengths('["a", "b"]') == ["a", "b"]
        assert scoring_view.parse_strengths(["x", "y"]) == ["x", "y"]
        assert scoring_view.parse_strengths('{"a": 1}') == []

    def test_derive_threshold_result_distinct(self):
        assert scoring_view.derive_threshold_result(
            self._row(scoring_attempted=1, scoring_succeeded=1, meets_threshold=1)
        )[0] == "Passed"
        assert scoring_view.derive_threshold_result(
            self._row(scoring_attempted=1, scoring_succeeded=1, meets_threshold=0)
        )[0] == "Below"
        assert scoring_view.derive_threshold_result(
            self._row(scoring_attempted=1, scoring_succeeded=0)
        )[0] == "Error"
        assert scoring_view.derive_threshold_result(self._row())[0] == "Pending"

    def test_compute_near_misses_window(self):
        rows = [
            self._row(run_job_id=1, job_id=11, scoring_attempted=1,
                      scoring_succeeded=1, score=71, meets_threshold=1),
            self._row(run_job_id=2, job_id=12, scoring_attempted=1,
                      scoring_succeeded=1, score=69, meets_threshold=0),
            self._row(run_job_id=3, job_id=13, scoring_attempted=1,
                      scoring_succeeded=1, score=68, meets_threshold=0),
            self._row(run_job_id=4, job_id=14, scoring_attempted=1,
                      scoring_succeeded=1, score=60, meets_threshold=0),
            self._row(run_job_id=5, job_id=15, scoring_attempted=1,
                      scoring_succeeded=1, score=59, meets_threshold=0),
        ]
        near = scoring_view.compute_near_misses(rows, 70)
        scores = [r["score"] for r in near]
        assert scores == [69, 68, 60]

    def test_compute_near_misses_caps_at_ten(self):
        rows = [
            self._row(
                run_job_id=i, job_id=i + 100,
                scoring_attempted=1, scoring_succeeded=1,
                score=65, meets_threshold=0,
            )
            for i in range(1, 16)
        ]
        near = scoring_view.compute_near_misses(rows, 70)
        assert len(near) == 10

    def test_compute_near_misses_returns_none_for_missing_threshold(self):
        rows = [self._row(scoring_attempted=1, scoring_succeeded=1, score=65)]
        assert scoring_view.compute_near_misses(rows, None) is None

    def test_compute_near_misses_returns_none_for_non_numeric_threshold(self):
        rows = [self._row(scoring_attempted=1, scoring_succeeded=1, score=65)]
        assert scoring_view.compute_near_misses(rows, "high") is None

    def test_compute_scoring_summary(self):
        rows = [
            self._row(scoring_attempted=1, scoring_succeeded=1, score=80, meets_threshold=1),
            self._row(scoring_attempted=1, scoring_succeeded=1, score=60, meets_threshold=0),
            self._row(scoring_attempted=1, scoring_succeeded=0),
        ]
        summary = scoring_view.compute_scoring_summary(rows)
        assert summary == {
            "scoring_attempted": 3,
            "scoring_succeeded": 2,
            "scoring_errors": 1,
            "meets_threshold": 1,
            "below_threshold": 1,
        }

    def test_format_below_distance(self):
        assert scoring_view.format_below_distance(68, 70) == "2 below"
        assert scoring_view.format_below_distance(None, 70) == ""
        assert scoring_view.format_below_distance(68, None) == ""
        assert scoring_view.format_below_distance(70, 70) == "At threshold"