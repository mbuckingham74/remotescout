import re
import urllib.request
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from xml.etree import ElementTree

from remotescout import db
from remotescout.discovery.models import DiscoveredJob

FEED_URL = "https://weworkremotely.com/remote-jobs.rss"
USER_AGENT = "RemoteScout/0.1 (+https://remotescout.forkstech.com)"
SOURCE = "weworkremotely"


def fetch_feed(timeout=30):
    request = urllib.request.Request(FEED_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "p",
        "br",
        "li",
        "div",
        "ul",
        "ol",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
    }

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")


def _html_to_text(html):
    extractor = _TextExtractor()
    extractor.feed(html)
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in "".join(extractor.parts).split("\n")
    ]
    return "\n".join(line for line in lines if line)


def _parse_item(item):
    title = item.findtext("title")
    link = item.findtext("link")
    if not title or not link:
        return None
    if ": " in title:
        employer, job_title = (part.strip() for part in title.split(": ", 1))
    else:
        employer, job_title = title, title
    guid = item.findtext("guid") or link
    source_job_id = guid.rsplit("/", 1)[-1] or None
    region = (item.findtext("region") or "").strip()
    country = (item.findtext("country") or "").strip()
    state = (item.findtext("state") or "").strip()
    location = next((value for value in (region, country, state) if value), None)
    posted_at = None
    pub_date = item.findtext("pubDate")
    if pub_date:
        try:
            posted_at = parsedate_to_datetime(pub_date).date().isoformat()
        except (TypeError, ValueError):
            posted_at = None
    description = item.findtext("description")
    return DiscoveredJob(
        source=SOURCE,
        source_job_id=source_job_id,
        source_url=link.strip(),
        title=job_title,
        employer=employer,
        location=location,
        description=_html_to_text(description) if description else "",
        posted_at=posted_at,
    )


def parse_feed(feed):
    root = ElementTree.fromstring(feed)
    return [job for job in (_parse_item(item) for item in root.iter("item")) if job]


def fetch_jobs(timeout=30):
    return parse_feed(fetch_feed(timeout=timeout))


def ingest(connection, jobs=None, timeout=30):
    if jobs is None:
        jobs = fetch_jobs(timeout=timeout)
    for job in jobs:
        db.upsert_job(connection, job)
    connection.commit()
    return {"fetched": len(jobs), "saved": len(jobs)}
