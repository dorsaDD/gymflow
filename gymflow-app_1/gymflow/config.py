import os

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))


class Config:
    """Application configuration.

    Mirrors the non-functional requirements from Chapter 3.3 of the report:
    - passwords are never stored or transmitted in clear text (see card_manager.py)
    - the system is designed to stay responsive under concurrent card edits
      (see CardLock in models.py)
    """
    SECRET_KEY = os.environ.get("GYMFLOW_SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "GYMFLOW_DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'gymflow.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # A busier SQLite "database is locked" timeout than the driver's default
    # -- relevant once more than one GymFlow instance shares the same
    # database file, which is exactly the multi-instance deployment in
    # Section 4.10's redundancy demo (see docker-compose.yml and
    # docs/FAULT_TOLERANCE_DEMO.md). Only meaningful for SQLite -- harmless
    # no-op if GYMFLOW_DATABASE_URL points at a real server instead.
    if SQLALCHEMY_DATABASE_URI.startswith("sqlite:"):
        SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 15}}
    # Deliberately kept OUTSIDE gymflow/static/: Flask serves everything
    # under static/ to anyone at /static/<path>, with no permission check.
    # Attachments (e.g. medical certificates) must only be reachable through
    # members.download_attachment(), which enforces _can_view() first.
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "instance", "uploads")
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 MB uploads
    # Card lock timeout (see CardLock / act_card_modification.png): a lock that is
    # older than this is considered stale and can be taken over.
    CARD_LOCK_TIMEOUT_SECONDS = 5 * 60
