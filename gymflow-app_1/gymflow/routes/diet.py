"""Diet & Nutrition routes — Section 4.11. A member's own food/exercise
diary (self-service) plus a trainer/administrator view of any member's
diary for coaching and diet-plan assignment (staff-service), mirroring the
existing card view_card/edit_card self-vs-staff split in routes/members.py.
"""
from datetime import date as date_cls
from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from gymflow import nutrition_manager
from gymflow.card_manager import PermissionError as CardPermissionError
from gymflow.card_manager import ValidationError
from gymflow.models import Card, DietPlan, ExerciseLog, FoodEntry
from gymflow.permissions import current_user, has_permission, login_required

diet_bp = Blueprint("diet", __name__, url_prefix="/diet")


def _get_card_or_404(card_id):
    card = Card.query.get(card_id)
    if not card:
        abort(404)
    return card


def _can_manage_diet(user, card) -> bool:
    if user.id == card.user_id and has_permission(user.role, "manage_own_diet"):
        return True
    return has_permission(user.role, "manage_member_diet")


def _parse_view_date():
    raw = request.args.get("date")
    if not raw:
        return date_cls.today()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return date_cls.today()


def _render_diary(card, user):
    on_date = _parse_view_date()
    summary = nutrition_manager.daily_summary(card, on_date)
    return render_template(
        "diet_diary.html", card=card, user=user, summary=summary,
        meal_types=nutrition_manager.MEAL_TYPES, can_manage=_can_manage_diet(user, card),
        can_assign_plan=has_permission(user.role, "manage_member_diet"),
    )


@diet_bp.route("/")
@login_required
def own_diary():
    user = current_user()
    if not user.card:
        abort(404)
    return _render_diary(user.card, user)


@diet_bp.route("/<int:card_id>")
@login_required
def view_diary(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    if not (user.id == card.user_id or has_permission(user.role, "manage_member_diet")):
        abort(403)
    return _render_diary(card, user)


@diet_bp.route("/<int:card_id>/food", methods=["POST"])
@login_required
def log_food(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    if not _can_manage_diet(user, card):
        abort(403)
    try:
        nutrition_manager.log_food(card, request.form)
        flash("Food logged.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(_diary_url(card, request.form.get("date")))


@diet_bp.route("/food/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_food(entry_id):
    user = current_user()
    entry = FoodEntry.query.get_or_404(entry_id)
    card = entry.card
    if not _can_manage_diet(user, card):
        abort(403)
    nutrition_manager.delete_food_entry(entry)
    flash("Entry removed.", "success")
    return redirect(_diary_url(card, request.args.get("date")))


@diet_bp.route("/<int:card_id>/exercise", methods=["POST"])
@login_required
def log_exercise(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    if not _can_manage_diet(user, card):
        abort(403)
    try:
        nutrition_manager.log_exercise(card, request.form)
        flash("Exercise logged.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(_diary_url(card, request.form.get("date")))


@diet_bp.route("/exercise/<int:log_id>/delete", methods=["POST"])
@login_required
def delete_exercise(log_id):
    user = current_user()
    log = ExerciseLog.query.get_or_404(log_id)
    card = log.card
    if not _can_manage_diet(user, card):
        abort(403)
    nutrition_manager.delete_exercise_log(log)
    flash("Entry removed.", "success")
    return redirect(_diary_url(card, request.args.get("date")))


@diet_bp.route("/<int:card_id>/goal", methods=["POST"])
@login_required
def set_goal(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    if not _can_manage_diet(user, card):
        abort(403)
    try:
        nutrition_manager.set_goal(card, user, request.form)
        flash("Nutrition goal saved.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(_diary_url(card, request.form.get("date")))


@diet_bp.route("/<int:card_id>/plan", methods=["POST"])
@login_required
def assign_plan(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    if not has_permission(user.role, "manage_member_diet"):
        abort(403)
    try:
        nutrition_manager.assign_diet_plan(card, user, request.form.get("title", ""),
                                            request.form.get("details", ""))
        flash("Diet plan assigned.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(_diary_url(card, None))


@diet_bp.route("/plan/<int:plan_id>/delete", methods=["POST"])
@login_required
def delete_plan(plan_id):
    user = current_user()
    plan = DietPlan.query.get_or_404(plan_id)
    card = plan.card
    try:
        nutrition_manager.remove_diet_plan(plan, user)
        flash("Diet plan removed.", "success")
    except CardPermissionError as exc:
        flash(str(exc), "danger")
    return redirect(_diary_url(card, None))


def _diary_url(card, date_str):
    if date_str:
        return url_for("diet.view_diary", card_id=card.id, date=date_str)
    return url_for("diet.view_diary", card_id=card.id)
