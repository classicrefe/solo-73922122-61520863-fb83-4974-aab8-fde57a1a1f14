from __future__ import annotations

import os

from flask import Flask

from . import db


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DATABASE=os.environ.get(
            "DATABASE_PATH", os.path.join(app.instance_path, "inspection.sqlite3")
        ),
        SQLITE_BUSY_TIMEOUT_MS=int(os.environ.get("SQLITE_BUSY_TIMEOUT_MS", "15000")),
    )
    if test_config:
        app.config.update(test_config)

    db_path = app.config["DATABASE"]
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    db.init_app(app)

    from . import routes

    app.register_blueprint(routes.bp)

    with app.app_context():
        db.initialise()

    return app
