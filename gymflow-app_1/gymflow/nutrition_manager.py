"""NutritionManager — Section 4.11: Diet & Nutrition tracking.

Adds a real food/exercise diary on top of the existing gym-class-booking
side of GymFlow: members log what they ate and what exercise they did each
day (including activity outside GymFlow's own classes), set a daily
calorie/macro goal (self-set, or set by their trainer), and a trainer can
assign a DietPlan (the nutrition equivalent of the existing WorkoutPlan) and
follow up using real logged data rather than guesswork -- see
adherence_summary(), which is what feeds the "Nutrition follow-up" panel on
the trainer/administrator dashboard, the same idea as the Section 4.8
at-risk-members panel but for diet adherence instead of class no-shows.

The AI agent (ai_agent.py) also reads this data into a member's grounding
context, so "how am I doing on calories today" is a real, data-backed
question it can answer -- see _member_context() there.

Method names and validation style deliberately mirror the existing
card_manager.py managers (assign_workout_plan/remove_workout_plan) so this
reads as part of the same codebase, not a bolted-on module.
"""
from datetime import date as date_cls
from datetime import datetime, timedelta

from gymflow.card_manager import PermissionError, ValidationError
from gymflow.extensions import db
from gymflow.mail_manager import send_email
from gymflow.models import Card, DietPlan, ExerciseLog, FoodEntry, NutritionGoal, User

MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")


def _parse_date(raw) -> date_cls:
    if not raw:
        return date_cls.today()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError("Invalid date (expected YYYY-MM-DD).")


def _parse_number(raw, field: str, *, allow_float: bool = False, required: bool = True):
    if raw is None or str(raw).strip() == "":
        if required:
            raise ValidationError(f"'{field}' is required.")
        return 0.0 if allow_float else 0
    try:
        value = float(raw) if allow_float else int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"'{field}' must be a number.")
    if value < 0:
        raise ValidationError(f"'{field}' cannot be negative.")
    return value


# ---------------------------------------------------------------------------
# Food diary
# ---------------------------------------------------------------------------
def log_food(card: Card, data: dict) -> FoodEntry:
    meal = (data.get("meal") or "").strip().lower()
    food_name = (data.get("food_name") or "").strip()
    if meal not in MEAL_TYPES:
        raise ValidationError("Meal must be breakfast, lunch, dinner or snack.")
    if not food_name:
        raise ValidationError("Food name is required.")
    entry = FoodEntry(
        card_id=card.id, date=_parse_date(data.get("date")), meal=meal, food_name=food_name,
        calories=_parse_number(data.get("calories"), "calories"),
        protein_g=_parse_number(data.get("protein_g"), "protein", allow_float=True, required=False),
        carbs_g=_parse_number(data.get("carbs_g"), "carbs", allow_float=True, required=False),
        fat_g=_parse_number(data.get("fat_g"), "fat", allow_float=True, required=False),
    )
    db.session.add(entry)
    db.session.commit()
    return entry


def delete_food_entry(entry: FoodEntry) -> None:
    db.session.delete(entry)
    db.session.commit()


# ---------------------------------------------------------------------------
# Exercise diary
# ---------------------------------------------------------------------------
def log_exercise(card: Card, data: dict) -> ExerciseLog:
    activity = (data.get("activity") or "").strip()
    if not activity:
        raise ValidationError("Activity is required.")
    log = ExerciseLog(
        card_id=card.id, date=_parse_date(data.get("date")), activity=activity,
        duration_minutes=_parse_number(data.get("duration_minutes"), "duration (minutes)"),
        calories_burned=_parse_number(data.get("calories_burned"), "calories burned", required=False),
        notes=(data.get("notes") or "").strip() or None,
    )
    db.session.add(log)
    db.session.commit()
    return log


def delete_exercise_log(log: ExerciseLog) -> None:
    db.session.delete(log)
    db.session.commit()


# ---------------------------------------------------------------------------
# Daily calorie/macro goal
# ---------------------------------------------------------------------------
def set_goal(card: Card, setter: User, data: dict) -> NutritionGoal:
    daily_calories = _parse_number(data.get("daily_calories"), "daily calories")
    protein_g = _parse_number(data.get("protein_g"), "protein target")
    carbs_g = _parse_number(data.get("carbs_g"), "carbs target")
    fat_g = _parse_number(data.get("fat_g"), "fat target")
    # Read setter.id up front, before a new NutritionGoal is added to the
    # session below. Under real SQLAlchemy (unlike the project's lenient
    # offline shim), `setter` can be an EXPIRED instance -- e.g. right after
    # a previous db.session.commit() elsewhere -- so touching `setter.id`
    # later triggers a lazy-refresh query, which autoflushes the session
    # first. That would flush the just-added, still-incomplete `goal` row
    # (set_by_id not assigned yet) and fail its NOT NULL constraint. Reading
    # the id now, before `goal` exists, avoids that ordering trap entirely.
    setter_id = setter.id

    goal = card.nutrition_goal  # upsert: one goal row per card
    if goal is None:
        goal = NutritionGoal(card_id=card.id)
        db.session.add(goal)
    goal.daily_calories = daily_calories
    goal.protein_g = protein_g
    goal.carbs_g = carbs_g
    goal.fat_g = fat_g
    goal.set_by_id = setter_id
    goal.updated_at = datetime.utcnow()
    db.session.commit()
    return goal


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------
def daily_summary(card: Card, on_date: date_cls) -> dict:
    """Everything the diary page for one day needs: entries, totals, and
    (if a goal is set) percent-of-goal for the progress bars."""
    entries = [e for e in card.food_entries if e.date == on_date]
    exercises = [x for x in card.exercise_logs if x.date == on_date]
    consumed = {
        "calories": sum(e.calories for e in entries),
        "protein_g": sum(e.protein_g for e in entries),
        "carbs_g": sum(e.carbs_g for e in entries),
        "fat_g": sum(e.fat_g for e in entries),
    }
    burned = sum(x.calories_burned for x in exercises)
    goal = card.nutrition_goal
    percent_of_goal = None
    if goal and goal.daily_calories:
        percent_of_goal = round(consumed["calories"] / goal.daily_calories * 100)
    return {
        "date": on_date, "entries": entries, "exercises": exercises, "consumed": consumed,
        "burned": burned, "net_calories": consumed["calories"] - burned,
        "goal": goal, "percent_of_goal": percent_of_goal,
    }


def adherence_summary(card: Card, days: int = 7) -> dict:
    """The "follow up based on real data" figure for the trainer/admin
    dashboard: average daily calories logged over the last `days` days vs
    the member's goal, plus how many of those days actually got a food
    entry at all -- so a trainer can tell "consistently over/under target"
    apart from "isn't logging," which need very different follow-up.
    """
    goal = card.nutrition_goal
    today = date_cls.today()
    window_start = today - timedelta(days=days - 1)
    totals_by_day: dict = {}
    for e in card.food_entries:
        if window_start <= e.date <= today:
            totals_by_day[e.date] = totals_by_day.get(e.date, 0) + e.calories
    days_logged = len(totals_by_day)
    avg_calories = round(sum(totals_by_day.values()) / days_logged) if days_logged else None
    avg_vs_goal_pct = None
    if avg_calories is not None and goal and goal.daily_calories:
        avg_vs_goal_pct = round(avg_calories / goal.daily_calories * 100)
    return {
        "days_logged": days_logged, "window_days": days,
        "avg_calories": avg_calories, "goal": goal, "avg_vs_goal_pct": avg_vs_goal_pct,
    }


# ---------------------------------------------------------------------------
# Diet plan (trainer-assigned) -- mirrors card_manager.assign_workout_plan
# ---------------------------------------------------------------------------
def assign_diet_plan(card: Card, trainer: User, title: str, details: str) -> DietPlan:
    if not title or not title.strip():
        raise ValidationError("The plan must have a title.")
    if not details or not details.strip():
        raise ValidationError("The plan must include dietary guidance.")
    plan = DietPlan(card_id=card.id, trainer_id=trainer.id, title=title.strip(), details=details.strip())
    db.session.add(plan)
    db.session.commit()
    if card.owner.notifications_enabled:
        send_email(
            card.owner.email, "A new diet plan was assigned to you",
            f"{trainer.full_name} assigned you a new diet plan: '{plan.title}'.\n\n{plan.details}",
        )
    return plan


def remove_diet_plan(plan: DietPlan, remover: User) -> None:
    if remover.role != "administrator" and remover.id != plan.trainer_id:
        raise PermissionError("Only the assigning trainer or an administrator can remove this plan.")
    db.session.delete(plan)
    db.session.commit()
