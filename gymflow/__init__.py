import os
from pathlib import Path

from dotenv import load_dotenv

# Load the standard project-root .env and the legacy outputs/env file that
# ships with this project. The latter was documented for local setup, but
# was not read here, which left OPENAI_API_KEY unset even after users edited
# that file. Existing process environment values take precedence.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(PROJECT_ROOT / "outputs" / "env", override=False)

from flask import Flask, jsonify, redirect, render_template, send_from_directory, session, url_for

from gymflow.config import Config
from gymflow.extensions import db


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)

    from gymflow.routes.auth import auth_bp
    from gymflow.routes.members import members_bp
    from gymflow.routes.classes import classes_bp
    from gymflow.routes.admin import admin_bp
    from gymflow.routes.notifications import notifications_bp
    from gymflow.routes.outbox import outbox_bp
    from gymflow.routes.assistant import assistant_bp
    from gymflow.routes.diet import diet_bp
    from gymflow.routes.billing import billing_bp
    from gymflow.routes.loyalty import loyalty_bp, admin_loyalty_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(classes_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(outbox_bp)
    app.register_blueprint(assistant_bp)
    app.register_blueprint(diet_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(loyalty_bp)
    app.register_blueprint(admin_loyalty_bp)

    from gymflow.permissions import current_user, has_permission

    @app.context_processor
    def inject_permission_helper():
        def can(permission):
            user = current_user()
            return bool(user) and has_permission(user.role, permission)
        return {"can": can}

    # ------------------------------------------------------------------
    # Security pass (report roadmap item, Second Revision): CSRF protection
    # on every state-changing request -- see gymflow/security.py for why
    # this is hand-rolled and how it stays out of the existing test suite's
    # way. csrf_token() is exposed to every template (hidden form field /
    # meta tag), and validate_csrf_request() runs before every route.
    # ------------------------------------------------------------------
    from gymflow import security
    app.jinja_env.globals["csrf_token"] = security.csrf_token

    @app.before_request
    def _check_csrf():
        security.validate_csrf_request()

    @app.route("/")
    def index():
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        return redirect(url_for("members.dashboard"))

    # ------------------------------------------------------------------
    # PWA: installable "Add to Home Screen" support (Section 3.3.4 Usability)
    # ------------------------------------------------------------------
    # sw.js must be served from the ROOT path (not /static/sw.js) so its
    # default scope covers the whole app, not just the static folder.
    @app.route("/sw.js")
    def service_worker():
        resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.route("/offline")
    def offline():
        return render_template("offline.html")

    # ------------------------------------------------------------------
    # Section 4.10 (fault tolerance): infrastructure-level redundancy. This
    # single Flask process can be run as several instances behind a load
    # balancer (see docker-compose.yml, nginx/nginx.conf and
    # docs/FAULT_TOLERANCE_DEMO.md) -- /health is what the load balancer
    # polls to detect a dead instance and stop routing traffic to it, and
    # GYMFLOW_INSTANCE_NAME (one env var per container) lets a demo prove
    # requests really are moving between instances after one is killed.
    # ------------------------------------------------------------------
    @app.route("/health")
    def health_check():
        from gymflow.models import User
        try:
            User.query.first()  # cheapest possible real query: proves the DB is reachable
            db_ok = True
        except Exception:  # noqa: BLE001 -- any DB problem means this instance is unhealthy
            db_ok = False
        instance = os.environ.get("GYMFLOW_INSTANCE_NAME", "unknown")
        status_code = 200 if db_ok else 503
        return jsonify(status="ok" if db_ok else "degraded", instance=instance), status_code

    @app.after_request
    def _tag_serving_instance(response):
        response.headers["X-Served-By"] = os.environ.get("GYMFLOW_INSTANCE_NAME", "unknown")
        return response

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", code=403,
                                message="You do not have permission to do that."), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, message="Page not found."), 404

    # Section 4.10 (fault tolerance): the last line of defence. Everywhere
    # feasible, a failing dependency is retried/circuit-broken and degrades
    # to a fallback *within* the page (see gymflow/resilience.py,
    # mail_manager.py, ai_agent.py) -- but if something still raises an
    # unhandled exception, this makes sure the user sees a normal-looking
    # "something went wrong" page instead of a raw stack trace, i.e. exactly
    # the "a page fails, then something [else] comes up" behaviour.
    @app.errorhandler(500)
    def server_error(e):
        app.logger.error("Unhandled exception: %s", e, exc_info=True)
        return render_template(
            "error.html", code=500,
            message="Something went wrong on our end. Please try again in a moment.",
        ), 500

    with app.app_context():
        db.create_all()
        _migrate_schema()
        _seed_admin()

    return app


def _migrate_schema():
    """db.create_all() only creates tables that don't exist yet -- it never
    ALTERs an existing one, so columns added to models after the database
    file already exists need additive-only migrations here. In particular,
    older GymFlow databases may not yet have Card.photo_filename,
    Card.card_color, Booking.attended, or the pricing/rewards columns below.
    Wrapped so it's a no-op on a fresh database (column already present from
    create_all()) and safe to run on every start-up.

    Guarded with getattr(db, "engine", None): the sandbox test suite
    (_devtools/test_smoke.py, test_unit.py) runs against an in-memory
    flask_sqlalchemy shim (see _devtools/shim/) that has no real engine at
    all -- there's no schema to migrate there, so this is just a no-op under
    the shim instead of an AttributeError.
    """
    engine = getattr(db, "engine", None)
    if engine is None:
        return

    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "cards" not in inspector.get_table_names():
        return  # fresh DB about to be created by create_all() above; nothing to migrate
    existing_columns = {col["name"] for col in inspector.get_columns("cards")}
    if "photo_filename" not in existing_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE cards ADD COLUMN photo_filename VARCHAR(255)"))
    if "card_color" not in existing_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE cards ADD COLUMN card_color VARCHAR(20)"))
    if "invoices" in inspector.get_table_names():
        invoice_columns = {col["name"] for col in inspector.get_columns("invoices")}
        with engine.begin() as conn:
            if "discount_amount" not in invoice_columns:
                conn.execute(text("ALTER TABLE invoices ADD COLUMN discount_amount FLOAT NOT NULL DEFAULT 0"))
            if "paid_amount" not in invoice_columns:
                conn.execute(text("ALTER TABLE invoices ADD COLUMN paid_amount FLOAT"))
    if "rewards" in inspector.get_table_names():
        reward_columns = {col["name"] for col in inspector.get_columns("rewards")}
        if "minimum_tier" not in reward_columns or "max_per_member" not in reward_columns:
            with engine.begin() as conn:
                if "minimum_tier" not in reward_columns:
                    conn.execute(text("ALTER TABLE rewards ADD COLUMN minimum_tier VARCHAR(40)"))
                if "max_per_member" not in reward_columns:
                    conn.execute(text("ALTER TABLE rewards ADD COLUMN max_per_member INTEGER"))
    if "bookings" in inspector.get_table_names():
        booking_columns = {col["name"] for col in inspector.get_columns("bookings")}
        if "attended" not in booking_columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE bookings ADD COLUMN attended BOOLEAN"))
        if "reward_redemption_id" not in booking_columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE bookings ADD COLUMN reward_redemption_id INTEGER"))
    if "reward_redemptions" in inspector.get_table_names():
        redemption_columns = {col["name"] for col in inspector.get_columns("reward_redemptions")}
        if "policy_id" not in redemption_columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE reward_redemptions ADD COLUMN policy_id INTEGER"))


def _seed_admin():
    """Ensures a first Administrator account always exists (bootstrap problem:
    only an admin can create trainers/receptionists, and only a receptionist
    can register members — see Section 3.5.8).

    Section 4.10: this runs once per WORKER PROCESS at start-up (create_app()
    is called per-worker), and in the multi-instance redundancy demo several
    such processes share the same database file -- so it is a real,
    observed race: two workers can both see "no admin yet" and both try to
    insert one, and the loser hits a UNIQUE-constraint error on email
    instead of a clean no-op. Catching that here (rather than letting a
    worker crash on boot) is the same "don't let a concurrency hiccup take
    an instance down" principle as the rest of Section 4.10.
    """
    from gymflow.models import User

    if User.query.filter_by(role="administrator").first():
        _seed_loyalty_tiers()
        return
    admin = User(role="administrator", name="System", surname="Administrator",
                 email="admin@gymflow.local")
    admin.set_password("Admin123!")
    db.session.add(admin)
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001 -- another process/worker won the race; the admin exists either way
        db.session.rollback()
        return
    print("[seed] Created default administrator: admin@gymflow.local / Admin123!")
    _seed_loyalty_tiers()


def _seed_loyalty_tiers():
    """Editable sample rewards thresholds; achievement badges stay separate."""
    from gymflow.models import LoyaltyTier

    if LoyaltyTier.query.count():
        return
    for order, (name, threshold, benefits) in enumerate([
        ("Bronze", 0, "Entry rewards tier"),
        ("Silver", 500, "Sample benefit: 5% invoice discount reward"),
        ("Gold", 1200, "Sample benefit: priority access to selected rewards"),
    ]):
        db.session.add(LoyaltyTier(name=name, threshold=threshold,
                                   sort_order=order, benefits=benefits, active=True))
    db.session.commit()
