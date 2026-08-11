import datetime

from flask import Flask, g, render_template

from remotescout import db
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
        rows = db.get_recommendations(db.get_db(), today)
        return render_template("recommendations.html", day=today, recommendations=rows)

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
