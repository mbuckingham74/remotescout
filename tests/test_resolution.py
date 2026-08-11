import types

import pytest

from remotescout import db
from remotescout.app import create_app
from remotescout.discovery import DiscoveredJob
from remotescout.resolution import (
    ASHBY,
    CAREERS_PAGE,
    GREENHOUSE,
    LEVER,
    ResolutionResult,
    extract_urls,
    resolve_job,
)


def make_job(**overrides):
    fields = {
        "source": "weworkremotely",
        "source_url": "https://weworkremotely.com/remote-jobs/acme-senior-product-manager",
        "title": "Senior Product Manager (Remote)",
        "employer": "Acme Inc.",
        "description": "Headquarters: https://acme.com/careers",
        "location": "Anywhere in the World",
    }
    fields.update(overrides)
    return DiscoveredJob(**fields)


class FakeResponse:
    def __init__(self, status=200, json=None, html=""):
        self.status = status
        self._json = json
        self.html_content = html

    def json(self):
        return self._json


class FakeFetch:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class RecordingDynamicFetch:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


EMPTY_PAGE = "<html><body></body></html>"


GREENHOUSE_BOARD = "https://boards.greenhouse.io/acme/jobs/1234"
GREENHOUSE_JOBS = {
    "jobs": [
        {
            "id": 1234,
            "title": "Senior Product Manager",
            "company_name": "Acme Inc.",
            "absolute_url": GREENHOUSE_BOARD,
        },
        {
            "id": 9999,
            "title": "Customer Success Manager",
            "company_name": "Acme Inc.",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/9999",
        },
    ]
}

LEVER_POSTING = "https://jobs.lever.co/acme/abc123-def456"
LEVER_JOBS = [
    {
        "id": "abc123-def456",
        "text": "Senior Product Manager",
        "hostedUrl": LEVER_POSTING,
        "state": "published",
    },
    {
        "id": "closed-1",
        "text": "Senior Product Manager",
        "hostedUrl": "https://jobs.lever.co/acme/closed-1",
        "state": "closed",
    },
]

ASHBY_POSTING = "https://jobs.ashbyhq.com/acme/x1y2z3"
ASHBY_JOBS = {
    "jobs": [
        {"id": "x1y2z3", "title": "Senior Product Manager", "isListed": True, "jobUrl": ASHBY_POSTING},
        {"id": "unlisted-1", "title": "Senior Product Manager", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/acme/unlisted-1"},
    ]
}


def test_useful_employer_url_extracted():
    job = make_job(
        description=(
            "Headquarters: https://acme.com/careers\n"
            "To apply: https://weworkremotely.com/remote-jobs/acme-senior-product-manager"
        )
    )
    urls = extract_urls(job)
    assert "https://acme.com/careers" in urls
    assert all("weworkremotely.com" not in url for url in urls)


def test_aggregate_job_board_urls_rejected():
    job = make_job(
        description=(
            "Apply on LinkedIn: https://www.linkedin.com/jobs/view/42 "
            "or Indeed: https://www.indeed.com/viewjob?jk=abc. "
            "Careers: https://acme.com/careers."
        )
    )
    urls = extract_urls(job)
    assert "https://acme.com/careers" in urls
    assert "linkedin.com" not in urls
    assert "indeed.com" not in urls


def test_unrelated_external_url_not_auto_accepted():
    job = make_job(description="Our blog: https://blog.acme.com/engineering or https://acme.com/careers")
    no_links = "<html><body><p>No job links here</p></body></html>"
    fetch = FakeFetch(FakeResponse(html=no_links), FakeResponse(html=no_links))
    dynamic = RecordingDynamicFetch(FakeResponse(html=EMPTY_PAGE), FakeResponse(html=EMPTY_PAGE))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved is False
    assert result.employer_url is None
    assert len(fetch.calls) == 2
    assert len(dynamic.calls) == 2


def test_greenhouse_matching_posting_resolves():
    job = make_job(description=f"Apply: {GREENHOUSE_BOARD}")
    fetch = FakeFetch(FakeResponse(json=GREENHOUSE_JOBS))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved
    assert result.employer_url == GREENHOUSE_BOARD
    assert result.requisition_id == "1234"
    assert result.method == GREENHOUSE
    assert "boards-api.greenhouse.io" in fetch.calls[0]


def test_greenhouse_normalized_title_match_resolves():
    job = make_job(title="Senior Product Manager (Remote)", description=f"Apply: {GREENHOUSE_BOARD}")
    fetch = FakeFetch(FakeResponse(json=GREENHOUSE_JOBS))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved
    assert result.requisition_id == "1234"


def test_greenhouse_missing_posting_unresolved():
    job = make_job(title="Chief Astronaut", description=f"Apply: {GREENHOUSE_BOARD}")
    fetch = FakeFetch(FakeResponse(json=GREENHOUSE_JOBS))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved is False


def test_greenhouse_ambiguous_match_unresolved():
    jobs = {"jobs": GREENHOUSE_JOBS["jobs"] + [dict(GREENHOUSE_JOBS["jobs"][0], id=7777, absolute_url="https://boards.greenhouse.io/acme/jobs/7777")]}
    fetch = FakeFetch(FakeResponse(json=jobs))
    result = resolve_job(make_job(description=f"Apply: {GREENHOUSE_BOARD}"), fetch=fetch)
    assert result.resolved is False


def test_greenhouse_employer_mismatch_unresolved():
    jobs = {"jobs": [dict(GREENHOUSE_JOBS["jobs"][0], company_name="Widgets Corp.")]}
    fetch = FakeFetch(FakeResponse(json=jobs))
    result = resolve_job(make_job(description=f"Apply: {GREENHOUSE_BOARD}"), fetch=fetch)
    assert result.resolved is False


def test_lever_matching_published_posting_resolves():
    job = make_job(description=f"Apply: {LEVER_POSTING}")
    fetch = FakeFetch(FakeResponse(json=LEVER_JOBS))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved
    assert result.employer_url == LEVER_POSTING
    assert result.requisition_id == "abc123-def456"
    assert result.method == LEVER
    assert "api.lever.co" in fetch.calls[0]


def test_lever_unpublished_posting_does_not_resolve():
    fetch = FakeFetch(FakeResponse(json=[p for p in LEVER_JOBS if p["state"] == "closed"]))
    result = resolve_job(make_job(description=f"Apply: {LEVER_POSTING}"), fetch=fetch)
    assert result.resolved is False


def test_lever_missing_posting_unresolved():
    job = make_job(title="Chief Astronaut", description=f"Apply: {LEVER_POSTING}")
    fetch = FakeFetch(FakeResponse(json=LEVER_JOBS))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved is False


def test_lever_ambiguous_match_unresolved():
    postings = LEVER_JOBS + [dict(LEVER_JOBS[0], id="second-1", hostedUrl="https://jobs.lever.co/acme/second-1")]
    fetch = FakeFetch(FakeResponse(json=postings))
    result = resolve_job(make_job(description=f"Apply: {LEVER_POSTING}"), fetch=fetch)
    assert result.resolved is False


def test_ashby_matching_listed_posting_resolves():
    job = make_job(description=f"Apply: https://jobs.ashbyhq.com/acme")
    fetch = FakeFetch(FakeResponse(json=ASHBY_JOBS))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved
    assert result.employer_url == ASHBY_POSTING
    assert result.requisition_id == "x1y2z3"
    assert result.method == ASHBY
    assert "api.ashbyhq.com" in fetch.calls[0]


def test_ashby_unlisted_posting_does_not_resolve():
    jobs = {"jobs": [p for p in ASHBY_JOBS["jobs"] if not p["isListed"]]}
    fetch = FakeFetch(FakeResponse(json=jobs))
    result = resolve_job(make_job(description="Apply: https://jobs.ashbyhq.com/acme"), fetch=fetch)
    assert result.resolved is False


def test_ashby_missing_posting_unresolved():
    job = make_job(title="Chief Astronaut", description="Apply: https://jobs.ashbyhq.com/acme")
    fetch = FakeFetch(FakeResponse(json=ASHBY_JOBS))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved is False


def test_ashby_ambiguous_match_unresolved():
    jobs = {"jobs": ASHBY_JOBS["jobs"] + [dict(ASHBY_JOBS["jobs"][0], id="second-1", jobUrl="https://jobs.ashbyhq.com/acme/second-1")]}
    fetch = FakeFetch(FakeResponse(json=jobs))
    result = resolve_job(make_job(description="Apply: https://jobs.ashbyhq.com/acme"), fetch=fetch)
    assert result.resolved is False


def test_careers_page_clear_match_resolves():
    job = make_job(description="Careers: https://acme.com/careers")
    html = (
        "<html><body>"
        "<a href=\"/jobs/senior-product-manager\">Senior Product Manager</a>"
        "</body></html>"
    )
    fetch = FakeFetch(FakeResponse(html=html))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved
    assert result.employer_url == "https://acme.com/jobs/senior-product-manager"
    assert result.method == CAREERS_PAGE


def test_careers_page_without_job_does_not_resolve():
    job = make_job(description="Careers: https://acme.com/careers")
    fetch = FakeFetch(FakeResponse(html="<html><body><a href=\"/about\">About us</a></body></html>"))
    dynamic = RecordingDynamicFetch(FakeResponse(html=EMPTY_PAGE))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved is False
    assert len(dynamic.calls) == 1


def test_careers_page_ambiguous_matches_do_not_resolve():
    job = make_job(description="Careers: https://acme.com/careers")
    html = (
        "<html><body>"
        "<a href=\"/jobs/a\">Senior Product Manager</a>"
        "<a href=\"/jobs/b\">Senior Product Manager</a>"
        "</body></html>"
    )
    fetch = FakeFetch(FakeResponse(html=html))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved is False


def test_non_200_careers_page_does_not_resolve():
    job = make_job(description="Careers: https://acme.com/careers")
    fetch = FakeFetch(FakeResponse(status=403, html=""))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved is False


def test_ats_failure_falls_back_to_careers_page():
    job = make_job(description=f"Apply: {GREENHOUSE_BOARD} or https://acme.com/careers")
    html = "<html><body><a href=\"/careers/senior-product-manager\">Senior Product Manager</a></body></html>"
    fetch = FakeFetch(
        FakeResponse(status=200, json={"jobs": [{"id": 1, "title": "Other Role", "company_name": "Acme Inc.", "absolute_url": "x"}]}),
        FakeResponse(html=html),
    )
    result = resolve_job(job, fetch=fetch)
    assert result.resolved
    assert result.employer_url == "https://acme.com/careers/senior-product-manager"
    assert result.method == CAREERS_PAGE
    assert len(fetch.calls) == 2


def test_careers_page_greenhouse_link_resolves_structurally():
    job = make_job(description="Careers: https://acme.com/careers")
    html = (
        "<html><body>"
        f"<a href=\"{GREENHOUSE_BOARD}\">Senior Product Manager</a>"
        "</body></html>"
    )
    fetch = FakeFetch(FakeResponse(html=html), FakeResponse(json=GREENHOUSE_JOBS))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved
    assert result.employer_url == GREENHOUSE_BOARD
    assert result.requisition_id == "1234"
    assert result.method == GREENHOUSE
    assert len(fetch.calls) == 2
    assert "boards-api.greenhouse.io" in fetch.calls[1]


def test_careers_page_lever_link_resolves_structurally():
    job = make_job(description="Careers: https://acme.com/careers")
    html = f"<html><body><a href=\"{LEVER_POSTING}\">Senior Product Manager</a></body></html>"
    fetch = FakeFetch(FakeResponse(html=html), FakeResponse(json=LEVER_JOBS))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved
    assert result.employer_url == LEVER_POSTING
    assert result.requisition_id == "abc123-def456"
    assert result.method == LEVER
    assert "api.lever.co" in fetch.calls[1]


def test_careers_page_ashby_link_resolves_structurally():
    job = make_job(description="Careers: https://acme.com/careers")
    html = "<html><body><a href=\"https://jobs.ashbyhq.com/acme\">Senior Product Manager</a></body></html>"
    fetch = FakeFetch(FakeResponse(html=html), FakeResponse(json=ASHBY_JOBS))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved
    assert result.employer_url == ASHBY_POSTING
    assert result.requisition_id == "x1y2z3"
    assert result.method == ASHBY
    assert "api.ashbyhq.com" in fetch.calls[1]


def test_careers_page_ats_link_but_job_not_published_unresolved():
    job = make_job(description="Careers: https://acme.com/careers")
    html = f"<html><body><a href=\"{GREENHOUSE_BOARD}\">Senior Product Manager</a></body></html>"
    fetch = FakeFetch(
        FakeResponse(html=html),
        FakeResponse(json={"jobs": [{"id": 9999, "title": "Customer Success Manager", "company_name": "Acme Inc.", "absolute_url": "x"}]}),
    )
    result = resolve_job(job, fetch=fetch)
    assert result.resolved is False


def test_careers_page_multiple_ats_links_ambiguous_unresolved():
    job = make_job(description="Careers: https://acme.com/careers")
    other_board = "https://boards.greenhouse.io/acme-eu/jobs/5555"
    html = (
        "<html><body>"
        f"<a href=\"{GREENHOUSE_BOARD}\">Senior Product Manager</a>"
        f"<a href=\"{other_board}\">Senior Product Manager</a>"
        "</body></html>"
    )
    fetch = FakeFetch(
        FakeResponse(html=html),
        FakeResponse(json=GREENHOUSE_JOBS),
        FakeResponse(json={"jobs": [dict(GREENHOUSE_JOBS["jobs"][0], id=5555, absolute_url=other_board)]}),
    )
    result = resolve_job(job, fetch=fetch)
    assert result.resolved is False
    assert len(fetch.calls) == 3


def test_appended_location_text_anchor_resolves():
    job = make_job(description="Careers: https://acme.com/careers")
    html = (
        "<html><body>"
        "<a href=\"/jobs/senior-product-manager\">Senior Product Manager\nRemote - United States</a>"
        "</body></html>"
    )
    fetch = FakeFetch(FakeResponse(html=html))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved
    assert result.employer_url == "https://acme.com/jobs/senior-product-manager"
    assert result.method == CAREERS_PAGE


def test_appended_type_text_anchor_resolves():
    job = make_job(description="Careers: https://acme.com/careers")
    html = (
        "<html><body>"
        "<a href=\"/jobs/senior-product-manager\">Senior Product Manager Full-Time</a>"
        "</body></html>"
    )
    fetch = FakeFetch(FakeResponse(html=html))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved
    assert result.employer_url == "https://acme.com/jobs/senior-product-manager"


def test_partial_title_anchor_does_not_resolve():
    job = make_job(title="Group Product Manager, Finance Technology", description="Careers: https://acme.com/careers")
    html = "<html><body><a href=\"/jobs/gpm\">Group Product Manager</a></body></html>"
    fetch = FakeFetch(FakeResponse(html=html))
    dynamic = RecordingDynamicFetch(FakeResponse(html=EMPTY_PAGE))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved is False
    assert len(dynamic.calls) == 1


def test_title_in_middle_of_unrelated_anchor_text_does_not_resolve():
    job = make_job(description="Careers: https://acme.com/careers")
    html = (
        "<html><body>"
        "<a href=\"/about\">Learn more about the Senior Product Manager team</a>"
        "</body></html>"
    )
    fetch = FakeFetch(FakeResponse(html=html))
    dynamic = RecordingDynamicFetch(FakeResponse(html=EMPTY_PAGE))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved is False
    assert len(dynamic.calls) == 1


def test_two_prefix_compatible_anchors_unresolved():
    job = make_job(description="Careers: https://acme.com/careers")
    html = (
        "<html><body>"
        "<a href=\"/jobs/a\">Senior Product Manager Remote - US</a>"
        "<a href=\"/jobs/b\">Senior Product Manager Full-Time</a>"
        "</body></html>"
    )
    fetch = FakeFetch(FakeResponse(html=html))
    result = resolve_job(job, fetch=fetch)
    assert result.resolved is False


def test_resolution_stored_without_touching_source_url(tmp_path):
    app = create_app({"DATABASE_PATH": str(tmp_path / "test.db")})
    with app.app_context():
        connection = db.get_db()
        job = make_job(
            source_url="https://weworkremotely.com/remote-jobs/acme-senior-product-manager"
        )
        job_id = db.upsert_job(connection, job)
        connection.commit()

        db.set_resolution(connection, job_id, GREENHOUSE_BOARD, "1234")
        connection.commit()

        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["employer_url"] == GREENHOUSE_BOARD
        assert row["requisition_id"] == "1234"
        assert row["source_url"] == "https://weworkremotely.com/remote-jobs/acme-senior-product-manager"
        assert row["source"] == "weworkremotely"


def test_static_resolution_never_triggers_dynamic():
    job = make_job(description="Careers: https://acme.com/careers")
    html = "<html><body><a href=\"/jobs/senior-product-manager\">Senior Product Manager</a></body></html>"
    fetch = FakeFetch(FakeResponse(html=html))
    dynamic = RecordingDynamicFetch(FakeResponse(html=EMPTY_PAGE))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved
    assert result.method == CAREERS_PAGE
    assert dynamic.calls == []


def test_static_ats_resolution_never_triggers_dynamic():
    job = make_job(description="Careers: https://acme.com/careers")
    html = f"<html><body><a href=\"{GREENHOUSE_BOARD}\">Senior Product Manager</a></body></html>"
    fetch = FakeFetch(FakeResponse(html=html), FakeResponse(json=GREENHOUSE_JOBS))
    dynamic = RecordingDynamicFetch(FakeResponse(html=EMPTY_PAGE))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved
    assert result.method == GREENHOUSE
    assert dynamic.calls == []


def test_empty_static_page_triggers_one_dynamic_fetch():
    job = make_job(description="Careers: https://acme.com/careers")
    fetch = FakeFetch(FakeResponse(html=EMPTY_PAGE))
    dynamic = RecordingDynamicFetch(FakeResponse(html=EMPTY_PAGE))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved is False
    assert len(dynamic.calls) == 1
    assert dynamic.calls[0] == "https://acme.com/careers"


def test_ambiguous_static_anchors_do_not_trigger_dynamic():
    job = make_job(description="Careers: https://acme.com/careers")
    html = (
        "<html><body>"
        "<a href=\"/jobs/a\">Senior Product Manager Remote - US</a>"
        "<a href=\"/jobs/b\">Senior Product Manager Full-Time</a>"
        "</body></html>"
    )
    fetch = FakeFetch(FakeResponse(html=html))
    dynamic = RecordingDynamicFetch(FakeResponse(html=EMPTY_PAGE))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved is False
    assert dynamic.calls == []


def test_dynamic_page_reveals_greenhouse_link():
    job = make_job(description="Careers: https://acme.com/careers")
    rendered = f"<html><body><a href=\"{GREENHOUSE_BOARD}\">Senior Product Manager</a></body></html>"
    fetch = FakeFetch(FakeResponse(html=EMPTY_PAGE), FakeResponse(json=GREENHOUSE_JOBS))
    dynamic = RecordingDynamicFetch(FakeResponse(html=rendered))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved
    assert result.method == GREENHOUSE
    assert result.employer_url == GREENHOUSE_BOARD
    assert result.requisition_id == "1234"
    assert len(dynamic.calls) == 1
    assert "boards-api.greenhouse.io" in fetch.calls[1]


def test_dynamic_page_reveals_lever_link():
    job = make_job(description="Careers: https://acme.com/careers")
    rendered = f"<html><body><a href=\"{LEVER_POSTING}\">Senior Product Manager</a></body></html>"
    fetch = FakeFetch(FakeResponse(html=EMPTY_PAGE), FakeResponse(json=LEVER_JOBS))
    dynamic = RecordingDynamicFetch(FakeResponse(html=rendered))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved
    assert result.method == LEVER
    assert result.employer_url == LEVER_POSTING
    assert result.requisition_id == "abc123-def456"
    assert "api.lever.co" in fetch.calls[1]


def test_dynamic_page_reveals_ashby_link():
    job = make_job(description="Careers: https://acme.com/careers")
    rendered = "<html><body><a href=\"https://jobs.ashbyhq.com/acme\">Senior Product Manager</a></body></html>"
    fetch = FakeFetch(FakeResponse(html=EMPTY_PAGE), FakeResponse(json=ASHBY_JOBS))
    dynamic = RecordingDynamicFetch(FakeResponse(html=rendered))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved
    assert result.method == ASHBY
    assert result.employer_url == ASHBY_POSTING
    assert result.requisition_id == "x1y2z3"
    assert "api.ashbyhq.com" in fetch.calls[1]


def test_dynamic_page_reveals_generic_title_anchor():
    job = make_job(description="Careers: https://acme.com/careers")
    rendered = "<html><body><a href=\"/jobs/senior-product-manager\">Senior Product Manager</a></body></html>"
    fetch = FakeFetch(FakeResponse(html=EMPTY_PAGE))
    dynamic = RecordingDynamicFetch(FakeResponse(html=rendered))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved
    assert result.method == CAREERS_PAGE
    assert result.employer_url == "https://acme.com/jobs/senior-product-manager"


def test_dynamic_page_stays_empty_unresolved():
    job = make_job(description="Careers: https://acme.com/careers")
    fetch = FakeFetch(FakeResponse(html=EMPTY_PAGE))
    dynamic = RecordingDynamicFetch(FakeResponse(html="<html><body><a href=\"/about\">About</a></body></html>"))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved is False
    assert len(dynamic.calls) == 1


def test_dynamic_page_ambiguous_unresolved():
    job = make_job(description="Careers: https://acme.com/careers")
    rendered = (
        "<html><body>"
        "<a href=\"/jobs/a\">Senior Product Manager Remote - US</a>"
        "<a href=\"/jobs/b\">Senior Product Manager Full-Time</a>"
        "</body></html>"
    )
    fetch = FakeFetch(FakeResponse(html=EMPTY_PAGE))
    dynamic = RecordingDynamicFetch(FakeResponse(html=rendered))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved is False
    assert len(dynamic.calls) == 1


def test_dynamic_fetch_exception_unresolved_no_second_attempt():
    job = make_job(description="Careers: https://acme.com/careers")
    fetch = FakeFetch(FakeResponse(html=EMPTY_PAGE))
    dynamic = RecordingDynamicFetch(RuntimeError("browser launch failed"))
    result = resolve_job(job, fetch=fetch, dynamic_fetch=dynamic)
    assert result.resolved is False
    assert len(dynamic.calls) == 1
