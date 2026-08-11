import datetime

from flask import Flask, abort, current_app, g, redirect, render_template, url_for

from remotescout import db, engine
from remotescout.config import load_config


def create_app(config_overrides=None):
    app = Flask(__name__)
    app.config.update(load_config())
    if config_overrides:
        app.config.update(config_overrides)

    db.init_db(app.config["DATABASE_PATH"])
    app.teardown_appcontext(db.close_db)

    @app.route("/")
    def recommendations():
        today = datetime.date.today().isoformat()
        connection = db.get_db()
        pinned = db.get_recommendations(connection, today)
        error = False
        if not pinned and not db.is_recommendation_day_complete(connection, today):
            try:
                pinned = engine.build_daily_recommendations(
                    connection, recommendation_date=today
                )
            except Exception:
                current_app.logger.exception("Failed to build daily recommendations")
                pinned = []
                error = True
        applied_job_ids = {row["job_id"] for row in db.get_applied_jobs(connection)}
        active = [row for row in pinned if row["job_id"] not in applied_job_ids]
        if error:
            state = "error"
        elif not pinned:
            state = "empty"
        elif not active:
            state = "all_applied"
        else:
            state = "active"
        return render_template(
            "recommendations.html",
            day=today,
            recommendations=active,
            state=state,
        )

    @app.route("/recommendations/<int:job_id>/applied", methods=["POST"])
    def mark_applied(job_id):
        today = datetime.date.today().isoformat()
        connection = db.get_db()
        recommended = {row["job_id"] for row in db.get_recommendations(connection, today)}
        if job_id not in recommended:
            abort(404)
        db.mark_job_applied(connection, job_id, today)
        return redirect(url_for("recommendations"))

    @app.route("/tracker")
    def tracker():
        connection = db.get_db()
        applications = db.get_applications(connection)
        history = {
            row["id"]: db.get_application_events(connection, row["id"])
            for row in applications
        }
        return render_template("tracker.html", applications=applications, history=history)

    return app
