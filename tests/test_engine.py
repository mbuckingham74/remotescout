import pytest

from remotescout import db
from remotescout.app import create_app
from remotescout.discovery import DiscoveredJob
from remotescout.engine import build_daily_recommendations
from remotescout.resolution import ResolutionResult
from remotescout.scoring import MissingApiKeyError, ScoreResult, ScoringError

DAY = "2026-08-11"
PRIOR_DAY = "2026-08-10"


def make_job(**overrides):
    fields = {
        "source": "weworkremotely",
        "source_url": "https://weworkremotely.com/remote-jobs/acme-senior-tpm",
        "source_job_id": "acme-senior-tpm",
        "title": "Senior Technical Program Manager",
        "employer": "Acme Inc.",
        "location": "Anywhere in the World",
        "description": "Headquarters: https://acme.com/careers",
    }
    fields.update(overrides)
    return DiscoveredJob(**fields)


def result(score, explanation="Strong fit for the role."):
    return ScoreResult(
        score=score,
        fit_explanation=explanation,
        strengths=["Direct delivery leadership"],
        gaps=["No fintech domain experience"],
    )


def ok_resolution(url, requisition_id=None):
    return ResolutionResult(
        resolved=True, employer_url=url, requisition_id=requisition_id, method="greenhouse"
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


class MidRunInspectingResolver:
    def __init__(self, results):
        self.results = dict(results)
        self.calls = []
        self.mid_run_counts = []

    def __call__(self, job):
        self.calls.append(job.title)
        connection = db.get_db()
        count = connection.execute(
            "SELECT COUNT(*) FROM recommendations WHERE date = ?", (DAY,)
        ).fetchone()[0]
        self.mid_run_counts.append(count)
        return self.results[job.title]


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE_PATH": str(tmp_path / "test.db")})


def run_pipeline(app, discover, scorer, resolver, threshold=70, day=DAY):
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
        )


def insert_applied_job(connection, **overrides):
    fields = {
        "title": "Senior Technical Program Manager",
        "employer": "Acme Inc.",
        "location": "Anywhere in the World",
        "source": "otherboard",
        "source_url": "https://other.example/jobs/1",
        "source_job_id": "other-1",
        "description": "Run infrastructure delivery programs.",
    }
    fields.update(overrides)
    job = DiscoveredJob(**fields)
    job_id = db.create_job(
        connection,
        title=job.title,
        employer=job.employer,
        location=job.location,
        source=job.source,
        source_url=job.source_url,
        source_job_id=job.source_job_id,
        identity_key=db.identity_key(job),
    )
    db.create_application(connection, job_id, applied_at="2026-08-01")
    connection.commit()
    return job_id


def insert_pinned_recommendation(connection, day, job_id, score=88, explanation="Pinned"):
    connection.execute(
        "INSERT INTO recommendations (date, rank, job_id, score, explanation) "
        "VALUES (?, ?, ?, ?, ?)",
        (day, 1, job_id, score, explanation),
    )
    connection.commit()


def test_filtered_job_is_never_scored(app):
    jobs = [
        make_job(
            title="Plumber",
            employer="Pipe Co.",
            source_job_id="plumber-1",
            source_url="https://weworkremotely.com/remote-jobs/plumber-1",
        ),
        make_job(
            source_job_id="spm-1",
            source_url="https://weworkremotely.com/remote-jobs/spm-1",
        ),
    ]
    scorer = FakeScorer({"Senior Technical Program Manager": result(90)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/spm")}
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert scorer.calls == ["Senior Technical Program Manager"]
    assert len(recommendations) == 1


def test_applied_job_is_never_scored(app):
    with app.app_context():
        connection = db.get_db()
        job_id = db.upsert_job(connection, make_job(source_job_id="spm-applied"))
        db.create_application(connection, job_id, applied_at="2026-08-01")
        connection.commit()
    scorer = FakeScorer({})
    resolver = FakeResolver({})
    recommendations = run_pipeline(
        app, FakeDiscover([make_job(source_job_id="spm-applied")]), scorer, resolver
    )
    assert scorer.calls == []
    assert recommendations == []


def test_below_threshold_job_is_scored_but_never_resolved(app):
    scorer = FakeScorer({"Senior Technical Program Manager": result(55)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/spm")}
    )
    recommendations = run_pipeline(app, FakeDiscover([make_job()]), scorer, resolver)
    assert scorer.calls == ["Senior Technical Program Manager"]
    assert resolver.calls == []
    assert recommendations == []


def test_eligible_candidates_resolved_in_descending_score_order(app):
    jobs = [
        make_job(
            title="Senior Technical Program Manager",
            source_job_id="pe-1",
            source_url="https://weworkremotely.com/remote-jobs/pe-1",
        ),
        make_job(
            title="Technical Program Manager",
            source_job_id="tpm-1",
            source_url="https://weworkremotely.com/remote-jobs/tpm-1",
        ),
        make_job(
            source_job_id="spm-1",
            source_url="https://weworkremotely.com/remote-jobs/spm-1",
        ),
    ]
    scorer = FakeScorer(
        {
            "Senior Technical Program Manager": result(80),
            "Technical Program Manager": result(95),
            "Senior Technical Program Manager": result(87),
        }
    )
    resolver = FakeResolver(
        {
            "Senior Technical Program Manager": ok_resolution("https://acme.com/careers/pe"),
            "Technical Program Manager": ok_resolution("https://acme.com/careers/tpm"),
            "Senior Technical Program Manager": ok_resolution("https://acme.com/careers/spm"),
        }
    )
    run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert resolver.calls == [
        "Technical Program Manager",
        "Senior Technical Program Manager",
        "Senior Technical Program Manager",
    ]


def _make_n_jobs(count):
    jobs = []
    scorer_results = {}
    resolver_results = {}
    for index in range(count):
        title = f"Senior Technical Program Manager {index}"
        jobs.append(
            make_job(
                title=title,
                source_job_id=f"pe-{index}",
                source_url=f"https://weworkremotely.com/remote-jobs/pe-{index}",
            )
        )
        scorer_results[title] = result(90 - index)
        resolver_results[title] = ok_resolution(f"https://acme.com/careers/pe-{index}")
    return jobs, scorer_results, resolver_results


def test_more_than_three_valid_jobs_persist_exactly_three(app):
    jobs, scorer_results, resolver_results = _make_n_jobs(6)
    recommendations = run_pipeline(
        app,
        FakeDiscover(jobs),
        FakeScorer(scorer_results),
        FakeResolver(resolver_results),
    )
    assert len(recommendations) == 3
    assert recommendations[0]["rank"] == 1
    assert recommendations[2]["rank"] == 3


def test_resolution_stops_after_third_successful_recommendation(app):
    jobs, scorer_results, resolver_results = _make_n_jobs(6)
    resolver = FakeResolver(resolver_results)
    recommendations = run_pipeline(
        app, FakeDiscover(jobs), FakeScorer(scorer_results), resolver
    )
    assert resolver.calls == [
        "Senior Technical Program Manager 0",
        "Senior Technical Program Manager 1",
        "Senior Technical Program Manager 2",
    ]
    assert len(recommendations) == 3


def test_failed_resolution_tries_next_highest_candidate(app):
    titles = ["Director, Technical Program Management", "Senior Technical Program Manager", "Principal Technical Program Manager", "Program Delivery Director"]
    jobs = [
        make_job(title=title, source_job_id=title.lower().replace(" ", "-"))
        for title in titles
    ]
    scorer = FakeScorer(
        {
            "Director, Technical Program Management": result(95),
            "Senior Technical Program Manager": result(93),
            "Principal Technical Program Manager": result(91),
            "Program Delivery Director": result(89),
        }
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": failed_resolution(),
            "Senior Technical Program Manager": ok_resolution("https://acme.com/careers/beta"),
            "Principal Technical Program Manager": ok_resolution("https://acme.com/careers/gamma"),
            "Program Delivery Director": ok_resolution("https://acme.com/careers/delta"),
        }
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert resolver.calls == titles
    assert [row["title"] for row in recommendations] == titles[1:]
    assert len(recommendations) == 3


def test_fewer_than_three_resolved_jobs_produce_fewer_recommendations(app):
    titles = ["Director, Technical Program Management", "Senior Technical Program Manager", "Principal Technical Program Manager"]
    jobs = [
        make_job(title=title, source_job_id=title.lower().replace(" ", "-"))
        for title in titles
    ]
    scorer = FakeScorer(
        {
            "Director, Technical Program Management": result(95),
            "Senior Technical Program Manager": result(93),
            "Principal Technical Program Manager": result(91),
        }
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution("https://acme.com/careers/alpha"),
            "Senior Technical Program Manager": failed_resolution(),
            "Principal Technical Program Manager": failed_resolution(),
        }
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert len(recommendations) == 1
    assert recommendations[0]["title"] == "Director, Technical Program Management"
    assert recommendations[0]["rank"] == 1


def test_zero_qualifying_jobs_is_valid(app):
    jobs = [
        make_job(
            title="Plumber",
            employer="Pipe Co.",
            source_job_id="plumber-1",
            source_url="https://weworkremotely.com/remote-jobs/plumber-1",
        ),
        make_job(
            title="Junior Designer",
            employer="Design Co.",
            source_job_id="jd-1",
            source_url="https://weworkremotely.com/remote-jobs/jd-1",
        ),
    ]
    scorer = FakeScorer({})
    resolver = FakeResolver({})
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert recommendations == []
    assert scorer.calls == []
    assert resolver.calls == []


def test_exact_previously_applied_job_excluded(app):
    with app.app_context():
        connection = db.get_db()
        applied_id = db.upsert_job(
            connection,
            make_job(
                title="Senior Technical Program Manager",
                source_job_id="applied-1",
                source_url="https://weworkremotely.com/remote-jobs/applied-1",
            ),
        )
        db.create_application(connection, applied_id, applied_at="2026-08-01")
        connection.commit()
    jobs = [
        make_job(
            title="Senior Technical Program Manager",
            source_job_id="applied-1",
            source_url="https://weworkremotely.com/remote-jobs/applied-1",
        ),
        make_job(
            title="Director, Technical Program Management",
            source_job_id="fresh-1",
            source_url="https://weworkremotely.com/remote-jobs/fresh-1",
        ),
    ]
    scorer = FakeScorer({"Director, Technical Program Management": result(90)})
    resolver = FakeResolver(
        {"Director, Technical Program Management": ok_resolution("https://acme.com/careers/fresh")}
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert scorer.calls == ["Director, Technical Program Management"]
    assert len(recommendations) == 1
    assert recommendations[0]["title"] == "Director, Technical Program Management"


def test_fallback_identity_match_to_applied_job_excluded_before_scoring(app):
    with app.app_context():
        connection = db.get_db()
        insert_applied_job(connection, title="Senior Technical Program Manager")
    candidate = make_job(
        title="Senior Technical Program Manager",
        source_job_id="wwr-pe-1",
        source_url="https://weworkremotely.com/remote-jobs/wwr-pe-1",
    )
    scorer = FakeScorer({})
    resolver = FakeResolver({})
    recommendations = run_pipeline(app, FakeDiscover([candidate]), scorer, resolver)
    assert scorer.calls == []
    assert recommendations == []


def test_applied_after_canonical_resolution_excluded(app):
    with app.app_context():
        connection = db.get_db()
        job_id = db.create_job(
            connection,
            title="Senior Technical Program Manager",
            employer="Acme Inc.",
            source="greenhouse",
            source_url="https://boards.greenhouse.io/acme/jobs/1234",
            source_job_id="gh-1234",
            employer_url="https://boards.greenhouse.io/acme/jobs/1234",
            requisition_id="1234",
        )
        db.create_application(connection, job_id, applied_at="2026-08-01")
        connection.commit()
    candidate = make_job(
        title="Senior Technical Program Manager",
        source_job_id="wwr-pe-1",
        source_url="https://weworkremotely.com/remote-jobs/wwr-pe-1",
    )
    scorer = FakeScorer({"Senior Technical Program Manager": result(92)})
    resolver = FakeResolver(
        {
            "Senior Technical Program Manager": ok_resolution(
                "https://boards.greenhouse.io/acme/jobs/1234", requisition_id="1234"
            )
        }
    )
    recommendations = run_pipeline(app, FakeDiscover([candidate]), scorer, resolver)
    assert scorer.calls == ["Senior Technical Program Manager"]
    assert resolver.calls == ["Senior Technical Program Manager"]
    assert recommendations == []


def test_next_ranked_candidate_takes_place_of_excluded(app):
    with app.app_context():
        connection = db.get_db()
        applied_id = db.create_job(
            connection,
            title="Applied Job",
            employer="Acme Inc.",
            source="greenhouse",
            source_url="https://boards.greenhouse.io/acme/jobs/99",
            employer_url="https://boards.greenhouse.io/acme/jobs/99",
            requisition_id="99",
        )
        db.create_application(connection, applied_id, applied_at="2026-08-01")
        connection.commit()
    titles = ["Director, Technical Program Management", "Senior Technical Program Manager", "Principal Technical Program Manager", "Program Delivery Director"]
    jobs = [
        make_job(title=title, source_job_id=title.lower().replace(" ", "-"))
        for title in titles
    ]
    scorer = FakeScorer(
        {
            "Director, Technical Program Management": result(92),
            "Senior Technical Program Manager": result(90),
            "Principal Technical Program Manager": result(85),
            "Program Delivery Director": result(80),
        }
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://boards.greenhouse.io/acme/jobs/99", requisition_id="99"
            ),
            "Senior Technical Program Manager": ok_resolution("https://acme.com/careers/beta"),
            "Principal Technical Program Manager": ok_resolution("https://acme.com/careers/gamma"),
            "Program Delivery Director": ok_resolution("https://acme.com/careers/delta"),
        }
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert resolver.calls == titles
    assert [row["title"] for row in recommendations] == titles[1:]
    assert [row["rank"] for row in recommendations] == [1, 2, 3]


def test_same_day_recommendations_returned_without_pipeline(app):
    with app.app_context():
        connection = db.get_db()
        job_id = db.create_job(
            connection,
            title="Pinned Role",
            employer="Pinned Co.",
            employer_url="https://pinned.example/jobs/1",
        )
        insert_pinned_recommendation(connection, DAY, job_id, score=88)
    discover = FakeDiscover([make_job(title="Pinned Role")])
    scorer = FakeScorer({})
    resolver = FakeResolver({})
    recommendations = run_pipeline(app, discover, scorer, resolver)
    assert discover.calls == 0
    assert scorer.calls == []
    assert resolver.calls == []
    assert len(recommendations) == 1
    assert recommendations[0]["title"] == "Pinned Role"
    assert recommendations[0]["score"] == 88


def test_prior_day_recommendation_still_eligible_today(app):
    with app.app_context():
        connection = db.get_db()
        job_id = db.upsert_job(connection, make_job(source_job_id="same-1"))
        insert_pinned_recommendation(connection, PRIOR_DAY, job_id, score=90)
    scorer = FakeScorer({"Senior Technical Program Manager": result(92)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/spm")}
    )
    recommendations = run_pipeline(
        app,
        FakeDiscover([make_job(source_job_id="same-1")]),
        scorer,
        resolver,
    )
    assert scorer.calls == ["Senior Technical Program Manager"]
    assert len(recommendations) == 1
    assert recommendations[0]["job_id"] == job_id


def test_recommendation_history_never_suppresses(app):
    with app.app_context():
        connection = db.get_db()
        job_id = db.upsert_job(connection, make_job(source_job_id="yesterday-listing"))
        insert_pinned_recommendation(connection, PRIOR_DAY, job_id, score=90)
    scorer = FakeScorer({"Senior Technical Program Manager": result(92)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/spm")}
    )
    recommendations = run_pipeline(
        app,
        FakeDiscover([make_job(source_job_id="today-listing")]),
        scorer,
        resolver,
    )
    assert scorer.calls == ["Senior Technical Program Manager"]
    assert len(recommendations) == 1
    assert recommendations[0]["job_id"] != job_id


def test_scoring_error_candidate_skipped_pipeline_continues(app):
    jobs = [
        make_job(
            title="Technical Program Manager",
            source_job_id="bad-1",
            source_url="https://weworkremotely.com/remote-jobs/bad-1",
        ),
        make_job(
            source_job_id="good-1",
            source_url="https://weworkremotely.com/remote-jobs/good-1",
        ),
    ]
    scorer = FakeScorer(
        results={"Senior Technical Program Manager": result(90)},
        failures={"Technical Program Manager": ScoringError("malformed output")},
    )
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/good")}
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert set(scorer.calls) == {"Technical Program Manager", "Senior Technical Program Manager"}
    assert len(recommendations) == 1
    assert recommendations[0]["title"] == "Senior Technical Program Manager"


def test_non_scoring_error_propagates(app):
    scorer = FakeScorer(
        results={},
        failures={"Senior Technical Program Manager": RuntimeError("anthropic api failure")},
    )
    with pytest.raises(RuntimeError):
        run_pipeline(app, FakeDiscover([make_job()]), scorer, FakeResolver({}))


def test_successful_score_persisted_with_explanation(app):
    scorer = FakeScorer(
        {"Senior Technical Program Manager": result(91, explanation="Excellent direct delivery leadership.")}
    )
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/spm")}
    )
    run_pipeline(app, FakeDiscover([make_job()]), scorer, resolver)
    with app.app_context():
        connection = db.get_db()
        row = connection.execute(
            "SELECT score, fit_explanation FROM jobs WHERE title = ?",
            ("Senior Technical Program Manager",),
        ).fetchone()
    assert row["score"] == 91
    assert row["fit_explanation"] == "Excellent direct delivery leadership."


def test_successful_resolution_persisted(app):
    scorer = FakeScorer({"Senior Technical Program Manager": result(90)})
    resolver = FakeResolver(
        {
            "Senior Technical Program Manager": ok_resolution(
                "https://boards.greenhouse.io/acme/jobs/1234", requisition_id="1234"
            )
        }
    )
    run_pipeline(app, FakeDiscover([make_job()]), scorer, resolver)
    with app.app_context():
        connection = db.get_db()
        row = connection.execute(
            "SELECT employer_url, requisition_id FROM jobs WHERE title = ?",
            ("Senior Technical Program Manager",),
        ).fetchone()
    assert row["employer_url"] == "https://boards.greenhouse.io/acme/jobs/1234"
    assert row["requisition_id"] == "1234"


def test_recommendation_ranks_descending_score_order(app):
    titles = ["Director, Technical Program Management", "Senior Technical Program Manager", "Principal Technical Program Manager"]
    jobs = [
        make_job(title=title, source_job_id=title.lower().replace(" ", "-"))
        for title in titles
    ]
    scorer = FakeScorer(
        {
            "Director, Technical Program Management": result(88),
            "Senior Technical Program Manager": result(92),
            "Principal Technical Program Manager": result(79),
        }
    )
    resolver = FakeResolver(
        {
            title: ok_resolution(f"https://acme.com/careers/{title.lower()}")
            for title in titles
        }
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert [row["rank"] for row in recommendations] == [1, 2, 3]
    assert [row["score"] for row in recommendations] == [92, 88, 79]
    assert [row["title"] for row in recommendations] == [
        "Senior Technical Program Manager",
        "Director, Technical Program Management",
        "Principal Technical Program Manager",
    ]


def test_source_aggregate_url_preserved(app):
    scorer = FakeScorer({"Senior Technical Program Manager": result(90)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/spm")}
    )
    run_pipeline(
        app,
        FakeDiscover(
            [make_job(source_url="https://weworkremotely.com/remote-jobs/acme-senior-product-manager")]
        ),
        scorer,
        resolver,
    )
    with app.app_context():
        connection = db.get_db()
        row = connection.execute(
            "SELECT source_url FROM jobs WHERE title = ?",
            ("Senior Technical Program Manager",),
        ).fetchone()
    assert row["source_url"] == "https://weworkremotely.com/remote-jobs/acme-senior-product-manager"


def test_recommendations_committed_together_at_end(app):
    jobs, scorer_results, resolver_results = _make_n_jobs(3)
    resolver = MidRunInspectingResolver(resolver_results)
    recommendations = run_pipeline(
        app, FakeDiscover(jobs), FakeScorer(scorer_results), resolver
    )
    assert resolver.mid_run_counts == [0, 0, 0]
    assert len(recommendations) == 3
    with app.app_context():
        connection = db.get_db()
        count = connection.execute(
            "SELECT COUNT(*) FROM recommendations WHERE date = ?", (DAY,)
        ).fetchone()[0]
    assert count == 3


def test_fatal_exception_mid_run_leaves_zero_recommendations(app):
    titles = ["Director, Technical Program Management", "Senior Technical Program Manager", "Principal Technical Program Manager", "Program Delivery Director"]
    jobs = [
        make_job(title=title, source_job_id=title.lower().replace(" ", "-"))
        for title in titles
    ]
    scorer = FakeScorer({title: result(95 - index) for index, title in enumerate(titles)})
    resolver = FakeResolver(
        results={
            "Director, Technical Program Management": ok_resolution("https://acme.com/careers/alpha"),
            "Senior Technical Program Manager": ok_resolution("https://acme.com/careers/beta"),
            "Program Delivery Director": ok_resolution("https://acme.com/careers/delta"),
        },
        failures={"Principal Technical Program Manager": RuntimeError("resolution network failure")},
    )
    with pytest.raises(RuntimeError):
        run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    with app.app_context():
        connection = db.get_db()
        count = connection.execute(
            "SELECT COUNT(*) FROM recommendations WHERE date = ?", (DAY,)
        ).fetchone()[0]
    assert count == 0


def test_retry_after_failed_run_not_pinned(app):
    titles = ["Director, Technical Program Management", "Senior Technical Program Manager", "Principal Technical Program Manager", "Program Delivery Director"]
    jobs = [
        make_job(title=title, source_job_id=title.lower().replace(" ", "-"))
        for title in titles
    ]
    failing_first_scorer = FakeScorer(
        results={
            "Senior Technical Program Manager": result(93),
            "Principal Technical Program Manager": result(91),
            "Program Delivery Director": result(89),
        },
        failures={
            "Director, Technical Program Management": MissingApiKeyError("anthropic key missing"),
        },
    )
    with pytest.raises(MissingApiKeyError):
        run_pipeline(
            app,
            FakeDiscover(jobs),
            failing_first_scorer,
            FakeResolver(
                {
                    title: ok_resolution(f"https://acme.com/careers/{title.lower()}")
                    for title in titles
                }
            ),
        )
    resolver = FakeResolver(
        {
            title: ok_resolution(f"https://acme.com/careers/{title.lower()}")
            for title in titles
        }
    )
    scorer = FakeScorer({title: result(95 - index) for index, title in enumerate(titles)})
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert len(recommendations) == 3
    assert [row["rank"] for row in recommendations] == [1, 2, 3]


def test_duplicate_canonical_url_yields_single_recommendation(app):
    titles = ["Director, Technical Program Management", "Senior Technical Program Manager", "Principal Technical Program Manager"]
    jobs = [
        make_job(title=title, source_job_id=title.lower().replace(" ", "-"))
        for title in titles
    ]
    scorer = FakeScorer(
        {
            "Director, Technical Program Management": result(94),
            "Senior Technical Program Manager": result(92),
            "Principal Technical Program Manager": result(90),
        }
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://boards.greenhouse.io/acme/jobs/1234", requisition_id="1234"
            ),
            "Senior Technical Program Manager": ok_resolution(
                "https://boards.greenhouse.io/acme/jobs/1234", requisition_id="1234"
            ),
            "Principal Technical Program Manager": ok_resolution(
                "https://boards.greenhouse.io/acme/jobs/5678", requisition_id="5678"
            ),
        }
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert resolver.calls == titles
    assert [row["title"] for row in recommendations] == ["Director, Technical Program Management", "Principal Technical Program Manager"]
    assert [row["rank"] for row in recommendations] == [1, 2]


def test_duplicate_canonical_url_trailing_slash_normalized(app):
    titles = ["Director, Technical Program Management", "Senior Technical Program Manager", "Principal Technical Program Manager"]
    jobs = [
        make_job(title=title, source_job_id=title.lower().replace(" ", "-"))
        for title in titles
    ]
    scorer = FakeScorer(
        {
            "Director, Technical Program Management": result(94),
            "Senior Technical Program Manager": result(92),
            "Principal Technical Program Manager": result(90),
        }
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution("https://boards.greenhouse.io/acme/jobs/1234"),
            "Senior Technical Program Manager": ok_resolution("https://boards.greenhouse.io/acme/jobs/1234/"),
            "Principal Technical Program Manager": ok_resolution("https://boards.greenhouse.io/acme/jobs/5678"),
        }
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert [row["title"] for row in recommendations] == ["Director, Technical Program Management", "Principal Technical Program Manager"]
    assert len(recommendations) == 2


def test_duplicate_suppressed_by_employer_and_requisition_id(app):
    titles = ["Director, Technical Program Management", "Senior Technical Program Manager", "Principal Technical Program Manager"]
    jobs = [
        make_job(title=title, source_job_id=title.lower().replace(" ", "-"))
        for title in titles
    ]
    scorer = FakeScorer(
        {
            "Director, Technical Program Management": result(94),
            "Senior Technical Program Manager": result(92),
            "Principal Technical Program Manager": result(90),
        }
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution(
                "https://boards.greenhouse.io/acme/jobs/1234", requisition_id="1234"
            ),
            "Senior Technical Program Manager": ok_resolution(
                "https://job-boards.greenhouse.io/acme/jobs/1234",
                requisition_id="1234",
            ),
            "Principal Technical Program Manager": ok_resolution(
                "https://boards.greenhouse.io/acme/jobs/5678", requisition_id="5678"
            ),
        }
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert [row["title"] for row in recommendations] == ["Director, Technical Program Management", "Principal Technical Program Manager"]
    assert len(recommendations) == 2


def test_duplicate_canonical_position_skips_to_next_unique(app):
    titles = ["Director, Technical Program Management", "Senior Technical Program Manager", "Principal Technical Program Manager", "Program Delivery Director"]
    jobs = [
        make_job(title=title, source_job_id=title.lower().replace(" ", "-"))
        for title in titles
    ]
    scorer = FakeScorer(
        {
            "Director, Technical Program Management": result(94),
            "Senior Technical Program Manager": result(92),
            "Principal Technical Program Manager": result(90),
            "Program Delivery Director": result(88),
        }
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution("https://boards.greenhouse.io/acme/jobs/1234"),
            "Senior Technical Program Manager": ok_resolution("https://boards.greenhouse.io/acme/jobs/1234"),
            "Principal Technical Program Manager": ok_resolution("https://boards.greenhouse.io/acme/jobs/5678"),
            "Program Delivery Director": ok_resolution("https://boards.greenhouse.io/acme/jobs/9012"),
        }
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert resolver.calls == titles
    assert [row["title"] for row in recommendations] == [
        "Director, Technical Program Management",
        "Principal Technical Program Manager",
        "Program Delivery Director",
    ]
    assert [row["rank"] for row in recommendations] == [1, 2, 3]


def test_three_unique_accepted_stop_resolution_immediately(app):
    titles = ["Director, Technical Program Management", "Senior Technical Program Manager", "Principal Technical Program Manager", "Program Delivery Director", "Director, EPMO"]
    jobs = [
        make_job(title=title, source_job_id=title.lower().replace(" ", "-"))
        for title in titles
    ]
    scorer = FakeScorer(
        {title: result(95 - index) for index, title in enumerate(titles)}
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution("https://boards.greenhouse.io/acme/jobs/1234"),
            "Senior Technical Program Manager": ok_resolution("https://boards.greenhouse.io/acme/jobs/1234"),
            "Principal Technical Program Manager": ok_resolution("https://boards.greenhouse.io/acme/jobs/5678"),
            "Program Delivery Director": ok_resolution("https://boards.greenhouse.io/acme/jobs/9012"),
            "Director, EPMO": ok_resolution("https://boards.greenhouse.io/acme/jobs/3456"),
        }
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert resolver.calls == [
        "Director, Technical Program Management",
        "Senior Technical Program Manager",
        "Principal Technical Program Manager",
        "Program Delivery Director",
    ]
    assert len(recommendations) == 3
    assert [row["title"] for row in recommendations] == [
        "Director, Technical Program Management",
        "Principal Technical Program Manager",
        "Program Delivery Director",
    ]


def test_employer_url_trailing_slash_compare_equal():
    assert db.normalize_employer_url(
        "https://careers.example.com/jobs/123"
    ) == db.normalize_employer_url("https://careers.example.com/jobs/123/")


def test_employer_url_scheme_host_casing_compare_equal():
    assert db.normalize_employer_url(
        "HTTPS://Careers.Example.COM/jobs/123"
    ) == db.normalize_employer_url("https://careers.example.com/jobs/123")


def test_employer_url_fragment_does_not_create_distinct_identity():
    assert db.normalize_employer_url(
        "https://careers.example.com/jobs/123#apply"
    ) == db.normalize_employer_url("https://careers.example.com/jobs/123#section-2")


def test_employer_url_different_query_values_remain_distinct():
    assert db.normalize_employer_url(
        "https://careers.example.com/apply?job=123"
    ) != db.normalize_employer_url("https://careers.example.com/apply?job=456")


def test_employer_url_identical_query_urls_compare_equal():
    assert db.normalize_employer_url(
        "https://careers.example.com/apply?job=123"
    ) == db.normalize_employer_url("https://careers.example.com/apply?job=123")


def test_query_differentiated_canonical_urls_occupy_two_slots(app):
    titles = ["Director, Technical Program Management", "Senior Technical Program Manager", "Principal Technical Program Manager"]
    jobs = [
        make_job(title=title, source_job_id=title.lower().replace(" ", "-"))
        for title in titles
    ]
    scorer = FakeScorer(
        {
            "Director, Technical Program Management": result(94),
            "Senior Technical Program Manager": result(92),
            "Principal Technical Program Manager": result(90),
        }
    )
    resolver = FakeResolver(
        {
            "Director, Technical Program Management": ok_resolution("https://careers.example.com/apply?job=123"),
            "Senior Technical Program Manager": ok_resolution("https://careers.example.com/apply?job=456"),
            "Principal Technical Program Manager": ok_resolution("https://careers.example.com/apply?job=789"),
        }
    )
    recommendations = run_pipeline(app, FakeDiscover(jobs), scorer, resolver)
    assert resolver.calls == titles
    assert [row["title"] for row in recommendations] == titles
    assert len(recommendations) == 3


def test_source_url_normalization_unchanged_for_upsert_dedup(app):
    assert db.normalize_url(
        "https://weworkremotely.com/remote-jobs/job-123/"
    ) == "https://weworkremotely.com/remote-jobs/job-123"
    assert db.normalize_url(
        "HTTPS://WeworkRemotely.COM/remote-jobs/job-123"
    ) == "https://weworkremotely.com/remote-jobs/job-123"
    with app.app_context():
        connection = db.get_db()
        first = db.upsert_job(
            connection,
            make_job(
                source_job_id=None,
                source_url="https://weworkremotely.com/remote-jobs/same-1",
            ),
        )
        second = db.upsert_job(
            connection,
            make_job(
                source_job_id=None,
                source_url="https://weworkremotely.com/remote-jobs/same-1/",
            ),
        )
        connection.commit()
    assert first == second


def test_applied_suppression_uses_safe_employer_normalization(app):
    with app.app_context():
        connection = db.get_db()
        applied_id = db.create_job(
            connection,
            title="Senior Technical Program Manager",
            employer="Acme Inc.",
            source="greenhouse",
            source_url="https://boards.greenhouse.io/acme/jobs/1234",
            source_job_id="gh-1234",
            employer_url="https://boards.greenhouse.io/acme/jobs/1234/",
            requisition_id="1234",
        )
        db.create_application(connection, applied_id, applied_at="2026-08-01")
        connection.commit()
    candidate = make_job(
        title="Senior Technical Program Manager",
        source_job_id="wwr-pe-1",
        source_url="https://weworkremotely.com/remote-jobs/wwr-pe-1",
    )
    scorer = FakeScorer({"Senior Technical Program Manager": result(92)})
    resolver = FakeResolver(
        {
            "Senior Technical Program Manager": ok_resolution(
                "https://boards.greenhouse.io/acme/jobs/1234",
                requisition_id="1234",
            )
        }
    )
    recommendations = run_pipeline(app, FakeDiscover([candidate]), scorer, resolver)
    assert scorer.calls == ["Senior Technical Program Manager"]
    assert resolver.calls == ["Senior Technical Program Manager"]
    assert recommendations == []


def test_zero_result_run_creates_completed_day_marker(app):
    jobs = [
        make_job(
            title="Plumber",
            employer="Pipe Co.",
            source_job_id="plumber-1",
            source_url="https://weworkremotely.com/remote-jobs/plumber-1",
        ),
        make_job(
            title="Junior Designer",
            employer="Design Co.",
            source_job_id="jd-1",
            source_url="https://weworkremotely.com/remote-jobs/jd-1",
        ),
    ]
    recommendations = run_pipeline(app, FakeDiscover(jobs), FakeScorer({}), FakeResolver({}))
    assert recommendations == []
    with app.app_context():
        connection = db.get_db()
        marker = connection.execute(
            "SELECT 1 FROM recommendation_days WHERE recommendation_date = ?", (DAY,)
        ).fetchone()
        row_count = connection.execute(
            "SELECT COUNT(*) FROM recommendations WHERE date = ?", (DAY,)
        ).fetchone()[0]
    assert marker is not None
    assert row_count == 0


def test_completed_zero_day_second_invocation_does_no_work(app):
    job = make_job()
    discover = FakeDiscover([job])
    scorer = FakeScorer({"Senior Technical Program Manager": result(55)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/spm")}
    )
    first = run_pipeline(app, discover, scorer, resolver)
    assert first == []
    assert discover.calls == 1
    assert scorer.calls == ["Senior Technical Program Manager"]
    assert resolver.calls == []
    second = run_pipeline(app, discover, scorer, resolver)
    assert second == []
    assert discover.calls == 1
    assert scorer.calls == ["Senior Technical Program Manager"]
    assert resolver.calls == []


def test_completed_one_result_day_is_marked_complete(app):
    scorer = FakeScorer({"Senior Technical Program Manager": result(90)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/spm")}
    )
    recommendations = run_pipeline(app, FakeDiscover([make_job()]), scorer, resolver)
    assert len(recommendations) == 1
    with app.app_context():
        connection = db.get_db()
        marker = connection.execute(
            "SELECT 1 FROM recommendation_days WHERE recommendation_date = ?", (DAY,)
        ).fetchone()
    assert marker is not None


def test_completed_three_result_day_is_marked_complete(app):
    jobs, scorer_results, resolver_results = _make_n_jobs(3)
    recommendations = run_pipeline(
        app,
        FakeDiscover(jobs),
        FakeScorer(scorer_results),
        FakeResolver(resolver_results),
    )
    assert len(recommendations) == 3
    with app.app_context():
        connection = db.get_db()
        marker = connection.execute(
            "SELECT 1 FROM recommendation_days WHERE recommendation_date = ?", (DAY,)
        ).fetchone()
    assert marker is not None


def test_final_persistence_failure_leaves_no_rows_or_marker(app, monkeypatch):
    jobs, scorer_results, resolver_results = _make_n_jobs(2)

    def boom(*args, **kwargs):
        raise RuntimeError("marker insert failed")

    monkeypatch.setattr("remotescout.db.mark_recommendation_day_complete", boom)
    with pytest.raises(RuntimeError):
        run_pipeline(
            app,
            FakeDiscover(jobs),
            FakeScorer(scorer_results),
            FakeResolver(resolver_results),
        )
    with app.app_context():
        connection = db.get_db()
        row_count = connection.execute(
            "SELECT COUNT(*) FROM recommendations WHERE date = ?", (DAY,)
        ).fetchone()[0]
        marker = connection.execute(
            "SELECT 1 FROM recommendation_days WHERE recommendation_date = ?", (DAY,)
        ).fetchone()
    assert row_count == 0
    assert marker is None


def test_fatal_pipeline_exception_creates_no_completion_marker(app):
    jobs, scorer_results, _ = _make_n_jobs(2)
    resolver = FakeResolver(
        results={"Senior Technical Program Manager 0": ok_resolution("https://acme.com/careers/pe-0")},
        failures={"Senior Technical Program Manager 1": RuntimeError("resolution network failure")},
    )
    with pytest.raises(RuntimeError):
        run_pipeline(app, FakeDiscover(jobs), FakeScorer(scorer_results), resolver)
    with app.app_context():
        connection = db.get_db()
        marker = connection.execute(
            "SELECT 1 FROM recommendation_days WHERE recommendation_date = ?", (DAY,)
        ).fetchone()
    assert marker is None


def test_retry_after_fatal_failure_marks_day_complete(app):
    jobs, scorer_results, resolver_results = _make_n_jobs(3)
    failing_first_scorer = FakeScorer(
        results={},
        failures={
            "Senior Technical Program Manager 0": MissingApiKeyError("anthropic key missing"),
        },
    )
    with pytest.raises(MissingApiKeyError):
        run_pipeline(app, FakeDiscover(jobs), failing_first_scorer, FakeResolver(resolver_results))
    recommendations = run_pipeline(
        app,
        FakeDiscover(jobs),
        FakeScorer(scorer_results),
        FakeResolver(resolver_results),
    )
    assert len(recommendations) == 3
    with app.app_context():
        connection = db.get_db()
        marker = connection.execute(
            "SELECT 1 FROM recommendation_days WHERE recommendation_date = ?", (DAY,)
        ).fetchone()
    assert marker is not None


def test_completed_marker_with_rows_returns_pinned_without_work(app):
    with app.app_context():
        connection = db.get_db()
        job_id = db.upsert_job(connection, make_job(source_job_id="seeded-1"))
        insert_pinned_recommendation(connection, DAY, job_id, score=90)
        connection.execute(
            "INSERT INTO recommendation_days (recommendation_date, completed_at) "
            "VALUES (?, datetime('now'))",
            (DAY,),
        )
        connection.commit()
    discover = FakeDiscover([make_job(source_job_id="seeded-1")])
    scorer = FakeScorer({})
    resolver = FakeResolver({})
    recommendations = run_pipeline(app, discover, scorer, resolver)
    assert len(recommendations) == 1
    assert recommendations[0]["job_id"] == job_id
    assert discover.calls == 0
    assert scorer.calls == []
    assert resolver.calls == []


def test_prior_day_completion_does_not_suppress_today(app):
    with app.app_context():
        connection = db.get_db()
        connection.execute(
            "INSERT INTO recommendation_days (recommendation_date, completed_at) "
            "VALUES (?, datetime('now'))",
            (PRIOR_DAY,),
        )
        connection.commit()
    scorer = FakeScorer({"Senior Technical Program Manager": result(90)})
    resolver = FakeResolver(
        {"Senior Technical Program Manager": ok_resolution("https://acme.com/careers/spm")}
    )
    recommendations = run_pipeline(app, FakeDiscover([make_job()]), scorer, resolver)
    assert scorer.calls == ["Senior Technical Program Manager"]
    assert len(recommendations) == 1
