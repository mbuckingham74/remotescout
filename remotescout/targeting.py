"""Deterministic positive target-role gate for Package 8 cost containment.

The existing :mod:`remotescout.filtering` module asks a single negative
question: "is this obviously wrong?". This module asks the second
question that drives the new cost-containment behavior:

    Is there affirmative deterministic evidence that this job belongs
    to a target career family?

The gate is intentionally narrow and conservative:

- It is pure Python. No API calls, no embeddings, no fuzzy service.
- It returns a single ``GateResult`` describing the strongest match
  class plus the supporting evidence.
- It only ever passes jobs that demonstrate one of the explicit,
  reviewable role-family patterns listed in the Package 8 spec.
- It is used solely to decide whether a candidate may proceed to the
  scoring budget. Scoring itself remains authoritative.

False negatives are acceptable: any job the gate rejects here simply
remains unscored this run and is observable in run telemetry.

Package 8 revision 2 widens the gate to honor obvious variants of
target role titles that the previous revision accidentally rejected:

* Bare bounded ``TPM`` is allowed when the title carries either
  leadership (senior / principal / director / lead / head / vp /
  chief) or technical scope (technical / platform / infrastructure /
  cloud / engineering / software / digital). Bare ``TPM`` alone is
  still context-required and may be allowed through the contextual
  check.
* Compositional matching of ``Technical Program Manager`` and
  ``Technical Program Management`` variants so obvious punctuation and
  seniority prefixes (Senior / Sr. / Principal / Senior Principal /
  Director / Senior Director / Sr. Director) all pass strongly.
* Senior / Principal / Director ``Product Manager`` / ``Product
  Management`` titles are allowed only when the title or description
  provides bounded supporting technical scope (platform, infra,
  cloud, developer platform, enterprise systems, technical delivery,
  cross-functional program delivery, technology transformation,
  engineering, SaaS). Generic consumer / marketing product roles
  continue to fail.
"""
import re
from dataclasses import dataclass

from remotescout.discovery.models import DiscoveredJob


STRONG_TITLE_REASON = "strong_target_title"
CONTEXT_TITLE_REASON = "context_target_title"
CONTEXT_PRODUCT_REASON = "context_product_leadership"
ADJACENT_TITLE_REASON = "adjacent_target_title"


def _phrase(*phrases):
    return tuple(re.compile(re.escape(phrase), re.IGNORECASE) for phrase in phrases)


# Words that signal seniority / leadership scope, used to bind the
# bare ``TPM`` token and to weight contextual matches.
LEADERSHIP_WORDS = (
    "senior",
    "sr.",
    "sr",
    "principal",
    "lead",
    "head",
    "director",
    "vp",
    "vice president",
    "chief",
    "associate",
)


# Bounded technical-scope vocabulary. Each entry is used as a strict
# whole-word match against the normalized title and description. ``IT``
# is intentionally NOT in the list: matching arbitrary substrings
# ``IT`` would over-admit generic IT support roles. Bare ``product``
# is also intentionally excluded: a generic ``Senior Product Manager``
# title must not promote itself to a context match solely because the
# word "product" appears in it.
TECHNICAL_SCOPE_WORDS = (
    "platform",
    "infrastructure",
    "cloud",
    "developer platform",
    "enterprise systems",
    "technical delivery",
    "cross-functional program delivery",
    "technology transformation",
    "engineering",
    "saas",
    "technical",
    "technology",
    "software",
    "digital transformation",
    "engineering",
    "transformation",
)

# Adjacent role phrases that pass on title alone — these unambiguously
# identify a target career family even without supporting context.
ADJACENT_TITLE_PHRASES = (
    "technology transformation",
    "digital transformation",
    "technical delivery",
    "technology delivery",
    "infrastructure program",
    "cloud program",
    "platform program",
    "enterprise technology",
    "technical operations delivery",
    "head of technology",
    "head of engineering delivery",
    "head of platform",
    "head of infrastructure",
    "head of product",
)


# Highly specific target titles that pass strongly on the title alone.
# Listed explicitly per the Package 8 spec's strong-match examples.
SPECIFIC_STRONG_TITLE_PHRASES = (
    "program delivery director",
    "director, epmo",
    "director of epmo",
    "enterprise program management office",
    "director of program management",
    "director, program management",
    "portfolio director",
    "director of portfolio",
    "director, portfolio",
    "delivery director",
)


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str
    rank_points: int


def _title_text(job):
    return (job.title or "").lower()


def _description_text(job):
    return (job.description or "").lower()


def _has_leadership(text):
    for word in LEADERSHIP_WORDS:
        if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text):
            return True
    return False


def _has_technical_scope(text):
    """Return True iff text contains a bounded technical-scope token.

    Each token is matched as a whole phrase (``\\b...\\b``), never as
    an arbitrary substring. ``IT`` is deliberately excluded.
    """
    for token in TECHNICAL_SCOPE_WORDS:
        pattern = r"(?<![\w-])" + re.escape(token).replace(r"\ ", r"\s+") + r"(?![\w-])"
        if re.search(pattern, text):
            return True
    return False


def _matches_strong_phrase(text, phrase):
    pattern = r"(?<![\w-])" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?![\w-])"
    return bool(re.search(pattern, text))


def _matches_any_phrase(text, phrases):
    for phrase in phrases:
        if _matches_strong_phrase(text, phrase):
            return True
    return False


def _matches_director_tpm_combo(text):
    """Detect ``Director, TPM`` style bounded TPM roles.

    Matches the literal ``TPM`` word token accompanied by either a
    leadership prefix in the same title, or a technical-scope token in
    the title/description.
    """
    if not re.search(r"(?<![\w-])tpm(?![\w-])", text):
        return False
    return _has_leadership(text) or _has_technical_scope(text)


# Compositional regex for the full ``Technical Program Manager`` and
# ``Technical Program Management`` title family. Leadership prefixes
# (Senior, Sr., Principal, Senior Principal, Director, Senior
# Director, Sr. Director, Associate) are matched via an alternation
# group so obvious punctuation variants pass without enumerating every
# possible surface form.
_TPM_FAMILY_RE = re.compile(
    r"""
    \b
    (?P<leadership> senior\ principal| senior\ director| sr\.\ director| sr\ director
      | senior| sr\.| sr| principal| director| lead| head| associate )?
    [\s,]*
    (?:technical\s+program\s+manager|technical\s+program\s+management
      |technical\s+program\s+director)
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _matches_tpm_family(text):
    return _TPM_FAMILY_RE.search(text) is not None


# Compositional regex for context-allowed product leadership titles
# such as ``Senior Product Manager``, ``Director, Product Management``,
# ``Principal Product Manager``. These are intentionally NOT strong
# matches on their own — they require supporting technical scope.
_PRODUCT_LEADERSHIP_RE = re.compile(
    r"""
    \b
    (?P<leadership> senior\ principal| senior\ director| sr\.\ director| sr\ director
      | senior| sr\.| sr| principal| director )?
    [\s,]*
    (?:product\s+manager|product\s+management|product\s+lead)
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _is_context_product_leadership(title, description):
    """Senior product leadership is allowed only when bounded technical
    scope appears in the title or the description and the role is not
    clearly a consumer/marketing-adjacent product role.
    """
    if not _PRODUCT_LEADERSHIP_RE.search(title):
        return False
    if not _has_leadership(title):
        return False
    if _is_generic_product_leadership(title) or _is_generic_product_leadership(description):
        return False
    return _has_technical_scope(title) or _has_technical_scope(description)


def _is_generic_product_leadership(text):
    """Generic consumer/marketing product leadership fails the gate."""
    if not _PRODUCT_LEADERSHIP_RE.search(text):
        return False
    consumer_keywords = (
        "consumer",
        "marketing",
        "growth",
        "social media",
        "brand",
        "retail",
        "commerce",
        "seo",
        "ppc",
        "email",
        "content",
        "media",
        "ads",
        "advertising",
    )
    for keyword in consumer_keywords:
        if re.search(rf"(?<![\w-]){re.escape(keyword)}(?![\w-])", text):
            return True
    return False


# Program / delivery / PMO / portfolio family, used as the
# context-required match when no strong evidence exists. Each match
# requires bounded supporting scope.
_PROGRAM_FAMILY_RE = re.compile(
    r"""
    \b
    (?:program\s+director|program\s+manager|program\s+management
      |program\s+lead|delivery\s+director|delivery\s+lead
      |delivery\s+manager|portfolio\s+director|portfolio\s+manager
      |portfolio\s+program|pmo|epmo
      |program\s+delivery\s+director|director\ of\ program\ management
      |director,\ program\ management)
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def evaluate(job: DiscoveredJob) -> GateResult:
    """Return the gate decision for ``job``.

    Order is significant: the strongest available match wins.

    1. Compositional ``Technical Program Manager`` / ``Technical
       Program Management`` family → strong.
    2. Bare bounded ``TPM`` with leadership or technical scope →
       strong.
    3. Adjacent role phrases (digital transformation, head of
       platform, etc.) → strong.
    4. Program/delivery/PMO/portfolio with bounded technical scope →
       contextual.
    5. Senior/Principal/Director product leadership with bounded
       technical scope (and no consumer/marketing signal) →
       contextual product leadership.
    6. Bare ``TPM`` with no leadership and no technical scope →
       contextual.
    7. Otherwise fail with ``outside_target_role_families``.
    """
    title = _title_text(job)
    description = _description_text(job)

    if _matches_tpm_family(title):
        return GateResult(passed=True, reason=STRONG_TITLE_REASON, rank_points=10)

    if _matches_director_tpm_combo(title):
        return GateResult(passed=True, reason=STRONG_TITLE_REASON, rank_points=10)

    if _matches_any_phrase(title, SPECIFIC_STRONG_TITLE_PHRASES):
        return GateResult(passed=True, reason=STRONG_TITLE_REASON, rank_points=10)

    if _matches_any_phrase(title, ADJACENT_TITLE_PHRASES):
        return GateResult(passed=True, reason=ADJACENT_TITLE_REASON, rank_points=8)

    if _PROGRAM_FAMILY_RE.search(title):
        if _has_technical_scope(title) or _has_technical_scope(description):
            return GateResult(passed=True, reason=CONTEXT_TITLE_REASON, rank_points=4)
        return GateResult(passed=False, reason="context_required_no_signal", rank_points=0)

    if _is_context_product_leadership(title, description):
        return GateResult(passed=True, reason=CONTEXT_PRODUCT_REASON, rank_points=3)

    if re.search(r"(?<![\w-])tpm(?![\w-])", title) and _has_technical_scope(
        f"{title} {description}"
    ):
        return GateResult(passed=True, reason=CONTEXT_TITLE_REASON, rank_points=4)

    return GateResult(passed=False, reason="outside_target_role_families", rank_points=0)