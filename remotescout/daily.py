"""Daily recommendation command entry point.

Intended to be invoked by the host scheduler (cron / systemd timer):

    python -m remotescout.daily

Builds today's 0-3 recommendations, persists the recommendation-day
completion marker, and exits. Scheduling is owned by the host operating
system; this module only exposes the command.
"""
import datetime
import sys

from remotescout import db, engine
from remotescout.config import load_config


def run_daily(build=None):
    """Build today's recommendations against the configured database.

    Returns a (exit_code, message) pair. An already-completed day is a
    successful no-op that returns the existing pinned recommendations
    without rediscovery, rescoring, or re-resolution.
    """
    day = datetime.date.today().isoformat()
    config = load_config()
    if build is None:
        build = engine.build_daily_recommendations

    db.init_db(config["DATABASE_PATH"])
    connection = db.connect(config["DATABASE_PATH"])
    try:
        if db.is_recommendation_day_complete(connection, day):
            pinned = db.get_recommendations(connection, day)
            return (
                0,
                f"Remote Scout daily recommendations already complete: "
                f"{len(pinned)} recommendations for {day}",
            )
        recommendations = build(connection, recommendation_date=day)
        return (
            0,
            f"Remote Scout daily recommendations complete: "
            f"{len(recommendations)} recommendations for {day}",
        )
    finally:
        connection.close()


def main():
    try:
        exit_code, message = run_daily()
    except Exception as error:
        print(f"Remote Scout daily recommendations failed: {error}", file=sys.stderr)
        return 1
    print(message)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
