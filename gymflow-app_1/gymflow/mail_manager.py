"""MailManager — mocked outgoing mail (see class diagram, Section 3.6).

A real deployment would hand these off to an SMTP relay (see the "Mail
Server" node in the deployment diagram, Section 3.8). For this coursework
prototype, sendEmail() simply records the message in the mail_outbox table
and prints it to the console, so every notification-driving use case
(password recovery, staff registration, booking confirmations, ...) can be
demonstrated end-to-end without configuring a real mail server.

Section 4.10 (fault tolerance): the one genuinely external, sometimes-
unreliable step is handing the message to that relay -- _deliver() below
stands in for that network hop. It is wrapped in gymflow.resilience's
retry-with-backoff + circuit breaker so a transient relay outage doesn't
lose the notification or, more importantly, block the business transaction
that triggered it (a class booking must still succeed even if the
confirmation email can't go out right now). Set
GYMFLOW_SIMULATE_MAIL_OUTAGE=1 to make every delivery attempt fail, which is
the easiest way to demo the whole fallback path end-to-end: watch a booking
still succeed, watch the outbox entry come out "queued" instead of "sent",
then unset the variable and click Retry on /outbox.
"""
import os

from gymflow.extensions import db
from gymflow.models import MailOutbox
from gymflow import resilience


def _deliver(to_email: str, subject: str, body: str) -> None:
    """Stands in for the actual network call to a 3rd-party SMTP relay.
    Isolated here (like ai_agent._call_llm and class_manager's other
    single-purpose helpers) so tests and demos can force it to fail without
    needing a real mail server -- see the module docstring.
    """
    if os.environ.get("GYMFLOW_SIMULATE_MAIL_OUTAGE") == "1":
        raise ConnectionError("simulated 3rd-party mail relay outage")
    print(f"[MailManager] -> {to_email} :: {subject}")


def send_email(to_email: str, subject: str, body: str) -> MailOutbox:
    """sendEmail(to, msg) from the class diagram. Always records the
    message and always returns a MailOutbox row -- callers throughout the
    app fire-and-forget this and must never have their own transaction fail
    just because the notification couldn't be delivered right now.
    """
    message = MailOutbox(to_email=to_email, subject=subject, body=body, status="sent")
    try:
        resilience.call_with_retry(
            lambda: _deliver(to_email, subject, body), service="mail", backoff_seconds=0.05,
        )
    except Exception as exc:  # noqa: BLE001 -- CircuitOpenError, or the retries' last exception
        message.status = "queued"
        message.last_error = f"{type(exc).__name__}: {exc}"
    db.session.add(message)
    db.session.commit()
    return message


def retry_delivery(message: MailOutbox) -> bool:
    """Manual fallback path for a queued message (see routes/outbox.py):
    an administrator asks GymFlow to try the relay again. Returns whether
    it succeeded; either way the row is updated in place.
    """
    try:
        resilience.call_with_retry(
            lambda: _deliver(message.to_email, message.subject, message.body),
            service="mail", backoff_seconds=0.05,
        )
    except Exception as exc:  # noqa: BLE001
        message.last_error = f"{type(exc).__name__}: {exc}"
        db.session.commit()
        return False
    message.status = "sent"
    message.last_error = None
    db.session.commit()
    return True
