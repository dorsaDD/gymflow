"""Fitness class routes — 3.5.11 Class booking, plus class schedule
management for trainers, the Section 4.7 waitlist, Section 4.8 attendance
marking, and the Section 4.8 no-show risk model.
"""
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from gymflow import class_manager, risk_model, scheduling
from gymflow.card_manager import ValidationError
from gymflow.models import Booking, FitnessClass, RewardRedemption, User, Waitlist
from gymflow.permissions import current_user, has_permission, login_required, permission_required

classes_bp = Blueprint("classes", __name__, url_prefix="/classes")


@classes_bp.route("/")
@login_required
def catalog():
    user = current_user()
    classes = class_manager.list_upcoming_classes()
    my_booking_class_ids = set()
    my_waitlist_positions = {}
    free_class_rewards = []
    if user.role == "member":
        my_booking_class_ids = {
            b.class_id for b in Booking.query.filter_by(member_id=user.id, status="confirmed").all()
        }
        my_entries = Waitlist.query.filter_by(member_id=user.id, status="waiting").all()
        for entry in my_entries:
            my_waitlist_positions[entry.class_id] = (
                entry.id, class_manager.waitlist_position(user, entry.fitness_class)
            )
        from gymflow import loyalty_engine
        account = loyalty_engine.get_account(user.card, create=False) if user.card else None
        if account:
            free_class_rewards = [item for item in RewardRedemption.query.filter_by(account_id=account.id, status="reserved").all()
                                  if item.reward.target == "class_booking"]
    return render_template("classes_catalog.html", classes=classes,
                            my_booking_class_ids=my_booking_class_ids,
                            my_waitlist_positions=my_waitlist_positions,
                            free_class_rewards=free_class_rewards)


@classes_bp.route("/new", methods=["GET", "POST"])
@permission_required("manage_classes")
def new_class():
    user = current_user()
    if request.method == "POST":
        try:
            class_manager.create_class(user, request.form)
            flash("Class created.", "success")
            return redirect(url_for("classes.catalog"))
        except ValidationError as exc:
            flash(str(exc), "danger")
    return render_template("class_new.html")


@classes_bp.route("/suggest-slots")
@permission_required("manage_classes")
def suggest_slots():
    """AJAX endpoint backing the "Suggest good times" button on the new-class
    form (see scheduling.py for the actual conflict-detection + historical
    demand algorithm). Requires a room the trainer has already typed in, so
    the search actually knows what to check for conflicts against.
    """
    user = current_user()
    room = request.args.get("room", "")
    slots = scheduling.suggest_slots(user, room)
    return jsonify(slots=[
        {
            "value": s["start"].strftime("%Y-%m-%dT%H:%M"),
            "label": s["start"].strftime("%a %b %d, %H:%M"),
            "score": round(s["score"] * 100),
            "has_history": s["has_history"],
        }
        for s in slots
    ])


@classes_bp.route("/<int:class_id>/book", methods=["POST"])
@permission_required("book_class")
def book(class_id):
    user = current_user()
    fitness_class = FitnessClass.query.get_or_404(class_id)
    try:
        class_manager.book_class(user, fitness_class, request.form.get("reward_redemption_id", type=int))
        flash(f"Booked '{fitness_class.name}'. A confirmation email was sent (see Mail Outbox).", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("classes.catalog"))


@classes_bp.route("/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
def cancel(booking_id):
    user = current_user()
    booking = Booking.query.get_or_404(booking_id)
    if not has_permission(user.role, "cancel_own_booking") or booking.member_id != user.id:
        abort(403)
    try:
        class_manager.cancel_booking(user, booking)
        flash("Booking cancelled.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(request.referrer or url_for("classes.catalog"))


# ---------------------------------------------------------------------------
# 4.7 Waitlist
# ---------------------------------------------------------------------------
@classes_bp.route("/<int:class_id>/waitlist/join", methods=["POST"])
@permission_required("join_waitlist")
def join_waitlist(class_id):
    user = current_user()
    fitness_class = FitnessClass.query.get_or_404(class_id)
    try:
        class_manager.join_waitlist(user, fitness_class)
        position = class_manager.waitlist_position(user, fitness_class)
        flash(f"You're on the waitlist for '{fitness_class.name}' (position {position}).", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("classes.catalog"))


@classes_bp.route("/waitlist/<int:entry_id>/leave", methods=["POST"])
@login_required
def leave_waitlist(entry_id):
    user = current_user()
    entry = Waitlist.query.get_or_404(entry_id)
    if not has_permission(user.role, "leave_own_waitlist") or entry.member_id != user.id:
        abort(403)
    try:
        class_manager.leave_waitlist(user, entry)
        flash("Removed from the waitlist.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("classes.catalog"))


# ---------------------------------------------------------------------------
# 4.8 Attendance marking (feeds the reliability score and the risk model)
# ---------------------------------------------------------------------------
@classes_bp.route("/<int:class_id>/roster")
@permission_required("mark_attendance")
def roster(class_id):
    fitness_class = FitnessClass.query.get_or_404(class_id)
    bookings = (
        Booking.query.filter_by(class_id=class_id, status="confirmed")
        .join(Booking.member)
        .order_by(User.surname.asc())
        .all()
    )
    # One model trained once per request and reused for every unmarked
    # booking on this roster (see risk_model.predict_with()'s docstring for
    # why NOT to call train() once per booking).
    trained = risk_model.train()
    risk_by_booking = {
        b.id: risk_model.predict_with(trained, b) for b in bookings if b.attended is None
    }
    return render_template("class_roster.html", fitness_class=fitness_class, bookings=bookings,
                            risk_by_booking=risk_by_booking, risk_note=trained["note"])


@classes_bp.route("/model-insights")
@permission_required("view_model_insights")
def model_insights():
    """Section 4.8, made checkable: shows exactly how the no-show model was
    trained and why the current algorithm was chosen over the alternative,
    instead of just asserting "there's ML in here somewhere."
    """
    trained = risk_model.train()
    return render_template(
        "model_insights.html", trained=trained, feature_names=risk_model.FEATURE_NAMES,
        min_samples=risk_model.MIN_TRAINING_SAMPLES,
    )


@classes_bp.route("/bookings/<int:booking_id>/attendance", methods=["POST"])
@permission_required("mark_attendance")
def mark_attendance(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    attended = request.form.get("attended") == "1"
    try:
        class_manager.mark_attendance(booking, attended)
        flash(f"Marked {booking.member.full_name} as {'attended' if attended else 'no-show'}.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("classes.roster", class_id=booking.class_id))
