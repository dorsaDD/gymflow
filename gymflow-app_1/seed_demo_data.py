"""Populate GymFlow with realistic sample data for a presentation.

Everything is created through GymFlow's own business functions, by the
person who would really do it, so every screen shows genuine activity:

- the administrator creates the rewards tiers, rewards and offers
  (with their change history), and makes a point adjustment;
- trainers Tara and Marco run seven weeks of classes and mark attendance,
  which earns points through the live offers;
- receptionist Rita issues membership / class-pack / personal-training
  invoices, and refunds one;
- members pay invoices with the mock card processor (no real money),
  redeem points, book upcoming classes with free-class credits and join a
  waitlist.

Run once from the project folder (with your virtual environment active):

    python seed_demo_data.py

For a clean result, run it on a new database: stop the server, delete
gymflow.db (next to run.py), then run this script. Running it again is safe;
existing records are reused and nothing is duplicated.
"""
import json
import random
from datetime import datetime, timedelta

from gymflow import card_manager, class_manager, create_app, loyalty_engine, nutrition_manager, payment_manager
from gymflow.card_manager import _next_member_code
from gymflow.extensions import db
from gymflow.models import (
    Booking, Card, DietPlan, ExerciseLog, FitnessClass, FoodEntry, Invoice, LoyaltyTier,
    NutritionGoal, PointsLedgerEntry, PolicyApplication, PolicyAudit, PricingPolicy, ProgressNote,
    Reward, RewardRedemption, User, Waitlist, WorkoutPlan,
)

SAMPLE_PASSWORD = "Demo123!"
TEST_CARD = {"card_number": "4242424242424242", "expiry": "12/30", "cvv": "123"}  # mock processor only
NOW = datetime.utcnow().replace(second=0, microsecond=0)
LAUNCH = (NOW - timedelta(days=49)).replace(hour=9, minute=0)  # rewards programme start
rng = random.Random(2026)  # fixed seed: the same "realistic" history every time

app = create_app()


def say(kind, text):
    print(f"{kind:<15} {text}")


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------
def staff(role, name, surname, email):
    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(role=role, name=name, surname=surname, email=email, must_change_password=False)
        user.set_password(SAMPLE_PASSWORD)
        db.session.add(user)
        say("Created:", f"{role:<13} {name} {surname} <{email}>")
    else:
        user.name, user.surname = name, surname  # tidy names from older sample data
    db.session.commit()
    return user


def member(name, surname, email, birthdate, sex, address, phone, joined_days_ago):
    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(role="member", name=name, surname=surname, email=email, must_change_password=False)
        user.set_password(SAMPLE_PASSWORD)
        db.session.add(user)
        db.session.flush()
        db.session.add(Card(user_id=user.id, member_code=_next_member_code(),
                            birthdate=datetime.strptime(birthdate, "%Y-%m-%d").date(),
                            sex=sex, address=address, phone=phone))
        user.created_at = NOW - timedelta(days=joined_days_ago)  # real join date
        say("Created:", f"member        {name} {surname} <{email}>")
    else:
        user.name, user.surname = name, surname
    db.session.commit()
    return user


# ---------------------------------------------------------------------------
# Rewards programme set-up (administrator)
# ---------------------------------------------------------------------------
def reward(admin, name, description, reward_type, points_cost, value=None, minimum_tier=None,
           max_per_member=None, max_redemptions=None):
    existing = Reward.query.filter_by(name=name).first()
    if existing:
        return existing
    values = loyalty_engine.validate_reward({
        "name": name, "description": description, "reward_type": reward_type,
        "points_cost": str(points_cost), "value": "" if value is None else str(value),
        "target": "class_booking" if reward_type == "free_class" else "invoice",
        "minimum_tier": minimum_tier or "",
        "max_per_member": "" if max_per_member is None else str(max_per_member),
        "max_redemptions": "" if max_redemptions is None else str(max_redemptions),
    })
    item = Reward(**values, active=True, created_by_id=admin.id)
    item.created_at = LAUNCH
    db.session.add(item)
    db.session.commit()
    say("Reward:", name)
    return item


def _snapshot(policy):
    return json.dumps({"name": policy.name, "description": policy.description, "status": policy.status,
                       "event_type": policy.event_type, "definition": json.loads(policy.definition),
                       "priority": policy.priority,
                       "starts_at": policy.starts_at.isoformat() if policy.starts_at else None,
                       "ends_at": policy.ends_at.isoformat() if policy.ends_at else None,
                       "max_uses_per_member": policy.max_uses_per_member,
                       "max_uses_total": policy.max_uses_total})


def offer(admin, name, description, event_type, rule, priority=100, max_uses_per_member=None,
          max_uses_total=None, starts_at=None, ends_at=None, created=None):
    existing = PricingPolicy.query.filter_by(name=name).first()
    if existing:
        return existing
    definition = loyalty_engine.parse_rule_text(rule, event_type)  # same parser as the admin form
    created = created or LAUNCH
    policy = PricingPolicy(name=name, description=description, event_type=event_type,
                           definition=json.dumps(definition), status="active", priority=priority,
                           max_uses_per_member=max_uses_per_member, max_uses_total=max_uses_total,
                           starts_at=starts_at, ends_at=ends_at,
                           created_by_id=admin.id, updated_by_id=admin.id)
    policy.created_at = policy.updated_at = created
    db.session.add(policy)
    db.session.flush()
    audit = PolicyAudit(policy_id=policy.id, changed_by_id=admin.id, action="created", snapshot=_snapshot(policy))
    audit.created_at = created
    db.session.add(audit)
    db.session.commit()
    say("Offer:", name)
    return policy


def setup_rewards_programme(admin):
    tiers = {
        "Bronze": (0, "Welcome bonus, members' offers and rewards"),
        "Silver": (500, "Extra points on yoga & mobility, 10% off membership renewals, Silver rewards"),
        "Gold": (1200, "A free class every month, 15% off membership renewals, extra class points, Gold rewards"),
    }
    sample_texts = {"Entry rewards tier", "Sample benefit: 5% invoice discount reward",
                    "Sample benefit: priority access to selected rewards"}
    for tier in LoyaltyTier.query.all():
        if tier.name in tiers and (not tier.benefits or tier.benefits in sample_texts):
            tier.benefits = tiers[tier.name][1]
    db.session.commit()

    rewards = {
        "free_class": reward(admin, "Free class", "Swap points for one class of your choice.",
                             "free_class", 200, max_per_member=3),
        "ten_percent": reward(admin, "10% off one invoice", "Use on any invoice: membership, class pack or PT.",
                              "percent_discount", 250, value=10, minimum_tier="Silver", max_per_member=2),
        "ten_off": reward(admin, "$10 off one invoice", "A fixed $10 saving for Gold members.",
                          "fixed_discount", 400, value=10, minimum_tier="Gold", max_per_member=2),
        "gold_class": reward(admin, "Gold monthly free class", "Included with Gold: one free class every month.",
                             "free_class", 300, minimum_tier="Gold"),
        "wellness_class": reward(admin, "50+ wellness class", "A complimentary mobility or yoga class for members aged 50+.",
                                 "free_class", 150, max_per_member=1),
    }

    offer(admin, "Class attendance points",
          "Earn 10 points every time you attend a class (booking alone does not count).",
          "class_attended", "THEN award_points(10)", priority=10)
    offer(admin, "Monthly 8-class milestone",
          "Reach 8 attended classes in a calendar month and get a 50-point bonus.",
          "class_attended", "WHEN member.attended_classes_month == 8 THEN award_points(50)", priority=20)
    offer(admin, "Silver & Gold: extra points for yoga and mobility",
          "Silver and Gold members earn 10 extra points on yoga and mobility classes.",
          "class_attended",
          'WHEN member.tier IN ["Silver", "Gold"]\nAND (class.category == "yoga" OR class.category == "mobility")\n'
          "THEN award_points(10)", priority=30)
    offer(admin, "Purchase points",
          "Earn 25 points for every paid invoice.",
          "invoice_paid", "THEN award_points(25)", priority=50)
    offer(admin, "Gold: 15% off membership renewals",
          "Gold members save 15% when they renew their membership.",
          "invoice_paid",
          'WHEN member.tier == "Gold" AND member.membership_purchases >= 1\n'
          'THEN apply_discount(percent=15, target="membership")', priority=5)
    offer(admin, "Active training reward: 10% off membership",
          "Attend 8 or more classes in 30 days and your next membership payment is 10% off.",
          "invoice_paid",
          'WHEN member.attended_classes_30d >= 8 THEN apply_discount(percent=10, target="membership")', priority=10)
    offer(admin, "Silver: 10% off membership renewals",
          "Silver members save 10% when they renew their membership.",
          "invoice_paid",
          'WHEN member.tier == "Silver" AND member.membership_purchases >= 1\n'
          'THEN apply_discount(percent=10, target="membership")', priority=15)
    offer(admin, "Second purchase within 30 days: 5% off",
          "Buy again within 30 days of your first purchase and save 5% (once).",
          "invoice_paid",
          "WHEN member.purchase_count == 1 AND member.days_since_first_purchase <= 30\nTHEN apply_discount(percent=5)",
          priority=20, max_uses_per_member=1)
    offer(admin, "Welcome bonus",
          "New members get 20 bonus points on their first visit to the app.",
          "member_activity", 'WHEN member.tier == "Bronze" THEN award_points(20)',
          priority=10, max_uses_per_member=1)
    offer(admin, "Gold: free class every month",
          "Gold members receive a free class credit each month.",
          "member_activity", f'WHEN member.tier == "Gold" THEN grant_reward({rewards["gold_class"].id})', priority=5)
    offer(admin, "50+ wellness: one free class",
          "Members aged 50 and over receive one complimentary mobility or yoga class.",
          "member_activity", f'WHEN member.age >= 50 THEN grant_reward({rewards["wellness_class"].id})',
          priority=20, max_uses_per_member=1)
    offer(admin, "One-year loyalty bonus",
          "100 points to thank members who have been with us for a year.",
          "member_activity", "WHEN member.active_days >= 365 THEN award_points(100)",
          priority=30, max_uses_per_member=1)
    # A time-limited campaign that has already ended, and one scheduled ahead.
    summer = offer(admin, "Late-summer double points",
                   "Extra 10 points per attended class during the late-summer campaign.",
                   "class_attended", "THEN award_points(10)", priority=40,
                   starts_at=LAUNCH, ends_at=LAUNCH + timedelta(days=14))
    if summer.status == "active":
        summer.status = "expired"
        summer.updated_at = LAUNCH + timedelta(days=14)
        audit = PolicyAudit(policy_id=summer.id, changed_by_id=admin.id, action="expired", snapshot=_snapshot(summer))
        audit.created_at = LAUNCH + timedelta(days=14, hours=1)
        db.session.add(audit)
        db.session.commit()
    black_friday_start = (NOW + timedelta(days=25)).replace(hour=0, minute=0)
    offer(admin, "Black Friday weekend: 20% off membership",
          "Scheduled campaign: 20% off any membership bought over the Black Friday weekend.",
          "invoice_paid", 'THEN apply_discount(percent=20, target="membership")', priority=1,
          starts_at=black_friday_start, ends_at=black_friday_start + timedelta(days=4), created=NOW - timedelta(days=2))
    return rewards


def opening_balance(user, points):
    """Points carried over from the gym's previous paper stamp card when the
    digital programme launched (counts toward the tier)."""
    account = loyalty_engine.get_account(user.card)
    key = f"opening-balance:{user.id}"
    if PointsLedgerEntry.query.filter_by(event_key=key).first():
        return
    account.balance += points
    account.lifetime_points += points
    entry = PointsLedgerEntry(account_id=account.id, event_key=key, entry_type="earned", points=points,
                              qualifying_points=points, note="Opening balance from previous stamp card")
    entry.created_at = LAUNCH
    db.session.add(entry)
    db.session.commit()


# ---------------------------------------------------------------------------
# Classes and attendance (trainers)
# ---------------------------------------------------------------------------
WEEKLY = [  # (weekday Mon=0, hour, minute, name, category, trainer key, room, capacity)
    (0, 7, 0, "Sunrise Spin", "spin", "marco", "Cycle Studio", 12),
    (0, 18, 0, "Strength Foundations", "strength", "marco", "Weights Room", 10),
    (1, 8, 0, "Morning Yoga", "yoga", "tara", "Studio A", 14),
    (1, 19, 0, "Evening HIIT", "hiit", "marco", "Studio B", 10),
    (2, 10, 0, "Mobility Flow", "mobility", "tara", "Studio A", 12),
    (3, 8, 0, "Vinyasa Yoga", "yoga", "tara", "Studio A", 14),
    (3, 18, 30, "Pilates Core", "pilates", "tara", "Studio A", 12),
    (5, 10, 0, "Gentle Mobility (50+)", "mobility", "tara", "Studio A", 12),
]


def schedule_class(trainer, name, start, room, capacity, category):
    existing = FitnessClass.query.filter_by(name=name, schedule=start).first()
    if existing:
        return existing
    return class_manager.create_class(trainer, {
        "name": name, "schedule": start.strftime("%Y-%m-%dT%H:%M"), "room": room,
        "capacity": str(capacity), "category": category,
    }, allow_past=start < datetime.utcnow())


def weekly_classes(trainers, first_day, days):
    classes = []
    for offset in range(days):
        day = (first_day + timedelta(days=offset)).replace(second=0, microsecond=0)
        for weekday, hour, minute, name, category, trainer_key, room, capacity in WEEKLY:
            if day.weekday() == weekday:
                start = day.replace(hour=hour, minute=minute)
                classes.append(schedule_class(trainers[trainer_key], name, start, room, capacity, category))
    return sorted(classes, key=lambda c: c.schedule)


# Who goes to what, and how reliably (attend probability when booked).
HABITS = {
    "mona@example.com": ({"yoga", "pilates", "mobility"}, 0.75, 0.95),
    "leo@example.com": ({"strength", "hiit", "spin"}, 0.70, 0.90),
    "elena@example.com": ({"yoga", "pilates", "mobility"}, 0.95, 0.97),
    "giorgio@example.com": ({"mobility"}, 0.90, 0.95),
    "paolo@example.com": ({"hiit", "strength"}, 0.45, 0.85),
    "ivan@example.com": ({"spin", "hiit"}, 0.60, 0.15),  # books, rarely shows up
    "sara@example.com": ({"yoga", "hiit"}, 0.45, 0.85),
    "chiara@example.com": ({"pilates", "yoga"}, 0.40, 0.90),
    "luca@example.com": ({"spin", "hiit", "strength"}, 0.95, 0.95),
    "anna@example.com": ({"pilates"}, 0.30, 0.90),
    "matteo@example.com": ({"spin"}, 0.25, 0.80),
    "marta@example.com": ({"yoga", "pilates"}, 0.80, 0.90, 26),     # stopped coming ~4 weeks ago
    "davide@example.com": ({"strength", "spin"}, 0.75, 0.85, 22),   # stopped coming ~3 weeks ago
    "sofia@example.com": ({"yoga", "hiit"}, 0.60, 0.90),
}


def backdate_attendance(booking):
    """Ledger rows and policy applications get the class time, not 'now'."""
    when = booking.fitness_class.schedule + timedelta(hours=1)
    suffix = f"attendance:{booking.id}"
    for row in PolicyApplication.query.filter_by(member_id=booking.member_id, event_type="class_attended").all():
        if row.event_key.endswith(suffix):
            row.created_at = when
    account = loyalty_engine.get_account(booking.member.card, create=False)
    if account:
        for entry in PointsLedgerEntry.query.filter_by(account_id=account.id).all():
            if entry.event_key.endswith(suffix):
                entry.created_at = when
        for redemption in RewardRedemption.query.filter_by(account_id=account.id).all():
            if redemption.redeemed_at >= NOW - timedelta(minutes=5) and redemption.policy_id:
                redemption.redeemed_at = when
    db.session.commit()


def run_class_history(members, past_classes):
    for user in members:
        user.notifications_enabled = False  # don't fill the outbox with seven weeks of booking emails
    db.session.commit()
    for fitness_class in past_classes:
        for user in members:
            if user.card is None or user.created_at > fitness_class.schedule:
                continue  # not a member yet
            categories, book_rate, show_rate, *stopped = HABITS[user.email]
            if stopped and fitness_class.schedule > NOW - timedelta(days=stopped[0]):
                continue  # this member stopped coming
            if fitness_class.category not in categories or rng.random() > book_rate:
                continue
            if Booking.query.filter_by(class_id=fitness_class.id, member_id=user.id).first():
                continue
            if not fitness_class.check_availability():
                continue
            booking = class_manager.book_class(user, fitness_class)
            booking.created_at = fitness_class.schedule - timedelta(days=rng.randint(1, 4))
            db.session.commit()
            class_manager.mark_attendance(booking, attended=rng.random() < show_rate)
            backdate_attendance(booking)
    for user in members:
        user.notifications_enabled = True
    db.session.commit()


# ---------------------------------------------------------------------------
# Billing (receptionist issues, member pays)
# ---------------------------------------------------------------------------
def invoice(rita, user, description, amount, category, issued_days_ago, due_in_days=14,
            pay=False, paid_days_ago=None, reward_redemption=None):
    existing = Invoice.query.filter_by(card_id=user.card.id, description=description).first()
    if existing:
        return existing
    issued = NOW - timedelta(days=issued_days_ago)
    item = payment_manager.create_invoice(user.card, rita, {
        "description": description, "amount": str(amount), "category": category,
        "due_date": (issued + timedelta(days=due_in_days)).strftime("%Y-%m-%d"),
    })
    item.issued_at = issued
    db.session.commit()
    if pay:
        data = dict(TEST_CARD)
        if reward_redemption is not None:
            data["reward_redemption_id"] = str(reward_redemption.id)
        payment_manager.pay_invoice(item, data)
        if paid_days_ago is not None:
            paid = NOW - timedelta(days=paid_days_ago)
            item.paid_at = paid
            for row in PolicyApplication.query.filter_by(invoice_id=item.id).all():
                row.created_at = paid
            account = loyalty_engine.get_account(user.card, create=False)
            for entry in PointsLedgerEntry.query.filter_by(account_id=account.id).all():
                if entry.event_key.endswith(f"invoice:{item.id}"):
                    entry.created_at = paid
            db.session.commit()
        discount = f", {item.discount_amount:.2f} off" if item.discount_amount else ""
        say("Paid:", f"{user.full_name}: {description} (${item.paid_amount:.2f}{discount})")
    else:
        say("Invoice:", f"{user.full_name}: {description} (${amount:.2f}, pending)")
    return item


def monthly_check(user):
    """Exactly what opening the dashboard does: this month's member_activity check."""
    loyalty_engine.apply_event("member_activity", f"member:{user.id}:{datetime.utcnow():%Y-%m}", user)
    db.session.commit()


def seed_everything():
    admin = User.query.filter_by(role="administrator").first()
    tara = staff("trainer", "Tara", "Bianchi", "tara@gymflow.local")
    marco = staff("trainer", "Marco", "Conti", "marco@gymflow.local")
    rita = staff("receptionist", "Rita", "Esposito", "rita@gymflow.local")

    mona = member("Mona", "Rossi", "mona@example.com", "1995-04-12", "F", "Via Roma 12, Milano", "+39 333 100 1001", 200)
    leo = member("Leo", "Lopez", "leo@example.com", "1990-09-03", "M", "Via Torino 45, Milano", "+39 333 100 1002", 320)
    elena = member("Elena", "Greco", "elena@example.com", "1974-02-18", "F", "Viale Monza 88, Milano", "+39 333 100 1005", 120)
    giorgio = member("Giorgio", "Romano", "giorgio@example.com", "1962-07-30", "M", "Via Padova 7, Milano", "+39 333 100 1006", 420)
    paolo = member("Paolo", "Marino", "paolo@example.com", "1986-11-02", "M", "Corso Buenos Aires 3, Milano", "+39 333 100 1007", 60)
    ivan = member("Ivan", "Petrov", "ivan@example.com", "1993-11-11", "M", "Via Mecenate 5, Milano", "+39 333 100 1004", 90)
    sara = member("Sara", "Ferrari", "sara@example.com", "1998-01-20", "F", "Via Tortona 9, Milano", "+39 333 100 1003", 24)
    chiara = member("Chiara", "Colombo", "chiara@example.com", "2000-05-14", "F", "Via Solari 21, Milano", "+39 333 100 1008", 6)
    luca = member("Luca", "Bruno", "luca@example.com", "1997-03-08", "M", "Via Vigevano 14, Milano", "+39 333 100 1009", 250)
    anna = member("Anna", "Gallo", "anna@example.com", "1991-06-25", "F", "Via Lecco 2, Milano", "+39 333 100 1010", 180)
    matteo = member("Matteo", "Rizzo", "matteo@example.com", "1988-12-01", "M", "Via Bergamo 30, Milano", "+39 333 100 1011", 200)
    marta = member("Marta", "Ricci", "marta@example.com", "1985-09-17", "F", "Via Savona 52, Milano", "+39 333 100 1012", 300)
    davide = member("Davide", "Costa", "davide@example.com", "1979-04-04", "M", "Viale Lombardia 19, Milano", "+39 333 100 1013", 400)
    sofia = member("Sofia", "Fontana", "sofia@example.com", "2003-02-11", "F", "Via Porpora 77, Milano", "+39 333 100 1014", 10)
    members = [mona, leo, elena, giorgio, paolo, ivan, sara, chiara, luca, anna, matteo, marta, davide, sofia]

    old_samples = PricingPolicy.query.filter(PricingPolicy.name.like("DEMO:%")).count()
    if old_samples:
        print("\nNOTE: this database still has older sample offers named 'DEMO: ...'.")
        print("      For a clean presentation: stop the server, delete gymflow.db, run this script again.\n")

    if PricingPolicy.query.filter_by(name="Class attendance points").first():
        print("\nSample history already exists; only checking accounts and this month's perks.")
        for user in members:
            monthly_check(user)
        return

    rewards = setup_rewards_programme(admin)
    for user, points in ((mona, 420), (leo, 1120), (elena, 150), (giorgio, 280), (paolo, 60), (ivan, 40),
                         (luca, 700), (anna, 120), (matteo, 90), (marta, 380), (davide, 520)):
        opening_balance(user, points)
    say("Points:", "opening balances from the previous stamp card")

    # First membership payments, 5-7 weeks ago (before the class history).
    invoice(rita, mona, "Monthly membership — September", 49.00, "membership", 45, pay=True, paid_days_ago=44)
    invoice(rita, leo, "Quarterly membership — Sep–Nov", 135.00, "membership", 48, pay=True, paid_days_ago=47)
    invoice(rita, elena, "Monthly membership — September", 49.00, "membership", 40, pay=True, paid_days_ago=39)
    invoice(rita, giorgio, "Monthly membership — September", 49.00, "membership", 42, pay=True, paid_days_ago=41)
    invoice(rita, paolo, "Monthly membership — October", 49.00, "membership", 21, pay=True, paid_days_ago=20)
    ivan_pack = invoice(rita, ivan, "10-class pack", 90.00, "class_pack", 38, pay=True, paid_days_ago=37)
    invoice(rita, sara, "Monthly membership — October", 49.00, "membership", 24, pay=True, paid_days_ago=23)
    invoice(rita, luca, "Quarterly membership — Sep–Nov", 135.00, "membership", 50, pay=True, paid_days_ago=49)
    invoice(rita, luca, "Personal training — 4 sessions", 160.00, "personal_training", 12, pay=True, paid_days_ago=11)
    invoice(rita, anna, "10-class pack", 90.00, "class_pack", 60, pay=True, paid_days_ago=58)
    invoice(rita, matteo, "10-class pack", 90.00, "class_pack", 70, pay=True, paid_days_ago=69)
    invoice(rita, marta, "Monthly membership — September", 49.00, "membership", 46, pay=True, paid_days_ago=45)
    invoice(rita, davide, "Monthly membership — September", 49.00, "membership", 47, pay=True, paid_days_ago=46)
    invoice(rita, sofia, "Monthly membership — November (new member)", 49.00, "membership", 9, pay=True, paid_days_ago=9)

    trainers = {"tara": tara, "marco": marco}
    past = [c for c in weekly_classes(trainers, NOW - timedelta(days=42), 42) if c.schedule < NOW]
    upcoming = [c for c in weekly_classes(trainers, NOW + timedelta(days=1), 7)]
    say("Classes:", f"{len(past)} past and {len(upcoming)} upcoming classes (Tara Bianchi, Marco Conti)")
    run_class_history(members, past)
    marked = Booking.query.filter(Booking.attended.isnot(None)).count()
    say("Attendance:", f"{marked} bookings marked by the trainers (points earned through the live offers)")

    # Refund: Ivan stopped coming; Rita refunds his class pack (points are reversed).
    payment_manager.refund_invoice(ivan_pack, rita)
    ivan_pack.refunded_at = NOW - timedelta(days=10)
    db.session.commit()
    say("Refund:", "Rita refunded Ivan Petrov's 10-class pack")

    # This month's dashboard check for everyone (welcome bonus, Gold perk, 50+ class, 1-year bonus).
    for user in members:
        monthly_check(user)

    # Members use their points.
    loyalty_engine.redeem_reward(mona, rewards["ten_percent"])
    say("Redeemed:", "Mona Rossi: 10% off one invoice (250 points)")
    loyalty_engine.redeem_reward(elena, rewards["free_class"])
    say("Redeemed:", "Elena Greco: Free class (200 points)")

    # Renewals this week — the automatic offers apply at payment time.
    invoice(rita, leo, "Quarterly membership — Dec–Feb", 135.00, "membership", 3, pay=True)   # Gold 15%
    invoice(rita, elena, "Monthly membership — November", 49.00, "membership", 2, pay=True)  # 8+ classes in 30 days
    invoice(rita, giorgio, "Monthly membership — November", 49.00, "membership", 2, pay=True)
    invoice(rita, paolo, "Personal training — 2 sessions", 80.00, "personal_training", 1, pay=True)  # 2nd purchase < 30 days
    invoice(rita, chiara, "Monthly membership — November (new member)", 49.00, "membership", 5, pay=True, paid_days_ago=5)
    # Open items for the Billing pages.
    invoice(rita, mona, "Personal training — 1 session", 45.00, "personal_training", 1)  # Mona can use her 10% reward
    invoice(rita, mona, "Monthly membership — November", 49.00, "membership", 1)
    invoice(rita, ivan, "Monthly membership — October", 49.00, "membership", 30, due_in_days=10)  # overdue
    invoice(rita, sara, "Monthly membership — November", 49.00, "membership", 4)
    invoice(rita, marta, "Monthly membership — October", 49.00, "membership", 32, due_in_days=10)  # overdue

    # Upcoming bookings, including free-class credits and a full class with a waitlist.
    def book(user, fitness_class, credit=None):
        if not Booking.query.filter_by(class_id=fitness_class.id, member_id=user.id, status="confirmed").first():
            class_manager.book_class(user, fitness_class, credit.id if credit else None)

    first = {}
    for c in upcoming:
        first.setdefault(c.name, c)
    giorgio_credit = RewardRedemption.query.join(Reward).filter(
        Reward.name == "50+ wellness class", RewardRedemption.status == "reserved",
        RewardRedemption.account_id == loyalty_engine.get_account(giorgio.card).id).first()
    book(giorgio, first["Gentle Mobility (50+)"], giorgio_credit)
    if giorgio_credit:
        say("Booked:", "Giorgio Romano used his 50+ wellness class for Gentle Mobility")
    book(mona, first["Morning Yoga"])
    book(mona, first["Pilates Core"])
    book(elena, first["Vinyasa Yoga"])
    book(sara, first["Morning Yoga"])
    book(paolo, first["Strength Foundations"])
    hiit = first["Evening HIIT"]
    hiit.capacity = 4
    db.session.commit()
    for user in (leo, paolo, sara, chiara):
        book(user, hiit)
    if not Waitlist.query.filter_by(class_id=hiit.id, member_id=ivan.id).first():
        class_manager.join_waitlist(ivan, hiit)
    say("Booked:", "upcoming classes; Evening HIIT is full and Ivan Petrov is on its waitlist")

    # Administrator saw the "At risk" group on the Member segments page
    # (K-Means) and created a targeted win-back offer for it.
    offer(admin, "Win-back: 50 points for at-risk members",
          "Members in the 'At risk' segment get 50 bonus points the next time they attend a class.",
          "class_attended", 'WHEN member.segment == "At risk" THEN award_points(50)',
          priority=15, max_uses_per_member=1, created=NOW - timedelta(days=1))

    # Administrator: a goodwill adjustment and an edited offer (shows in the change history).
    loyalty_engine.adjust_points(paolo, 30, "Goodwill: Strength class on 14 Oct cancelled by the gym", admin)
    say("Adjustment:", "admin added 30 points to Paolo Marino (cancelled class)")
    milestone = PricingPolicy.query.filter_by(name="Monthly 8-class milestone").first()
    milestone.description = "Reach 8 attended classes in a calendar month and get a 50-point bonus. Counts every class type."
    milestone.updated_at = NOW - timedelta(days=12)
    audit = PolicyAudit(policy_id=milestone.id, changed_by_id=admin.id, action="updated", snapshot=_snapshot(milestone))
    audit.created_at = NOW - timedelta(days=12)
    db.session.add(audit)
    db.session.commit()

    # Trainer notes, plans and nutrition (Sections 3.5.12, 3.5.13, 4.11).
    if not ProgressNote.query.filter_by(card_id=mona.card.id).first():
        card_manager.add_progress_note(mona.card, tara, "Great progress in Pilates Core — plank hold up to 90 s.")
        card_manager.add_progress_note(leo.card, marco, "Deadlift form solid at 100 kg; next block focuses on hip mobility.")
        card_manager.add_progress_note(giorgio.card, tara, "Shoulder range of motion improving; keep the band routine at home.")
        card_manager.add_progress_note(ivan.card, marco, "Missed several booked classes — follow up on schedule fit.")
    if not WorkoutPlan.query.filter_by(card_id=mona.card.id).first():
        card_manager.assign_workout_plan(mona.card, tara, "Core & Flexibility — 6 weeks",
                                         "2x Pilates Core, 1x Vinyasa Yoga per week; 10 min daily hip-opener routine.")
        card_manager.assign_workout_plan(leo.card, marco, "Strength Block B — 4 weeks",
                                         "Mon: squat 5x5, bench 5x5. Thu: deadlift 3x5, rows 4x8. Sat: 30 min Zone 2 cardio.")
        card_manager.assign_workout_plan(giorgio.card, tara, "Healthy Joints Programme",
                                         "2x Gentle Mobility per week, daily 15 min walk, resistance-band shoulder set.")
    seed_nutrition(tara, mona, leo)


def seed_nutrition(tara, mona, leo):
    if NutritionGoal.query.filter_by(card_id=mona.card.id).first():
        return
    nutrition_manager.set_goal(mona.card, tara, {"daily_calories": "2000", "protein_g": "120",
                                                 "carbs_g": "200", "fat_g": "65"})
    nutrition_manager.assign_diet_plan(
        mona.card, tara, "High-Protein Lean Muscle Plan — 6 weeks",
        "Target ~2000 kcal/day: 120g protein, 200g carbs, 65g fat. A lean protein source at every meal, "
        "whole grains over refined carbs, and a post-workout shake within 30 minutes of training.")
    meal_sets = [
        [("breakfast", "Oatmeal with banana & almond butter", 420, 14, 58, 14),
         ("lunch", "Grilled chicken breast, brown rice & broccoli", 620, 52, 60, 16),
         ("dinner", "Salmon, quinoa & roasted vegetables", 580, 42, 45, 22),
         ("snack", "Greek yogurt with berries", 180, 15, 20, 3)],
        [("breakfast", "Scrambled eggs, whole-wheat toast & avocado", 450, 24, 34, 24),
         ("lunch", "Turkey & hummus wrap with side salad", 540, 38, 52, 18),
         ("dinner", "Lean beef stir-fry with brown rice", 610, 46, 58, 20),
         ("snack", "Protein shake", 150, 25, 8, 2)],
    ]
    for days_ago in range(7):
        log_date = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        for meal, food, kcal, protein, carbs, fat in meal_sets[days_ago % 2]:
            nutrition_manager.log_food(mona.card, {"date": log_date, "meal": meal, "food_name": food,
                                                   "calories": kcal, "protein_g": protein,
                                                   "carbs_g": carbs, "fat_g": fat})
    for days_ago, activity, minutes, kcal, notes in [
        (0, "Running", 30, 300, "Easy pace, 5K loop"), (1, "Strength training", 45, 250, "Upper body"),
        (3, "Running", 35, 320, ""), (5, "Strength training", 50, 280, "Lower body")]:
        nutrition_manager.log_exercise(mona.card, {"date": (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d"),
                                                   "activity": activity, "duration_minutes": minutes,
                                                   "calories_burned": kcal, "notes": notes})
    nutrition_manager.set_goal(leo.card, tara, {"daily_calories": "2200", "protein_g": "140",
                                                "carbs_g": "220", "fat_g": "70"})
    nutrition_manager.assign_diet_plan(
        leo.card, tara, "Calorie Deficit Reset — 4 weeks",
        "Logged intake has been running well above target. Swap fast food for a lean-protein-plus-vegetables "
        "plate at lunch and dinner, keep dessert to twice a week, and check in with Tara after 2 weeks.")
    for days_ago in (0, 2, 4):
        log_date = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        for meal, food, kcal, protein, carbs, fat in [
            ("lunch", "Double cheeseburger with fries", 1150, 45, 90, 60),
            ("dinner", "Pepperoni pizza (half pie)", 1400, 55, 140, 65),
            ("snack", "Ice cream", 350, 5, 45, 15)]:
            nutrition_manager.log_food(leo.card, {"date": log_date, "meal": meal, "food_name": food,
                                                  "calories": kcal, "protein_g": protein,
                                                  "carbs_g": carbs, "fat_g": fat})
    say("Nutrition:", "goals, diet plans and diaries for Mona (on target) and Leo (over target)")


def summary():
    print("\nRewards overview:")
    for user in User.query.filter_by(role="member").order_by(User.surname).all():
        account = loyalty_engine.get_account(user.card, create=False) if user.card else None
        tier = loyalty_engine.current_tier(account)
        ready = RewardRedemption.query.filter_by(account_id=account.id, status="reserved").count() if account else 0
        print(f"  {user.full_name:<17} {user.email:<22} {(tier.name if tier else '-'):<7} "
              f"{(account.lifetime_points if account else 0):>5} lifetime  {max(account.balance, 0) if account else 0:>5} spendable"
              f"  {ready} reward(s) ready")


with app.app_context():
    print("Seeding GymFlow sample data...\n")
    seed_everything()
    summary()
    print("\nDone! Open http://localhost:5000 and log in (password for all sample accounts: Demo123!):\n")
    print("  admin@gymflow.local  / Admin123!  administrator: Pricing & Rewards, Staff, Mail Outbox, all billing")
    print("  tara@gymflow.local   trainer (yoga, pilates, mobility)   marco@gymflow.local  trainer (strength, HIIT, spin)")
    print("  rita@gymflow.local   receptionist: issues invoices, refunds, registers members")
    print("  leo@example.com      Gold: monthly free class ready, 15% Gold renewal discount on his latest invoice")
    print("  mona@example.com     Silver: 10% reward ready for her pending personal-training invoice")
    print("  elena@example.com    52, very active: 10% 'active training' membership discount, 50+ free class")
    print("  giorgio@example.com  64, member for over a year: 1-year bonus, used his 50+ free class to book")
    print("  ivan@example.com     at-risk: many no-shows, refunded pack, overdue invoice, on a waitlist")
    print("  sara / paolo / chiara @example.com  newer members (Bronze; Paolo got the 2nd-purchase 5% discount)")
    print("  luca / anna / matteo / marta / davide / sofia @example.com  more members for the K-Means segments")
    print("\n  Trainers/admin: open 'ML Insights' for the no-show model and the Member segments page.")
