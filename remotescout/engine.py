from remotescout import db, filtering, ranking, resolution, scoring, targeting
from remotescout import resume as resume_module
from remotescout.business_time import business_today
from remotescout.config import load_config
from remotescout.discovery import weworkremotely

RECOMMENDATION_LIMIT = 3
SCORING_BUDGET_KEYWORD = "scoring_budget"


def _normalize_employer(name):
    return " ".join((name or "").lower().split())


class _AppliedEvidence:
    def __init__(self, rows):
        self.job_ids = set()
        self.source_ids = set()
        self.identity_keys = set()
        self.employer_urls = set()
        self.requisitions = set()
        for row in rows:
            self.job_ids.add(row["job_id"])
            if row["source"] and row["source_job_id"]:
                self.source_ids.add((row["source"], row["source_job_id"]))
            if row["identity_key"]:
                self.identity_keys.add(row["identity_key"])
            if row["employer_url"]:
                self.employer_urls.add(db.normalize_employer_url(row["employer_url"]))
            if row["employer"] and row["requisition_id"]:
                self.requisitions.add(
                    (_normalize_employer(row["employer"]), row["requisition_id"])
                )


def _applied_before_scoring(evidence, job_id, job):
    if job_id in evidence.job_ids:
        return True
    if job.source_job_id and (job.source, job.source_job_id) in evidence.source_ids:
        return True
    identity = db.identity_key(job)
    return bool(identity and identity in evidence.identity_keys)


def _applied_after_resolution(evidence, resolved, employer):
    url_key = _canonical_url_key(resolved.employer_url)
    if url_key and url_key in evidence.employer_urls:
        return True
    return bool(
        resolved.requisition_id
        and (_normalize_employer(employer), resolved.requisition_id)
        in evidence.requisitions
    )


def _canonical_url_key(url):
    return db.normalize_employer_url(url) if url else None


def _duplicate_canonical(accepted_urls, accepted_requisitions, resolved, employer):
    url_key = _canonical_url_key(resolved.employer_url)
    if url_key and url_key in accepted_urls:
        return True
    if resolved.requisition_id and employer:
        requisition_key = (_normalize_employer(employer), resolved.requisition_id)
        if requisition_key in accepted_requisitions:
            return True
    return False


def _remember_canonical(accepted_urls, accepted_requisitions, resolved, employer):
    url_key = _canonical_url_key(resolved.employer_url)
    if url_key:
        accepted_urls.add(url_key)
    if resolved.requisition_id and employer:
        accepted_requisitions.add(
            (_normalize_employer(employer), resolved.requisition_id)
        )


def _budget_slice(ranked, budget):
    """Return the budgeted slice of the ranked candidate list.

    Split out as a module-level function so adversarial tests can
    monkey-patch it to demonstrate that the budget gate fails loudly
    if any caller attempts to bypass it.
    """
    return ranked[:budget]


def build_daily_recommendations(
    connection,
    recommendation_date=None,
    *,
    discover=None,
    score=None,
    resolve=None,
    resume_text=None,
    threshold=None,
    source_id=None,
    scoring_budget=None,
):
    """Build and persist the day's up-to-three best verified recommendations.

    Every genuine attempt creates a durable :class:`pipeline_runs` record,
    one :class:`pipeline_source_attempts` record per source invocation,
    and one :class:`pipeline_run_jobs` record per discovered job. The
    pipeline_runs row is committed before any expensive work begins so a
    crash leaves recoverable evidence.

    Package 8 cost-containment ordering (before any paid scoring):

    1. Discover broadly.
    2. Suppress jobs already successfully scored in any prior run on a
       different business date (already-processed).
    3. Apply the existing deterministic hard-reject filter.
    4. Apply the existing deterministic applied-job suppression.
    5. Apply the new positive target-role gate.
    6. For gate survivors, reuse the durable successful scoring row from
       any prior same-date attempt when one exists. A reused job is
       never charged to the scoring budget and never reaches
       :func:`scoring.score_job`; the prior ``score``,
       ``fit_explanation``, ``strengths``, ``gaps``, and
       ``meets_threshold`` are reused verbatim so the job can continue
       into threshold/resolution/recommendation.
    7. Rank remaining (non-reused) candidates deterministically by
       relevance.
    8. Apply the hard scoring budget; mark excess rows as
       budget-deferred. Only the budget survivors reach
       :func:`scoring.score_job`.
    9. Reused and freshly scored survivors are merged into a single
       score-ordered stream that feeds resolution and recommendation.
    """
    day = recommendation_date or business_today().isoformat()

    pinned = db.get_recommendations(connection, day)
    if pinned:
        return list(pinned)
    if db.is_recommendation_day_complete(connection, day):
        return list(db.get_recommendations(connection, day))

    config = load_config()
    if discover is None:
        discover = weworkremotely.fetch_jobs
    if source_id is None:
        source_id = weworkremotely.SOURCE
    if resume_text is None:
        resume_text = resume_module.extract_resume_text(config["RESUME_PATH"])
    if score is None:
        score = lambda job, text: scoring.score_job(job, text)
    if resolve is None:
        resolve = resolution.resolve_job
    if threshold is None:
        threshold = config["RECOMMENDATION_THRESHOLD"]
    if scoring_budget is None:
        scoring_budget = config["SCORING_BUDGET"]
    scoring_model = config["ANTHROPIC_MODEL"]

    run_id = db.create_pipeline_run(connection, day, threshold, scoring_model)
    connection.commit()

    source_attempt_id = db.create_pipeline_source_attempt(
        connection, run_id, source_id
    )
    connection.commit()

    try:
        try:
            discovered_jobs = list(discover())
        except Exception as error:
            db.finish_pipeline_source_attempt_failed(
                connection,
                source_attempt_id,
                type(error).__name__,
                str(error),
            )
            connection.commit()
            raise

        db.finish_pipeline_source_attempt_succeeded(
            connection, source_attempt_id, len(discovered_jobs)
        )
        connection.commit()

        candidates = []
        for job in discovered_jobs:
            job_id = db.upsert_job(connection, job)
            candidates.append((job_id, job))
        connection.commit()

        evidence = _AppliedEvidence(db.get_applied_jobs(connection))
        already_processed = db.get_already_processed_job_ids(connection, day)
        same_day_reused = db.get_same_day_reused_results(connection, day, run_id)

        filter_passed_candidates = []
        for job_id, job in candidates:
            filter_result = filtering.filter_job(job)
            db.record_pipeline_run_job(
                connection,
                run_id,
                job_id,
                source_id,
                filter_result.passed,
                filter_result.reasons,
            )
            if not filter_result.passed:
                continue
            if job_id in already_processed:
                db.mark_pipeline_run_job_suppressed_already_processed(
                    connection, run_id, job_id
                )
                continue
            if _applied_before_scoring(evidence, job_id, job):
                db.mark_pipeline_run_job_suppressed_pre_score(
                    connection, run_id, job_id
                )
                continue
            filter_passed_candidates.append((job_id, job))
        connection.commit()

        gate_survivors = []
        reused_survivors = []
        for job_id, job in filter_passed_candidates:
            gate = targeting.evaluate(job)
            db.record_pipeline_run_job_positive_gate(
                connection,
                run_id,
                job_id,
                gate.passed,
                gate.reason if gate.passed else "outside_target_role_families",
                gate.rank_points,
            )
            if not gate.passed:
                continue
            if job_id in same_day_reused:
                reused_payload = same_day_reused[job_id]
                db.mark_pipeline_run_job_scoring_reused(
                    connection,
                    run_id,
                    job_id,
                    reused_payload["score"],
                    reused_payload["fit_explanation"],
                    reused_payload["strengths"],
                    reused_payload["gaps"],
                    reused_payload["meets_threshold"],
                )
                reused_survivors.append((job_id, job, reused_payload))
                continue
            gate_survivors.append((job_id, job, gate.reason, gate.rank_points))
        connection.commit()

        ranked = ranking.rank_candidates(
            [(job_id, job) for job_id, job, _reason, _points in gate_survivors]
        )
        ranked_for_run = [
            (candidate.job_id, candidate.job, candidate.gate_reason, candidate.relevance_score)
            for candidate in ranked
        ]

        budgeted = _budget_slice(ranked_for_run, scoring_budget)
        deferred = ranked_for_run[scoring_budget:]
        for job_id, _job, _reason, _points in deferred:
            db.mark_pipeline_run_job_suppressed_scoring_budget(
                connection, run_id, job_id
            )
        connection.commit()

        scored = []
        for job_id, job, _reason, _points in budgeted:
            try:
                result = score(job, resume_text)
            except scoring.MissingApiKeyError as error:
                db.record_pipeline_run_job_scoring_error(
                    connection,
                    run_id,
                    job_id,
                    type(error).__name__,
                    str(error),
                )
                connection.commit()
                raise
            except scoring.ScoringError as error:
                db.record_pipeline_run_job_scoring_error(
                    connection,
                    run_id,
                    job_id,
                    type(error).__name__,
                    str(error),
                )
                connection.commit()
                continue
            except Exception as error:
                db.record_pipeline_run_job_scoring_error(
                    connection,
                    run_id,
                    job_id,
                    type(error).__name__,
                    str(error),
                )
                connection.commit()
                raise
            db.set_job_score(connection, job_id, result.score, result.fit_explanation)
            db.record_pipeline_run_job_scoring_succeeded(
                connection,
                run_id,
                job_id,
                result.score,
                result.fit_explanation,
                result.strengths,
                result.gaps,
                scoring.meets_threshold(result, threshold),
            )
            connection.commit()
            if not scoring.meets_threshold(result, threshold):
                continue
            scored.append((job_id, job, result))

        for job_id, job, reused_payload in reused_survivors:
            if not reused_payload["meets_threshold"]:
                continue
            reused_result = scoring.ScoreResult(
                score=reused_payload["score"],
                fit_explanation=reused_payload["fit_explanation"],
                strengths=reused_payload["strengths"],
                gaps=reused_payload["gaps"],
            )
            scored.append((job_id, job, reused_result))

        ranked = sorted(scored, key=lambda item: (-item[2].score, item[0]))

        accepted = []
        accepted_urls = set()
        accepted_requisitions = set()
        for job_id, job, result in ranked:
            try:
                resolved = resolve(job)
            except Exception as error:
                db.mark_pipeline_run_job_resolution_attempted(
                    connection, run_id, job_id
                )
                connection.commit()
                raise
            db.record_pipeline_run_job_resolution(
                connection,
                run_id,
                job_id,
                resolved.resolved,
                resolved.employer_url,
                resolved.requisition_id,
                resolved.method,
            )
            if not resolved.resolved:
                connection.commit()
                continue
            db.set_resolution(
                connection, job_id, resolved.employer_url, resolved.requisition_id
            )
            connection.commit()
            if _applied_after_resolution(evidence, resolved, job.employer):
                db.mark_pipeline_run_job_suppressed_post_resolution(
                    connection, run_id, job_id
                )
                continue
            if _duplicate_canonical(
                accepted_urls, accepted_requisitions, resolved, job.employer
            ):
                db.mark_pipeline_run_job_suppressed_canonical_duplicate(
                    connection, run_id, job_id
                )
                continue
            accepted.append((job_id, result))
            _remember_canonical(accepted_urls, accepted_requisitions, resolved, job.employer)
            if len(accepted) >= RECOMMENDATION_LIMIT:
                break
        connection.commit()

        try:
            for rank, (job_id, result) in enumerate(accepted, start=1):
                connection.execute(
                    "INSERT INTO recommendations (date, rank, job_id, score, explanation) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (day, rank, job_id, result.score, result.fit_explanation),
                )
                db.set_pipeline_run_job_accepted_rank(
                    connection, run_id, job_id, rank
                )
            db.mark_recommendation_day_complete(connection, day)
            db.finish_pipeline_run_succeeded(connection, run_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        return list(db.get_recommendations(connection, day))
    except Exception as error:
        try:
            db.finish_pipeline_run_failed(
                connection,
                run_id,
                type(error).__name__,
                str(error),
            )
            connection.commit()
        except Exception:
            pass
        raise
