"""Mail outbox viewer — lets a grader see every email GymFlow "sent" during
a demo, since no real SMTP server is configured (see mail_manager.py and
the "Mail Server" node in the deployment diagram, Section 3.8).

Section 4.10: also exposes the manual fallback for a "queued" message (one
whose delivery to the simulated 3rd-party relay failed every automatic
retry) -- an administrator can ask GymFlow to try again once the relay is
back.
"""
from flask import Blueprint, flash, redirect, render_template, url_for

from gymflow import mail_manager
from gymflow.models import MailOutbox
from gymflow.permissions import permission_required

outbox_bp = Blueprint("outbox", __name__)


@outbox_bp.route("/outbox")
@permission_required("view_outbox")
def outbox():
    messages = MailOutbox.query.order_by(MailOutbox.sent_at.desc()).all()
    return render_template("outbox.html", messages=messages)


@outbox_bp.route("/outbox/<int:message_id>/retry", methods=["POST"])
@permission_required("view_outbox")
def retry(message_id):
    message = MailOutbox.query.get_or_404(message_id)
    if message.status == "sent":
        flash("That message was already delivered.", "warning")
        return redirect(url_for("outbox.outbox"))
    if mail_manager.retry_delivery(message):
        flash(f"Delivered to {message.to_email} on retry.", "success")
    else:
        flash(f"Still couldn't reach the relay ({message.last_error}). Try again shortly.", "warning")
    return redirect(url_for("outbox.outbox"))
