import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from remotescout.discovery.models import DiscoveredJob

GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"
CAREERS_PAGE = "careers_page"

_AGGREGATE_DOMAINS = (
    "weworkremotely.com", "indeed.com", "linkedin.com", "glassdoor.com",
    "ziprecruiter.com", "monster.com", "careerbuilder.com", "dice.com",
    "remoteok.com", "remotive.com", "flexjobs.com", "wellfound.com",
    "angel.co", "builtin.com", "jobot.com", "simplyhired.com",
    "snagajob.com", "upwork.com", "fiverr.com", "instagram.com",
    "facebook.com", "fb.me", "twitter.com", "x.com", "youtube.com",
    "tiktok.com", "reddit.com", "medium.com", "substack.com",
    "discord.com", "whatsapp.com",
)

_ATS_PATTERNS = (
    (GREENHOUSE, re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)")),
    (LEVER, re.compile(r"jobs\.lever\.co/([^/?#]+)")),
    (ASHBY, re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)")),
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+")


@dataclass
class ResolutionResult:
    resolved: bool
    employer_url: str | None = None
    requisition_id: str | None = None
    method: str | None = None


def default_fetch(url, timeout=20):
    from scrapling import Fetcher

    return Fetcher.get(url, timeout=timeout)


def default_dynamic_fetch(url, timeout=30):
    from scrapling import DynamicFetcher

    return DynamicFetcher.fetch(url, timeout=timeout * 1000, retries=1)


def _normalize(text):
    text = (text or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"\([^)]*remote[^)]*\)", " ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _is_aggregate(url):
    host = urlsplit(url).netloc.lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    return any(host == domain or host.endswith("." + domain) for domain in _AGGREGATE_DOMAINS)


def extract_urls(job):
    candidates = []
    for field in (job.description or "", job.location or ""):
        for match in _URL_RE.finditer(field):
            url = match.group(0).rstrip(".,;:")
            if _is_aggregate(url):
                continue
            if url not in candidates:
                candidates.append(url)
    return candidates


def _recognize_ats(url):
    for family, pattern in _ATS_PATTERNS:
        match = pattern.search(url)
        if match:
            return family, match.group(1)
    return None


def _match_single(postings, job, url_key, id_key, method, title_key="title"):
    normalized = _normalize(job.title)
    matches = [p for p in postings if _normalize(p.get(title_key) or "") == normalized]
    if len(matches) != 1:
        return None
    posting = matches[0]
    return ResolutionResult(
        resolved=True,
        employer_url=posting.get(url_key),
        requisition_id=str(posting.get(id_key)),
        method=method,
    )


def _employers_match(aggregate, company):
    aggregate = _normalize(aggregate)
    company = _normalize(company)
    if not aggregate or not company:
        return False
    return aggregate == company or aggregate in company or company in aggregate


def _resolve_greenhouse(identifier, job, fetch):
    response = fetch(f"https://boards-api.greenhouse.io/v1/boards/{identifier}/jobs")
    if response.status != 200:
        return None
    data = response.json()
    postings = [
        p for p in data.get("jobs") or []
        if _employers_match(job.employer, p.get("company_name"))
    ]
    return _match_single(postings, job, "absolute_url", "id", GREENHOUSE)


def _resolve_lever(identifier, job, fetch):
    response = fetch(f"https://api.lever.co/v0/postings/{identifier}?mode=json")
    if response.status != 200:
        return None
    postings = [
        p for p in response.json() or []
        if p.get("state") == "published"
    ]
    return _match_single(postings, job, "hostedUrl", "id", LEVER, title_key="text")


def _resolve_ashby(identifier, job, fetch):
    response = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{identifier}")
    if response.status != 200:
        return None
    data = response.json()
    postings = [
        p for p in data.get("jobs") or []
        if p.get("isListed") is True
    ]
    return _match_single(postings, job, "jobUrl", "id", ASHBY)


class _AnchorCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self._current = None
        self._parts = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "").strip()
            if href and not href.startswith(("#", "mailto:", "javascript:", "tel:")):
                self._current = href
                self._parts = []

    def handle_data(self, data):
        if self._current is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._current is not None:
            self.links.append((self._current, "".join(self._parts).strip()))
            self._current = None


def _resolve_from_page_ats_links(links, job, fetch):
    boards = {}
    for href, _text in links:
        recognized = _recognize_ats(href)
        if recognized is not None:
            boards[recognized] = True
    results = []
    seen = set()
    for family, identifier in boards:
        result = _resolve_ats(family, identifier, job, fetch)
        if result is not None and (result.employer_url, result.requisition_id) not in seen:
            seen.add((result.employer_url, result.requisition_id))
            results.append(result)
    return results


def _resolve_generic_anchor(links, base_url, job):
    target = _normalize(job.title)
    matches = []
    for href, text in links:
        if _recognize_ats(href) is not None:
            continue
        normalized = _normalize(text)
        if not normalized:
            continue
        if normalized == target or normalized.startswith(target + " "):
            matches.append(urljoin(base_url, href))
    matches = list(dict.fromkeys(matches))
    if len(matches) != 1:
        return None
    return ResolutionResult(resolved=True, employer_url=matches[0], method=CAREERS_PAGE)


def _collect_links(html):
    collector = _AnchorCollector()
    collector.feed(html)
    return collector.links


def _has_job_signals(links, job):
    target = _normalize(job.title)
    for href, text in links:
        if _recognize_ats(href) is not None:
            return True
        normalized = _normalize(text)
        if normalized and (normalized == target or normalized.startswith(target + " ")):
            return True
    return False


def _resolve_anchors(links, base_url, job, fetch):
    ats_results = _resolve_from_page_ats_links(links, job, fetch)
    if ats_results:
        if len(ats_results) == 1:
            return ats_results[0]
        return None
    return _resolve_generic_anchor(links, base_url, job)


def _resolve_careers_page(url, job, fetch, dynamic_fetch=None):
    response = fetch(url)
    if response.status != 200:
        return None
    html = getattr(response, "html_content", None)
    if not html:
        return None
    links = _collect_links(html)
    result = _resolve_anchors(links, url, job, fetch)
    if result is not None:
        return result
    if _has_job_signals(links, job) or dynamic_fetch is None:
        return None
    try:
        rendered = dynamic_fetch(url)
    except Exception:
        return None
    if rendered.status != 200:
        return None
    rendered_html = getattr(rendered, "html_content", None)
    if not rendered_html:
        return None
    return _resolve_anchors(_collect_links(rendered_html), url, job, fetch)


def resolve_job(job: DiscoveredJob, fetch=None, dynamic_fetch=None, timeout=20) -> ResolutionResult:
    fetch = fetch or (lambda url: default_fetch(url, timeout=timeout))
    dynamic_fetch = dynamic_fetch or default_dynamic_fetch
    urls = extract_urls(job)
    for url in urls:
        recognized = _recognize_ats(url)
        if recognized:
            family, identifier = recognized
            result = _resolve_ats(family, identifier, job, fetch)
            if result is not None:
                return result
    for url in urls:
        if _recognize_ats(url) is None:
            result = _resolve_careers_page(url, job, fetch, dynamic_fetch)
            if result is not None:
                return result
    return ResolutionResult(resolved=False)


def _resolve_ats(family, identifier, job, fetch):
    if family == GREENHOUSE:
        return _resolve_greenhouse(identifier, job, fetch)
    if family == LEVER:
        return _resolve_lever(identifier, job, fetch)
    if family == ASHBY:
        return _resolve_ashby(identifier, job, fetch)
    return None
