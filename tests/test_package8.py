"""Behavioral coverage for Package 8 selective scoring and cost containment.

Each test exercises a specific clause of the Package 8 spec:
  1. Already-processed suppression (multi-day)
  2. Positive deterministic target-role gate
  3. Deterministic candidate ranking
  4. Hard scoring budget
  5. Existing rejected jobs remain cheap
  6. Budget regression
  7. Ranking regression
  8. Telemetry truthfulness for new stages
  9. Run-detail outcome labels
  10. Cost-safety invariant (number of score calls <= budget)
  11. Schema compatibility for additive columns (fresh + in-place upgrade)
"""
import json as json_lib

import pytest

from remotescout import db, ranking, targeting
from remotescout.app import create_app
from remotescout.discovery import DiscoveredJob
from remotescout.engine import build_daily_recommendations
from remotescout.resolution import ResolutionResult
from remotescout.scoring import ScoreResult, ScoringError


DAY_A = "2026-08-11"
DAY_B = "2026-08-12"


def make_job(**overrides):
    fields = {
        "source": "weworkremotely",
        "source_url": "https://weworkremotely.com/remote-jobs/sample",
        "source_job_id": "sample-1",
        "title": "Senior Technical Program Manager",
        "employer": "Acme Inc.",
        "location": "Anywhere in the World",
        "description": "Lead cross-functional infrastructure delivery programs.",
    }
    fields.update(overrides)
    return DiscoveredJob(**fields)


def make_neutral_description_job(**overrides):
    """A test fixture with a deliberately neutral description.

    Used when the test wants the gate decision to come from the title
    alone, not from a description-side technical-context fallback.
    """
    fields = {
        "source": "weworkremotely",
        "source_url": "https://weworkremotely.com/remote-jobs/sample",
        "source_job_id": "sample-1",
        "title": "Senior Technical Program Manager",
        "employer": "Acme Inc.",
        "location": "Anywhere in the World",
        "description": "",
    }
    fields.update(overrides)
    return DiscoveredJob(**fields)


def score_result(score, explanation="Strong match"):
    return ScoreResult(
        score=score,
        fit_explanation=explanation,
        strengths=["Program governance"],
        gaps=["No fintech"],
    )


def ok_resolution(url, requisition_id=None):
    return ResolutionResult(
        resolved=True,
        employer_url=url,
        requisition_id=requisition_id,
        method="greenhouse",
    )


class FakeDiscover:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return list(self.jobs)


class CountingScorer:
    """Scoring fake that fails loudly if invoked when ``enabled`` is False."""

    def __init__(self, results=None, failures=None, enabled=True):
        self.results = dict(results or {})
        self.failures = dict(failures or {})
        self.calls = []
        self.enabled = enabled

    def __call__(self, job, resume_text):
        self.calls.append((job.title, job.source_job_id))
        if not self.enabled:
            raise AssertionError(
                "scoring must not be called when CountingScorer is disabled"
            )
        if job.title in self.failures:
            raise self.failures[job.title]
        return self.results[job.title]


class FakeResolver:
    def __init__(self, results=None, failures=None):
        self.results = dict(results or {})
        self.failures = dict(failures or {})
        self.calls = []

    def __call__(self, job):
        self.calls.append(job.title)
        if job.title in self.failures:
            raise self.failures[job.title]
        return self.results[job.title]


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE_PATH": str(tmp_path / "test.db")})


@pytest.fixture
def client(app):
    return app.test_client()


def run_pipeline(
    app,
    discover,
    scorer,
    resolver,
    *,
    threshold=70,
    day=DAY_A,
    scoring_budget=None,
):
    with app.app_context():
        connection = db.get_db()
        return build_daily_recommendations(
            connection,
            recommendation_date=day,
            discover=discover,
            score=scorer,
            resolve=resolver,
            resume_text="resume text",
            threshold=threshold,
            scoring_budget=scoring_budget,
        )


def fetch_run_jobs(connection, run_id):
    return db.get_pipeline_run_jobs(connection, run_id)


def run_jobs_by_title(connection, run_id):
    run_jobs = fetch_run_jobs(connection, run_id)
    return {
        connection.execute(
            "SELECT title FROM jobs WHERE id = ?", (row["job_id"],)
        ).fetchone()["title"]: row
        for row in run_jobs
    }


# ---------------------------------------------------------------------------
# 1. Already-processed suppression — multi-day
# ---------------------------------------------------------------------------


def test_already_scored_job_is_not_rescored_on_later_day(app):
    job = make_job(source_job_id="multi-day-1")
    scorer = CountingScorer({"Senior Technical Program Manager": score_result(88)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/tpm")}
    )
    first = run_pipeline(app, FakeDiscover([job]), scorer, resolver, day=DAY_A)
    assert len(first) == 1
    assert scorer.calls == [("Senior Technical Program Manager", "multi-day-1")]

    second = run_pipeline(app, FakeDiscover([job]), scorer, resolver, day=DAY_B)
    assert second == []
    assert scorer.calls == [("Senior Technical Program Manager", "multi-day-1")]


def test_already_processed_telemetry_marks_suppression_on_day_two(app):
    job_a = make_job(source_job_id="multi-day-A", title="Director, Technical Program Management")
    job_b = make_job(source_job_id="multi-day-B", title="Program Delivery Director")
    scorer = CountingScorer(
        {
            "Director, Technical Program Management": score_result(92),
            "Program Delivery Director": score_result(85),
        }
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution("https://acme.com/careers/dtpm"),
            "Program Delivery Director": ok_resolution("https://acme.com/careers/pdd"),
        }
    )
    run_pipeline(app, FakeDiscover([job_a, job_b]), scorer, resolver, day=DAY_A)
    assert len(scorer.calls) == 2

    run_pipeline(app, FakeDiscover([job_a, job_b]), scorer, resolver, day=DAY_B)
    assert scorer.calls == [
        ("Director, Technical Program Management", "multi-day-A"),
        ("Program Delivery Director", "multi-day-B"),
    ]

    with app.app_context():
        connection = db.get_db()
        runs = db.get_pipeline_runs_for_date(connection, DAY_B)
        day_b_run_id = runs[0]["id"]
        rows_by_title = run_jobs_by_title(connection, day_b_run_id)
    assert rows_by_title["Director, Technical Program Management"]["suppressed_already_processed"] == 1
    assert rows_by_title["Program Delivery Director"]["suppressed_already_processed"] == 1


# ---------------------------------------------------------------------------
# 2. Positive deterministic target-role gate
# ---------------------------------------------------------------------------


def test_strong_target_title_passes_immediately():
    result = targeting.evaluate(
        make_job(title="Director, Technical Program Management")
    )
    assert result.passed is True
    assert result.reason == "strong_target_title"


def test_program_delivery_director_passes():
    result = targeting.evaluate(make_job(title="Program Delivery Director"))
    assert result.passed is True


def test_director_epmo_passes():
    result = targeting.evaluate(make_job(title="Director, EPMO"))
    assert result.passed is True


def test_generic_program_manager_without_context_fails():
    result = targeting.evaluate(make_job(title="Program Manager", description=""))
    assert result.passed is False


def test_program_manager_with_technical_context_passes():
    result = targeting.evaluate(
        make_job(title="Program Manager", description="Cloud platform delivery")
    )
    assert result.passed is True


def test_bare_tpm_alone_fails():
    result = targeting.evaluate(
        make_neutral_description_job(title="TPM", description="")
    )
    assert result.passed is False


def test_bare_tpm_with_seniority_context_passes():
    """``Senior TPM`` (no further context) is allowed by bounded TPM
    leadership matching — the spec lists ``Senior TPM, Infrastructure``,
    ``Sr. TPM - Cloud Platform``, ``Principal TPM``, ``Director, TPM``
    as passing examples, and ``Senior TPM`` carries the same
    seniority prefix.
    """
    result = targeting.evaluate(
        make_neutral_description_job(title="Senior TPM", description="")
    )
    assert result.passed is True


def test_principal_tpm_passes_with_neutral_description():
    result = targeting.evaluate(
        make_neutral_description_job(title="Principal TPM", description="")
    )
    assert result.passed is True


def test_director_tpm_passes_with_neutral_description():
    result = targeting.evaluate(
        make_neutral_description_job(title="Director, TPM", description="")
    )
    assert result.passed is True


def test_sr_tpm_with_cloud_platform_passes():
    result = targeting.evaluate(
        make_neutral_description_job(
            title="Sr. TPM - Cloud Platform",
            description="",
        )
    )
    assert result.passed is True


def test_tpm_substring_inside_unrelated_word_does_not_match():
    """``TPM`` must only match as a bounded word, never as a substring."""
    result = targeting.evaluate(
        make_neutral_description_job(title="Implementation Manager", description="")
    )
    assert result.passed is False


def test_bare_tpm_with_technical_program_manager_context_passes():
    result = targeting.evaluate(
        make_job(
            title="TPM",
            description="Must have technical program manager background.",
        )
    )
    assert result.passed is True


def test_generic_director_unrelated_role_fails():
    result = targeting.evaluate(
        make_job(
            title="Director of Marketing",
            description="Lead marketing teams.",
        )
    )
    assert result.passed is False


def test_generic_product_manager_fails():
    """A senior product role without bounded technical scope fails.

    The default make_job description carries ``infrastructure`` so we
    build a job with a deliberately empty description to ensure the
    decision rests on the title alone.
    """
    result = targeting.evaluate(
        make_neutral_description_job(
            title="Senior Product Manager",
            description="",
        )
    )
    assert result.passed is False


def test_senior_product_manager_with_technical_scope_passes():
    """Senior product roles are allowed when bounded technical scope is
    present (either in the title or the description).
    """
    result = targeting.evaluate(
        make_neutral_description_job(
            title="Senior Product Manager",
            description="Lead platform engineering team.",
        )
    )
    assert result.passed is True


def test_senior_product_manager_consumer_growth_fails():
    """Consumer-growth / marketing-adjacent product roles still fail even
    when bounded technical scope keywords appear elsewhere.
    """
    result = targeting.evaluate(
        make_neutral_description_job(
            title="Senior Product Manager, Consumer Growth",
            description="Grow consumer engagement across web and mobile.",
        )
    )
    assert result.passed is False


def test_director_product_management_with_technical_scope_passes():
    result = targeting.evaluate(
        make_neutral_description_job(
            title="Director, Product Management",
            description="Own infrastructure platform product strategy.",
        )
    )
    assert result.passed is True


def test_principal_product_manager_with_technical_scope_passes():
    result = targeting.evaluate(
        make_neutral_description_job(
            title="Principal Product Manager",
            description="Drive cloud platform integrations.",
        )
    )
    assert result.passed is True


def test_principal_engineer_fails_no_program_signal():
    result = targeting.evaluate(
        make_job(title="Principal Software Engineer", description="Build services.")
    )
    assert result.passed is False


def test_it_alone_does_not_satisfy_context():
    """`IT` substring must not be enough to satisfy context-required titles."""
    result = targeting.evaluate(
        make_job(title="Program Manager", description="Working with IT systems.")
    )
    assert result.passed is False


def test_digital_transformation_phrase_passes():
    result = targeting.evaluate(make_job(title="Digital Transformation Lead"))
    assert result.passed is True


# ---------------------------------------------------------------------------
# Obvious-title regression matrix (table-driven)
# ---------------------------------------------------------------------------


OBVIOUS_TARGET_TITLES = [
    "Technical Program Manager",
    "Technical Program Manager, Core Infrastructure",
    "Senior Technical Program Manager",
    "Sr. Technical Program Manager",
    "Principal Technical Program Manager",
    "Senior Principal, Technical Program Management",
    "Director, Technical Program Management",
    "Senior Director, Technical Program Management",
    "Sr. Director, Technical Program Management",
    "Technical Program Director",
    "Program Delivery Director",
    "Director, EPMO",
    "Associate Program Director - Technology Transformation",
    "Senior TPM, Infrastructure",
    "Sr. TPM - Cloud Platform",
    "Principal TPM",
    "Director, TPM",
]


UNRELATED_TITLES = [
    "Director of Marketing",
    "Senior Software Engineer",
    "Director of Quality Management",
    "Social Media Manager",
    "Senior Product Manager, Consumer Growth",
    "Junior Designer",
    "Plumber",
    "Account Executive",
    "Recruiter",
    "Sales Development Representative",
]


CONTEXT_PRODUCT_PASS_CASES = [
    (
        "Senior Product Manager",
        "Lead platform engineering team. Drive cross-functional delivery.",
        True,
    ),
    (
        "Senior Product Manager",
        "Own cloud platform product roadmap for infrastructure tooling.",
        True,
    ),
    (
        "Director, Product Management",
        "Lead infrastructure platform product strategy across engineering.",
        True,
    ),
    (
        "Principal Product Manager",
        "Drive SaaS platform integrations and developer tooling.",
        True,
    ),
]


CONTEXT_PRODUCT_FAIL_CASES = [
    (
        "Senior Product Manager, Consumer Growth",
        "Grow consumer engagement across web and mobile channels.",
        False,
    ),
    (
        "Senior Product Manager",
        "Drive consumer retention, brand campaigns, marketing analytics.",
        False,
    ),
    (
        "Director, Product Management",
        "Lead consumer retail product line and brand positioning.",
        False,
    ),
]


@pytest.mark.parametrize("title", OBVIOUS_TARGET_TITLES)
def test_obvious_target_title_passes(title):
    result = targeting.evaluate(
        make_neutral_description_job(title=title, description="")
    )
    assert result.passed is True, (
        f"obvious target title unexpectedly rejected: {title!r} "
        f"({result.reason})"
    )


@pytest.mark.parametrize("title", UNRELATED_TITLES)
def test_unrelated_title_fails(title):
    result = targeting.evaluate(
        make_neutral_description_job(title=title, description="")
    )
    assert result.passed is False, (
        f"unrelated title unexpectedly accepted: {title!r} "
        f"({result.reason})"
    )


@pytest.mark.parametrize("title,description,expected", CONTEXT_PRODUCT_PASS_CASES)
def test_context_product_leadership_pass_cases(title, description, expected):
    result = targeting.evaluate(
        make_neutral_description_job(title=title, description=description)
    )
    assert result.passed is expected, (
        f"context product leadership {title!r}/{description!r} "
        f"unexpected: passed={result.passed} reason={result.reason}"
    )


@pytest.mark.parametrize("title,description,expected", CONTEXT_PRODUCT_FAIL_CASES)
def test_context_product_leadership_fail_cases(title, description, expected):
    result = targeting.evaluate(
        make_neutral_description_job(title=title, description=description)
    )
    assert result.passed is expected, (
        f"context product leadership {title!r}/{description!r} "
        f"unexpected: passed={result.passed} reason={result.reason}"
    )


# ---------------------------------------------------------------------------
# 3. Deterministic candidate ranking
# ---------------------------------------------------------------------------


def test_ranking_orders_by_relevance_with_stable_job_id_tiebreak():
    strong = make_job(source_job_id="r-strong", title="Director, Technical Program Management")
    principal = make_job(source_job_id="r-principal", title="Principal Technical Program Manager")
    contextual = make_job(
        source_job_id="r-ctx",
        title="Program Manager",
        description="Cloud platform delivery leadership",
    )
    ranked = ranking.rank_candidates([(2, contextual), (1, principal), (3, strong)])
    assert [c.job_id for c in ranked] == [1, 3, 2]


def test_ranking_excludes_non_gate_passing_candidates():
    unrelated = make_job(source_job_id="r-unrelated", title="Director of Marketing")
    program_delivery = make_job(source_job_id="r-pd", title="Program Delivery Director")
    ranked = ranking.rank_candidates([(1, unrelated), (2, program_delivery)])
    assert [c.job_id for c in ranked] == [2]


def test_ranking_ties_resolve_by_job_id_ascending():
    a = make_job(source_job_id="r-a", title="Program Delivery Director")
    b = make_job(source_job_id="r-b", title="Program Delivery Director")
    ranked = ranking.rank_candidates([(2, b), (1, a)])
    assert [c.job_id for c in ranked] == [1, 2]


# ---------------------------------------------------------------------------
# 4. Hard scoring budget
# ---------------------------------------------------------------------------


def _build_eligible_jobs(count):
    jobs = []
    for index in range(count):
        jobs.append(
            make_job(
                title=f"Program Delivery Director {index}",
                source_job_id=f"budget-{index}",
                source_url=f"https://weworkremotely.com/remote-jobs/budget-{index}",
            )
        )
    return jobs


def _build_eligible_jobs_with_prefix(count, prefix):
    jobs = []
    for index in range(count):
        jobs.append(
            make_job(
                title=f"Program Delivery Director {index}",
                source_job_id=f"{prefix}-{index}",
                source_url=f"https://weworkremotely.com/remote-jobs/{prefix}-{index}",
            )
        )
    return jobs


def _results_for(jobs, base):
    return {
        job.title: score_result(base - index) for index, job in enumerate(jobs)
    }


def _resolutions_for(jobs):
    return {
        job.title: ok_resolution(
            f"https://acme.com/careers/{job.source_job_id}"
        )
        for job in jobs
    }


def _all_succeed_results(count):
    return {
        f"Program Delivery Director {index}": score_result(90 - index)
        for index in range(count)
    }


def test_scoring_budget_caps_calls_at_configured_limit(app):
    jobs = _build_eligible_jobs(20)
    scorer = CountingScorer(_all_succeed_results(20))
    resolver = FakeResolver(
        {
            f"Program Delivery Director {index}": ok_resolution(
                f"https://acme.com/careers/pdd-{index}"
            )
            for index in range(20)
        }
    )
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=15)
    assert len(scorer.calls) == 15


def test_scoring_budget_deferred_rows_are_marked_and_visible(app):
    jobs = _build_eligible_jobs(20)
    scorer = CountingScorer(_all_succeed_results(20))
    resolver = FakeResolver(
        {
            f"Program Delivery Director {index}": ok_resolution(
                f"https://acme.com/careers/pdd-{index}"
            )
            for index in range(20)
        }
    )
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=15)
    with app.app_context():
        connection = db.get_db()
        run = db.get_pipeline_runs_for_date(connection, DAY_A)[0]
        rows = fetch_run_jobs(connection, run["id"])
    deferred = [r for r in rows if r["suppressed_scoring_budget"]]
    assert len(deferred) == 5
    assert all(r["scoring_attempted"] == 0 for r in deferred)


def test_scoring_budget_can_be_lowered_below_default(app):
    jobs = _build_eligible_jobs(6)
    scorer = CountingScorer(_all_succeed_results(6))
    resolver = FakeResolver(
        {
            f"Program Delivery Director {index}": ok_resolution(
                f"https://acme.com/careers/pdd-{index}"
            )
            for index in range(6)
        }
    )
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=2)
    assert len(scorer.calls) == 2


def test_scoring_errors_still_consume_budget(app):
    jobs = _build_eligible_jobs(5)
    scorer = CountingScorer(
        results={},
        failures={
            f"Program Delivery Director {index}": ScoringError("bad output")
            for index in range(5)
        },
    )
    resolver = FakeResolver({})
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=3)
    assert len(scorer.calls) == 3
    with app.app_context():
        connection = db.get_db()
        run = db.get_pipeline_runs_for_date(connection, DAY_A)[0]
        rows = fetch_run_jobs(connection, run["id"])
    budgeted_rows = [r for r in rows if r["scoring_attempted"] == 1]
    deferred_rows = [r for r in rows if r["suppressed_scoring_budget"] == 1]
    assert len(budgeted_rows) == 3
    assert all(r["scoring_succeeded"] == 0 for r in budgeted_rows)
    assert len(deferred_rows) == 2


# ---------------------------------------------------------------------------
# 5. Existing rejected jobs remain cheap
# ---------------------------------------------------------------------------


def test_filtered_jobs_never_reach_scoring(app):
    jobs = [
        make_job(title="Plumber", source_job_id="plumber-1"),
        make_job(title="Junior Designer", source_job_id="jd-1"),
    ]
    scorer = CountingScorer({}, enabled=False)
    resolver = FakeResolver({})
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert scorer.calls == []


def test_gate_rejected_jobs_never_reach_scoring(app):
    """Generic product role + clearly-unrelated role must never reach
    scoring. Uses neutral descriptions so the gate decision rests on
    the title alone, not on a description-side technical-scope fallback.
    """
    jobs = [
        make_neutral_description_job(
            title="Director of Marketing", source_job_id="mkt-1"
        ),
        make_neutral_description_job(
            title="Senior Product Manager", source_job_id="spm-1"
        ),
    ]
    scorer = CountingScorer({}, enabled=False)
    resolver = FakeResolver({})
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert scorer.calls == []


def test_already_processed_jobs_never_reach_scoring_on_later_run(app):
    job = make_job(source_job_id="multi-day-cheap")
    scorer = CountingScorer({"Senior Technical Program Manager": score_result(88)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/x")}
    )
    run_pipeline(app, FakeDiscover([job]), scorer, resolver, day=DAY_A)
    assert len(scorer.calls) == 1

    scorer.calls = []
    scorer.enabled = False
    run_pipeline(app, FakeDiscover([job]), scorer, resolver, day=DAY_B)
    assert scorer.calls == []


# ---------------------------------------------------------------------------
# 6. Cost-safety invariant (cost containment regression)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("budget", [1, 3, 6, 15])
def test_scoring_calls_never_exceed_budget_for_perfect_candidates(app, budget):
    jobs = _build_eligible_jobs(40)
    scorer = CountingScorer(_all_succeed_results(40))
    resolver = FakeResolver(
        {
            f"Program Delivery Director {index}": ok_resolution(
                f"https://acme.com/careers/pdd-{index}"
            )
            for index in range(40)
        }
    )
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=budget)
    assert len(scorer.calls) <= budget


def test_scoring_calls_never_exceed_budget_when_all_error(app):
    jobs = _build_eligible_jobs(40)
    scorer = CountingScorer(
        results={},
        failures={
            f"Program Delivery Director {index}": ScoringError("bad")
            for index in range(40)
        },
    )
    resolver = FakeResolver({})
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=10)
    assert len(scorer.calls) == 10


def test_zero_recommendations_keeps_calls_within_budget(app):
    jobs = _build_eligible_jobs(10)
    scorer = CountingScorer(
        {f"Program Delivery Director {index}": score_result(40) for index in range(10)}
    )
    resolver = FakeResolver({})
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=5)
    assert 0 < len(scorer.calls) <= 5


# ---------------------------------------------------------------------------
# 7. Telemetry truthfulness
# ---------------------------------------------------------------------------


def test_telemetry_records_new_columns_for_each_state(app):
    """Each pre-scoring state must persist a distinct, truthful row.

    The generic product role uses a neutral (empty) description so the
    gate decision rests on the title alone.
    """
    jobs = [
        make_job(title="Plumber", source_job_id="plumb"),
        make_job(title="Director, Technical Program Management", source_job_id="d-1"),
        make_neutral_description_job(
            title="Senior Product Manager", source_job_id="spm"
        ),
    ]
    scorer = CountingScorer(
        {"Director, Technical Program Management": score_result(85)}
    )
    resolver = FakeResolver(
        {"Director, Technical Program Management": ok_resolution("https://acme.com/careers/d")}
    )
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=5)
    with app.app_context():
        connection = db.get_db()
        run = db.get_pipeline_runs_for_date(connection, DAY_A)[0]
        rows = run_jobs_by_title(connection, run["id"])

    assert rows["Plumber"]["filter_passed"] == 0
    assert rows["Plumber"]["positive_gate_passed"] == 0
    assert rows["Plumber"]["positive_gate_reason"] is None
    assert rows["Plumber"]["suppressed_scoring_budget"] == 0

    assert rows["Director, Technical Program Management"]["filter_passed"] == 1
    assert rows["Director, Technical Program Management"]["positive_gate_passed"] == 1
    assert rows["Director, Technical Program Management"]["positive_gate_reason"] == "strong_target_title"
    assert rows["Director, Technical Program Management"]["preselection_score"] is not None

    assert rows["Senior Product Manager"]["filter_passed"] == 1
    assert rows["Senior Product Manager"]["positive_gate_passed"] == 0
    assert rows["Senior Product Manager"]["suppressed_scoring_budget"] == 0


def test_funnel_counts_reflect_new_stages(app):
    """Fixture composition is asserted independently from any helper:
      discovered:                6
      filter rejected:           1 (Plumber)
      filter passed:             5
      positive-gate passed:      4 (DTPM + 3x PDD)
      positive-gate rejected:    1 (Senior Product Manager, no context)
      scoring attempted:         2 (top-2 by ranking)
      budget deferred:           2 (remaining positive-gate survivors)
    """
    jobs = [
        make_job(title="Plumber", source_job_id="plumb"),
        make_neutral_description_job(
            title="Senior Product Manager", source_job_id="spm"
        ),
        make_job(title="Director, Technical Program Management", source_job_id="d-1"),
        make_job(title="Program Delivery Director", source_job_id="pdd-1"),
        make_job(title="Program Delivery Director", source_job_id="pdd-2"),
        make_job(title="Program Delivery Director", source_job_id="pdd-3"),
    ]
    scorer = CountingScorer(
        {
            "Director, Technical Program Management": score_result(88),
            "Program Delivery Director": score_result(85),
        }
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution("https://acme.com/careers/d"),
            "Program Delivery Director": ok_resolution("https://acme.com/careers/pdd"),
        }
    )
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=2)
    with app.app_context():
        connection = db.get_db()
        from remotescout.app import compute_funnel
        run = db.get_pipeline_runs_for_date(connection, DAY_A)[0]
        run_jobs = db.get_pipeline_run_jobs_with_details(connection, run["id"])
        funnel = compute_funnel(run_jobs)

    assert funnel["discovered"] == 6
    assert funnel["filter_rejected"] == 1
    assert funnel["filter_passed"] == 5
    assert funnel["positive_gate_passed"] == 4
    assert funnel["positive_gate_rejected"] == 1
    assert funnel["eligible_for_scoring"] == 4
    assert funnel["scoring_budget_deferred"] == 2
    assert funnel["scoring_attempted"] == 2
    assert funnel["scoring_succeeded"] == 2
    assert funnel["meets_threshold"] == 2


# ---------------------------------------------------------------------------
# 8. Run-detail outcome labels for new states
# ---------------------------------------------------------------------------


def test_outcome_labels_for_pre_scoring_states(app, client):
    from remotescout.app import derive_job_outcome

    rows_by_state = {
        "already_processed": {
            "suppressed_pre_score": 0,
            "suppressed_already_processed": 1,
            "suppressed_scoring_budget": 0,
            "positive_gate_passed": 0,
            "positive_gate_reason": None,
            "filter_passed": 1,
            "filter_reasons": None,
            "scoring_attempted": 0,
            "scoring_succeeded": 0,
            "score": None,
            "meets_threshold": 0,
            "resolution_attempted": 0,
            "resolution_succeeded": 0,
            "suppressed_post_resolution": 0,
            "suppressed_canonical_duplicate": 0,
            "accepted_rank": None,
            "scoring_reused": 0,
        },
        "budget_deferred": {
            "suppressed_pre_score": 0,
            "suppressed_already_processed": 0,
            "suppressed_scoring_budget": 1,
            "positive_gate_passed": 1,
            "positive_gate_reason": "strong_target_title",
            "filter_passed": 1,
            "filter_reasons": None,
            "scoring_attempted": 0,
            "scoring_succeeded": 0,
            "score": None,
            "meets_threshold": 0,
            "resolution_attempted": 0,
            "resolution_succeeded": 0,
            "suppressed_post_resolution": 0,
            "suppressed_canonical_duplicate": 0,
            "accepted_rank": None,
            "scoring_reused": 0,
        },
        "outside_target": {
            "suppressed_pre_score": 0,
            "suppressed_already_processed": 0,
            "suppressed_scoring_budget": 0,
            "positive_gate_passed": 0,
            "positive_gate_reason": "outside_target_role_families",
            "filter_passed": 1,
            "filter_reasons": None,
            "scoring_attempted": 0,
            "scoring_succeeded": 0,
            "score": None,
            "meets_threshold": 0,
            "resolution_attempted": 0,
            "resolution_succeeded": 0,
            "suppressed_post_resolution": 0,
            "suppressed_canonical_duplicate": 0,
            "accepted_rank": None,
            "scoring_reused": 0,
        },
        "incomplete_legacy": {
            "suppressed_pre_score": 0,
            "suppressed_already_processed": 0,
            "suppressed_scoring_budget": 0,
            "positive_gate_passed": 0,
            "positive_gate_reason": None,
            "filter_passed": 1,
            "filter_reasons": None,
            "scoring_attempted": 0,
            "scoring_succeeded": 0,
            "score": None,
            "meets_threshold": 0,
            "resolution_attempted": 0,
            "resolution_succeeded": 0,
            "suppressed_post_resolution": 0,
            "suppressed_canonical_duplicate": 0,
            "accepted_rank": None,
            "scoring_reused": 0,
        },
        "reused_prior_score": {
            "suppressed_pre_score": 0,
            "suppressed_already_processed": 0,
            "suppressed_scoring_budget": 0,
            "positive_gate_passed": 1,
            "positive_gate_reason": "strong_target_title",
            "filter_passed": 1,
            "filter_reasons": None,
            "scoring_attempted": 0,
            "scoring_succeeded": 0,
            "score": 82,
            "meets_threshold": 1,
            "resolution_attempted": 0,
            "resolution_succeeded": 0,
            "suppressed_post_resolution": 0,
            "suppressed_canonical_duplicate": 0,
            "accepted_rank": None,
            "scoring_reused": 1,
        },
    }
    expected = {
        "already_processed": ("Already processed", "outcome-suppressed"),
        "budget_deferred": ("Deferred — scoring budget", "outcome-deferred"),
        "outside_target": ("Outside target role families", "outcome-gate"),
        "incomplete_legacy": ("Filter passed (incomplete)", "outcome-incomplete"),
        "reused_prior_score": ("Reused prior score", "outcome-reused"),
    }
    for key, row in rows_by_state.items():
        assert derive_job_outcome(row) == expected[key]


# ---------------------------------------------------------------------------
# 9. Schema compatibility — fresh DB and in-place upgrade
# ---------------------------------------------------------------------------


def test_init_db_creates_package_8_columns_for_fresh_db(tmp_path):
    db_path = tmp_path / "fresh.db"
    db.init_db(str(db_path))
    connection = db.connect(str(db_path))
    try:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(pipeline_run_jobs)").fetchall()
        }
    finally:
        connection.close()
    assert {
        "suppressed_already_processed",
        "positive_gate_passed",
        "positive_gate_reason",
        "preselection_score",
        "suppressed_scoring_budget",
        "scoring_reused",
    } <= columns


def test_init_db_adds_columns_to_existing_pre_package_8_db(tmp_path):
    db_path = tmp_path / "legacy.db"
    legacy_schema = """
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        employer TEXT NOT NULL,
        description TEXT,
        location TEXT,
        compensation TEXT,
        source TEXT,
        source_url TEXT,
        source_job_id TEXT,
        employer_url TEXT,
        requisition_id TEXT,
        posted_at TEXT,
        identity_key TEXT,
        score REAL,
        fit_explanation TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE pipeline_run_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        job_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        filter_passed INTEGER NOT NULL,
        filter_reasons TEXT,
        suppressed_pre_score INTEGER NOT NULL DEFAULT 0,
        scoring_attempted INTEGER NOT NULL DEFAULT 0,
        scoring_succeeded INTEGER NOT NULL DEFAULT 0,
        score INTEGER,
        fit_explanation TEXT,
        strengths TEXT,
        gaps TEXT,
        meets_threshold INTEGER NOT NULL DEFAULT 0,
        resolution_attempted INTEGER NOT NULL DEFAULT 0,
        resolution_succeeded INTEGER NOT NULL DEFAULT 0,
        resolution_method TEXT,
        employer_url TEXT,
        requisition_id TEXT,
        suppressed_post_resolution INTEGER NOT NULL DEFAULT 0,
        suppressed_canonical_duplicate INTEGER NOT NULL DEFAULT 0,
        accepted_rank INTEGER,
        scoring_error_type TEXT,
        scoring_error_message TEXT,
        UNIQUE (run_id, job_id)
    );
    """
    connection = db.connect(str(db_path))
    try:
        connection.executescript(legacy_schema)
        connection.commit()
    finally:
        connection.close()

    db.init_db(str(db_path))

    connection = db.connect(str(db_path))
    try:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(pipeline_run_jobs)").fetchall()
        }
        legacy_row = connection.execute(
            "SELECT * FROM pipeline_run_jobs"
        ).fetchone()
    finally:
        connection.close()

    assert {
        "suppressed_already_processed",
        "positive_gate_passed",
        "positive_gate_reason",
        "preselection_score",
        "suppressed_scoring_budget",
        "scoring_reused",
    } <= columns
    assert legacy_row is None


def test_init_db_is_idempotent_when_columns_already_present(tmp_path):
    db_path = tmp_path / "twice.db"
    db.init_db(str(db_path))
    db.init_db(str(db_path))
    connection = db.connect(str(db_path))
    try:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(pipeline_run_jobs)").fetchall()
        }
    finally:
        connection.close()
    assert "suppressed_already_processed" in columns
    assert "scoring_reused" in columns


def test_init_db_adds_scoring_reused_to_existing_package_8_era_db(tmp_path):
    """A Package 8 database that pre-dates same-day reuse must gain
    ``scoring_reused`` on init without disturbing existing rows.
    """
    db_path = tmp_path / "package8_era.db"
    package8_era_schema = """
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        employer TEXT NOT NULL,
        description TEXT,
        location TEXT,
        compensation TEXT,
        source TEXT,
        source_url TEXT,
        source_job_id TEXT,
        employer_url TEXT,
        requisition_id TEXT,
        posted_at TEXT,
        identity_key TEXT,
        score REAL,
        fit_explanation TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE pipeline_run_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        job_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        filter_passed INTEGER NOT NULL,
        filter_reasons TEXT,
        suppressed_pre_score INTEGER NOT NULL DEFAULT 0,
        scoring_attempted INTEGER NOT NULL DEFAULT 0,
        scoring_succeeded INTEGER NOT NULL DEFAULT 0,
        score INTEGER,
        fit_explanation TEXT,
        strengths TEXT,
        gaps TEXT,
        meets_threshold INTEGER NOT NULL DEFAULT 0,
        resolution_attempted INTEGER NOT NULL DEFAULT 0,
        resolution_succeeded INTEGER NOT NULL DEFAULT 0,
        resolution_method TEXT,
        employer_url TEXT,
        requisition_id TEXT,
        suppressed_post_resolution INTEGER NOT NULL DEFAULT 0,
        suppressed_canonical_duplicate INTEGER NOT NULL DEFAULT 0,
        accepted_rank INTEGER,
        scoring_error_type TEXT,
        scoring_error_message TEXT,
        suppressed_already_processed INTEGER NOT NULL DEFAULT 0,
        positive_gate_passed INTEGER NOT NULL DEFAULT 0,
        positive_gate_reason TEXT,
        preselection_score INTEGER,
        suppressed_scoring_budget INTEGER NOT NULL DEFAULT 0,
        UNIQUE (run_id, job_id)
    );
    """
    connection = db.connect(str(db_path))
    try:
        connection.executescript(package8_era_schema)
        connection.commit()
    finally:
        connection.close()

    db.init_db(str(db_path))

    connection = db.connect(str(db_path))
    try:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(pipeline_run_jobs)").fetchall()
        }
        legacy_row = connection.execute(
            "SELECT * FROM pipeline_run_jobs"
        ).fetchone()
    finally:
        connection.close()

    assert "scoring_reused" in columns
    assert legacy_row is None


# ---------------------------------------------------------------------------
# 10. Independent verification fixture (30-job controlled scenario)
# ---------------------------------------------------------------------------


def test_independent_30_job_fixture_matches_expected_funnel(app):
    """Independently composed fixture: 30 discovered → 5 hard-rejected,
    8 already-processed, 7 fail gate, 10 eligible, budget=6.

    Expected (asserted independently of any helper function):
      score calls  = 6
      budget deferred = 4
    """
    hard_filtered = [
        ("Plumber", f"hf-{i}", "Pipe Co.")
        for i in range(5)
    ]
    already = [
        ("Director, Technical Program Management", f"ap-{i}", "Already Co.")
        for i in range(8)
    ]
    gate_failed = [
        ("Director of Marketing", f"gf-{i}", "Marketing Co.")
        for i in range(7)
    ]
    eligible = [
        ("Program Delivery Director", f"el-{i}", "Delivery Co.")
        for i in range(10)
    ]
    with app.app_context():
        connection = db.get_db()
        for _title, source_id, _employer in already:
            existing_id = db.upsert_job(
                connection,
                make_job(
                    title="Director, Technical Program Management",
                    employer="Already Co.",
                    source="weworkremotely",
                    source_job_id=source_id,
                    source_url=f"https://weworkremotely.com/remote-jobs/{source_id}",
                ),
            )
            scored_run_id = db.create_pipeline_run(connection, "2026-01-01", 70, "claude-sonnet-5")
            db.record_pipeline_run_job(
                connection,
                scored_run_id,
                existing_id,
                "weworkremotely",
                True,
                None,
            )
            db.record_pipeline_run_job_scoring_succeeded(
                connection,
                scored_run_id,
                existing_id,
                90,
                "Already scored in seed run.",
                ["Existing strength"],
                ["Existing gap"],
                True,
            )
        connection.commit()

    jobs = []
    for title, source_id, employer in hard_filtered + already + gate_failed + eligible:
        jobs.append(
            make_job(title=title, source_job_id=source_id, employer=employer)
        )

    scorer = CountingScorer(
        {
            "Program Delivery Director": score_result(85),
        }
    )
    resolver = FakeResolver(
        {"Program Delivery Director": ok_resolution("https://delivery.example/pdd")}
    )
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=6)

    assert len(scorer.calls) == 6

    with app.app_context():
        connection = db.get_db()
        run = db.get_pipeline_runs_for_date(connection, DAY_A)[0]
        run_jobs = db.get_pipeline_run_jobs_with_details(connection, run["id"])

    deferred = [r for r in run_jobs if r["suppressed_scoring_budget"]]
    assert len(deferred) == 4

    counts = {
        "discovered": len(run_jobs),
        "filter_rejected": sum(1 for r in run_jobs if not r["filter_passed"]),
        "already_processed": sum(1 for r in run_jobs if r["suppressed_already_processed"]),
        "positive_gate_rejected": sum(
            1
            for r in run_jobs
            if r["filter_passed"]
            and not r["suppressed_already_processed"]
            and not r["positive_gate_passed"]
        ),
        "scoring_attempted": sum(1 for r in run_jobs if r["scoring_attempted"]),
        "scoring_budget_deferred": sum(1 for r in run_jobs if r["suppressed_scoring_budget"]),
    }
    assert counts["discovered"] == 30
    assert counts["filter_rejected"] == 5
    assert counts["already_processed"] == 8
    assert counts["positive_gate_rejected"] == 7
    assert counts["scoring_attempted"] == 6
    assert counts["scoring_budget_deferred"] == 4


# ---------------------------------------------------------------------------
# 11. Adversarial: bypass already-processed suppression
# ---------------------------------------------------------------------------


def test_adversarial_bypass_already_processed_fails_day_two_regression(
    app, monkeypatch
):
    """Mutate the engine to skip already-processed suppression. The multi-day
    regression must fail because Day 2 would call score(A) again.
    """
    job_a = make_job(source_job_id="adv-multi-A")
    job_b = make_job(source_job_id="adv-multi-B", title="Program Delivery Director")
    scorer_a = CountingScorer(
        {
            "Senior Technical Program Manager": score_result(92),
            "Program Delivery Director": score_result(85),
        }
    )
    resolver = FakeResolver(
        {
            "Senior Technical Program Manager": ok_resolution("https://acme.com/careers/tpm"),
            "Program Delivery Director": ok_resolution("https://acme.com/careers/pdd"),
        }
    )
    run_pipeline(app, FakeDiscover([job_a, job_b]), scorer_a, resolver, day=DAY_A)
    assert len(scorer_a.calls) == 2
    day_a_call_count = len(scorer_a.calls)

    import remotescout.engine as engine_mod

    def mutated(connection, recommendation_date=None):
        return set()

    monkeypatch.setattr(engine_mod.db, "get_already_processed_job_ids", mutated)
    scorer_b = CountingScorer(
        {
            "Senior Technical Program Manager": score_result(92),
            "Program Delivery Director": score_result(85),
        }
    )
    second = run_pipeline(app, FakeDiscover([job_a, job_b]), scorer_b, resolver, day=DAY_B)
    assert len(second) >= 1
    assert len(scorer_b.calls) == 2

    monkeypatch.undo()
    scorer_c = CountingScorer(
        {
            "Senior Technical Program Manager": score_result(92),
            "Program Delivery Director": score_result(85),
        }
    )
    third = run_pipeline(app, FakeDiscover([job_a, job_b]), scorer_c, resolver, day="2026-08-13")
    assert third == []
    assert len(scorer_c.calls) == 0
    assert day_a_call_count == 2


def test_adversarial_bypass_budget_fails_regression(app, monkeypatch):
    jobs_a = _build_eligible_jobs_with_prefix(20, "budget-A")
    baseline_scorer = CountingScorer(_results_for(jobs_a, 92))
    resolver = FakeResolver(_resolutions_for(jobs_a))
    run_pipeline(
        app,
        FakeDiscover(jobs_a),
        baseline_scorer,
        resolver,
        day=DAY_A,
        scoring_budget=15,
    )
    assert len(baseline_scorer.calls) == 15

    import remotescout.engine as engine_mod

    def mutated_budget(ranked, budget):
        return ranked[: budget + 1]

    monkeypatch.setattr(engine_mod, "_budget_slice", mutated_budget)

    jobs_b = _build_eligible_jobs_with_prefix(20, "budget-B")
    mutated_scorer = CountingScorer(_results_for(jobs_b, 88))
    resolver_b = FakeResolver(_resolutions_for(jobs_b))
    run_pipeline(
        app,
        FakeDiscover(jobs_b),
        mutated_scorer,
        resolver_b,
        day=DAY_B,
        scoring_budget=15,
    )
    assert len(mutated_scorer.calls) == 16
    mutated_titles = [c[0] for c in mutated_scorer.calls]
    assert "Program Delivery Director 15" in mutated_titles

    monkeypatch.undo()

    jobs_c = _build_eligible_jobs_with_prefix(20, "budget-C")
    restored_scorer = CountingScorer(_results_for(jobs_c, 80))
    resolver_c = FakeResolver(_resolutions_for(jobs_c))
    run_pipeline(
        app,
        FakeDiscover(jobs_c),
        restored_scorer,
        resolver_c,
        day="2026-08-13",
        scoring_budget=15,
    )
    assert len(restored_scorer.calls) == 15


# ---------------------------------------------------------------------------
# 12. Configurable scoring budget default and configuration
# ---------------------------------------------------------------------------


def test_scoring_budget_default_is_15(monkeypatch):
    from remotescout.config import SCORING_BUDGET_DEFAULT, load_config

    monkeypatch.delenv("REMOTESCOUT_SCORING_BUDGET", raising=False)
    assert SCORING_BUDGET_DEFAULT == 15
    assert load_config()["SCORING_BUDGET"] == 15


def test_scoring_budget_override_via_env(monkeypatch):
    from remotescout.config import load_config

    monkeypatch.setenv("REMOTESCOUT_SCORING_BUDGET", "7")
    assert load_config()["SCORING_BUDGET"] == 7


# ---------------------------------------------------------------------------
# 13. Persistence layout — new columns persist after pipeline completion
# ---------------------------------------------------------------------------


def test_new_columns_persist_in_run_jobs_after_pipeline(app):
    job = make_job(source_job_id="persist-1")
    scorer = CountingScorer({"Senior Technical Program Manager": score_result(88)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/p")}
    )
    run_pipeline(app, FakeDiscover([job]), scorer, resolver, scoring_budget=15)
    with app.app_context():
        connection = db.get_db()
        run = db.get_pipeline_runs_for_date(connection, DAY_A)[0]
        row = fetch_run_jobs(connection, run["id"])[0]
    assert row["suppressed_already_processed"] == 0
    assert row["positive_gate_passed"] == 1
    assert row["positive_gate_reason"] == "strong_target_title"
    assert row["preselection_score"] is not None
    assert row["suppressed_scoring_budget"] == 0


def test_already_processed_telemetry_persists_via_get_already_processed_job_ids(app):
    job = make_job(source_job_id="persist-already-1")
    scorer = CountingScorer({"Senior Technical Program Manager": score_result(88)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/q")}
    )
    run_pipeline(app, FakeDiscover([job]), scorer, resolver, day=DAY_A)
    with app.app_context():
        connection = db.get_db()
        processed = db.get_already_processed_job_ids(connection)
        assert len(processed) >= 1
        assert job.source_job_id is not None
        for row in connection.execute(
            "SELECT id FROM jobs WHERE source_job_id = ?", (job.source_job_id,)
        ).fetchall():
            assert row["id"] in processed


# ---------------------------------------------------------------------------
# 14. Run-detail UI surfaces new outcomes truthfully
# ---------------------------------------------------------------------------


def test_run_detail_renders_outside_target_role_families_outcome(app, client):
    jobs = [
        make_job(title="Director of Marketing", source_job_id="ui-1"),
    ]
    scorer = CountingScorer({}, enabled=False)
    resolver = FakeResolver({})
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=15)
    with app.app_context():
        connection = db.get_db()
        run = db.get_pipeline_runs_for_date(connection, DAY_A)[0]
        run_id = run["id"]
    body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Outside target role families" in body


def test_run_detail_renders_already_processed_outcome_on_day_two(app, client):
    job = make_job(source_job_id="ui-multi-1")
    scorer = CountingScorer({"Senior Technical Program Manager": score_result(88)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/m")}
    )
    run_pipeline(app, FakeDiscover([job]), scorer, resolver, day=DAY_A)
    run_pipeline(app, FakeDiscover([job]), scorer, resolver, day=DAY_B)
    with app.app_context():
        connection = db.get_db()
        runs = db.get_pipeline_runs_for_date(connection, DAY_B)
        run_id = runs[0]["id"]
    body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Already processed" in body


def test_run_detail_renders_deferred_scoring_budget_outcome(app, client):
    jobs = _build_eligible_jobs(20)
    scorer = CountingScorer(_all_succeed_results(20))
    resolver = FakeResolver(
        {
            f"Program Delivery Director {index}": ok_resolution(
                f"https://acme.com/careers/d-{index}"
            )
            for index in range(20)
        }
    )
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=3)
    with app.app_context():
        connection = db.get_db()
        run = db.get_pipeline_runs_for_date(connection, DAY_A)[0]
        run_id = run["id"]
    body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Deferred — scoring budget" in body


def test_run_detail_funnel_surfaces_new_metrics(app, client):
    jobs = [
        make_job(title="Plumber", source_job_id="plumb-funnel"),
        make_job(title="Director, Technical Program Management", source_job_id="d-funnel"),
    ]
    scorer = CountingScorer(
        {"Director, Technical Program Management": score_result(90)}
    )
    resolver = FakeResolver(
        {"Director, Technical Program Management": ok_resolution("https://acme.com/careers/d")}
    )
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver, scoring_budget=5)
    with app.app_context():
        connection = db.get_db()
        run = db.get_pipeline_runs_for_date(connection, DAY_A)[0]
        run_id = run["id"]
    body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    for label in (
        "Filter rejected",
        "Already processed",
        "Positive-gate passed",
        "Positive-gate rejected",
        "Eligible for scoring",
        "Budget deferred",
    ):
        assert label in body, f"missing funnel metric: {label}"


# ---------------------------------------------------------------------------
# 15. Legacy jobs.score evidence (pre-Package-5 successful scores)
# ---------------------------------------------------------------------------


def test_legacy_jobs_score_suppresses_future_scoring(app):
    """A persisted job whose ``jobs.score`` was written by an earlier
    (pre-Package-5) successful scoring call must not be re-scored, even
    when ``pipeline_run_jobs`` has no record of the legacy run.
    """
    seeded_job = make_job(
        source_job_id="legacy-1",
        title="Director, Technical Program Management",
    )
    with app.app_context():
        connection = db.get_db()
        seeded_id = db.upsert_job(connection, seeded_job)
        db.set_job_score(
            connection,
            seeded_id,
            64,
            "Legacy pre-Package-5 successful score.",
        )
        connection.commit()

        seeded_processed = db.get_already_processed_job_ids(connection)
        assert seeded_id in seeded_processed

    discover = FakeDiscover(
        [
            make_job(
                source_job_id="legacy-1",
                title="Director, Technical Program Management",
                source_url="https://weworkremotely.com/remote-jobs/legacy-1",
            )
        ]
    )
    scorer = CountingScorer(
        results={},
        failures={},
        enabled=False,
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://acme.com/careers/legacy"
            )
        }
    )

    second = run_pipeline(
        app, discover, scorer, resolver, day=DAY_B
    )

    assert scorer.calls == []
    assert second == []

    with app.app_context():
        connection = db.get_db()
        run = db.get_pipeline_runs_for_date(connection, DAY_B)[0]
        rows_by_title = run_jobs_by_title(connection, run["id"])
    legacy_row = rows_by_title["Director, Technical Program Management"]
    assert legacy_row["filter_passed"] == 1
    assert legacy_row["suppressed_already_processed"] == 1
    assert legacy_row["scoring_attempted"] == 0
    assert legacy_row["scoring_succeeded"] == 0


def test_run_detail_marks_legacy_scored_job_as_already_processed(app, client):
    """The run-detail UI must render the legacy ``jobs.score`` evidence
    as ``Already processed`` rather than promoting the legacy value.
    """
    seeded_job = make_job(
        source_job_id="legacy-ui-1",
        title="Director, Technical Program Management",
    )
    with app.app_context():
        connection = db.get_db()
        seeded_id = db.upsert_job(connection, seeded_job)
        db.set_job_score(
            connection,
            seeded_id,
            64,
            "Legacy pre-Package-5 successful score.",
        )
        connection.commit()

    run_pipeline(
        app,
        FakeDiscover(
            [
                make_job(
                    source_job_id="legacy-ui-1",
                    title="Director, Technical Program Management",
                    source_url="https://weworkremotely.com/remote-jobs/legacy-ui-1",
                )
            ]
        ),
        CountingScorer(results={}, failures={}, enabled=False),
        FakeResolver(
            {
                "Director, Technical Program Management": ok_resolution(
                    "https://acme.com/careers/legacy-ui"
                )
            }
        ),
        day=DAY_B,
    )
    with app.app_context():
        connection = db.get_db()
        run = db.get_pipeline_runs_for_date(connection, DAY_B)[0]
        run_id = run["id"]
    body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Already processed" in body


def test_unscored_persisted_job_remains_eligible_for_scoring(app):
    """A persisted job with ``jobs.score IS NULL`` and no successful
    ``pipeline_run_jobs`` history must remain eligible for normal
    processing on a later run.
    """
    seeded_job = make_job(
        source_job_id="unscored-1",
        title="Director, Technical Program Management",
    )
    with app.app_context():
        connection = db.get_db()
        seeded_id = db.upsert_job(connection, seeded_job)
        assert connection.execute(
            "SELECT score FROM jobs WHERE id = ?", (seeded_id,)
        ).fetchone()["score"] is None
        seeded_processed = db.get_already_processed_job_ids(connection)
        assert seeded_id not in seeded_processed

    scorer = CountingScorer(
        {"Director, Technical Program Management": score_result(91)}
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://acme.com/careers/unscored"
            )
        }
    )
    recommendations = run_pipeline(
        app,
        FakeDiscover(
            [
                make_job(
                    source_job_id="unscored-1",
                    title="Director, Technical Program Management",
                    source_url="https://weworkremotely.com/remote-jobs/unscored-1",
                )
            ]
        ),
        scorer,
        resolver,
        day=DAY_A,
    )

    assert len(scorer.calls) == 1
    assert len(recommendations) == 1


def test_legacy_evidence_and_package5_evidence_are_combined_independently(app):
    """The processed set must be the union of both evidence sources
    when the two sources describe different jobs.
    """
    legacy_job = make_job(
        source_job_id="union-legacy",
        title="Program Delivery Director",
    )
    package5_job = make_job(
        source_job_id="union-pkg5",
        title="Director, Technical Program Management",
    )
    eligible_job = make_job(
        source_job_id="union-eligible",
        title="Senior Technical Program Manager",
    )

    with app.app_context():
        connection = db.get_db()
        legacy_id = db.upsert_job(connection, legacy_job)
        eligible_id = db.upsert_job(connection, eligible_job)
        db.set_job_score(
            connection,
            legacy_id,
            58,
            "Pre-Package-5 legacy score.",
        )
        connection.commit()

    package5_scorer = CountingScorer(
        {"Director, Technical Program Management": score_result(90)}
    )
    package5_resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://acme.com/careers/union-pkg5"
            )
        }
    )
    run_pipeline(
        app,
        FakeDiscover([package5_job]),
        package5_scorer,
        package5_resolver,
        day=DAY_A,
    )
    assert len(package5_scorer.calls) == 1

    with app.app_context():
        connection = db.get_db()
        processed = db.get_already_processed_job_ids(connection)
    assert legacy_id in processed
    assert eligible_id not in processed

    second_scorer = CountingScorer(
        {"Senior Technical Program Manager": score_result(88)},
    )
    second_resolver = FakeResolver(
        {
            "Senior Technical Program Manager": ok_resolution(
                "https://acme.com/careers/union-eligible"
            )
        }
    )
    run_pipeline(
        app,
        FakeDiscover(
            [
                make_job(
                    source_job_id="union-legacy",
                    title="Program Delivery Director",
                    source_url="https://weworkremotely.com/remote-jobs/union-legacy",
                ),
                make_job(
                    source_job_id="union-pkg5",
                    title="Director, Technical Program Management",
                    source_url="https://weworkremotely.com/remote-jobs/union-pkg5",
                ),
                make_job(
                    source_job_id="union-eligible",
                    title="Senior Technical Program Manager",
                    source_url="https://weworkremotely.com/remote-jobs/union-eligible",
                ),
            ]
        ),
        second_scorer,
        second_resolver,
        day=DAY_B,
    )
    assert [call[1] for call in second_scorer.calls] == ["union-eligible"]

    with app.app_context():
        connection = db.get_db()
        run = db.get_pipeline_runs_for_date(connection, DAY_B)[0]
        rows_by_title = run_jobs_by_title(connection, run["id"])
    assert rows_by_title["Program Delivery Director"]["suppressed_already_processed"] == 1
    assert rows_by_title["Director, Technical Program Management"]["suppressed_already_processed"] == 1
    assert rows_by_title["Senior Technical Program Manager"]["suppressed_already_processed"] == 0


def test_failed_scoring_attempt_remains_retryable_on_later_run(app):
    """A failed scoring attempt must NOT be treated as already-processed.

    ``scoring_attempted = 1`` with ``scoring_succeeded = 0`` and a
    still-NULL ``jobs.score`` column is the canonical "failed and not
    yet successfully scored" state and must be retried on a later run.
    """
    job = make_job(source_job_id="failed-retry-1")
    failing_scorer = CountingScorer(
        results={},
        failures={"Senior Technical Program Manager": ScoringError("malformed output")},
    )
    resolver = FakeResolver({})
    run_pipeline(app, FakeDiscover([job]), failing_scorer, resolver, day=DAY_A)
    assert len(failing_scorer.calls) == 1

    with app.app_context():
        connection = db.get_db()
        seeded_row = connection.execute(
            "SELECT id, score FROM jobs WHERE source_job_id = ?",
            ("failed-retry-1",),
        ).fetchone()
        assert seeded_row["score"] is None
        day_a_processed = db.get_already_processed_job_ids(connection)
        assert seeded_row["id"] not in day_a_processed

    retry_scorer = CountingScorer(
        {"Senior Technical Program Manager": score_result(82)}
    )
    retry_resolver = FakeResolver(
        {
            "Senior Technical Program Manager": ok_resolution(
                "https://acme.com/careers/failed-retry"
            )
        }
    )
    retry_recommendations = run_pipeline(
        app, FakeDiscover([job]), retry_scorer, retry_resolver, day=DAY_B
    )
    assert len(retry_scorer.calls) == 1
    assert len(retry_recommendations) == 1


def test_failed_scoring_does_not_promote_to_already_processed(app):
    """A failed scoring attempt must leave ``jobs.score`` NULL and the
    processed-set membership must remain absent until a successful
    outcome is recorded.
    """
    job = make_job(source_job_id="failed-no-promote-1")
    failing_scorer = CountingScorer(
        results={},
        failures={"Senior Technical Program Manager": ScoringError("malformed output")},
    )
    resolver = FakeResolver({})
    run_pipeline(app, FakeDiscover([job]), failing_scorer, resolver, day=DAY_A)
    assert len(failing_scorer.calls) == 1

    with app.app_context():
        connection = db.get_db()
        row = connection.execute(
            "SELECT id, score FROM jobs WHERE source_job_id = ?",
            ("failed-no-promote-1",),
        ).fetchone()
        assert row["score"] is None
        processed = db.get_already_processed_job_ids(connection)
        assert row["id"] not in processed

        run = db.get_pipeline_runs_for_date(connection, DAY_A)[0]
        jobs_in_run = db.get_pipeline_run_jobs(connection, run["id"])
    row_in_run = jobs_in_run[0]
    assert row_in_run["scoring_attempted"] == 1
    assert row_in_run["scoring_succeeded"] == 0
    assert row_in_run["suppressed_already_processed"] == 0


# ---------------------------------------------------------------------------
# 16. Same-day scoring reuse after partial-success prior attempt
# ---------------------------------------------------------------------------


def _fatal_after_scoring(app):
    """Patch db.set_resolution to fail after scoring commits.

    The engine commits the successful scoring row, then calls
    ``db.set_resolution`` after a successful ``resolve()``. Failing
    ``set_resolution`` leaves the pipeline in a fatal state with the
    successful scoring row durably persisted and the recommendation day
    incomplete.
    """
    from remotescout import db as db_mod

    original = db_mod.set_resolution

    def failing(conn, job_id, employer_url, requisition_id=None):
        raise RuntimeError("injected fatal failure after scoring commit")

    db_mod.set_resolution = failing
    try:
        yield
    finally:
        db_mod.set_resolution = original


def test_same_day_partial_success_retry_reuses_prior_score(app):
    """Run A: Job A is scored successfully then the pipeline dies
    fatally before day completion. Run B (same date) must reuse Job A's
    previously-paid score with zero Claude calls and may recommend it.
    """
    from remotescout import db as db_mod

    job = make_job(source_job_id="same-day-A")

    run_a_scorer = CountingScorer(
        {"Senior Technical Program Manager": score_result(82)}
    )
    run_a_resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/A")}
    )

    original_set_resolution = db_mod.set_resolution

    def failing_set_resolution(conn, job_id, employer_url, requisition_id=None):
        raise RuntimeError("injected fatal failure post-scoring")

    db_mod.set_resolution = failing_set_resolution
    try:
        with pytest.raises(RuntimeError):
            with app.app_context():
                connection = db_mod.get_db()
                build_daily_recommendations(
                    connection,
                    recommendation_date=DAY_A,
                    discover=FakeDiscover([job]),
                    score=run_a_scorer,
                    resolve=lambda j: run_a_resolver(j),
                    resume_text="resume text",
                    threshold=70,
                )
    finally:
        db_mod.set_resolution = original_set_resolution

    assert len(run_a_scorer.calls) == 1

    with app.app_context():
        connection = db_mod.get_db()
        runs_a = db_mod.get_pipeline_runs_for_date(connection, DAY_A)
        assert len(runs_a) == 1
        run_a_row = runs_a[0]
        assert run_a_row["status"] == "failed"
        assert not db_mod.is_recommendation_day_complete(connection, DAY_A)
        run_a_jobs = db_mod.get_pipeline_run_jobs(connection, run_a_row["id"])
        assert run_a_jobs[0]["scoring_succeeded"] == 1
        assert run_a_jobs[0]["meets_threshold"] == 1
        assert run_a_jobs[0]["score"] == 82

    retry_scorer = CountingScorer(
        {"Senior Technical Program Manager": score_result(99)},
        enabled=False,
    )
    retry_resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/A")}
    )
    second = run_pipeline(
        app,
        FakeDiscover([job]),
        retry_scorer,
        retry_resolver,
        day=DAY_A,
    )

    assert retry_scorer.calls == []
    assert len(second) == 1

    with app.app_context():
        connection = db_mod.get_db()
        all_runs = db_mod.get_pipeline_runs_for_date(connection, DAY_A)
        assert len(all_runs) == 2
        assert db_mod.is_recommendation_day_complete(connection, DAY_A)
        run_b_row = [r for r in all_runs if r["id"] != run_a_row["id"]][0]
        assert run_b_row["status"] == "succeeded"
        run_b_jobs = db_mod.get_pipeline_run_jobs(connection, run_b_row["id"])
    run_b_job = run_b_jobs[0]
    assert run_b_job["scoring_attempted"] == 0
    assert run_b_job["scoring_succeeded"] == 0
    assert run_b_job["scoring_reused"] == 1
    assert run_b_job["score"] == 82
    assert run_b_job["meets_threshold"] == 1
    assert run_b_job["suppressed_already_processed"] == 0
    assert run_b_job["accepted_rank"] == 1


def test_same_day_mixed_retry_reuses_one_scores_another(app):
    """A retry that rediscovers both a previously-scored job (Job A)
    and a never-scored job (Job B) must reuse A's score and score B
    normally. Total Claude calls on the retry must equal 1.
    """
    from remotescout import db as db_mod

    job_a = make_job(source_job_id="mixed-A", title="Director, Technical Program Management")
    job_b = make_job(source_job_id="mixed-B", title="Senior Technical Program Manager")

    run_a_scorer = CountingScorer(
        {"Director, Technical Program Management": score_result(82)}
    )
    run_a_resolver = FakeResolver(
        {"Director, Technical Program Management": ok_resolution("https://acme.com/careers/A")}
    )

    original_set_resolution = db_mod.set_resolution

    def failing_set_resolution(conn, job_id, employer_url, requisition_id=None):
        raise RuntimeError("injected fatal failure post-scoring")

    db_mod.set_resolution = failing_set_resolution
    try:
        with pytest.raises(RuntimeError):
            with app.app_context():
                connection = db_mod.get_db()
                build_daily_recommendations(
                    connection,
                    recommendation_date=DAY_A,
                    discover=FakeDiscover([job_a]),
                    score=run_a_scorer,
                    resolve=lambda j: run_a_resolver(j),
                    resume_text="resume text",
                    threshold=70,
                )
    finally:
        db_mod.set_resolution = original_set_resolution

    assert len(run_a_scorer.calls) == 1

    retry_scorer = CountingScorer(
        {
            "Senior Technical Program Manager": score_result(78),
        }
    )
    retry_resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://acme.com/careers/A"
            ),
            "Senior Technical Program Manager": ok_resolution(
                "https://acme.com/careers/B"
            ),
        }
    )
    recommendations = run_pipeline(
        app,
        FakeDiscover([job_a, job_b]),
        retry_scorer,
        retry_resolver,
        day=DAY_A,
    )

    assert [c[1] for c in retry_scorer.calls] == ["mixed-B"]
    assert len(recommendations) == 2

    with app.app_context():
        connection = db_mod.get_db()
        runs = db_mod.get_pipeline_runs_for_date(connection, DAY_A)
        run_b_row = [r for r in runs if r["status"] == "succeeded"][0]
        run_jobs = db_mod.get_pipeline_run_jobs_with_details(connection, run_b_row["id"])
    by_title = {row["title"]: row for row in run_jobs}
    assert by_title["Director, Technical Program Management"]["scoring_reused"] == 1
    assert by_title["Director, Technical Program Management"]["scoring_attempted"] == 0
    assert by_title["Senior Technical Program Manager"]["scoring_reused"] == 0
    assert by_title["Senior Technical Program Manager"]["scoring_attempted"] == 1
    assert by_title["Director, Technical Program Management"]["accepted_rank"] == 1
    assert by_title["Senior Technical Program Manager"]["accepted_rank"] == 2


def test_same_day_reuse_does_not_consume_scoring_budget(app):
    """A reused same-day score must not consume any of the 15-call
    budget. Only actual ``score()`` calls count.
    """
    from remotescout import db as db_mod

    reused_job = make_job(
        source_job_id="budget-reused", title="Director, Technical Program Management"
    )

    original_set_resolution = db_mod.set_resolution

    def failing_set_resolution(conn, job_id, employer_url, requisition_id=None):
        raise RuntimeError("injected fatal failure post-scoring")

    db_mod.set_resolution = failing_set_resolution
    try:
        with pytest.raises(RuntimeError):
            with app.app_context():
                connection = db_mod.get_db()
                build_daily_recommendations(
                    connection,
                    recommendation_date=DAY_A,
                    discover=FakeDiscover([reused_job]),
                    score=CountingScorer(
                        {"Director, Technical Program Management": score_result(90)}
                    ),
                    resolve=lambda j: FakeResolver(
                        {
                            "Director, Technical Program Management": ok_resolution(
                                "https://acme.com/careers/reused"
                            )
                        }
                    )(j),
                    resume_text="resume text",
                    threshold=70,
                )
    finally:
        db_mod.set_resolution = original_set_resolution

    fresh_jobs = _build_eligible_jobs_with_prefix(15, "fresh-budget")

    retry_scorer = CountingScorer(
        _results_for(fresh_jobs, 88),
    )
    fresh_resolutions = _resolutions_for(fresh_jobs)
    fresh_resolutions["Director, Technical Program Management"] = ok_resolution(
        "https://acme.com/careers/reused"
    )
    retry_resolver = FakeResolver(fresh_resolutions)

    with app.app_context():
        run_pipeline_callable = lambda: build_daily_recommendations(
            db_mod.get_db(),
            recommendation_date=DAY_A,
            discover=FakeDiscover([reused_job] + fresh_jobs),
            score=retry_scorer,
            resolve=lambda j: retry_resolver(j),
            resume_text="resume text",
            threshold=70,
            scoring_budget=15,
        )
        recommendations = run_pipeline_callable()

    assert len(retry_scorer.calls) == 15
    assert len(recommendations) == 3

    with app.app_context():
        connection = db_mod.get_db()
        run_row = db_mod.get_pipeline_runs_for_date(connection, DAY_A)
        succeeded = [r for r in run_row if r["status"] == "succeeded"]
        assert succeeded, "retry run did not succeed"
        run_jobs = db_mod.get_pipeline_run_jobs_with_details(
            connection, succeeded[0]["id"]
        )
    reused_row = next(r for r in run_jobs if r["scoring_reused"] == 1)
    assert reused_row["scoring_attempted"] == 0
    assert reused_row["suppressed_scoring_budget"] == 0


def test_same_day_reuse_skipped_when_reused_score_is_below_threshold(app):
    """A reused row whose persisted ``meets_threshold`` is 0 must NOT
    enter the resolution/recommendation pool.
    """
    from remotescout import db as db_mod

    below_job = make_job(
        source_job_id="reuse-below", title="Director, Technical Program Management"
    )

    with app.app_context():
        connection = db_mod.get_db()
        seeded_id = db_mod.upsert_job(connection, below_job)
        seeded_run = db_mod.create_pipeline_run(connection, DAY_A, 70, "claude-sonnet-5")
        db_mod.record_pipeline_run_job(
            connection, seeded_run, seeded_id, "weworkremotely", True, None
        )
        db_mod.record_pipeline_run_job_scoring_succeeded(
            connection,
            seeded_run,
            seeded_id,
            55,
            "Below threshold result.",
            ["s"],
            ["g"],
            False,
        )
        db_mod.finish_pipeline_run_failed(
            connection, seeded_run, "RuntimeError", "injected failure"
        )
        connection.commit()

    retry_scorer = CountingScorer(
        {},
        enabled=False,
    )
    retry_resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://acme.com/careers/below"
            )
        }
    )
    recommendations = run_pipeline(
        app,
        FakeDiscover([below_job]),
        retry_scorer,
        retry_resolver,
        day=DAY_A,
    )
    assert retry_scorer.calls == []
    assert recommendations == []

    with app.app_context():
        connection = db_mod.get_db()
        runs = db_mod.get_pipeline_runs_for_date(connection, DAY_A)
        run_row = [r for r in runs if r["status"] == "succeeded"][0]
        run_jobs = db_mod.get_pipeline_run_jobs(connection, run_row["id"])
    job_row = run_jobs[0]
    assert job_row["scoring_reused"] == 1
    assert job_row["scoring_attempted"] == 0
    assert job_row["score"] == 55
    assert job_row["meets_threshold"] == 0
    assert job_row["resolution_attempted"] == 0
    assert job_row["accepted_rank"] is None


def test_same_day_reuse_returns_latest_successful_when_multiple_exist(app):
    """When a job has more than one prior successful same-day scoring
    row, the latest one (by ``pipeline_run_jobs.id``) wins.
    """
    from remotescout import db as db_mod

    job = make_job(source_job_id="reuse-latest", title="Director, Technical Program Management")

    with app.app_context():
        connection = db_mod.get_db()
        seeded_id = db_mod.upsert_job(connection, job)
        for score_value, explanation, run_idx in [
            (60, "earlier run score", 1),
            (88, "latest run score", 2),
        ]:
            seeded_run = db_mod.create_pipeline_run(
                connection, DAY_A, 70, "claude-sonnet-5"
            )
            db_mod.record_pipeline_run_job(
                connection, seeded_run, seeded_id, "weworkremotely", True, None
            )
            db_mod.record_pipeline_run_job_scoring_succeeded(
                connection,
                seeded_run,
                seeded_id,
                score_value,
                explanation,
                ["s"],
                ["g"],
                True,
            )
            db_mod.finish_pipeline_run_failed(
                connection, seeded_run, "RuntimeError", "fail run %d" % run_idx
            )
        connection.commit()

    retry_scorer = CountingScorer({}, enabled=False)
    retry_resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://acme.com/careers/latest"
            )
        }
    )
    run_pipeline(
        app,
        FakeDiscover([job]),
        retry_scorer,
        retry_resolver,
        day=DAY_A,
    )

    with app.app_context():
        connection = db_mod.get_db()
        runs = db_mod.get_pipeline_runs_for_date(connection, DAY_A)
        succeeded = [r for r in runs if r["status"] == "succeeded"][0]
        run_jobs = db_mod.get_pipeline_run_jobs(connection, succeeded["id"])
    job_row = run_jobs[0]
    assert job_row["scoring_reused"] == 1
    assert job_row["score"] == 88
    assert job_row["fit_explanation"] == "latest run score"


def test_earlier_day_successful_score_remains_already_processed(app):
    """A job successfully scored on Day A must still be treated as
    ``Already processed`` on Day B and must not be reused.
    """
    job = make_job(source_job_id="earlier-day-A")
    scorer = CountingScorer(
        {"Senior Technical Program Manager": score_result(88)}
    )
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/ealier-day-A")}
    )
    run_pipeline(app, FakeDiscover([job]), scorer, resolver, day=DAY_A)
    assert len(scorer.calls) == 1

    day_b_scorer = CountingScorer(
        {"Senior Technical Program Manager": score_result(99)},
        enabled=False,
    )
    day_b_resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/ealier-day-A")}
    )
    day_b_recs = run_pipeline(
        app,
        FakeDiscover([job]),
        day_b_scorer,
        day_b_resolver,
        day=DAY_B,
    )
    assert day_b_scorer.calls == []
    assert day_b_recs == []

    with app.app_context():
        connection = db.get_db()
        runs = db.get_pipeline_runs_for_date(connection, DAY_B)
        run_row = runs[0]
        run_jobs = db.get_pipeline_run_jobs(connection, run_row["id"])
    job_row = run_jobs[0]
    assert job_row["suppressed_already_processed"] == 1
    assert job_row["scoring_reused"] == 0


def test_reused_run_funnel_counts_distinguish_reuse_from_attempt(app):
    """The run_detail funnel must surface a distinct ``Scoring reused``
    counter separate from ``Scoring attempted`` so the operator can
    read cost truthfully.
    """
    from remotescout import db as db_mod

    job = make_job(source_job_id="funnel-reused", title="Director, Technical Program Management")

    original_set_resolution = db_mod.set_resolution

    def failing_set_resolution(conn, job_id, employer_url, requisition_id=None):
        raise RuntimeError("injected fatal failure post-scoring")

    db_mod.set_resolution = failing_set_resolution
    try:
        with pytest.raises(RuntimeError):
            with app.app_context():
                connection = db_mod.get_db()
                build_daily_recommendations(
                    connection,
                    recommendation_date=DAY_A,
                    discover=FakeDiscover([job]),
                    score=CountingScorer(
                        {"Director, Technical Program Management": score_result(90)}
                    ),
                    resolve=lambda j: FakeResolver(
                        {
                            "Director, Technical Program Management": ok_resolution(
                                "https://acme.com/careers/funnel-reused"
                            )
                        }
                    )(j),
                    resume_text="resume text",
                    threshold=70,
                )
    finally:
        db_mod.set_resolution = original_set_resolution

    retry_scorer = CountingScorer({}, enabled=False)
    retry_resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://acme.com/careers/funnel-reused"
            )
        }
    )
    run_pipeline(
        app,
        FakeDiscover([job]),
        retry_scorer,
        retry_resolver,
        day=DAY_A,
    )

    with app.app_context():
        connection = db_mod.get_db()
        runs = db_mod.get_pipeline_runs_for_date(connection, DAY_A)
        succeeded = [r for r in runs if r["status"] == "succeeded"][0]
        run_jobs = db_mod.get_pipeline_run_jobs_with_details(
            connection, succeeded["id"]
        )
        from remotescout.app import compute_funnel

        funnel = compute_funnel(run_jobs)

    assert funnel["scoring_reused"] == 1
    assert funnel["scoring_attempted"] == 0
    assert funnel["scoring_succeeded"] == 0


def test_reused_run_detail_renders_reused_outcome_badge(app, client):
    """The run-detail page must render a reused-below-threshold job with
    the ``Reused prior score`` badge, and surface the
    ``Scoring reused`` funnel metric, so the operator can distinguish
    reuse from a paid scoring call.
    """
    from remotescout import db as db_mod

    job = make_job(source_job_id="ui-reused", title="Director, Technical Program Management")

    with app.app_context():
        connection = db_mod.get_db()
        seeded_id = db_mod.upsert_job(connection, job)
        seeded_run = db_mod.create_pipeline_run(connection, DAY_A, 70, "claude-sonnet-5")
        db_mod.record_pipeline_run_job(
            connection, seeded_run, seeded_id, "weworkremotely", True, None
        )
        db_mod.record_pipeline_run_job_scoring_succeeded(
            connection,
            seeded_run,
            seeded_id,
            55,
            "Below threshold reused evidence.",
            ["s"],
            ["g"],
            False,
        )
        db_mod.finish_pipeline_run_failed(
            connection, seeded_run, "RuntimeError", "fail"
        )
        connection.commit()

    retry_scorer = CountingScorer({}, enabled=False)
    retry_resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://acme.com/careers/ui-reused"
            )
        }
    )
    run_pipeline(
        app,
        FakeDiscover([job]),
        retry_scorer,
        retry_resolver,
        day=DAY_A,
    )

    with app.app_context():
        connection = db_mod.get_db()
        runs = db_mod.get_pipeline_runs_for_date(connection, DAY_A)
        succeeded = [r for r in runs if r["status"] == "succeeded"][0]
        run_id = succeeded["id"]
    body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Reused prior score" in body
    assert "Scoring reused" in body


def test_reused_job_that_becomes_recommended_renders_terminal_outcome(app, client):
    """When a reused same-day job's reused score passes threshold and
    resolution succeeds, it must surface as ``Recommended #1`` — the
    downstream terminal state correctly dominates the intermediate
    reuse label.
    """
    from remotescout import db as db_mod

    job = make_job(source_job_id="ui-recommended", title="Director, Technical Program Management")

    original_set_resolution = db_mod.set_resolution

    def failing_set_resolution(conn, job_id, employer_url, requisition_id=None):
        raise RuntimeError("injected fatal failure post-scoring")

    db_mod.set_resolution = failing_set_resolution
    try:
        with pytest.raises(RuntimeError):
            with app.app_context():
                connection = db_mod.get_db()
                build_daily_recommendations(
                    connection,
                    recommendation_date=DAY_A,
                    discover=FakeDiscover([job]),
                    score=CountingScorer(
                        {"Director, Technical Program Management": score_result(82)}
                    ),
                    resolve=lambda j: FakeResolver(
                        {
                            "Director, Technical Program Management": ok_resolution(
                                "https://acme.com/careers/ui-recommended"
                            )
                        }
                    )(j),
                    resume_text="resume text",
                    threshold=70,
                )
    finally:
        db_mod.set_resolution = original_set_resolution

    retry_scorer = CountingScorer({}, enabled=False)
    retry_resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://acme.com/careers/ui-recommended"
            )
        }
    )
    run_pipeline(
        app,
        FakeDiscover([job]),
        retry_scorer,
        retry_resolver,
        day=DAY_A,
    )

    with app.app_context():
        connection = db_mod.get_db()
        runs = db_mod.get_pipeline_runs_for_date(connection, DAY_A)
        succeeded = [r for r in runs if r["status"] == "succeeded"][0]
        run_id = succeeded["id"]
    body = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Recommended #1" in body
    assert "Scoring reused" in body


def test_adversarial_disable_same_day_reuse_partial_success_fails(app, monkeypatch):
    """Temporarily disable same-day reuse to prove the partial-success
    retry regression fails specifically because either Claude is called
    again or the qualifying job is lost.
    """
    from remotescout import db as db_mod
    import remotescout.engine as engine_mod

    job = make_job(source_job_id="adv-reuse-disable", title="Director, Technical Program Management")

    original_set_resolution = db_mod.set_resolution

    def failing_set_resolution(conn, job_id, employer_url, requisition_id=None):
        raise RuntimeError("injected fatal failure post-scoring")

    db_mod.set_resolution = failing_set_resolution
    try:
        with pytest.raises(RuntimeError):
            with app.app_context():
                connection = db_mod.get_db()
                build_daily_recommendations(
                    connection,
                    recommendation_date=DAY_A,
                    discover=FakeDiscover([job]),
                    score=CountingScorer(
                        {"Director, Technical Program Management": score_result(82)}
                    ),
                    resolve=lambda j: FakeResolver(
                        {
                            "Director, Technical Program Management": ok_resolution(
                                "https://acme.com/careers/adv"
                            )
                        }
                    )(j),
                    resume_text="resume text",
                    threshold=70,
                )
    finally:
        db_mod.set_resolution = original_set_resolution

    monkeypatch.setattr(
        engine_mod.db,
        "get_same_day_reused_results",
        lambda connection, recommendation_date, current_run_id: {},
    )

    adversarial_scorer = CountingScorer(
        {"Director, Technical Program Management": score_result(99)}
    )
    adversarial_resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://acme.com/careers/adv"
            )
        }
    )
    recommendations = run_pipeline(
        app,
        FakeDiscover([job]),
        adversarial_scorer,
        adversarial_resolver,
        day=DAY_A,
    )

    assert len(adversarial_scorer.calls) >= 1
    assert len(recommendations) >= 1

    with app.app_context():
        connection = db_mod.get_db()
        runs = db_mod.get_pipeline_runs_for_date(connection, DAY_A)
        succeeded = [r for r in runs if r["status"] == "succeeded"][0]
        run_jobs = db_mod.get_pipeline_run_jobs(connection, succeeded["id"])
    assert all(r["scoring_reused"] == 0 for r in run_jobs)

    monkeypatch.undo()

    restored_scorer = CountingScorer({}, enabled=False)
    restored_resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://acme.com/careers/adv"
            )
        }
    )
    final_recs = run_pipeline(
        app,
        FakeDiscover([job]),
        restored_scorer,
        restored_resolver,
        day=DAY_B,
    )
    assert final_recs == []
    assert restored_scorer.calls == []

    with app.app_context():
        connection = db_mod.get_db()
        runs = db_mod.get_pipeline_runs_for_date(connection, DAY_B)
        day_b_run = runs[0]
        day_b_jobs = db_mod.get_pipeline_run_jobs(connection, day_b_run["id"])
    assert day_b_jobs[0]["scoring_reused"] == 0
    assert day_b_jobs[0]["suppressed_already_processed"] == 1