from remotescout.discovery import DiscoveredJob
from remotescout.filtering import filter_job, filter_jobs


def make_job(**overrides):
    fields = {
        "source": "test",
        "source_url": "https://example.com/jobs/1",
        "title": "Product Manager",
        "employer": "Test Co.",
        "description": "Fully remote role.",
    }
    fields.update(overrides)
    return DiscoveredJob(**fields)


def test_plumbing_role_rejected():
    result = filter_job(make_job(title="Plumber"))
    assert not result.passed
    assert result.reasons == ["unrelated_occupation"]


def test_nursing_role_rejected():
    result = filter_job(make_job(title="Registered Nurse (Remote)"))
    assert not result.passed
    assert result.reasons == ["unrelated_occupation"]


def test_warehouse_role_rejected():
    result = filter_job(make_job(title="Warehouse Associate"))
    assert not result.passed
    assert result.reasons == ["unrelated_occupation"]


def test_product_manager_passes():
    result = filter_job(make_job(title="Product Manager (Remote)"))
    assert result.passed
    assert result.reasons == []


def test_technical_program_manager_passes():
    result = filter_job(make_job(title="Technical Program Manager - Cloud Infrastructure"))
    assert result.passed


def test_program_manager_passes():
    result = filter_job(make_job(title="Program Manager, Enterprise Delivery"))
    assert result.passed


def test_unusual_professional_title_passes():
    result = filter_job(make_job(title="Head of Cloud Infrastructure Delivery"))
    assert result.passed
    result = filter_job(make_job(title="Delivery Transformation Lead"))
    assert result.passed
    result = filter_job(make_job(title="Programme Manager - Network Operations"))
    assert result.passed


def test_internship_rejected():
    result = filter_job(make_job(title="Software Engineer Intern (Remote)"))
    assert not result.passed
    assert result.reasons == ["seniority_too_low"]


def test_junior_role_rejected():
    result = filter_job(make_job(title="Junior DevOps Engineer"))
    assert not result.passed
    assert result.reasons == ["seniority_too_low"]


def test_hybrid_role_rejected():
    result = filter_job(make_job(title="Data Engineer (Hybrid - Austin)"))
    assert not result.passed
    assert result.reasons == ["not_remote"]


def test_onsite_role_rejected():
    result = filter_job(make_job(title="Backend Engineer (Onsite NYC)"))
    assert not result.passed
    assert result.reasons == ["not_remote"]


def test_onsite_required_in_description_rejected():
    result = filter_job(
        make_job(description="This position requires you to be on-site 5 days per week.")
    )
    assert not result.passed
    assert result.reasons == ["not_remote"]


def test_fully_remote_role_passes():
    result = filter_job(
        make_job(
            title="Senior Program Manager",
            location="Anywhere in the World",
            description="Work from anywhere in the world. Fully remote team.",
        )
    )
    assert result.passed


def test_city_or_state_metadata_alone_does_not_reject():
    result = filter_job(make_job(title="Technical Account Manager", location="Texas"))
    assert result.passed
    result = filter_job(make_job(title="Customer Success Manager", location="California"))
    assert result.passed


def test_office_mention_does_not_reject():
    result = filter_job(
        make_job(
            description=(
                "We have offices in New York and London, but this position is fully remote "
                "and you can work from anywhere."
            )
        )
    )
    assert result.passed


def test_onsite_client_team_mention_does_not_reject():
    result = filter_job(
        make_job(
            description="You will lead a 12-person on-site team at the client data center."
        )
    )
    assert result.passed


def test_us_inclusive_geography_passes():
    result = filter_job(
        make_job(
            title="Program Manager",
            location="🇨🇦 Canada and 🇺🇸 United States of America",
        )
    )
    assert result.passed


def test_explicit_non_us_geography_rejected():
    result = filter_job(
        make_job(
            description=(
                "This is a remote role. Candidates must be based in the United Kingdom."
            )
        )
    )
    assert not result.passed
    assert result.reasons == ["geography_excluded"]


def test_explicit_europe_only_geography_rejected():
    result = filter_job(
        make_job(
            title="Engineering Manager",
            description="Remote - Europe only. Work within European time zones.",
        )
    )
    assert not result.passed
    assert result.reasons == ["geography_excluded"]


def test_ambiguous_geography_passes():
    result = filter_job(
        make_job(
            title="Account Executive DACH",
            location="Anywhere in the World",
            description="We are a UK-based company hiring globally.",
        )
    )
    assert result.passed
    result = filter_job(make_job(title="Delivery Manager", location="Europa"))
    assert result.passed


def test_multiple_rejection_reasons_returned():
    result = filter_job(make_job(title="Junior Warehouse Associate (Onsite in UK Only)"))
    assert not result.passed
    assert set(result.reasons) == {
        "unrelated_occupation",
        "seniority_too_low",
        "not_remote",
        "geography_excluded",
    }


def test_filter_jobs_returns_passed_and_rejected():
    jobs = [
        make_job(title="Senior Program Manager"),
        make_job(title="Plumber"),
        make_job(title="Junior Developer"),
    ]
    passed, rejected = filter_jobs(jobs)
    assert [job.title for job in passed] == ["Senior Program Manager"]
    assert [job.title for job, _ in rejected] == ["Plumber", "Junior Developer"]
    assert rejected[0][1].reasons == ["unrelated_occupation"]
    assert rejected[1][1].reasons == ["seniority_too_low"]
