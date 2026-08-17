from remotescout import db, filtering, resolution, scoring
from remotescout import resume as resume_module
from remotescout.business_time import business_today
from remotescout.config import load_config
from remotescout.discovery import weworkremotely

RECOMMENDATION_LIMIT = 3


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
):
    """Build and persist the day's up-to-three best verified recommendations.

    Every genuine attempt creates a durable :class:`pipeline_runs` record,
    one :class:`pipeline_source_attempts` record per source invocation,
    and one :class:`pipeline_run_jobs` record per discovered job. The
    pipeline_runs row is committed before any expensive work begins so a
    crash leaves recoverable evidence.
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

        plausible = []
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
            if _applied_before_scoring(evidence, job_id, job):
                db.mark_pipeline_run_job_suppressed_pre_score(
                    connection, run_id, job_id
                )
                continue
            plausible.append((job_id, job))
        connection.commit()

        scored = []
        for job_id, job in plausible:
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
