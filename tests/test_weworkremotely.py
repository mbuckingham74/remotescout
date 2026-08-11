import pytest

from remotescout import db
from remotescout.app import create_app
from remotescout.discovery import DiscoveredJob, weworkremotely

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss">
  <channel>
    <title>We Work Remotely: Remote jobs in design, programming, marketing and more</title>
    <link>https://weworkremotely.com/remote-jobs.rss</link>
    <description>We Work Remotely: Remote jobs in design, programming, marketing and more</description>
    <language>en-US</language>
    <ttl>60</ttl>
    <item>
      <title>Hospitable: Customer Advocate Lead (North America - Remote)</title>
      <region>Anywhere in the World</region>
      <country></country>
      <state></state>
      <skills>Documentation, Technical Support, Troubleshooting, Customer Support, Onboarding, Leadership, and Team Management</skills>
      <category>Customer Support</category>
      <type>Full-Time</type>
      <description>

&lt;p&gt;
  &lt;strong&gt;Headquarters:&lt;/strong&gt; United States
    &lt;br /&gt;&lt;strong&gt;URL:&lt;/strong&gt; &lt;a href="https://hospitable.com/careers"&gt;https://hospitable.com/careers&lt;/a&gt;
&lt;/p&gt;

&lt;p&gt;As the Customer Advocate Lead, you will lead and empower our US support team to deliver an exceptional customer experience.&amp;nbsp;&lt;/p&gt;
&lt;p&gt;To accomplish this, you will:&lt;/p&gt;
&lt;ul&gt;
&lt;li&gt;Manage queue distribution in our chat support system and workload.&lt;/li&gt;
&lt;li&gt;Coach and develop team members through regular 1:1s.&lt;/li&gt;
&lt;/ul&gt;

      </description>
      <pubDate>Tue, 11 Aug 2026 12:40:35 +0000</pubDate>
      <expires_at>Thu, 10 Sep 2026 12:40:35 +0000</expires_at>
      <guid>https://weworkremotely.com/remote-jobs/hospitable-customer-advocate-lead-north-america-remote-1</guid>
      <link>https://weworkremotely.com/remote-jobs/hospitable-customer-advocate-lead-north-america-remote-1</link>
    </item>
    <item>
      <title>Harney &amp; Sons Fine Teas: Associate – ERP Operations (SAP Business One)</title>
      <region>Anywhere in the World</region>
      <country></country>
      <state></state>
      <skills></skills>
      <category>ERP</category>
      <type>Full-Time</type>
      <description>

&lt;p&gt;We are looking for an Associate to support ERP operations in SAP Business One.&lt;/p&gt;

      </description>
      <pubDate>Mon, 10 Aug 2026 09:15:00 +0000</pubDate>
      <expires_at>Wed, 09 Sep 2026 09:15:00 +0000</expires_at>
      <guid>https://weworkremotely.com/remote-jobs/harney-sons-fine-teas-associate-erp-operations-sap-business-one</guid>
      <link>https://weworkremotely.com/remote-jobs/harney-sons-fine-teas-associate-erp-operations-sap-business-one</link>
    </item>
    <item>
      <title>Justworks: International Consultant, APAC</title>
      <region></region>
      <country>🇨🇦 Canada and 🇺🇸 United States of America</country>
      <state>California</state>
      <skills></skills>
      <category>Other</category>
      <type>Full-Time</type>
      <description>

&lt;p&gt;Consulting role supporting APAC operations.&lt;/p&gt;

      </description>
      <pubDate>Sun, 09 Aug 2026 18:30:22 +0000</pubDate>
      <expires_at>Tue, 08 Sep 2026 18:30:22 +0000</expires_at>
      <guid>https://weworkremotely.com/remote-jobs/justworks-international-consultant-apac</guid>
      <link>https://weworkremotely.com/remote-jobs/justworks-international-consultant-apac</link>
    </item>
    <item>
      <title>Acme Corp: Backend Engineer</title>
      <region></region>
      <country></country>
      <state></state>
      <skills></skills>
      <category>Back-End Programming</category>
      <type>Full-Time</type>
      <description>

&lt;p&gt;Build and maintain backend services.&lt;/p&gt;

      </description>
      <guid>https://weworkremotely.com/remote-jobs/acme-corp-backend-engineer</guid>
      <link>https://weworkremotely.com/remote-jobs/acme-corp-backend-engineer</link>
    </item>
    <item>
      <title>Independent Rollup</title>
      <region></region>
      <country></country>
      <state></state>
      <skills></skills>
      <category>Other</category>
      <type>Full-Time</type>
      <description>

&lt;p&gt;A posting without the standard employer prefix.&lt;/p&gt;

      </description>
      <guid>https://weworkremotely.com/remote-jobs/independent-rollup</guid>
      <link>https://weworkremotely.com/remote-jobs/independent-rollup</link>
    </item>
  </channel>
</rss>
"""


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE_PATH": str(tmp_path / "test.db")})


@pytest.fixture
def connection(app):
    with app.app_context():
        yield db.get_db()


def test_valid_feed_parses():
    jobs = weworkremotely.parse_feed(FEED.encode())
    assert isinstance(jobs, list)
    assert all(isinstance(job, DiscoveredJob) for job in jobs)


def test_multiple_entries_returned():
    assert len(weworkremotely.parse_feed(FEED.encode())) == 5


def test_employer_normalized():
    jobs = {job.source_job_id: job for job in weworkremotely.parse_feed(FEED.encode())}
    assert jobs["hospitable-customer-advocate-lead-north-america-remote-1"].employer == "Hospitable"
    assert jobs["harney-sons-fine-teas-associate-erp-operations-sap-business-one"].employer == "Harney & Sons Fine Teas"


def test_job_title_normalized():
    jobs = {job.source_job_id: job for job in weworkremotely.parse_feed(FEED.encode())}
    assert jobs["hospitable-customer-advocate-lead-north-america-remote-1"].title == "Customer Advocate Lead (North America - Remote)"
    assert jobs["harney-sons-fine-teas-associate-erp-operations-sap-business-one"].title == "Associate – ERP Operations (SAP Business One)"


def test_source_is_weworkremotely():
    jobs = weworkremotely.parse_feed(FEED.encode())
    assert all(job.source == "weworkremotely" for job in jobs)


def test_source_url_retained():
    jobs = weworkremotely.parse_feed(FEED.encode())
    assert jobs[0].source_url == "https://weworkremotely.com/remote-jobs/hospitable-customer-advocate-lead-north-america-remote-1"


def test_stable_source_id_retained():
    jobs = weworkremotely.parse_feed(FEED.encode())
    assert jobs[0].source_job_id == "hospitable-customer-advocate-lead-north-america-remote-1"
    assert jobs[1].source_job_id == "harney-sons-fine-teas-associate-erp-operations-sap-business-one"


def test_guid_missing_falls_back_to_link():
    jobs = weworkremotely.parse_feed(FEED.encode())
    assert jobs[3].source_job_id == "acme-corp-backend-engineer"


def test_description_retained_as_text():
    job = weworkremotely.parse_feed(FEED.encode())[0]
    assert "As the Customer Advocate Lead, you will lead and empower our US support team to deliver an exceptional customer experience." in job.description
    assert "Manage queue distribution in our chat support system and workload." in job.description
    assert "<strong>" not in job.description
    assert "<p>" not in job.description
    assert "&nbsp;" not in job.description


def test_location_only_when_available():
    jobs = {job.source_job_id: job for job in weworkremotely.parse_feed(FEED.encode())}
    assert jobs["hospitable-customer-advocate-lead-north-america-remote-1"].location == "Anywhere in the World"
    assert jobs["justworks-international-consultant-apac"].location == "🇨🇦 Canada and 🇺🇸 United States of America"
    assert jobs["acme-corp-backend-engineer"].location is None


def test_pub_date_normalized():
    jobs = {job.source_job_id: job for job in weworkremotely.parse_feed(FEED.encode())}
    assert jobs["hospitable-customer-advocate-lead-north-america-remote-1"].posted_at == "2026-08-11"
    assert jobs["harney-sons-fine-teas-associate-erp-operations-sap-business-one"].posted_at == "2026-08-10"


def test_missing_optional_fields_do_not_break_parsing():
    jobs = {job.source_job_id: job for job in weworkremotely.parse_feed(FEED.encode())}
    missing = jobs["acme-corp-backend-engineer"]
    assert missing.posted_at is None
    assert missing.location is None
    assert missing.description
    assert missing.title == "Backend Engineer"
    plain = jobs["independent-rollup"]
    assert plain.employer == "Independent Rollup"
    assert plain.title == "Independent Rollup"


def test_repeated_ingestion_does_not_duplicate(connection):
    jobs = weworkremotely.parse_feed(FEED.encode())
    first = weworkremotely.ingest(connection, jobs)
    assert first == {"fetched": 5, "saved": 5}
    second = weworkremotely.ingest(connection, jobs)
    assert second == {"fetched": 5, "saved": 5}
    count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count == 5
