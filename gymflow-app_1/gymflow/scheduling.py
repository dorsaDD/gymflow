"""ClassScheduler -- a real scheduling algorithm that recommends good time
slots when a trainer creates a NEW class, instead of leaving them to guess a
date/time and hope nothing clashes.

Two independent parts run in the same pass:

1. CONFLICT DETECTION (hard constraint, classic interval-overlap check).
   GymFlow's FitnessClass (models.py) stores only a start time, no explicit
   duration -- so, for scheduling purposes only, every class is treated as
   occupying a fixed DEFAULT_DURATION_MINUTES-long slot starting at its
   schedule time. Two slots conflict only if their intervals actually
   overlap (start_a < end_b and start_b < end_a) AND they share the same
   trainer (a person cannot teach two classes at once) or the same room (a
   room cannot hold two classes at once). This is O(n) in the number of
   already-scheduled classes near the candidate window -- fine at this
   scale, and the standard approach for a small number of resources; a
   larger deployment would index intervals (e.g. an interval tree) instead
   of scanning them, noted here rather than pretended away.

2. DEMAND SCORING (soft/optimisation criterion, not a hard rule). Among the
   conflict-free candidates, slots are ranked by how well past classes at
   the same weekday + hour-of-day bucket have actually filled, using the
   same Booking/attendance data risk_model.py (Section 4.8) trains on -- a
   trainer would rather open a class at a time slot that has reliably
   drawn a full room than one nobody has ever booked. A bucket with no
   history at all scores neutrally (0.5) rather than being penalised:
   "never tried" is not evidence of "won't work," the same small-sample
   honesty principle risk_model.py documents for its own predictions.

Nothing here is invented data -- every score comes from real seeded/booked
classes, and a slot with zero history is clearly distinguishable (score
exactly 0.5, no historical fill data) from one that is actually unpopular.
"""
from datetime import datetime, timedelta

from gymflow.models import FitnessClass

DEFAULT_DURATION_MINUTES = 60  # see module docstring: FitnessClass has no duration field
CANDIDATE_HOURS = (7, 9, 12, 15, 17, 18, 19)  # typical gym class start times
LOOKAHEAD_DAYS = 14
TOP_N = 5


def _interval(schedule: datetime, duration_minutes: int = DEFAULT_DURATION_MINUTES):
    return schedule, schedule + timedelta(minutes=duration_minutes)


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def _has_conflict(candidate_start: datetime, trainer_id: int, room: str, existing_classes: list) -> bool:
    c_start, c_end = _interval(candidate_start)
    room_norm = room.strip().lower()
    for other in existing_classes:
        o_start, o_end = _interval(other.schedule)
        if not _overlaps(c_start, c_end, o_start, o_end):
            continue
        if other.trainer_id == trainer_id or other.room.strip().lower() == room_norm:
            return True
    return False


def _demand_score(candidate_start: datetime, all_classes: list) -> float:
    """Historical average seat-fill fraction (0..1) among past classes that
    started on the same weekday at the same hour, across any trainer/room --
    0.5 (neutral) if that weekday+hour bucket has no history yet.
    """
    weekday, hour = candidate_start.weekday(), candidate_start.hour
    fills = [
        min(len(fc.confirmed_bookings()) / fc.capacity, 1.0)
        for fc in all_classes
        if fc.schedule.weekday() == weekday and fc.schedule.hour == hour and fc.capacity > 0
    ]
    return sum(fills) / len(fills) if fills else 0.5


def suggest_slots(trainer, room: str, after: datetime = None, top_n: int = TOP_N) -> list:
    """Returns up to `top_n` {"start": datetime, "score": float,
    "has_history": bool} dicts: conflict-free for this trainer AND this
    room, ranked by historical demand score (highest first), earliest
    start time first among ties. `after` defaults to now -- only future
    slots are ever suggested.
    """
    after = after or datetime.utcnow()
    room = (room or "").strip()
    if not room:
        return []  # a room is required to check for room-conflicts -- see module docstring
    all_classes = FitnessClass.query.all()
    # Only classes whose slot could possibly overlap the candidate window
    # matter for conflict-checking; no need to scan classes long past.
    nearby = [fc for fc in all_classes if fc.schedule >= after - timedelta(hours=6)]

    candidates = []
    day0 = after.date()
    for day_offset in range(LOOKAHEAD_DAYS):
        day = day0 + timedelta(days=day_offset)
        for hour in CANDIDATE_HOURS:
            start = datetime(day.year, day.month, day.day, hour, 0)
            if start <= after:
                continue
            if _has_conflict(start, trainer.id, room, nearby):
                continue
            weekday_h = (start.weekday(), start.hour)
            history = [
                fc for fc in all_classes
                if (fc.schedule.weekday(), fc.schedule.hour) == weekday_h and fc.capacity > 0
            ]
            score = _demand_score(start, all_classes)
            candidates.append({"start": start, "score": score, "has_history": bool(history)})

    candidates.sort(key=lambda c: (-c["score"], c["start"]))
    return candidates[:top_n]
