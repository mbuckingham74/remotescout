"""Tests for the canonical Pacific business-date helper.

These tests pin specific UTC instants around the Pacific date rollover and
assert the helper returns the expected Pacific calendar date. Expected
values are derived independently using ``zoneinfo.ZoneInfo`` to compute the
Pacific date from the same UTC instant; the production helper is not
consulted to establish the expectation.
"""
import datetime
from zoneinfo import ZoneInfo

import pytest

from remotescout.business_time import BUSINESS_TIMEZONE, business_today

INDEPENDENT_PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = datetime.timezone.utc


def utc(year, month, day, hour, minute):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=UTC)


def independent_pacific_date(instant_utc):
    """Independently derive the Pacific calendar date for a UTC instant."""
    return instant_utc.astimezone(INDEPENDENT_PACIFIC).date()


class TestBusinessTimezoneIdentity:
    def test_business_timezone_constant_is_america_los_angeles(self):
        assert BUSINESS_TIMEZONE == INDEPENDENT_PACIFIC

    def test_business_today_returns_date_instance(self):
        result = business_today()
        assert isinstance(result, datetime.date)


class TestPacificDateBoundaries:
    """Cases at the exact UTC/Pacific date rollover."""

    def test_summer_pdt_rollover_returns_prior_pacific_date(self):
        """03:30 UTC on 2026-08-17 is 20:30 PDT on 2026-08-16.

        The helper must return the Pacific calendar date, not the UTC date.
        """
        instant = utc(2026, 8, 17, 3, 30)
        expected = independent_pacific_date(instant)
        assert expected == datetime.date(2026, 8, 16)
        assert business_today(now_utc=instant) == datetime.date(2026, 8, 16)

    def test_winter_pst_rollover_returns_prior_pacific_date(self):
        """07:30 UTC on 2026-01-02 is 23:30 PST on 2026-01-01.

        The helper must return the Pacific calendar date, not the UTC date.
        """
        instant = utc(2026, 1, 2, 7, 30)
        expected = independent_pacific_date(instant)
        assert expected == datetime.date(2026, 1, 1)
        assert business_today(now_utc=instant) == datetime.date(2026, 1, 1)

    def test_pacific_morning_advances_to_same_date(self):
        """At 15:30 UTC on 2026-08-17 it is 08:30 PDT on 2026-08-17.

        After Pacific midnight the helper must return the matching
        Pacific calendar date.
        """
        instant = utc(2026, 8, 17, 15, 30)
        expected = independent_pacific_date(instant)
        assert expected == datetime.date(2026, 8, 17)
        assert business_today(now_utc=instant) == datetime.date(2026, 8, 17)


class TestIndependentCrossReference:
    """Spot-check several boundaries against an independent ZoneInfo lookup.

    Every expectation in this class is computed by the test using only the
    standard library; it does not import :mod:`remotescout.business_time`
    to derive the answer.
    """

    @pytest.mark.parametrize(
        "instant",
        [
            utc(2026, 7, 15, 12, 0),
            utc(2026, 11, 5, 12, 0),
            utc(2026, 12, 31, 23, 0),
            utc(2027, 1, 1, 23, 0),
            utc(2026, 3, 8, 11, 0),
            utc(2026, 3, 8, 10, 0),
        ],
    )
    def test_known_instant_matches_independent_zoneinfo(self, instant):
        pacific_date = instant.astimezone(INDEPENDENT_PACIFIC).date()
        assert business_today(now_utc=instant) == pacific_date
