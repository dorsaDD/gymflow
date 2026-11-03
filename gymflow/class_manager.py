"""ClassManager — fitness class scheduling and bookings (Section 3.5.11),
plus WaitlistManager (Section 4.7): the priority-queue algorithm that fills
a seat freed by a cancellation or a de-registration.
"""
from datetime import datetime
from typing import Optional

from gymflow.card_manager import ValidationError
from gymflow.extensions import db
from gymflow.mail_manager import send_email
from gymflow.models import Booking, FitnessClass, RewardRedemption, Waitlist


def create_class(trainer, data: dict, allow_past: bool = False) -> FitnessClass:
    """createClass(c) — a trainer defines a new fitness class.

    `allow_past=True` is for seed_demo_data.py ONLY, which intentionally
    creates a handful of already-happened classes so there is real
    attendance history (Section 4.7 reliability score, Section 4.8 no-show
    model) the moment you log in, instead of an empty "not enough data"
    message. Every real, trainer-facing path (the /classes/new form) uses
    the default False.
    """
    for field in ("name", "schedule", "room", "capacity"):
        if not data.get(field):
            raise ValidationError(f"'{field}' is required.")
    try:
        schedule = datetime.strptime(data["schedule"], "%Y-%m-%dT%H:%M")
        capacity = int(data["capacity"])
    except ValueError:
        raise ValidationError("Invalid schedule or capacity.")
    if capacity <= 0:
        raise ValidationError("Capacity must be a positive number.")
    # Known defect fix (Second Revision roadmap): a past schedule used to be
    # silently accepted, creating a class nobody could ever book -- it would
    # never appear in list_upcoming_classes() (which filters on
    # schedule >= now) and no member would ever see it, effectively a
    # permanently unreachable/dead record. Reject it up front instead,
    # except for the deliberate historical-seeding path above.
    if not allow_past and schedule < datetime.utcnow():
        raise ValidationError("Class schedule cannot be in the past.")

    fitness_class = FitnessClass(
        name=data["name"].strip(), trainer_id=trainer.id, schedule=schedule,
        room=data["room"].strip(), capacity=capacity,
    )
    db.session.add(fitness_class)
    db.session.commit()
    return fitness_class


def list_upcoming_classes():
    return (
        FitnessClass.query
        .filter(FitnessClass.schedule >= datetime.utcnow())
        .order_by(FitnessClass.schedule.asc())
        .all()
    )


def book_class(member, fitness_class: FitnessClass, reward_redemption_id=None) -> Booking:
    """bookClass(member, classId) — see the "Class Booking" sequence diagram."""
    existing = Booking.query.filter_by(
        class_id=fitness_class.id, member_id=member.id, status="confirmed"
    ).first()
    if existing:
        raise ValidationError("You already booked this class.")
    if not fitness_class.check_availability():
        raise ValidationError("This class is full.")

    redemption = None
    if reward_redemption_id:
        from gymflow import loyalty_engine
        redemption = RewardRedemption.query.get(reward_redemption_id)
        redemption = loyalty_engine.validate_redemption_for_member(redemption, member, "class_booking")
        if redemption.reward.reward_type != "free_class":
            raise ValidationError("That reward does not provide a free class credit.")

    booking = Booking(class_id=fitness_class.id, member_id=member.id, status="confirmed",
                      reward_redemption_id=redemption.id if redemption else None)
    if redemption:
        redemption.status = "used"
        redemption.used_at = datetime.utcnow()
    db.session.add(booking)
    db.session.commit()

    if member.notifications_enabled:
        send_email(
            member.email, "Booking confirmed",
            f"You are booked into '{fitness_class.name}' on "
            f"{fitness_class.schedule.strftime('%Y-%m-%d %H:%M')} ({fitness_class.room}).",
        )
    return booking


def cancel_booking(member, booking: Booking) -> None:
    """cancelBooking(m,c) — a member cancels their own confirmed booking."""
    if booking.member_id != member.id:
        raise ValidationError("You can only cancel your own bookings.")
    if booking.status != "confirmed":
        raise ValidationError("Only a confirmed booking can be cancelled.")
    fitness_class = booking.fitness_class
    booking.status = "cancelled"
    if booking.attended is True:
        from gymflow import loyalty_engine
        loyalty_engine.reverse_attendance_points(booking)
    if booking.reward_redemption_id:
        redemption = RewardRedemption.query.get(booking.reward_redemption_id)
        if redemption and redemption.status == "used":
            redemption.status = "reserved"
            redemption.used_at = None
    db.session.commit()
    if member.notifications_enabled:
        send_email(
            member.email, "Booking cancelled",
            f"Your booking for '{booking.fitness_class.name}' has been cancelled.",
        )
    promote_from_waitlist(fitness_class)


# ---------------------------------------------------------------------------
# 4.7 WaitlistManager — priority queue for a full class
# ---------------------------------------------------------------------------
# A member cannot book a full class, but can join its waitlist instead.
# When a seat frees up (a cancellation, above, or a de-registration in
# card_manager.deregister_user()), promote_from_waitlist() must decide WHICH
# waiting member gets it. A plain FIFO queue would reward whoever clicked
# first regardless of whether they reliably show up; instead the queue is
# ordered by a computed reliability score first and arrival time only as a
# tie-break, so a member with a track record of no-shows is not promoted
# ahead of a reliable one who joined the waitlist slightly later.
def reliability_score(member) -> float:
    """reliability(m) = attended-ratio over the member's past *marked*
    bookings, in [0.0, 1.0], defaulting to a neutral 0.5 when nothing has
    been marked yet (a brand-new member is neither penalised nor favoured).
    Only bookings staff have actually marked (attended is not None) count --
    an unmarked upcoming booking says nothing about reliability yet.
    """
    # Filtered in Python rather than with a SQL "IS NOT NULL" clause: this
    # keeps the query portable across the real SQLAlchemy engine and the
    # project's offline test shim (_devtools/shim/flask_sqlalchemy.py),
    # which only implements column equality.
    all_bookings = Booking.query.filter_by(member_id=member.id).all()
    marked = [b for b in all_bookings if b.attended is not None]
    if not marked:
        return 0.5
    attended = sum(1 for b in marked if b.attended)
    return attended / len(marked)


def join_waitlist(member, fitness_class: FitnessClass) -> Waitlist:
    """joinWaitlist(m, c) — a member queues for a class that is currently
    full. Rejected if the member already holds a confirmed booking or an
    active waitlist entry for this class, or if a seat is in fact free
    (they should just book it).
    """
    already_booked = Booking.query.filter_by(
        class_id=fitness_class.id, member_id=member.id, status="confirmed"
    ).first()
    if already_booked:
        raise ValidationError("You already booked this class.")
    already_waiting = Waitlist.query.filter_by(
        class_id=fitness_class.id, member_id=member.id, status="waiting"
    ).first()
    if already_waiting:
        raise ValidationError("You are already on the waitlist for this class.")
    if fitness_class.check_availability():
        raise ValidationError("A seat is free — book the class directly instead of waitlisting.")

    entry = Waitlist(class_id=fitness_class.id, member_id=member.id)
    db.session.add(entry)
    db.session.commit()
    return entry


def leave_waitlist(member, entry: Waitlist) -> None:
    """leaveWaitlist(m, e) — a member withdraws from a waitlist they joined."""
    if entry.member_id != member.id:
        raise ValidationError("You can only leave your own waitlist entry.")
    if entry.status != "waiting":
        raise ValidationError("This waitlist entry is no longer active.")
    entry.status = "left"
    db.session.commit()


def waitlist_position(member, fitness_class: FitnessClass) -> Optional[int]:
    """1-based position of `member` in `fitness_class`'s ordered waitlist, or
    None if they are not currently waiting. Same ordering as
    promote_from_waitlist() so what a member sees matches what will happen.
    """
    ordered = _ordered_waitlist(fitness_class)
    for i, entry in enumerate(ordered, start=1):
        if entry.member_id == member.id:
            return i
    return None


def _ordered_waitlist(fitness_class: FitnessClass) -> list:
    """The waiting entries for a class, ranked highest-priority first:
    primary key = reliability_score(member) descending, secondary key =
    joined_at ascending (earlier join wins a tie). O(n log n) in the size of
    that one class's waitlist, which is small and bounded by design (a
    member can only hold one active entry per class).
    """
    waiting = Waitlist.query.filter_by(class_id=fitness_class.id, status="waiting").all()
    return sorted(waiting, key=lambda e: (-reliability_score(e.member), e.joined_at))


def promote_from_waitlist(fitness_class: FitnessClass) -> Optional[Waitlist]:
    """promoteFromWaitlist(c) — called every time a confirmed seat on `c` is
    freed (cancellation or de-registration). Picks the highest-priority
    waiting entry (see _ordered_waitlist), converts it into a confirmed
    Booking, marks the entry "promoted", and notifies the member. Returns
    the promoted entry, or None if the class was full but nobody was
    waiting, or the freed seat was already re-filled.
    """
    if not fitness_class.check_availability():
        return None
    ordered = _ordered_waitlist(fitness_class)
    if not ordered:
        return None
    entry = ordered[0]
    entry.status = "promoted"
    booking = Booking(class_id=fitness_class.id, member_id=entry.member_id, status="confirmed")
    db.session.add(booking)
    db.session.commit()
    if entry.member.notifications_enabled:
        send_email(
            entry.member.email, "A seat opened up",
            f"A seat in '{fitness_class.name}' on "
            f"{fitness_class.schedule.strftime('%Y-%m-%d %H:%M')} freed up and you were "
            "next in line, so you are now booked in.",
        )
    return entry


# ---------------------------------------------------------------------------
# Attendance marking — the ground-truth label for reliability_score() above
# and for the no-show/churn risk model (Section 4.8).
# ---------------------------------------------------------------------------
def mark_attendance(booking: Booking, attended: bool) -> None:
    if booking.status != "confirmed":
        raise ValidationError("Only a confirmed booking can have its attendance marked.")
    was_attended = booking.attended is True
    booking.attended = attended
    db.session.commit()
    if attended and not was_attended:
        from gymflow import loyalty_engine
        loyalty_engine.apply_event("class_attended", f"attendance:{booking.id}", booking.member, booking=booking)
        db.session.commit()
    elif was_attended and not attended:
        from gymflow import loyalty_engine
        loyalty_engine.reverse_attendance_points(booking)
        db.session.commit()
