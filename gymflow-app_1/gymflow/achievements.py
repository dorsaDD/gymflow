"""Achievement badges for the member dashboard.

Every badge is computed live from the member's own real Booking/FoodEntry
rows -- nothing here is a fabricated or placeholder number. Three tiers
(bronze/silver/gold) per badge, each with a concrete numeric threshold, so
the UI can show both "what you've done" and "what's next".

    Dedicated   -- classes actually attended (Booking.attended == True)
    Explorer    -- distinct classes attended (variety, not just volume)
    Nutrition   -- distinct days with a logged FoodEntry (all-time)
    On a Roll   -- current CONSECUTIVE-day food-logging streak, ending
                   today or yesterday (so it doesn't reset the instant the
                   clock ticks past midnight before today's entry is in)

Used by routes/members.py: dashboard() passes compute_badges(user.card)
into templates/dashboard.html's "Your achievements" panel.
"""
from datetime import date, timedelta

from gymflow.models import Booking, FoodEntry

TIERS = ("bronze", "silver", "gold")


def _tier_progress(value: int, thresholds: tuple[int, int, int]):
    """Returns (tier_reached_or_None, percent_to_next_0_100, next_target_or_None)."""
    bronze, silver, gold = thresholds
    if value >= gold:
        return "gold", 100, None
    if value >= silver:
        baseline, target, tier = silver, gold, "silver"
    elif value >= bronze:
        baseline, target, tier = bronze, silver, "bronze"
    else:
        baseline, target, tier = 0, bronze, None
    span = target - baseline
    pct = 0 if span <= 0 else max(0, min(100, round((value - baseline) / span * 100)))
    return tier, pct, target


def _current_streak(logged_dates: set) -> int:
    if not logged_dates:
        return 0
    today = date.today()
    cursor = today if today in logged_dates else today - timedelta(days=1)
    if cursor not in logged_dates:
        return 0
    streak = 0
    while cursor in logged_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def compute_badges(card) -> list[dict]:
    """Real, per-member badge state -- see module docstring for the exact
    data each badge is computed from. Returns [] for a member with no card
    yet (e.g. mid-registration), so the caller/template can render nothing
    rather than erroring.
    """
    if not card:
        return []

    attended = Booking.query.filter_by(member_id=card.user_id, attended=True).all()
    classes_attended = len(attended)
    # Variety of ACTIVITY, not session count: two different weeks of the same
    # class still count once, but Yoga vs. Spin vs. HIIT count separately.
    distinct_classes = len({b.fitness_class.name for b in attended if b.fitness_class})

    logged_dates = {fe.date for fe in card.food_entries}
    days_logged = len(logged_dates)
    streak = _current_streak(logged_dates)

    defs = [
        {
            "key": "dedicated",
            "icon": "trophy",
            "name": "Dedicated",
            "unit": "classes attended",
            "value": classes_attended,
            "thresholds": (5, 15, 30),
        },
        {
            "key": "explorer",
            "icon": "compass",
            "name": "Explorer",
            "unit": "different classes tried",
            "value": distinct_classes,
            "thresholds": (3, 6, 10),
        },
        {
            "key": "nutrition",
            "icon": "apple",
            "name": "Nutrition Tracker",
            "unit": "days logged",
            "value": days_logged,
            "thresholds": (7, 21, 50),
        },
        {
            "key": "streak",
            "icon": "flame",
            "name": "On a Roll",
            "unit": "day logging streak",
            "value": streak,
            "thresholds": (3, 7, 14),
        },
    ]

    badges = []
    for d in defs:
        tier, pct, next_target = _tier_progress(d["value"], d["thresholds"])
        badges.append({
            "key": d["key"],
            "icon": d["icon"],
            "name": d["name"],
            "value": d["value"],
            "unit": d["unit"],
            "tier": tier,
            "percent": pct,
            "next_target": next_target,
            "thresholds": d["thresholds"],
        })
    return badges
