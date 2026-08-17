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
):
    """Build and persist the day's up-to-three best verified recommendations."""
    day = recommendation_date or business_today().isoformat()

    pinned = db.get_recommendations(connection, day)
    if pinned:
        return list(pinned)
    if db.is_recommendation_day_complete(connection, day):
        return list(db.get_recommendations(connection, day))

    config = load_config()
    if discover is None:
        discover = weworkremotely.fetch_jobs
    if resume_text is None:
        resume_text = resume_module.extract_resume_text(config["RESUME_PATH"])
    if score is None:
        score = lambda job, text: scoring.score_job(job, text)
    if resolve is None:
        resolve = resolution.resolve_job
    if threshold is None:
        threshold = config["RECOMMENDATION_THRESHOLD"]

    candidates = []
    for job in discover():
        job_id = db.upsert_job(connection, job)
        candidates.append((job_id, job))
    connection.commit()

    evidence = _AppliedEvidence(db.get_applied_jobs(connection))

    plausible = []
    for job_id, job in candidates:
        if not filtering.filter_job(job).passed:
            continue
        if _applied_before_scoring(evidence, job_id, job):
            continue
        plausible.append((job_id, job))

    scored = []
    for job_id, job in plausible:
        try:
            result = score(job, resume_text)
        except scoring.MissingApiKeyError:
            raise
        except scoring.ScoringError:
            continue
        db.set_job_score(connection, job_id, result.score, result.fit_explanation)
        connection.commit()
        if not scoring.meets_threshold(result, threshold):
            continue
        scored.append((job_id, job, result))

    ranked = sorted(scored, key=lambda item: (-item[2].score, item[0]))

    accepted = []
    accepted_urls = set()
    accepted_requisitions = set()
    for job_id, job, result in ranked:
        resolved = resolve(job)
        if not resolved.resolved:
            continue
        db.set_resolution(
            connection, job_id, resolved.employer_url, resolved.requisition_id
        )
        connection.commit()
        if _applied_after_resolution(evidence, resolved, job.employer):
            continue
        if _duplicate_canonical(
            accepted_urls, accepted_requisitions, resolved, job.employer
        ):
            continue
        accepted.append((job_id, result))
        _remember_canonical(accepted_urls, accepted_requisitions, resolved, job.employer)
        if len(accepted) >= RECOMMENDATION_LIMIT:
            break

    try:
        for rank, (job_id, result) in enumerate(accepted, start=1):
            connection.execute(
                "INSERT INTO recommendations (date, rank, job_id, score, explanation) "
                "VALUES (?, ?, ?, ?, ?)",
                (day, rank, job_id, result.score, result.fit_explanation),
            )
        db.mark_recommendation_day_complete(connection, day)
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return list(db.get_recommendations(connection, day))
