"""ClassRecommender -- item-based collaborative filtering ("members who
booked X also booked Y") that suggests upcoming classes a member hasn't
tried yet, instead of leaving them to scroll the whole catalog.

This is deliberately a small, from-scratch cosine-similarity computation
rather than pulled in from a library: at this data scale (dozens of
members, dozens of classes) a library would add a dependency without
adding any real accuracy, and writing the actual math out here keeps every
step of "why was this class recommended" checkable against the code --
the same honesty principle risk_model.py documents for its own
predictions.

Method
------
1. Build a member -> {class_name, ...} "has ever booked this class" set for
   every member with at least one booking. Grouped by CLASS NAME rather
   than individual FitnessClass row, because the same named class (e.g.
   "Yoga Flow") recurs at a new schedule every week, and what signals a
   taste in common is having both booked a Yoga class before, not the
   same literal database row (which will never repeat anyway).
2. For every pair of class names, compute the cosine similarity of their
   "which members booked this" vectors:
       similarity(A, B) = |members who booked both| / sqrt(|A's members| * |B's members|)
   the standard item-based collaborative filtering similarity measure.
3. For a target member, score every upcoming class NAME they have not yet
   tried as the sum of its similarity to every class name they HAVE
   booked, weighted higher for one they actually attended over one they
   only booked (attendance is stronger evidence of a real preference than
   a booking that was cancelled or never followed through on).
4. Rank descending, keep the single soonest upcoming instance of each
   distinct class name, return the top N.

Cold start (a member with no booking history at all, or nothing similar
currently on the schedule) is reported honestly via `reason` rather than
just showing an empty panel with no explanation.
"""
from collections import defaultdict
from math import sqrt

from gymflow.models import Booking

TOP_N = 3
MIN_SIMILARITY = 0.01  # cosine similarity; filters out near-zero noise from small samples


def _member_class_data():
    """Returns (member_sets, weights):
      member_sets: {member_id: {class_name, ...}} over every booking ever made
      weights:     {member_id: {class_name: preference_weight}}
    attended=True is the strongest signal (1.0), an unmarked/upcoming
    booking is a real but weaker signal (0.6), and a marked no-show is the
    weakest (0.3, they picked it but did not actually attend it).
    """
    sets = defaultdict(set)
    weights = defaultdict(dict)
    for booking in Booking.query.all():
        name = booking.fitness_class.name
        sets[booking.member_id].add(name)
        weight = 1.0 if booking.attended else (0.6 if booking.attended is None else 0.3)
        weights[booking.member_id][name] = max(weights[booking.member_id].get(name, 0.0), weight)
    return sets, weights


def _class_name_vectors(member_sets: dict) -> dict:
    """{class_name: {member_id, ...}} -- the inverse index of member_sets:
    which members have ever booked each class name.
    """
    vectors = defaultdict(set)
    for member_id, names in member_sets.items():
        for name in names:
            vectors[name].add(member_id)
    return vectors


def _similarity(a_members: set, b_members: set) -> float:
    if not a_members or not b_members:
        return 0.0
    shared = len(a_members & b_members)
    if shared == 0:
        return 0.0
    return shared / sqrt(len(a_members) * len(b_members))


def recommend_for(member, top_n: int = TOP_N) -> dict:
    """Returns {"recommendations": [FitnessClass, ...], "reason": str}.

    `recommendations` is capped at `top_n`, ranked highest-scoring first,
    restricted to classes that are actually still bookable right now
    (upcoming, a free seat, not already booked/waitlisted by this member,
    and not a class name they have already tried). `reason` is meant to be
    shown to the member directly, explaining why they're seeing this list
    -- or why it's empty -- rather than a silent gap in the dashboard.
    """
    from gymflow.class_manager import list_upcoming_classes
    from gymflow.models import Waitlist

    member_sets, weights = _member_class_data()
    my_names = member_sets.get(member.id, set())

    if not my_names:
        return {
            "recommendations": [],
            "reason": "Book your first class and recommendations will start tailoring to what you tend to like.",
        }

    name_vectors = _class_name_vectors(member_sets)
    my_booking_class_ids = {
        b.class_id for b in Booking.query.filter_by(member_id=member.id, status="confirmed").all()
    }
    my_waitlist_class_ids = {
        w.class_id for w in Waitlist.query.filter_by(member_id=member.id, status="waiting").all()
    }

    best_per_name = {}  # class_name -> (score, FitnessClass)
    for fc in list_upcoming_classes():
        if fc.id in my_booking_class_ids or fc.id in my_waitlist_class_ids:
            continue
        if fc.name in my_names or not fc.check_availability():
            continue
        score = sum(
            _similarity(name_vectors.get(fc.name, set()), name_vectors.get(my_name, set()))
            * weights[member.id].get(my_name, 0.5)
            for my_name in my_names
        )
        if score < MIN_SIMILARITY:
            continue
        if fc.name not in best_per_name or score > best_per_name[fc.name][0]:
            best_per_name[fc.name] = (score, fc)

    ranked = sorted(best_per_name.values(), key=lambda pair: -pair[0])[:top_n]
    if not ranked:
        return {
            "recommendations": [],
            "reason": "Nothing similar to what you've booked before is on the schedule right now — check back later or browse the full catalog.",
        }
    return {
        "recommendations": [fc for _, fc in ranked],
        "reason": "Based on classes that members with similar booking history to yours have also booked.",
    }
