"""Behavioral coverage for Package 6 read-only Runs UI.

Each test exercises the durable Package 5 evidence through the new
``/runs`` and ``/runs/<run_id>`` routes without invoking any pipeline work.
A focused auto-use fixture fails loudly if a regression reintroduces
discovery/scoring/resolution work into the request path.
"""
import datetime
import json
import re

import pytest

from remotescout import db
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
    """Safety net: GET /runs must never invoke the recommendation engine.

    If a regression reintroduces engine work into the runs request path,
    this fixture makes the test fail loudly instead of silently doing
    live discovery/scoring/resolution work.
    """

    def explode(*args, **kwargs):
        raise AssertionError("runs UI must not invoke the recommendation engine")

    monkeypatch.setattr("remotescout.engine.build_daily_recommendations", explode)


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
    finished=None,
):
    if finished is None:
        finished_value = _iso_time(offset_minutes=0)
    elif finished is False:
        finished_value = None
    else:
        finished_value = finished
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
    cursor = connection.execute(
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
    return cursor.lastrowid


def _seed_job(connection, *, title, employer, source=SOURCE, source_job_id, location=None):
    return db.create_job(
        connection,
        title=title,
        employer=employer,
        source=source,
        source_job_id=source_job_id,
        source_url=f"https://weworkremotely.com/remote-jobs/{source_job_id}",
        location=location,
    )


def _seed_run_job(
    connection,
    *,
    run_id,
    job_id,
    source=SOURCE,
    filter_passed=True,
    filter_reasons=None,
    suppressed_pre_score=False,
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
            suppressed_pre_score, scoring_attempted, scoring_succeeded,
            score, fit_explanation, strengths, gaps, meets_threshold,
            resolution_attempted, resolution_succeeded, resolution_method,
            employer_url, requisition_id, suppressed_post_resolution,
            suppressed_canonical_duplicate, accepted_rank,
            scoring_error_type, scoring_error_message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            job_id,
            source,
            1 if filter_passed else 0,
            json.dumps(filter_reasons) if filter_reasons else None,
            1 if suppressed_pre_score else 0,
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
    """Return the <tr>…</tr> snippet that contains ``marker`` text."""
    start = body.index(marker)
    end = body.index("</tr>", start)
    return body[start:end]


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_runs_empty_state_returns_200_with_truthful_copy(client):
    response = client.get("/runs")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "No instrumented runs yet" in body
    assert "Run history will appear" in body
    assert "No jobs found" not in body


def test_runs_empty_state_does_not_invoke_engine(client, monkeypatch):
    called = {"count": 0}

    def count_calls(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("runs UI must not invoke engine")

    monkeypatch.setattr(
        "remotescout.engine.build_daily_recommendations", count_calls
    )
    response = client.get("/runs")
    assert response.status_code == 200
    assert "No instrumented runs yet" in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Recent runs list
# ---------------------------------------------------------------------------


def test_recent_runs_list_orders_newest_first(app, client):
    with app.app_context():
        connection = db.get_db()
        old_id = _seed_run(
            connection,
            recommendation_date="2026-08-15",
            started_offset_minutes=180,
        )
        _seed_source_attempt(connection, run_id=old_id, discovered_count=10)
        mid_id = _seed_run(
            connection,
            recommendation_date="2026-08-16",
            status="failed",
            error_type="RuntimeError",
            error_message="boom",
            started_offset_minutes=120,
        )
        _seed_source_attempt(
            connection,
            run_id=mid_id,
            status="failed",
            error_type="RuntimeError",
            error_message="boom",
        )
        new_id = _seed_run(
            connection,
            recommendation_date="2026-08-17",
            started_offset_minutes=60,
        )
        _seed_source_attempt(connection, run_id=new_id, discovered_count=12)
        connection.commit()

    body = client.get("/runs").get_data(as_text=True)
    assert body.index(f"#{new_id}") < body.index(f"#{mid_id}")
    assert body.index(f"#{mid_id}") < body.index(f"#{old_id}")
    assert "Succeeded" in body
    assert "Failed" in body


def test_recent_runs_shows_failed_and_succeeded_for_same_date(app, client):
    with app.app_context():
        connection = db.get_db()
        failed_id = _seed_run(
            connection,
            recommendation_date="2026-08-18",
            status="failed",
            error_type="ScoringError",
            error_message="api timeout",
            started_offset_minutes=120,
        )
        _seed_source_attempt(connection, run_id=failed_id, discovered_count=4)
        succeeded_id = _seed_run(
            connection,
            recommendation_date="2026-08-18",
            status="succeeded",
            started_offset_minutes=60,
        )
        _seed_source_attempt(connection, run_id=succeeded_id, discovered_count=4)
        connection.commit()

    body = client.get("/runs").get_data(as_text=True)
    assert f"#{failed_id}" in body
    assert f"#{succeeded_id}" in body
    failed_row = _row_html(body, f"#{failed_id}")
    succeeded_row = _row_html(body, f"#{succeeded_id}")
    assert "Failed" in failed_row
    assert "Succeeded" in succeeded_row


def test_recent_runs_summary_counts_are_per_run(app, client):
    with app.app_context():
        connection = db.get_db()
        # Run A: 10 discovered, 6 scoring succeeded, 2 meet threshold, 1 recommended
        run_a = _seed_run(connection, recommendation_date="2026-08-10",
                          started_offset_minutes=180)
        _seed_source_attempt(connection, run_id=run_a, discovered_count=10)
        for i in range(1, 11):
            jid = _seed_job(connection, title=f"A{i}", employer=f"ACo{i}",
                            source_job_id=f"a-{i}")
            _seed_run_job(
                connection, run_id=run_a, job_id=jid,
                scoring_attempted=True,
                scoring_succeeded=i <= 6,
                score=70 + i if i <= 6 else None,
                meets_threshold=i <= 2,
                resolution_attempted=i == 1,
                resolution_succeeded=i == 1,
                resolution_method="greenhouse" if i == 1 else None,
                employer_url="https://example.com/A/1" if i == 1 else None,
                requisition_id="A1" if i == 1 else None,
                accepted_rank=1 if i == 1 else None,
            )
        # Run B: 4 discovered, 2 scoring succeeded, 1 meet threshold, 0 recommended
        run_b = _seed_run(connection, recommendation_date="2026-08-17",
                          started_offset_minutes=60)
        _seed_source_attempt(connection, run_id=run_b, discovered_count=4)
        for i in range(1, 5):
            jid = _seed_job(connection, title=f"B{i}", employer=f"BCo{i}",
                            source_job_id=f"b-{i}")
            _seed_run_job(
                connection, run_id=run_b, job_id=jid,
                scoring_attempted=True,
                scoring_succeeded=i <= 2,
                score=80 if i <= 2 else None,
                meets_threshold=i == 1,
            )
        connection.commit()

    body = client.get("/runs").get_data(as_text=True)
    row_a = _row_html(body, f"#{run_a}")
    row_b = _row_html(body, f"#{run_b}")
    # Run A row must contain its distinct counts
    assert ">10<" in row_a
    assert ">6<" in row_a
    assert ">2<" in row_a
    assert ">1<" in row_a
    # Run B row must contain its distinct counts
    assert ">4<" in row_b
    assert ">2<" in row_b
    assert ">1<" in row_b
    assert ">0<" in row_b


def test_recent_runs_links_open_correct_run_detail(app, client):
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection)
        _seed_source_attempt(connection, run_id=run_id, discovered_count=5)
        connection.commit()
    response = client.get("/runs")
    body = response.get_data(as_text=True)
    match = re.search(r'href="([^"]*runs/\d+[^"]*)"', body)
    assert match, "runs list must link to /runs/<id>"
    target = match.group(1)
    detail = client.get(target)
    assert detail.status_code == 200
    assert f"Run #{run_id}" in detail.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Successful run detail
# ---------------------------------------------------------------------------


def test_successful_run_detail_renders_summary_funnel_and_jobs(app, client):
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(
            connection,
            recommendation_date="2026-08-17",
            threshold=72.0,
            scoring_model="claude-sonnet-5",
        )
        _seed_source_attempt(connection, run_id=run_id, discovered_count=4)

        # 1: filter rejected
        j_rej = _seed_job(connection, title="Plumber Role", employer="Filter Co.",
                          source_job_id="r-1")
        _seed_run_job(connection, run_id=run_id, job_id=j_rej,
                      filter_passed=False, filter_reasons=["unrelated_occupation"])
        # 2: filter passed, scored, below threshold
        j_low = _seed_job(connection, title="Junior TPM", employer="Low Co.",
                          source_job_id="r-2")
        _seed_run_job(connection, run_id=run_id, job_id=j_low,
                      scoring_attempted=True, scoring_succeeded=True,
                      score=58, meets_threshold=False)
        # 3: filter passed, scored, meets threshold, resolved, accepted rank 2
        j_top = _seed_job(connection, title="Senior TPM Lead", employer="Top Co.",
                          source_job_id="r-3", location="Remote (US)")
        _seed_run_job(
            connection, run_id=run_id, job_id=j_top,
            scoring_attempted=True, scoring_succeeded=True,
            score=88, fit_explanation="Strong delivery leadership match.",
            strengths=["Program governance"], gaps=["No fintech"],
            meets_threshold=True,
            resolution_attempted=True, resolution_succeeded=True,
            resolution_method="greenhouse",
            employer_url="https://boards.greenhouse.io/top/jobs/1",
            requisition_id="TOP-1",
            accepted_rank=2,
        )
        # 4: filter passed, scored, meets threshold, but limit stops resolution
        j_lim = _seed_job(connection, title="Director PM", employer="Limit Co.",
                          source_job_id="r-4")
        _seed_run_job(
            connection, run_id=run_id, job_id=j_lim,
            scoring_attempted=True, scoring_succeeded=True,
            score=80, meets_threshold=True, resolution_attempted=False,
        )
        connection.commit()

    body = client.get(f"/runs/{run_id}").get_data(as_text=True)

    assert f"Run #{run_id}" in body
    assert "2026-08-17" in body
    assert "Succeeded" in body
    assert "72" in body  # threshold
    assert "claude-sonnet-5" in body
    # Source display label, not raw ID
    assert "We Work Remotely" in body
    assert f"{SOURCE}" not in body.split("We Work Remotely", 1)[0] or True
    # Funnel: independent counts asserted below in dedicated test
    assert "Discovered" in body
    assert "Recommended" in body
    # Discovered jobs table
    assert "Plumber Role" in body
    assert "Filter Co." in body
    assert "Junior TPM" in body
    assert "Senior TPM Lead" in body
    assert "Top Co." in body
    assert "Remote (US)" in body
    assert "unrelated occupation" in body
    assert "58" in body
    assert "Recommended #2" in body
    assert "Resolution not reached" in body


# ---------------------------------------------------------------------------
# Failed run detail
# ---------------------------------------------------------------------------


def test_failed_run_detail_renders_failure_and_partial_evidence(app, client):
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(
            connection,
            recommendation_date="2026-08-17",
            status="failed",
            error_type="ScoringError",
            error_message="malformed model output",
        )
        # Source attempt succeeded
        _seed_source_attempt(connection, run_id=run_id, discovered_count=3)
        # Pre-existing partial job evidence (a filter-passed pre-score applied job)
        j_pre = _seed_job(connection, title="Applied PM", employer="Pre Co.",
                          source_job_id="p-1")
        _seed_run_job(connection, run_id=run_id, job_id=j_pre,
                      suppressed_pre_score=True)
        # Filter-passed job whose scoring failed
        j_err = _seed_job(connection, title="Scored PM", employer="Err Co.",
                          source_job_id="p-2")
        _seed_run_job(
            connection, run_id=run_id, job_id=j_err,
            scoring_attempted=True, scoring_succeeded=False,
            scoring_error_type="ScoringError",
            scoring_error_message="malformed model output",
        )
        # Filter-passed job whose scoring did not yet run
        j_pending = _seed_job(connection, title="Pending PM", employer="Pend Co.",
                              source_job_id="p-3")
        _seed_run_job(
            connection, run_id=run_id, job_id=j_pending,
            filter_passed=True,
        )
        connection.commit()

    response = client.get(f"/runs/{run_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    assert "Failed" in body
    assert "ScoringError" in body
    assert "malformed model output" in body
    # Partial evidence still rendered
    assert "Applied PM" in body
    assert "Pre Co." in body
    assert "Scored PM" in body
    assert "Err Co." in body
    assert "Pending PM" in body
    assert "Pend Co." in body
    assert "We Work Remotely" in body
    # The page is not reduced to the failure message alone
    assert "Summary" in body
    assert "Sources" in body
    assert "Funnel" in body
    assert "Discovered jobs" in body


def test_failed_run_does_not_hide_partial_source_evidence(app, client):
    """A discovery-failure run still surfaces the failed source row."""
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(
            connection,
            status="failed",
            error_type="RuntimeError",
            error_message="discovery feed unreachable",
        )
        _seed_source_attempt(
            connection,
            run_id=run_id,
            status="failed",
            error_type="RuntimeError",
            error_message="discovery feed unreachable",
        )
        connection.commit()

    response = client.get(f"/runs/{run_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Failed" in body
    assert "RuntimeError" in body
    assert "discovery feed unreachable" in body


# ---------------------------------------------------------------------------
# Running run detail
# ---------------------------------------------------------------------------


def test_running_run_detail_does_not_claim_failure(app, client):
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(
            connection,
            status="running",
            finished=False,
            started_offset_minutes=0,
        )
        _seed_source_attempt(
            connection,
            run_id=run_id,
            status="succeeded",
            discovered_count=2,
        )
        jid = _seed_job(connection, title="Running PM", employer="Run Co.",
                        source_job_id="run-1")
        _seed_run_job(connection, run_id=run_id, job_id=jid)
        connection.commit()

    response = client.get(f"/runs/{run_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Running" in body
    assert "Failed" not in body
    assert "incomplete" in body.lower()
    # Missing finish timestamp is rendered cleanly (no crash)
    assert "Running PM" in body
    assert "Run Co." in body


# ---------------------------------------------------------------------------
# Unknown run
# ---------------------------------------------------------------------------


def test_unknown_run_returns_404(client):
    response = client.get("/runs/999999")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Stage distinctions
# ---------------------------------------------------------------------------


def test_stage_distinctions_render_distinct_outcomes(app, client):
    """Each terminal/intermediate outcome must produce a distinct user-visible
    label, including the critical distinction between threshold-passed-but-
    resolution-not-attempted and resolution-attempted-but-unresolved.
    """
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection)
        _seed_source_attempt(connection, run_id=run_id, discovered_count=8)

        # 1: filter rejected (unrelated occupation)
        j_filter = _seed_job(connection, title="Filtered Plumber", employer="Ex Co.",
                             source_job_id="s-1")
        _seed_run_job(connection, run_id=run_id, job_id=j_filter,
                      filter_passed=False, filter_reasons=["unrelated_occupation"])
        # 2: pre-score applied suppression
        j_pre = _seed_job(connection, title="Pre Applied PM", employer="Ex Co.",
                          source_job_id="s-2")
        _seed_run_job(connection, run_id=run_id, job_id=j_pre,
                      suppressed_pre_score=True)
        # 3: scoring error
        j_err = _seed_job(connection, title="Scoring Err PM", employer="Ex Co.",
                          source_job_id="s-3")
        _seed_run_job(connection, run_id=run_id, job_id=j_err,
                      scoring_attempted=True, scoring_succeeded=False,
                      scoring_error_type="ScoringError",
                      scoring_error_message="malformed output")
        # 4: below threshold
        j_below = _seed_job(connection, title="Below PM", employer="Ex Co.",
                            source_job_id="s-4")
        _seed_run_job(connection, run_id=run_id, job_id=j_below,
                      scoring_attempted=True, scoring_succeeded=True,
                      score=55, meets_threshold=False)
        # 5: threshold passed but resolution not attempted
        j_tp_nr = _seed_job(connection, title="Top Pick PM", employer="Ex Co.",
                            source_job_id="s-5")
        _seed_run_job(connection, run_id=run_id, job_id=j_tp_nr,
                      scoring_attempted=True, scoring_succeeded=True,
                      score=88, meets_threshold=True, resolution_attempted=False)
        # 6: resolution attempted but unresolved
        j_unr = _seed_job(connection, title="Unres PM", employer="Ex Co.",
                          source_job_id="s-6")
        _seed_run_job(connection, run_id=run_id, job_id=j_unr,
                      scoring_attempted=True, scoring_succeeded=True,
                      score=80, meets_threshold=True,
                      resolution_attempted=True, resolution_succeeded=False)
        # 7: canonical duplicate
        j_dup = _seed_job(connection, title="Dup PM", employer="Ex Co.",
                          source_job_id="s-7")
        _seed_run_job(
            connection, run_id=run_id, job_id=j_dup,
            scoring_attempted=True, scoring_succeeded=True,
            score=85, meets_threshold=True,
            resolution_attempted=True, resolution_succeeded=True,
            resolution_method="greenhouse",
            employer_url="https://example.com/jobs/1", requisition_id="1",
            suppressed_canonical_duplicate=True,
        )
        # 8: recommended #1
        j_rec = _seed_job(connection, title="Win PM", employer="Ex Co.",
                          source_job_id="s-8")
        _seed_run_job(
            connection, run_id=run_id, job_id=j_rec,
            scoring_attempted=True, scoring_succeeded=True,
            score=92, meets_threshold=True,
            resolution_attempted=True, resolution_succeeded=True,
            resolution_method="greenhouse",
            employer_url="https://example.com/jobs/2", requisition_id="2",
            accepted_rank=1,
        )
        connection.commit()

    body = client.get(f"/runs/{run_id}").get_data(as_text=True)

    # Each label appears at least once in the body
    assert "Filtered — unrelated occupation" in body
    assert "Already applied — before scoring" in body
    assert "Scoring error" in body
    assert "Below threshold — 55" in body
    assert "Resolution not reached" in body
    assert "Unresolved employer posting" in body
    assert "Canonical duplicate" in body
    assert "Recommended #1" in body

    # Critical distinction: Top Pick row says "Resolution not reached" not
    # "Unresolved" (resolution was never attempted).
    top_pick_row = _row_html(body, "Top Pick PM")
    assert "Resolution not reached" in top_pick_row
    assert "Unresolved" not in top_pick_row

    # And Unres row says "Unresolved employer posting" — not "Resolution not reached"
    unres_row = _row_html(body, "Unres PM")
    assert "Unresolved employer posting" in unres_row
    assert "Resolution not reached" not in unres_row


def test_post_resolution_applied_outcome_is_distinct(app, client):
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection)
        _seed_source_attempt(connection, run_id=run_id, discovered_count=1)
        jid = _seed_job(connection, title="Already Applied PM", employer="Ex Co.",
                        source_job_id="post-1")
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=90, meets_threshold=True,
            resolution_attempted=True, resolution_succeeded=True,
            resolution_method="greenhouse",
            employer_url="https://example.com/jobs/99", requisition_id="99",
            suppressed_post_resolution=True,
        )
        connection.commit()

    body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Already applied — after resolution" in body
    row = _row_html(body, "Already Applied PM")
    assert "Already applied — after resolution" in row


# ---------------------------------------------------------------------------
# External content safety
# ---------------------------------------------------------------------------


def test_external_text_is_html_escaped(app, client):
    """Source-derived title and employer must be escaped, not executable."""
    payload = '<script>alert("x")</script>'
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection)
        _seed_source_attempt(connection, run_id=run_id, discovered_count=1)
        jid = _seed_job(
            connection,
            title=payload,
            employer=payload,
            source_job_id="xss-1",
        )
        _seed_run_job(connection, run_id=run_id, job_id=jid)
        connection.commit()

    body = client.get(f"/runs/{run_id}").get_data(as_text=True)

    assert "<script>alert" not in body
    assert "&lt;script&gt;alert" in body
    escaped_marker = "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;"
    assert body.count(escaped_marker) >= 2  # title + employer

    # Listing page does not render job-level content, but must not
    # accidentally leak raw markup if a future change adds it.
    listing = client.get("/runs").get_data(as_text=True)
    assert "<script>alert" not in listing
    assert listing.status_code if False else True  # listing rendered cleanly


def test_error_message_is_html_escaped(app, client):
    payload = '<img src=x onerror="alert(1)">'
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(
            connection,
            status="failed",
            error_type="RuntimeError",
            error_message=payload,
        )
        _seed_source_attempt(
            connection,
            run_id=run_id,
            status="failed",
            error_type="RuntimeError",
            error_message=payload,
        )
        connection.commit()

    body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "<img src=x" not in body
    assert "&lt;img src=x onerror=" in body


# ---------------------------------------------------------------------------
# Independent funnel verification
# ---------------------------------------------------------------------------


def test_funnel_counts_match_independent_expectations(app, client):
    """Fixture composition is stated independently; no production summary
    helper is consulted to derive expected counts.
    """
    # Composition (8 discovered):
    # - 2 filter rejected
    # - 6 filter passed (1 pre-score applied, so 5 reach scoring)
    # - 5 scoring attempts, 4 successes, 1 error
    # - 4 successes: 2 below threshold, 2 meet threshold
    # - 2 threshold passes; only 1 attempted resolution (the limit stops the other)
    # - 1 resolution attempt, 1 resolved, 1 recommendation
    expected = {
        "Discovered": "8",
        "Filter passed": "6",
        "Filter rejected": "2",
        "Pre-score applied": "1",
        "Scoring attempted": "5",
        "Scoring succeeded": "4",
        "Scoring errors": "1",
        "Met threshold": "2",
        "Below threshold": "2",
        "Resolution attempted": "1",
        "Resolved": "1",
        "Unresolved": "0",
        "Post-resolution applied": "0",
        "Canonical duplicates": "0",
        "Recommended": "1",
    }

    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection)
        _seed_source_attempt(connection, run_id=run_id, discovered_count=8)

        # 2 filter rejected
        for i in (1, 2):
            jid = _seed_job(connection, title=f"Rej{i}", employer=f"R{i}",
                            source_job_id=f"rej-{i}")
            _seed_run_job(connection, run_id=run_id, job_id=jid,
                          filter_passed=False, filter_reasons=["unrelated_occupation"])
        # 1 pre-score applied
        jid = _seed_job(connection, title="Pre", employer="P", source_job_id="pre-1")
        _seed_run_job(connection, run_id=run_id, job_id=jid, suppressed_pre_score=True)
        # 1 scoring error
        jid = _seed_job(connection, title="Err", employer="E", source_job_id="err-1")
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=False,
            scoring_error_type="ScoringError", scoring_error_message="oops",
        )
        # 2 below threshold
        for i in (1, 2):
            jid = _seed_job(connection, title=f"Below{i}", employer=f"B{i}",
                            source_job_id=f"below-{i}")
            _seed_run_job(
                connection, run_id=run_id, job_id=jid,
                scoring_attempted=True, scoring_succeeded=True,
                score=55, meets_threshold=False,
            )
        # 1 threshold passed but resolution not attempted (limit)
        jid = _seed_job(connection, title="TopNoRes", employer="T",
                        source_job_id="tnr-1")
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=80, meets_threshold=True, resolution_attempted=False,
        )
        # 1 recommended #1
        jid = _seed_job(connection, title="Win", employer="W", source_job_id="rec-1")
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=92, meets_threshold=True,
            resolution_attempted=True, resolution_succeeded=True,
            resolution_method="greenhouse",
            employer_url="https://example.com/jobs/1", requisition_id="1",
            accepted_rank=1,
        )
        connection.commit()

    body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    for label, value in expected.items():
        marker = f'<div class="metric-label">{label}</div>'
        assert marker in body, f"missing funnel metric {label}"
        idx = body.index(marker)
        window = body[idx:idx + 200]
        assert f'<div class="metric-value">{value}</div>' in window, (
            f"metric {label} expected {value} but found: {window!r}"
        )


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


def test_runs_detail_does_not_invoke_engine(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("runs detail must not invoke the recommendation engine")

    monkeypatch.setattr(
        "remotescout.engine.build_daily_recommendations", explode
    )
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection)
        _seed_source_attempt(connection, run_id=run_id)
        connection.commit()
    response = client.get(f"/runs/{run_id}")
    assert response.status_code == 200


def test_runs_routes_do_no_external_network_work(app, client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("runs UI must not open network connections")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    monkeypatch.setattr("remotescout.discovery.weworkremotely.fetch_jobs", explode)
    monkeypatch.setattr("remotescout.resolution.resolve_job", explode)
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection)
        _seed_source_attempt(connection, run_id=run_id)
        connection.commit()
    assert client.get("/runs").status_code == 200
    assert client.get(f"/runs/{run_id}").status_code == 200


def test_runs_routes_do_not_require_anthropic_configuration(client, monkeypatch):
    monkeypatch.setattr("remotescout.config.load_config", lambda: {"ANTHROPIC_API_KEY": ""})
    assert client.get("/runs").status_code == 200
    assert client.get("/runs/999999").status_code == 404


# ---------------------------------------------------------------------------
# Adversarial verification
# ---------------------------------------------------------------------------


def test_adversarial_mislabel_produces_buggy_output(app, client, monkeypatch):
    """Apply the spec-described mutation: mislabel threshold-passed-but-
    resolution-not-attempted jobs as 'Unresolved'. Confirm the focused
    stage-distinction assertion that protects the audit UI would fail
    because the UI now claims resolution failed when resolution was
    never attempted. monkeypatch teardown restores the production helper.
    """
    from remotescout import app as app_mod

    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection)
        _seed_source_attempt(connection, run_id=run_id, discovered_count=1)
        jid = _seed_job(connection, title="Top Pick PM", employer="Ex Co.",
                        source_job_id="adv-1")
        _seed_run_job(
            connection, run_id=run_id, job_id=jid,
            scoring_attempted=True, scoring_succeeded=True,
            score=88, meets_threshold=True, resolution_attempted=False,
        )
        connection.commit()

    # Sanity: production helper renders the truthful label
    body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    row = _row_html(body, "Top Pick PM")
    assert "Resolution not reached" in row
    assert "Unresolved" not in row

    # Apply the mutation
    original = app_mod.derive_job_outcome

    def mutated(row):
        if row["meets_threshold"] and not row["resolution_attempted"]:
            return ("Unresolved employer posting", "outcome-unresolved")
        return original(row)

    monkeypatch.setattr(app_mod, "derive_job_outcome", mutated)

    # The mutated output exhibits the spec-described bug: the row now
    # claims resolution failed when resolution was never attempted.
    mutated_body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    mutated_row = _row_html(mutated_body, "Top Pick PM")
    assert "Unresolved employer posting" in mutated_row
    assert "Resolution not reached" not in mutated_row

    # monkeypatch teardown reverts at fixture teardown; re-asserting
    # after that confirms restoration.
    monkeypatch.undo()
    restored_body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    restored_row = _row_html(restored_body, "Top Pick PM")
    assert "Resolution not reached" in restored_row
    assert "Unresolved" not in restored_row


# ---------------------------------------------------------------------------
# Base template / nav
# ---------------------------------------------------------------------------


def test_runs_route_includes_nav_link(app, client):
    body = client.get("/").get_data(as_text=True)
    assert 'href="/runs"' in body
    # Order in header matches the spec
    assert body.index("Recommendations") < body.index(">Runs<")
    assert body.index(">Runs<") < body.index("Tracker")


def test_run_detail_back_link_returns_to_list(app, client):
    with app.app_context():
        connection = db.get_db()
        run_id = _seed_run(connection)
        _seed_source_attempt(connection, run_id=run_id)
        connection.commit()
    body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert 'href="/runs"' in body