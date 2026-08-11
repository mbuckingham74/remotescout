import re
from dataclasses import dataclass

from remotescout.discovery.models import DiscoveredJob

REASONS = (
    "unrelated_occupation",
    "wrong_job_family",
    "seniority_too_low",
    "not_remote",
    "geography_excluded",
)


@dataclass
class FilterResult:
    passed: bool
    reasons: list[str]


@dataclass(frozen=True)
class _Rule:
    reason: str
    patterns: tuple[re.Pattern, ...]
    fields: tuple[str, ...]
    gate: tuple[str, ...] = ()


def _word_patterns(*terms):
    return tuple(re.compile(rf"\b{re.escape(term)}\b") for term in terms)


def _phrase_patterns(*phrases):
    return tuple(re.compile(re.escape(phrase)) for phrase in phrases)


_UNRELATED_OCCUPATION_TERMS = (
    "plumber", "electrician", "carpenter", "welder", "machinist", "mechanic",
    "roofer", "mason", "painter", "landscaper", "gardener", "janitor",
    "custodian", "housekeeper", "cleaner", "laundry", "cook", "baker",
    "barista", "bartender", "waiter", "waitress", "cashier", "forklift",
    "nurse", "nursing", "cna", "physician", "surgeon", "dentist", "hygienist",
    "pharmacist", "paramedic", "emt", "veterinarian", "radiologist",
    "anesthesiologist", "radiographer", "optometrist", "audiologist",
    "midwife", "caregiver", "babysitter", "barber", "hairdresser", "stylist",
    "receptionist", "telemarketer", "courier", "dishwasher", "hostess",
    "truck driver", "delivery driver", "bus driver", "taxi driver",
    "limousine driver", "warehouse associate", "warehouse worker",
    "warehouse operator", "warehouse packer", "warehouse staff",
    "assembly line", "factory worker", "machine operator",
    "production operator", "forklift operator", "crane operator",
    "security guard", "dog walker", "pet sitter", "nail technician",
    "hair stylist", "child care", "childcare", "daycare", "medical assistant",
    "physician assistant", "dental assistant", "physical therapist",
    "occupational therapist", "speech therapist", "speech pathologist",
    "pharmacy technician", "veterinary assistant", "animal groomer",
    "pest control", "pool cleaner", "house keeping", "food service",
    "fast food", "wait staff", "line cook", "sous chef", "short order cook",
    "restaurant cook", "head chef", "data entry", "call center",
    "customer service agent", "customer service representative",
    "customer support agent", "support representative",
    "administrative assistant", "executive assistant", "personal assistant",
    "admin assistant", "virtual assistant",
)

_WRONG_JOB_FAMILY_TERMS = (
    "designer", "illustrator", "animator", "photographer", "videographer",
    "video editor", "motion graphics", "3d artist", "artist", "voice actor",
    "voiceover", "musician", "composer", "dancer", "choreographer",
    "copywriter", "content writer", "technical writer", "journalist",
    "editor", "translator", "interpreter", "blogger", "seo specialist",
    "ppc specialist", "social media manager", "social media specialist",
    "marketing coordinator", "marketing associate", "email marketer",
    "accountant", "bookkeeper", "actuary", "payroll", "tax specialist",
    "tax accountant", "underwriter", "loan officer", "mortgage",
    "accounts payable", "accounts receivable", "controller", "paralegal",
    "legal assistant", "attorney", "lawyer", "legal counsel", "recruiter",
    "talent acquisition", "talent sourcer", "hr generalist", "hr specialist",
    "benefits administrator", "compensation analyst",
)

_SENIORITY_TERMS = (
    "internship", "intern", "junior", "entry level", "entry-level",
    "trainee", "apprentice", "new grad", "recent grad",
)

_NOT_REMOTE_TITLE_TERMS = (
    "hybrid", "onsite", "on-site", "in-office", "in office",
    "office-based", "office based",
)

_NOT_REMOTE_DESCRIPTION_PHRASES = (
    "hybrid", "in-office", "office-based", "office based",
    "work from the office", "work from office", "working from the office",
    "return to office", "office attendance", "attend the office",
    "office presence", "days in the office", "in the office",
    "come into the office", "must come to the office", "based in the office",
    "must be on-site", "must be onsite", "required to be on-site",
    "required to be onsite", "required on-site", "required onsite",
    "requires on-site", "requires onsite", "requires you to be on-site",
    "requires you to be onsite", "expected to be on-site",
    "expected to be onsite", "fully on-site", "fully onsite",
    "100% on-site", "100% onsite", "on-site role", "onsite role",
    "on-site position", "onsite position", "not remote", "not a remote",
    "no remote", "no work from home", "no working from home", "no wfh",
    "in-person role", "in-person position", "in-person work",
    "in person role", "in person position", "hybrid work",
    "hybrid schedule", "hybrid role", "hybrid position",
)

_EXCLUDED_GEOGRAPHIES = (
    "uk", "united kingdom", "great britain", "britain", "england",
    "scotland", "wales", "northern ireland", "ireland", "europe", "eu",
    "european union", "emea", "apac", "asia pacific", "asia-pacific",
    "canada", "india", "australia", "new zealand", "germany", "france",
    "spain", "portugal", "italy", "netherlands", "holland", "belgium",
    "luxembourg", "switzerland", "austria", "greece", "sweden", "norway",
    "denmark", "finland", "iceland", "poland", "czech", "czechia",
    "slovakia", "hungary", "romania", "bulgaria", "croatia", "serbia",
    "slovenia", "estonia", "latvia", "lithuania", "ukraine", "turkey",
    "türkiye", "israel", "uae", "dubai", "united arab emirates",
    "saudi arabia", "qatar", "kuwait", "bahrain", "oman", "jordan",
    "lebanon", "egypt", "morocco", "tunisia", "algeria", "nigeria",
    "ghana", "kenya", "tanzania", "uganda", "ethiopia", "south africa",
    "botswana", "zimbabwe", "pakistan", "bangladesh", "sri lanka",
    "nepal", "myanmar", "thailand", "vietnam", "cambodia", "laos",
    "indonesia", "malaysia", "philippines", "singapore", "hong kong",
    "taiwan", "macau", "china", "japan", "south korea", "mongolia",
    "kazakhstan", "brazil", "mexico", "argentina", "chile", "colombia",
    "peru", "venezuela", "ecuador", "bolivia", "paraguay", "uruguay",
    "costa rica", "panama", "guatemala", "honduras", "el salvador",
    "nicaragua", "cuba", "jamaica", "trinidad", "bahamas",
    "latin america", "latam", "south america", "central america",
    "caribbean", "africa", "middle east", "mena", "gcc",
)

_GEO_QUALIFIERS = (
    "must be based in", "must be located in", "must reside in",
    "must be a resident of", "open to candidates in",
    "open to applicants in", "restricted to", "limited to",
    "only open to", "candidates must be based in", "within the",
)

_GEO_GATE = (
    "must be", " only", "residents", "restricted", "limited", "citizens",
    "nationals", "open to", "within the", "time zone", "timezone",
)

_GEO_EXCEPTIONS = ("united states", " usa", "u.s.", "north america")


def _geo_patterns():
    patterns = []
    for geography in _EXCLUDED_GEOGRAPHIES:
        for qualifier in _GEO_QUALIFIERS:
            patterns.append(
                re.compile(rf"{re.escape(qualifier)} (?:the )?\b{re.escape(geography)}\b")
            )
        patterns.append(re.compile(rf"\b{re.escape(geography)} only\b"))
        patterns.append(re.compile(rf"{re.escape(geography)} time zones?\b"))
        patterns.append(re.compile(rf"{re.escape(geography)} timezones?\b"))
    patterns.append(re.compile(r"\beuropean time zones?\b"))
    patterns.append(re.compile(r"\beuropean timezones?\b"))
    return tuple(patterns)


def _build_rules():
    return (
        _Rule("unrelated_occupation", _word_patterns(*_UNRELATED_OCCUPATION_TERMS), ("title",)),
        _Rule("wrong_job_family", _word_patterns(*_WRONG_JOB_FAMILY_TERMS), ("title",)),
        _Rule("seniority_too_low", _word_patterns(*_SENIORITY_TERMS), ("title",)),
        _Rule("not_remote", _word_patterns(*_NOT_REMOTE_TITLE_TERMS), ("title", "location")),
        _Rule("not_remote", _phrase_patterns(*_NOT_REMOTE_DESCRIPTION_PHRASES), ("description",)),
        _Rule("geography_excluded", _geo_patterns(), ("title", "location")),
        _Rule(
            "geography_excluded",
            _geo_patterns(),
            ("description",),
            gate=_GEO_GATE,
        ),
    )


RULES = _build_rules()


def filter_job(job: DiscoveredJob) -> FilterResult:
    title = (job.title or "").lower()
    location = (job.location or "").lower()
    description = (job.description or "").lower()
    fields = {"title": title, "location": location, "description": description}
    reasons = []
    for rule in RULES:
        if rule.reason in reasons:
            continue
        for field_name in rule.fields:
            text = fields[field_name]
            if not text:
                continue
            if rule.reason == "geography_excluded" and any(
                exception in text for exception in _GEO_EXCEPTIONS
            ):
                continue
            if rule.gate and not any(gate in text for gate in rule.gate):
                continue
            if any(pattern.search(text) for pattern in rule.patterns):
                reasons.append(rule.reason)
                break
    return FilterResult(passed=not reasons, reasons=reasons)


def filter_jobs(jobs):
    passed = []
    rejected = []
    for job in jobs:
        result = filter_job(job)
        if result.passed:
            passed.append(job)
        else:
            rejected.append((job, result))
    return passed, rejected
