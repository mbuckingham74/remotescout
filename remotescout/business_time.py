"""Canonical Remote Scout business-date ownership.

The user-facing calendar day is fixed to America/Los_Angeles. This module is
the single source of truth for that rule; production routes and commands
import :func:`business_today` rather than calling :func:`datetime.date.today`
directly.

The optional ``now_utc`` parameter exists for test injection of a specific
UTC instant. Production callers omit it and rely on the actual current time.
"""
import datetime
from zoneinfo import ZoneInfo

BUSINESS_TIMEZONE = ZoneInfo("America/Los_Angeles")


def business_today(now_utc=None):
    """Return the current business calendar date in Pacific time.

    When ``now_utc`` is omitted, the helper reads the current UTC instant.
    Passing a timezone-aware :class:`datetime.datetime` in UTC is the
    supported way for tests to pin "now" without mutating the host clock.
    """
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    return now_utc.astimezone(BUSINESS_TIMEZONE).date()
