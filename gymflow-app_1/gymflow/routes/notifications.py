"""Notification settings — 3.5.10."""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from gymflow.extensions import db
from gymflow.permissions import current_user, permission_required

notifications_bp = Blueprint("notifications", __name__, url_prefix="/notifications")


@notifications_bp.route("/settings", methods=["GET", "POST"])
@permission_required("manage_own_notifications")
def settings():
    user = current_user()
    if request.method == "POST":
        choice = request.form.get("mode")  # "email" | "none"
        # System asks for confirmation -> here the form submit IS the confirmation
        user.notifications_enabled = (choice == "email")
        db.session.commit()
        flash("Notification preference updated.", "success")
        return redirect(url_for("members.dashboard"))
    return render_template("notifications.html", user=user)
