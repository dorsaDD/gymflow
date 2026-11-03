"""Member / card routes — 3.5.3 Registration, 3.5.7 Card modification,
3.5.9 Document upload, 3.5.12 Progress notes, plus the dashboard and the
revision-history view.
"""
from flask import (Blueprint, abort, current_app, flash, redirect,
                    render_template, request, send_from_directory, url_for)

from datetime import date as date_cls

from gymflow import achievements, card_manager, class_manager, nutrition_manager, payment_manager, recommendation, risk_model
from gymflow.card_manager import PermissionError as CardPermissionError
from gymflow.card_manager import ValidationError
from gymflow.class_manager import list_upcoming_classes
from gymflow.extensions import db
from gymflow.models import Attachment, Booking, Card, ProgressNote, User, Waitlist, WorkoutPlan
from gymflow.permissions import current_user, has_permission, login_required, permission_required

members_bp = Blueprint("members", __name__, url_prefix="/members")


@members_bp.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    context = {"user": user}
    if user.role == "member" and user.card:
        # Time-based loyalty milestones are checked on active member use.
        # A stable member event key makes the same milestone idempotent.
        from gymflow import loyalty_engine
        loyalty_engine.apply_event("member_activity", f"member:{user.id}", user)
        db.session.commit()
        my_bookings = (
            Booking.query.filter_by(member_id=user.id, status="confirmed")
            .join(Booking.fitness_class)
            .order_by(db.text("fitness_classes.schedule asc"))
            .all()
        )
        context["card"] = user.card
        context["bookings"] = my_bookings
        my_waitlist_entries = Waitlist.query.filter_by(member_id=user.id, status="waiting").all()
        context["waitlist_entries"] = [
            (entry, class_manager.waitlist_position(user, entry.fitness_class))
            for entry in my_waitlist_entries
        ]
        context["today_nutrition"] = nutrition_manager.daily_summary(user.card, date_cls.today())
        context["billing_balance"] = payment_manager.outstanding_balance(user.card)
        context["recommended"] = recommendation.recommend_for(user)
        context["badges"] = achievements.compute_badges(user.card)
    if user.role in ("trainer", "receptionist", "administrator"):
        context["member_count"] = Card.query.count()
        upcoming = list_upcoming_classes()
        context["class_count"] = len(upcoming)
        context["upcoming_classes"] = upcoming[:5]
    if user.role == "administrator":
        context["staff_count"] = User.query.filter(User.role.in_(["trainer", "receptionist"])).count()
    if user.role in ("trainer", "administrator"):
        trained = risk_model.train()
        context["at_risk_members"] = _at_risk_members(trained)
        context["risk_note"] = trained["note"]
        context["nutrition_followup"] = _nutrition_followup()
    if user.role in ("receptionist", "administrator"):
        # Billing follow-up is front-desk/admin territory (manage_billing),
        # not the trainer's -- a real gym's trainers don't chase invoices.
        context["billing_followup"] = payment_manager.billing_followup()
    return render_template("dashboard.html", **context)


def _at_risk_members(trained, limit=5):
    """The Section 4.8 "at risk" panel: members with the lowest reliability
    score among those who have at least one marked booking, worst first,
    each paired with the ML-predicted no-show risk for their soonest
    upcoming booking (if they have one). Deliberately ranks by the simple
    reliability ratio (Section 4.7), not the ML model's own output, so a
    member with a genuinely bad track record but no upcoming booking still
    shows up here -- the model can only score a booking that exists.
    """
    members = User.query.filter_by(role="member").all()
    scored = []
    for member in members:
        marked = [b for b in Booking.query.filter_by(member_id=member.id).all() if b.attended is not None]
        if not marked:
            continue
        reliability = class_manager.reliability_score(member)
        upcoming = (
            Booking.query.filter_by(member_id=member.id, status="confirmed")
            .join(Booking.fitness_class)
            .order_by(db.text("fitness_classes.schedule asc"))
            .all()
        )
        next_booking = next((b for b in upcoming if b.attended is None), None)
        next_risk = risk_model.predict_with(trained, next_booking) if next_booking else None
        scored.append({
            "member": member, "reliability": reliability, "marked_count": len(marked),
            "no_shows": sum(1 for b in marked if not b.attended),
            "next_booking": next_booking, "next_risk": next_risk,
        })
    scored.sort(key=lambda row: row["reliability"])
    return scored[:limit]


def _nutrition_followup(limit=5):
    """Section 4.11's dashboard panel: members with a nutrition goal set,
    ranked worst-first by how far their last-7-days average calorie intake
    strays from that goal -- members who aren't logging at all are ranked
    separately (nothing to follow up on numerically, but a trainer should
    still know), and members with no goal at all are left out entirely,
    same "only surface what there's real data for" principle as
    _at_risk_members() above.
    """
    # Filtered in Python rather than via a SQL join -- keeps this portable
    # across the offline test shim (_devtools/shim/flask_sqlalchemy.py,
    # whose .join() is a no-op) and real SQLAlchemy alike.
    cards_with_goals = [c for c in Card.query.all() if c.nutrition_goal is not None]
    rows = []
    for card in cards_with_goals:
        adherence = nutrition_manager.adherence_summary(card)
        deviation = (
            abs(adherence["avg_vs_goal_pct"] - 100) if adherence["avg_vs_goal_pct"] is not None else None
        )
        rows.append({"card": card, "adherence": adherence, "deviation": deviation})
    # Not-logging members first (nothing to measure -- most in need of a nudge),
    # then by how far off-target the ones who ARE logging are, worst first.
    rows.sort(key=lambda r: (r["deviation"] is not None, -(r["deviation"] or 0)))
    return rows[:limit]


# ---------------------------------------------------------------------------
# 3.5.3 Member registration and card creation
# ---------------------------------------------------------------------------
@members_bp.route("/register", methods=["GET", "POST"])
@permission_required("register_member")
def register():
    if request.method == "POST":
        try:
            user = card_manager.register_member(request.form)
            flash(f"Member {user.full_name} registered with code {user.card.member_code}. "
                  "A welcome email with a temporary password was sent (see Mail Outbox).", "success")
            return redirect(url_for("members.view_card", card_id=user.card.id))
        except ValidationError as exc:
            flash(str(exc), "danger")
    return render_template("register_member.html")


@members_bp.route("/")
@login_required
def list_members():
    user = current_user()
    if not has_permission(user.role, "view_any_card"):
        abort(403)
    cards = Card.query.join(Card.owner).order_by(User.surname.asc()).all()
    return render_template("members_list.html", cards=cards)


def _get_card_or_404(card_id):
    card = Card.query.get(card_id)
    if not card:
        abort(404)
    return card


def _can_view(user, card) -> bool:
    return user.id == card.user_id or has_permission(user.role, "view_any_card")


@members_bp.route("/<int:card_id>")
@login_required
def view_card(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    if not _can_view(user, card):
        abort(403)
    return render_template("card_detail.html", card=card, user=user,
                            card_color_presets=card_manager.CARD_COLOR_PRESETS)


@members_bp.route("/<int:card_id>/edit", methods=["GET", "POST"])
@permission_required("modify_card")
def edit_card(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    timeout = current_app.config["CARD_LOCK_TIMEOUT_SECONDS"]

    if request.method == "GET":
        # isLocked() -- see act_card_modification.png
        if card_manager.is_locked(card, user.id, timeout):
            flash(f"This card is currently being edited by {card.locked_by_id and User.query.get(card.locked_by_id).full_name}. "
                  "Please try again shortly.", "warning")
            return redirect(url_for("members.view_card", card_id=card.id))
        card_manager.acquire_lock(card, user.id, timeout)
        return render_template("card_edit.html", card=card)

    try:
        card_manager.modify_card(card, user, request.form)
        flash("Card updated.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    finally:
        card_manager.release_lock(card, user.id)
    return redirect(url_for("members.view_card", card_id=card.id))


@members_bp.route("/<int:card_id>/edit/cancel", methods=["POST"])
@permission_required("modify_card")
def cancel_edit(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    card_manager.release_lock(card, user.id)
    flash("Edit cancelled; the lock was released.", "success")
    return redirect(url_for("members.view_card", card_id=card.id))


# ---------------------------------------------------------------------------
# Profile photo (physical-card-style membership card view)
# ---------------------------------------------------------------------------
def _can_manage_photo(user, card) -> bool:
    if user.id == card.user_id and has_permission(user.role, "manage_own_photo"):
        return True
    return has_permission(user.role, "modify_card")


@members_bp.route("/<int:card_id>/photo", methods=["POST"])
@login_required
def upload_photo(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    if not _can_manage_photo(user, card):
        abort(403)
    try:
        card_manager.set_photo(card, user, request.files.get("photo"), current_app.config["UPLOAD_FOLDER"])
        flash("Profile photo updated.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("members.view_card", card_id=card.id))


@members_bp.route("/<int:card_id>/photo/delete", methods=["POST"])
@login_required
def delete_photo(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    if not _can_manage_photo(user, card):
        abort(403)
    card_manager.remove_photo(card, user, current_app.config["UPLOAD_FOLDER"])
    flash("Profile photo removed.", "success")
    return redirect(url_for("members.view_card", card_id=card.id))


@members_bp.route("/<int:card_id>/color", methods=["POST"])
@login_required
def set_card_color(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    # Same "own card, or staff who can modify cards" rule as the photo above
    # -- choosing a theme is no more sensitive than choosing a photo.
    if not _can_manage_photo(user, card):
        abort(403)
    try:
        card_manager.set_card_color(card, user, request.form.get("color", ""))
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("members.view_card", card_id=card.id))


@members_bp.route("/<int:card_id>/photo/view")
@login_required
def view_photo(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    if not _can_view(user, card):
        abort(403)
    if not card.photo_filename:
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], card.photo_filename)


# ---------------------------------------------------------------------------
# 3.5.9 Document upload
# ---------------------------------------------------------------------------
@members_bp.route("/<int:card_id>/attachments", methods=["POST"])
@permission_required("upload_attachment")
def upload_attachment(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    try:
        card_manager.upload_attachment(card, user, request.files.get("file"),
                                        current_app.config["UPLOAD_FOLDER"])
        flash("File attached to the card.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("members.view_card", card_id=card.id))


@members_bp.route("/attachments/<int:attachment_id>/delete", methods=["POST"])
@login_required
def delete_attachment(attachment_id):
    user = current_user()
    attachment = Attachment.query.get_or_404(attachment_id)
    try:
        card_manager.remove_attachment(attachment, user, current_app.config["UPLOAD_FOLDER"])
        flash("Attachment removed.", "success")
    except CardPermissionError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("members.view_card", card_id=attachment.card_id))


@members_bp.route("/attachments/<int:attachment_id>/download")
@login_required
def download_attachment(attachment_id):
    user = current_user()
    attachment = Attachment.query.get_or_404(attachment_id)
    if not _can_view(user, attachment.card):
        abort(403)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], attachment.stored_filename,
                                download_name=attachment.original_filename, as_attachment=True)


# ---------------------------------------------------------------------------
# 3.5.12 Progress notes on card
# ---------------------------------------------------------------------------
@members_bp.route("/<int:card_id>/notes", methods=["POST"])
@login_required
def add_note(card_id):
    user = current_user()
    if not has_permission(user.role, "add_progress_note"):
        abort(403)
    card = _get_card_or_404(card_id)
    try:
        card_manager.add_progress_note(card, user, request.form.get("text", ""))
        flash("Note added.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("members.view_card", card_id=card.id))


@members_bp.route("/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_note(note_id):
    user = current_user()
    note = ProgressNote.query.get_or_404(note_id)
    try:
        card_manager.remove_progress_note(note, user)
        flash("Note removed.", "success")
    except CardPermissionError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("members.view_card", card_id=note.card_id))


# ---------------------------------------------------------------------------
# 3.5.13 Workout plan assignment
# ---------------------------------------------------------------------------
@members_bp.route("/<int:card_id>/workout-plans", methods=["POST"])
@login_required
def add_workout_plan(card_id):
    user = current_user()
    if not has_permission(user.role, "assign_workout_plan"):
        abort(403)
    card = _get_card_or_404(card_id)
    try:
        card_manager.assign_workout_plan(
            card, user, request.form.get("title", ""), request.form.get("details", ""))
        flash("Workout plan assigned.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("members.view_card", card_id=card.id))


@members_bp.route("/workout-plans/<int:plan_id>/delete", methods=["POST"])
@login_required
def delete_workout_plan(plan_id):
    user = current_user()
    plan = WorkoutPlan.query.get_or_404(plan_id)
    try:
        card_manager.remove_workout_plan(plan, user)
        flash("Workout plan removed.", "success")
    except CardPermissionError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("members.view_card", card_id=plan.card_id))


# ---------------------------------------------------------------------------
# Revision history
# ---------------------------------------------------------------------------
@members_bp.route("/<int:card_id>/history")
@login_required
def history(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    if not _can_view(user, card):
        abort(403)
    return render_template("card_history.html", card=card)
