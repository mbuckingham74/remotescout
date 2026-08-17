"""Behavioral regression coverage for Package 5 persistent pipeline observability.

Every test exercises the durable evidence recorded by the pipeline so future
UI can answer funnel questions without log or schema archaeology.
"""
import json

import pytest

from remotescout import db
from remotescout.app import create_app
from remotescout.discovery import DiscoveredJob
from remotescout.engine import build_daily_recommendations
from remotescout.resolution import ResolutionResult
from remotescout.scoring import MissingApiKeyError, ScoreResult, ScoringError

DAY = "2026-08-17"
TEST_SOURCE = "test-observability"


def make_job(title, source_job_id, **overrides):
    fields = {
        "source": TEST_SOURCE,
        "source_url": f"https://weworkremotely.com/remote-jobs/{source_job_id}",
        "source_job_id": source_job_id,
        "title": title,
        "employer": "Example Co.",
        "location": "Anywhere in the World",
        "description": "Headquarters: https://example.com/careers",
    }
    fields.update(overrides)
    return DiscoveredJob(**fields)


def score_result(score, explanation="Strong delivery match."):
    return ScoreResult(
        score=score,
        fit_explanation=explanation,
        strengths=["Direct delivery leadership", "Program governance"],
        gaps=["No fintech domain experience"],
    )


def ok_resolution(url, requisition_id=None, method="greenhouse"):
    return ResolutionResult(
        resolved=True,
        employer_url=url,
        requisition_id=requisition_id,
        method=method,
    )


def failed_resolution():
    return ResolutionResult(resolved=False)


class FakeDiscover:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return list(self.jobs)


class FakeScorer:
    def __init__(self, results=None, failures=None):
        self.results = dict(results or {})
        self.failures = dict(failures or {})
        self.calls = []

    def __call__(self, job, resume_text):
        self.calls.append(job.title)
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


def run_pipeline(app, discover, scorer, resolver, threshold=70, day=DAY, source_id=TEST_SOURCE):
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
            source_id=source_id,
        )


def fetch_runs(connection, day):
    return db.get_pipeline_runs_for_date(connection, day)


def fetch_source_attempts(connection, run_id):
    return db.get_pipeline_source_attempts(connection, run_id)


def fetch_run_jobs(connection, run_id):
    return db.get_pipeline_run_jobs(connection, run_id)


def load_json_list(value):
    if value is None:
        return None
    return json.loads(value)


class TestSuccessfulZeroRecommendationRun:
    def test_persists_single_succeeded_run(self, app):
        jobs = [
            make_job("Plumber", "plumber-1"),
            make_job("Junior Designer", "jd-1"),
        ]
        recommendations = run_pipeline(app, FakeDiscover(jobs), FakeScorer({}), FakeResolver({}))
        assert recommendations == []

        with app.app_context():
            connection = db.get_db()
            runs = fetch_runs(connection, DAY)
        assert len(runs) == 1
        run = runs[0]
        assert run["recommendation_date"] == DAY
        assert run["status"] == "succeeded"
        assert run["started_at"]
        assert run["finished_at"]
        assert run["recommendation_threshold"] == 70
        assert run["scoring_model"]
        assert run["error_type"] is None
        assert run["error_message"] is None

    def test_persists_source_attempt_succeeded_with_discovered_count(self, app):
        jobs = [
            make_job("Plumber", "plumber-1"),
            make_job("Junior Designer", "jd-1"),
            make_job("Junior Cook", "jc-1"),
        ]
        run_pipeline(app, FakeDiscover(jobs), FakeScorer({}), FakeResolver({}))

        with app.app_context():
            connection = db.get_db()
            run = fetch_runs(connection, DAY)[0]
            attempts = fetch_source_attempts(connection, run["id"])
            assert len(attempts) == 1
            attempt = attempts[0]
            assert attempt["source"] == TEST_SOURCE
            assert attempt["status"] == "succeeded"
            assert attempt["started_at"]
            assert attempt["finished_at"]
            assert attempt["discovered_count"] == 3
            assert attempt["error_type"] is None

    def test_persists_run_job_evidence_for_each_discovered_job(self, app):
        jobs = [
            make_job("Plumber", "plumber-1"),
            make_job("Junior Designer", "jd-1"),
        ]
        run_pipeline(app, FakeDiscover(jobs), FakeScorer({}), FakeResolver({}))

        with app.app_context():
            connection = db.get_db()
            run = fetch_runs(connection, DAY)[0]
            run_jobs = fetch_run_jobs(connection, run["id"])
            titles_by_id = {
                row["job_id"]: connection.execute(
                    "SELECT title FROM jobs WHERE id = ?", (row["job_id"],)
                ).fetchone()["title"]
                for row in run_jobs
            }
        assert len(run_jobs) == 2
        assert set(titles_by_id.values()) == {"Plumber", "Junior Designer"}

    def test_zero_recommendation_run_marks_day_complete(self, app):
        jobs = [
            make_job("Plumber", "plumber-1"),
            make_job("Junior Designer", "jd-1"),
        ]
        run_pipeline(app, FakeDiscover(jobs), FakeScorer({}), FakeResolver({}))
        with app.app_context():
            connection = db.get_db()
            assert db.is_recommendation_day_complete(connection, DAY)
            assert db.get_recommendations(connection, DAY) == []


class TestRepresentativeFunnel:
    """A 9-job fixture that exercises every pipeline stage independently.

    Independently derived expected counts (NOT computed by any production
    summarization function):
      - discovered:               9
      - filter rejected:          2 (Plumber, Junior Designer)
      - filter passed:            7
      - pre-score applied:        1 (Senior Technical Program Manager Applied)
      - scoring attempted:        6
      - scoring succeeded:        5 (Below, Unresolved, PostApplied, Dup, Accepted)
      - scoring errors:           1 (Error)
      - meets threshold:          5
      - resolution attempted:     5
      - resolution succeeded:     3 (PostApplied, Dup, Accepted)
      - unresolved:               1 (Unresolved)
      - post-resolution applied:  1 (PostApplied matches seeded applied)
      - canonical duplicates:     1 (Dup matches Accepted's URL)
      - accepted recommendations: 1 (Accepted, rank 1)
    """

    @pytest.fixture
    def seeded_applied(self, app):
        with app.app_context():
            connection = db.get_db()
            job_id = db.create_job(
                connection,
                title="Senior Technical Program Manager Applied",
                employer="Example Co.",
                location="Anywhere in the World",
                source="otherboard",
                source_url="https://other.example/jobs/seeded-applied",
                source_job_id="seeded-applied",
                identity_key="example co. | senior technical program manager applied | anywhere in the world",
                employer_url="https://applied.example/jobs/A",
                requisition_id="A",
            )
            db.create_application(connection, job_id, applied_at="2026-08-01")
            connection.commit()

    def _fixture(self):
        return [
            make_job("Plumber", "plumber-1"),
            make_job("Junior Designer", "jd-1"),
            make_job("Senior Technical Program Manager Applied", "stpm-applied"),
            make_job("Senior Technical Program Manager Below", "stpm-below"),
            make_job("Senior Technical Program Manager Error", "stpm-error"),
            make_job("Senior Technical Program Manager Unresolved", "stpm-unresolved"),
            make_job("Senior Technical Program Manager PostApplied", "stpm-postapplied"),
            make_job("Senior Technical Program Manager Dup", "stpm-dup"),
            make_job("Senior Technical Program Manager Accepted", "stpm-accepted"),
        ]

    def _scorer(self):
        return FakeScorer(
            results={
                "Senior Technical Program Manager Below": score_result(55, "Below threshold result."),
                "Senior Technical Program Manager PostApplied": score_result(80, "Post-applied result."),
                "Senior Technical Program Manager Dup": score_result(85, "Duplicate candidate."),
                "Senior Technical Program Manager Unresolved": score_result(75, "Unresolved candidate."),
                "Senior Technical Program Manager Accepted": score_result(90, "Accepted candidate."),
            },
            failures={
                "Senior Technical Program Manager Error": ScoringError("malformed model output"),
            },
        )

    def _resolver(self):
        return FakeResolver(
            results={
                "Senior Technical Program Manager Unresolved": failed_resolution(),
                "Senior Technical Program Manager PostApplied": ok_resolution(
                    "https://applied.example/jobs/A", requisition_id="A"
                ),
                "Senior Technical Program Manager Dup": ok_resolution(
                    "https://unique.example/jobs/B", requisition_id="B"
                ),
                "Senior Technical Program Manager Accepted": ok_resolution(
                    "https://unique.example/jobs/B", requisition_id="B"
                ),
            },
        )

    def test_funnel_counts_match_independent_expectations(self, app, seeded_applied):
        recommendations = run_pipeline(
            app, FakeDiscover(self._fixture()), self._scorer(), self._resolver()
        )
        assert len(recommendations) == 1

        with app.app_context():
            connection = db.get_db()
            run = fetch_runs(connection, DAY)[0]
            run_jobs = fetch_run_jobs(connection, run["id"])
            attempts = fetch_source_attempts(connection, run["id"])
            rows_by_title = {
                connection.execute(
                    "SELECT title FROM jobs WHERE id = ?", (row["job_id"],)
                ).fetchone()["title"]: row
                for row in run_jobs
            }

        assert run["status"] == "succeeded"
        assert attempts[0]["status"] == "succeeded"
        assert attempts[0]["discovered_count"] == 9

        assert len(run_jobs) == 9

        assert rows_by_title["Plumber"]["filter_passed"] == 0
        assert load_json_list(rows_by_title["Plumber"]["filter_reasons"]) == [
            "unrelated_occupation"
        ]

        assert rows_by_title["Junior Designer"]["filter_passed"] == 0
        assert "seniority_too_low" in load_json_list(
            rows_by_title["Junior Designer"]["filter_reasons"]
        )
        assert "wrong_job_family" in load_json_list(
            rows_by_title["Junior Designer"]["filter_reasons"]
        )

        assert rows_by_title["Senior Technical Program Manager Applied"]["filter_passed"] == 1
        assert rows_by_title["Senior Technical Program Manager Applied"]["suppressed_pre_score"] == 1
        assert rows_by_title["Senior Technical Program Manager Applied"]["scoring_attempted"] == 0

        assert rows_by_title["Senior Technical Program Manager Below"]["filter_passed"] == 1
        assert rows_by_title["Senior Technical Program Manager Below"]["scoring_attempted"] == 1
        assert rows_by_title["Senior Technical Program Manager Below"]["scoring_succeeded"] == 1
        assert rows_by_title["Senior Technical Program Manager Below"]["score"] == 55
        assert rows_by_title["Senior Technical Program Manager Below"]["meets_threshold"] == 0
        assert rows_by_title["Senior Technical Program Manager Below"]["resolution_attempted"] == 0

        assert rows_by_title["Senior Technical Program Manager Error"]["scoring_attempted"] == 1
        assert rows_by_title["Senior Technical Program Manager Error"]["scoring_succeeded"] == 0
        assert rows_by_title["Senior Technical Program Manager Error"]["scoring_error_type"] == "ScoringError"
        assert "malformed" in rows_by_title["Senior Technical Program Manager Error"]["scoring_error_message"]

        assert rows_by_title["Senior Technical Program Manager Unresolved"]["resolution_attempted"] == 1
        assert rows_by_title["Senior Technical Program Manager Unresolved"]["resolution_succeeded"] == 0

        assert rows_by_title["Senior Technical Program Manager PostApplied"]["resolution_attempted"] == 1
        assert rows_by_title["Senior Technical Program Manager PostApplied"]["resolution_succeeded"] == 1
        assert rows_by_title["Senior Technical Program Manager PostApplied"]["suppressed_post_resolution"] == 1
        assert rows_by_title["Senior Technical Program Manager PostApplied"]["accepted_rank"] is None

        assert rows_by_title["Senior Technical Program Manager Dup"]["resolution_attempted"] == 1
        assert rows_by_title["Senior Technical Program Manager Dup"]["resolution_succeeded"] == 1
        assert rows_by_title["Senior Technical Program Manager Dup"]["suppressed_canonical_duplicate"] == 1
        assert rows_by_title["Senior Technical Program Manager Dup"]["accepted_rank"] is None

        accepted_row = rows_by_title["Senior Technical Program Manager Accepted"]
        assert accepted_row["resolution_attempted"] == 1
        assert accepted_row["resolution_succeeded"] == 1
        assert accepted_row["accepted_rank"] == 1

        assert rows_by_title["Plumber"]["filter_passed"] == 0
        assert load_json_list(rows_by_title["Plumber"]["filter_reasons"]) == [
            "unrelated_occupation"
        ]

        assert rows_by_title["Junior Designer"]["filter_passed"] == 0
        assert "seniority_too_low" in load_json_list(
            rows_by_title["Junior Designer"]["filter_reasons"]
        )
        assert "wrong_job_family" in load_json_list(
            rows_by_title["Junior Designer"]["filter_reasons"]
        )

        assert rows_by_title["Senior Technical Program Manager Applied"]["filter_passed"] == 1
        assert rows_by_title["Senior Technical Program Manager Applied"]["suppressed_pre_score"] == 1
        assert rows_by_title["Senior Technical Program Manager Applied"]["scoring_attempted"] == 0

        assert rows_by_title["Senior Technical Program Manager Below"]["filter_passed"] == 1
        assert rows_by_title["Senior Technical Program Manager Below"]["scoring_attempted"] == 1
        assert rows_by_title["Senior Technical Program Manager Below"]["scoring_succeeded"] == 1
        assert rows_by_title["Senior Technical Program Manager Below"]["score"] == 55
        assert rows_by_title["Senior Technical Program Manager Below"]["meets_threshold"] == 0
        assert rows_by_title["Senior Technical Program Manager Below"]["resolution_attempted"] == 0

        assert rows_by_title["Senior Technical Program Manager Error"]["scoring_attempted"] == 1
        assert rows_by_title["Senior Technical Program Manager Error"]["scoring_succeeded"] == 0
        assert rows_by_title["Senior Technical Program Manager Error"]["scoring_error_type"] == "ScoringError"
        assert "malformed" in rows_by_title["Senior Technical Program Manager Error"]["scoring_error_message"]

        assert rows_by_title["Senior Technical Program Manager Unresolved"]["resolution_attempted"] == 1
        assert rows_by_title["Senior Technical Program Manager Unresolved"]["resolution_succeeded"] == 0

        assert rows_by_title["Senior Technical Program Manager PostApplied"]["resolution_attempted"] == 1
        assert rows_by_title["Senior Technical Program Manager PostApplied"]["resolution_succeeded"] == 1
        assert rows_by_title["Senior Technical Program Manager PostApplied"]["suppressed_post_resolution"] == 1
        assert rows_by_title["Senior Technical Program Manager PostApplied"]["accepted_rank"] is None

        assert rows_by_title["Senior Technical Program Manager Dup"]["resolution_attempted"] == 1
        assert rows_by_title["Senior Technical Program Manager Dup"]["resolution_succeeded"] == 1
        assert rows_by_title["Senior Technical Program Manager Dup"]["suppressed_canonical_duplicate"] == 1
        assert rows_by_title["Senior Technical Program Manager Dup"]["accepted_rank"] is None

        accepted_row = rows_by_title["Senior Technical Program Manager Accepted"]
        assert accepted_row["scoring_attempted"] == 1
        assert accepted_row["scoring_succeeded"] == 1
        assert accepted_row["score"] == 90
        assert accepted_row["meets_threshold"] == 1
        assert accepted_row["resolution_attempted"] == 1
        assert accepted_row["resolution_succeeded"] == 1
        assert accepted_row["accepted_rank"] == 1


class TestStrengthsGapsRoundtrip:
    def test_controlled_score_result_persists_with_lists(self, app):
        controlled = ScoreResult(
            score=84,
            fit_explanation="Strong program delivery leadership aligned.",
            strengths=["Program governance", "Budget ownership", "Cross-functional leadership"],
            gaps=["No direct fintech domain experience"],
        )
        scorer = FakeScorer({"Senior Technical Program Manager": controlled})
        resolver = FakeResolver(
            {"Senior Technical Program Manager": ok_resolution("https://boards.example/jobs/1", requisition_id="1")}
        )
        run_pipeline(app, FakeDiscover([make_job("Senior Technical Program Manager", "stpm-1")]), scorer, resolver)

        with app.app_context():
            connection = db.get_db()
            run = fetch_runs(connection, DAY)[0]
            run_jobs = fetch_run_jobs(connection, run["id"])
        assert len(run_jobs) == 1
        row = run_jobs[0]
        assert row["score"] == 84
        assert row["fit_explanation"] == "Strong program delivery leadership aligned."
        assert load_json_list(row["strengths"]) == controlled.strengths
        assert load_json_list(row["gaps"]) == controlled.gaps

    def test_empty_lists_persist_as_empty_json(self, app):
        controlled = ScoreResult(
            score=72,
            fit_explanation="OK match.",
            strengths=[],
            gaps=[],
        )
        scorer = FakeScorer({"Senior Technical Program Manager": controlled})
        resolver = FakeResolver(
            {"Senior Technical Program Manager": ok_resolution("https://boards.example/jobs/1", requisition_id="1")}
        )
        run_pipeline(app, FakeDiscover([make_job("Senior Technical Program Manager", "stpm-1")]), scorer, resolver)
        with app.app_context():
            connection = db.get_db()
            run = fetch_runs(connection, DAY)[0]
            row = fetch_run_jobs(connection, run["id"])[0]
        assert load_json_list(row["strengths"]) == []
        assert load_json_list(row["gaps"]) == []


class TestFatalDiscoveryFailure:
    def test_failure_metadata_persists_and_exception_propagates(self, app):
        def boom():
            raise RuntimeError("discovery feed unreachable")

        discover = FakeDiscover([])
        boom_discover = type("BoomDiscover", (), {"__call__": lambda self: boom(), "calls": 0})()

        with pytest.raises(RuntimeError, match="discovery feed unreachable"):
            run_pipeline(app, boom_discover, FakeScorer({}), FakeResolver({}))

        with app.app_context():
            connection = db.get_db()
            runs = fetch_runs(connection, DAY)
        assert len(runs) == 1
        run = runs[0]
        assert run["status"] == "failed"
        assert run["error_type"] == "RuntimeError"
        assert "discovery feed unreachable" in run["error_message"]
        assert run["finished_at"]

        with app.app_context():
            connection = db.get_db()
            attempts = fetch_source_attempts(connection, run["id"])
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt["source"] == TEST_SOURCE
        assert attempt["status"] == "failed"
        assert attempt["error_type"] == "RuntimeError"
        assert "discovery feed unreachable" in attempt["error_message"]
        assert attempt["finished_at"]

        with app.app_context():
            connection = db.get_db()
            assert not db.is_recommendation_day_complete(connection, DAY)
            assert db.get_recommendations(connection, DAY) == []

    def test_no_run_job_evidence_recorded_on_discovery_failure(self, app):
        def boom():
            raise RuntimeError("discovery failed")

        with pytest.raises(RuntimeError):
            run_pipeline(app, boom, FakeScorer({}), FakeResolver({}))
        with app.app_context():
            connection = db.get_db()
            run = fetch_runs(connection, DAY)[0]
            assert fetch_run_jobs(connection, run["id"]) == []


class TestFatalScoringFailure:
    def test_missing_api_key_failure_records_run_failed_and_propagates(self, app):
        jobs = [make_job("Senior Technical Program Manager", "stpm-1")]
        scorer = FakeScorer(failures={"Senior Technical Program Manager": MissingApiKeyError("ANTHROPIC_API_KEY missing")})
        resolver = FakeResolver({})

        with pytest.raises(MissingApiKeyError):
            run_pipeline(app, FakeDiscover(jobs), scorer, resolver)

        with app.app_context():
            connection = db.get_db()
            run = fetch_runs(connection, DAY)[0]
        assert run["status"] == "failed"
        assert run["error_type"] == "MissingApiKeyError"
        assert "ANTHROPIC_API_KEY" in run["error_message"]
        assert run["finished_at"]

        with app.app_context():
            connection = db.get_db()
            attempts = fetch_source_attempts(connection, run["id"])
        assert attempts[0]["status"] == "succeeded"
        assert attempts[0]["discovered_count"] == 1

        with app.app_context():
            connection = db.get_db()
            run_jobs = fetch_run_jobs(connection, run["id"])
        assert len(run_jobs) == 1
        assert run_jobs[0]["scoring_attempted"] == 1
        assert run_jobs[0]["scoring_succeeded"] == 0
        assert run_jobs[0]["scoring_error_type"] == "MissingApiKeyError"

        with app.app_context():
            connection = db.get_db()
            assert not db.is_recommendation_day_complete(connection, DAY)


class TestGenericFatalScoringFailure:
    """A non-ScoringError escaping the scorer must finalize the run as failed.

    Network, SDK, or process-runtime errors raised by the underlying
    Anthropic client surface as plain Exception subclasses — not
    ScoringError. The general fatal-finalization boundary must mark the
    run failed and propagate the original exception.
    """

    def test_runtime_error_in_scorer_finalizes_run_failed_and_propagates(self, app):
        jobs = [make_job("Senior Technical Program Manager", "stpm-1")]
        scorer = FakeScorer(
            failures={"Senior Technical Program Manager": RuntimeError("scoring transport failed")}
        )
        resolver = FakeResolver({})

        with pytest.raises(RuntimeError, match="scoring transport failed"):
            run_pipeline(app, FakeDiscover(jobs), scorer, resolver)

        with app.app_context():
            connection = db.get_db()
            run = fetch_runs(connection, DAY)[0]
        assert run["status"] == "failed"
        assert run["error_type"] == "RuntimeError"
        assert "scoring transport failed" in run["error_message"]
        assert run["finished_at"]

        with app.app_context():
            connection = db.get_db()
            attempts = fetch_source_attempts(connection, run["id"])
        assert attempts[0]["status"] == "succeeded"
        assert attempts[0]["discovered_count"] == 1

        with app.app_context():
            connection = db.get_db()
            run_jobs = fetch_run_jobs(connection, run["id"])
        assert len(run_jobs) == 1
        assert run_jobs[0]["scoring_attempted"] == 1
        assert run_jobs[0]["scoring_succeeded"] == 0
        assert run_jobs[0]["scoring_error_type"] == "RuntimeError"
        assert "scoring transport failed" in run_jobs[0]["scoring_error_message"]
        assert run_jobs[0]["resolution_attempted"] == 0

        with app.app_context():
            connection = db.get_db()
            assert not db.is_recommendation_day_complete(connection, DAY)
            assert db.get_recommendations(connection, DAY) == []


class TestFailedRetryPreservesFailedAttempt:
    def test_failed_then_succeeded_run_yields_two_distinct_runs(self, app):
        def boom():
            raise RuntimeError("first attempt discovery failed")

        first_discover = type(
            "BoomDiscover", (), {"__call__": lambda self: boom(), "calls": 0}
        )()
        with pytest.raises(RuntimeError):
            run_pipeline(app, first_discover, FakeScorer({}), FakeResolver({}))

        scorer = FakeScorer({"Senior Technical Program Manager": score_result(90)})
        resolver = FakeResolver(
            {"Senior Technical Program Manager": ok_resolution("https://boards.example/jobs/1", requisition_id="1")}
        )
        second_discover = FakeDiscover([make_job("Senior Technical Program Manager", "stpm-1")])
        recommendations = run_pipeline(app, second_discover, scorer, resolver)
        assert len(recommendations) == 1

        with app.app_context():
            connection = db.get_db()
            runs = fetch_runs(connection, DAY)
        assert len(runs) == 2
        run_ids = [run["id"] for run in runs]
        assert len(set(run_ids)) == 2

        first = next(run for run in runs if run["status"] == "failed")
        second = next(run for run in runs if run["status"] == "succeeded")
        assert first["error_type"] == "RuntimeError"
        assert first["finished_at"]
        assert second["recommendation_threshold"] == 70
        assert second["finished_at"]

        with app.app_context():
            connection = db.get_db()
            assert db.is_recommendation_day_complete(connection, DAY)


class TestCompletedDayIdempotency:
    def test_re_invocation_after_completion_does_not_record_new_run(self, app):
        first_scorer = FakeScorer({"Senior Technical Program Manager": score_result(90)})
        first_resolver = FakeResolver(
            {"Senior Technical Program Manager": ok_resolution("https://boards.example/jobs/1", requisition_id="1")}
        )
        first_discover = FakeDiscover([make_job("Senior Technical Program Manager", "stpm-1")])
        first = run_pipeline(app, first_discover, first_scorer, first_resolver)
        assert len(first) == 1

        with app.app_context():
            connection = db.get_db()
            first_run_count = len(fetch_runs(connection, DAY))
            first_run_id = fetch_runs(connection, DAY)[0]["id"]
            first_attempt_count = len(fetch_source_attempts(connection, first_run_id))
            first_pinned = [row["job_id"] for row in db.get_recommendations(connection, DAY)]

        def explode(*args, **kwargs):
            raise AssertionError(
                "engine must not re-discover on a completed day"
            )

        second = run_pipeline(app, explode, explode, explode)
        assert [row["job_id"] for row in second] == first_pinned

        with app.app_context():
            connection = db.get_db()
            runs_after = fetch_runs(connection, DAY)
            attempts_after = fetch_source_attempts(connection, runs_after[0]["id"])
        assert len(runs_after) == first_run_count
        assert len(attempts_after) == first_attempt_count

    def test_completed_zero_result_day_does_not_record_new_run(self, app):
        first = run_pipeline(
            app,
            FakeDiscover([make_job("Plumber", "plumber-1")]),
            FakeScorer({}),
            FakeResolver({}),
        )
        assert first == []
        with app.app_context():
            connection = db.get_db()
            first_run_count = len(fetch_runs(connection, DAY))

        def explode(*args, **kwargs):
            raise AssertionError("discover must not run on a completed day")

        second = run_pipeline(app, explode, explode, explode)
        assert second == []
        with app.app_context():
            connection = db.get_db()
            assert len(fetch_runs(connection, DAY)) == first_run_count