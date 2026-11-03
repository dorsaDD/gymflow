"""Populate GymFlow with realistic demo data across the existing features,
including members, staff, classes, attendance, nutrition, rewards, points,
policies, and invoices, instead of empty screens after a fresh install.

Run once, with your virtual environment activated, from the project root
(the folder that has run.py in it):

    python seed_demo_data.py

Safe to run more than once — demo accounts, policies, invoices, and ledger
events have stable keys and are only added once. Existing policy/reward edits
are preserved.
"""
import json
from datetime import datetime, timedelta

from gymflow import card_manager, class_manager, create_app, loyalty_engine, nutrition_manager, payment_manager
from gymflow.card_manager import _next_member_code
from gymflow.extensions import db
from gymflow.models import (
    Booking, Card, DietPlan, ExerciseLog, FitnessClass, FoodEntry, Invoice, LoyaltyAccount,
    LoyaltyTier, NutritionGoal, PointsLedgerEntry, PolicyAudit, PricingPolicy, ProgressNote,
    Reward, RewardRedemption, User, WorkoutPlan,
)

DEMO_PASSWORD = "Demo123!"

app = create_app()


def get_or_create_staff(role, name, surname, email):
    user = User.query.filter_by(email=email).first()
    if user:
        print(f"Already exists: {role:<12} {email} / {DEMO_PASSWORD}")
        return user
    user = User(role=role, name=name, surname=surname, email=email, must_change_password=False)
    user.set_password(DEMO_PASSWORD)
    db.session.add(user)
    db.session.commit()
    print(f"Created:        {role:<12} {email} / {DEMO_PASSWORD}")
    return user


def get_or_create_member(name, surname, email, birthdate, sex, address, phone):
    user = User.query.filter_by(email=email).first()
    if user:
        print(f"Already exists: member       {email} / {DEMO_PASSWORD}  (card {user.card.member_code})")
        return user
    user = User(role="member", name=name, surname=surname, email=email, must_change_password=False)
    user.set_password(DEMO_PASSWORD)
    db.session.add(user)
    db.session.flush()  # get user.id before creating the card
    card = Card(
        user_id=user.id, member_code=_next_member_code(),
        birthdate=datetime.strptime(birthdate, "%Y-%m-%d").date(),
        sex=sex, address=address, phone=phone,
    )
    db.session.add(card)
    db.session.commit()
    print(f"Created:        member       {email} / {DEMO_PASSWORD}  (card {card.member_code})")
    return user


def get_or_create_class(trainer, name, days_from_now, hour, room, capacity):
    existing = FitnessClass.query.filter_by(name=name).first()
    if existing:
        print(f"Already exists: class        {name}")
        return existing
    schedule = (datetime.utcnow() + timedelta(days=days_from_now)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    # allow_past=True: a few seeded classes below are deliberately dated in
    # the past (days_from_now negative) so there is real attendance history
    # to show off Sections 4.7/4.8 immediately -- see create_class()'s
    # docstring for why this is the one legitimate use of that flag.
    fitness_class = class_manager.create_class(trainer, {
        "name": name, "schedule": schedule.strftime("%Y-%m-%dT%H:%M"),
        "room": room, "capacity": str(capacity),
    }, allow_past=(days_from_now < 0))
    print(f"Created:        class        {name} ({schedule.strftime('%Y-%m-%d %H:%M')}, {room})")
    return fitness_class


def get_or_create_demo_reward(admin, name, description, reward_type, points_cost,
                              value=None, target="invoice", minimum_tier=None,
                              max_per_member=None):
    reward = Reward.query.filter_by(name=name).first()
    if reward:
        return reward
    values = loyalty_engine.validate_reward({
        "name": name, "description": description, "reward_type": reward_type,
        "points_cost": str(points_cost), "value": "" if value is None else str(value),
        "target": target, "minimum_tier": minimum_tier,
        "max_per_member": "" if max_per_member is None else str(max_per_member),
    })
    reward = Reward(**values, active=True, created_by_id=admin.id)
    db.session.add(reward)
    db.session.commit()
    print(f"Added:          demo reward  {name}")
    return reward


def get_or_create_demo_policy(admin, name, description, event_type, definition,
                              priority, max_uses_per_member=None):
    policy = PricingPolicy.query.filter_by(name=name).first()
    if policy:
        return policy
    checked = loyalty_engine.validate_definition(event_type, definition)
    policy = PricingPolicy(
        name=name, description=description, event_type=event_type,
        definition=json.dumps(checked), status="active", priority=priority,
        max_uses_per_member=max_uses_per_member,
        created_by_id=admin.id, updated_by_id=admin.id,
    )
    db.session.add(policy)
    db.session.flush()
    db.session.add(PolicyAudit(
        policy_id=policy.id, changed_by_id=admin.id, action="created",
        snapshot=json.dumps({"name": name, "status": "active", "event_type": event_type,
                            "definition": checked, "source": "demo seed"}),
    ))
    db.session.commit()
    print(f"Added:          demo policy  {name}")
    return policy


def get_or_create_demo_invoice(card, admin, description, amount, pay=False):
    invoice = Invoice.query.filter_by(card_id=card.id, description=description).first()
    if not invoice:
        invoice = payment_manager.create_invoice(card, admin, {
            "description": description, "amount": str(amount),
        })
        print(f"Added:          demo invoice {description} (${amount:.2f})")
    if pay and invoice.status == "pending":
        payment_manager.pay_invoice(invoice, {
            # GymFlow's processor is a local mock; this never charges a real card.
            "card_number": "4242424242424242", "expiry": "12/30", "cvv": "123",
        })
        print(f"Paid:           demo invoice {description} through the mock processor")
    return invoice


def add_demo_qualifying_points(member, target):
    account = loyalty_engine.get_account(member.card)
    event_key = f"demo-seed-points:{member.id}"
    if PointsLedgerEntry.query.filter_by(event_key=event_key).first():
        return
    delta = max(0, target - account.lifetime_points)
    if not delta:
        return
    account.balance += delta
    account.lifetime_points += delta
    account.updated_at = datetime.utcnow()
    db.session.add(PointsLedgerEntry(
        account_id=account.id, event_key=event_key, entry_type="earned",
        points=delta, qualifying_points=delta,
        note="Demo seed: sample historical campaign points", created_by_id=None,
    ))
    db.session.commit()
    print(f"Added:          demo points  {member.full_name} +{delta} (lifetime qualifying points)")


def seed_loyalty_demo(admin, mona, leo, sara, ivan):
    """Seed visible, repeatable loyalty examples without replacing edited data."""
    tier_benefits = {
        "Bronze": "Entry rewards tier",
        "Silver": "Access to Silver rewards and member offers",
        "Gold": "Access to Gold rewards and premium offers",
    }
    for tier in LoyaltyTier.query.all():
        if tier.name in tier_benefits and not tier.benefits:
            tier.benefits = tier_benefits[tier.name]
    db.session.commit()

    silver_discount = get_or_create_demo_reward(
        admin, "Demo: Silver 10% invoice discount", "Redeem points for 10% off one invoice.",
        "percent_discount", 250, value=10, minimum_tier="Silver", max_per_member=2,
    )
    get_or_create_demo_reward(
        admin, "Demo: Gold $10 invoice discount", "A fixed discount for Gold Rewards Tier members.",
        "fixed_discount", 400, value=10, minimum_tier="Gold", max_per_member=2,
    )
    free_class = get_or_create_demo_reward(
        admin, "Demo: Free class credit", "Use points for a class booking, or receive one through the age offer.",
        "free_class", 200, target="class_booking", max_per_member=3,
    )

    get_or_create_demo_policy(
        admin, "DEMO: 10 points per attended class",
        "Awards only after a confirmed booking is marked attended.",
        "class_attended", {"conditions": [], "action": {"type": "award_points", "points": 10}}, 20,
    )
    get_or_create_demo_policy(
        admin, "DEMO: 50-point monthly class milestone",
        "Awards a monthly bonus after eight attended classes.",
        "class_attended", {"conditions": [
            {"fact": "member.attended_classes_month", "op": "gte", "value": 8},
        ], "action": {"type": "award_points", "points": 50}}, 30,
    )
    get_or_create_demo_policy(
        admin, "DEMO: 5% off a second invoice within 30 days",
        "Applies automatically to a second paid invoice in the 30-day window.",
        "invoice_paid", {"conditions": [
            {"fact": "member.purchase_count", "op": "eq", "value": 1},
            {"fact": "member.days_since_first_purchase", "op": "lte", "value": 30},
        ], "action": {"type": "discount_percent", "percent": 5}}, 10,
        max_uses_per_member=1,
    )
    get_or_create_demo_policy(
        admin, "DEMO: 25 points per paid invoice",
        "Purchase points are recorded only after a successful payment.",
        "invoice_paid", {"conditions": [], "action": {"type": "award_points", "points": 25}}, 20,
    )
    active_days_policy = get_or_create_demo_policy(
        admin, "DEMO: 50 points after 30 active days",
        "Checks account age on the member's next dashboard visit.",
        "member_activity", {"conditions": [
            {"fact": "member.active_days", "op": "gte", "value": 30},
        ], "action": {"type": "award_points", "points": 50}}, 20,
        max_uses_per_member=1,
    )
    age_offer = get_or_create_demo_policy(
        admin, "DEMO: Free class for members age 25–35",
        "Grants one free class credit after an attended class for this age range.",
        "class_attended", {"conditions": [
            {"fact": "member.age", "op": "gte", "value": 25},
            {"fact": "member.age", "op": "lte", "value": 35},
        ], "action": {"type": "grant_reward", "reward_id": free_class.id}}, 40,
        max_uses_per_member=1,
    )

    # Historical demo bookings were seeded before the loyalty policies existed.
    # Replay only the four named demo members; unrelated member history is untouched.
    for member in (mona, leo, sara, ivan):
        for booking in Booking.query.filter_by(member_id=member.id, status="confirmed").all():
            if booking.attended is True:
                loyalty_engine.apply_event("class_attended", f"attendance:{booking.id}", member,
                                           booking=booking)
        db.session.commit()

    # Make Mona's sample account old enough for the active-days rule without
    # altering dates or accounts belonging to other GymFlow users.
    if (datetime.utcnow() - mona.created_at).days < 30:
        mona.created_at = datetime.utcnow() - timedelta(days=45)
        db.session.commit()
    loyalty_engine.apply_event("member_activity", f"member:{mona.id}", mona)
    db.session.commit()

    # A real mock-processor payment pair makes the repeat-purchase policy and
    # paid-invoice points visible in the demo. The mock never moves real money.
    get_or_create_demo_invoice(leo.card, admin, "Demo first membership purchase", 49.99, pay=True)
    get_or_create_demo_invoice(leo.card, admin, "Demo second membership purchase", 79.99, pay=True)
    # Leave Mona one pending bill so her reserved reward can be shown in the
    # existing billing/payment form without paying anything during the seed.
    get_or_create_demo_invoice(mona.card, admin, "Demo membership invoice (pending)", 79.99)

    # One-time, explicitly labeled sample history demonstrates distinct tiers.
    add_demo_qualifying_points(mona, 650)  # Silver
    add_demo_qualifying_points(leo, 1250)  # Gold
    add_demo_qualifying_points(sara, 120)  # Bronze, progressing toward Silver

    account = loyalty_engine.get_account(mona.card)
    if not RewardRedemption.query.filter_by(account_id=account.id, reward_id=silver_discount.id).first():
        loyalty_engine.redeem_reward(mona, silver_discount)
        print("Reserved:       Mona's 10% reward for her demo invoice")
    print("Added:          loyalty demo is ready (Mona=Silver, Leo=Gold, Sara=Bronze)")


with app.app_context():
    print("Seeding GymFlow demo data...\n")

    tara = get_or_create_staff("trainer", "Tara", "Trainer", "tara@gymflow.local")
    rita = get_or_create_staff("receptionist", "Rita", "Reception", "rita@gymflow.local")

    mona = get_or_create_member("Mona", "Member", "mona@example.com",
                                 "1995-04-12", "F", "1 Main St", "555-1001")
    leo = get_or_create_member("Leo", "Lopez", "leo@example.com",
                                "1990-09-03", "M", "22 Oak Ave", "555-1002")
    sara = get_or_create_member("Sara", "Smith", "sara@example.com",
                                 "1998-01-20", "F", "9 Birch Rd", "555-1003")

    if not ProgressNote.query.filter_by(card_id=mona.card.id).first():
        card_manager.add_progress_note(mona.card, tara, "Great first session — good form on squats.")
        print("Added:          progress note on Mona's card")

    if not WorkoutPlan.query.filter_by(card_id=mona.card.id).first():
        card_manager.assign_workout_plan(
            mona.card, tara, "Week 1 — Strength Foundations",
            "3x12 squats, 3x10 push-ups, 3x10 rows, 20 min cardio cooldown.")
        print("Added:          workout plan on Mona's card")

    yoga = get_or_create_class(tara, "Morning Yoga", days_from_now=2, hour=8, room="Studio A", capacity=12)
    hiit = get_or_create_class(tara, "Evening HIIT", days_from_now=3, hour=18, room="Studio B", capacity=8)

    if not Booking.query.filter_by(class_id=yoga.id, member_id=mona.id, status="confirmed").first():
        class_manager.book_class(mona, yoga)
        print("Booked:         Mona -> Morning Yoga")
    if not Booking.query.filter_by(class_id=hiit.id, member_id=leo.id, status="confirmed").first():
        class_manager.book_class(leo, hiit)
        print("Booked:         Leo -> Evening HIIT")

    # -----------------------------------------------------------------------
    # Attendance history, so Section 4.7's reliability score and Section
    # 4.8's no-show risk model have real (if small) data to work with the
    # moment you log in, instead of showing "not enough data yet". Mona and
    # Leo are seeded as reliable attenders; a fourth member, Ivan, is seeded
    # as a habitual no-show, so the dashboard's "at-risk members" panel has
    # something genuine to show. 4 classes x 3 members = 12 marked
    # bookings -- exactly risk_model's threshold for a held-out
    # train/test split, so the Model Insights algorithm-comparison table
    # (Logistic Regression vs. Random Forest) has something to show right
    # after a fresh seed, without needing to mark extra attendance by hand.
    ivan = get_or_create_member("Ivan", "Irregular", "ivan@example.com",
                                 "1993-11-11", "M", "5 Elm St", "555-1004")
    history_members = [mona, leo, ivan]
    unreliable = {ivan.email}
    # get_or_create_class() and the per-booking check below are each
    # individually idempotent, so this whole block is safe to re-run.
    for i in range(1, 5):
        history_class = get_or_create_class(
            tara, f"Spin (past) {i}", days_from_now=-(30 - i * 7), hour=7, room="Studio C", capacity=6)
        for member in history_members:
            if not Booking.query.filter_by(class_id=history_class.id, member_id=member.id).first():
                booking = class_manager.book_class(member, history_class)
                class_manager.mark_attendance(booking, attended=member.email not in unreliable)
                print(f"Marked:         {member.name} -> {'attended' if member.email not in unreliable else 'no-show'} "
                      f"'{history_class.name}'")

    # Pricing, rewards, points history, and policy applications for the new
    # loyalty feature. Seed after attendance history so the rules can replay
    # confirmed attendance and the visible tiers reflect real sample ledger rows.
    demo_admin = User.query.filter_by(role="administrator").first()
    seed_loyalty_demo(demo_admin, mona, leo, sara, ivan)

    # -----------------------------------------------------------------------
    # Section 4.11: Diet & Nutrition tracking, seeded with realistic data so
    # the food/exercise diary, the progress bars, and the trainer/admin
    # dashboard's "Nutrition follow-up" panel all have something genuine to
    # show the moment you log in, rather than empty states everywhere.
    #
    # Mona gets a full, well-adhered-to week (goal set, diet plan assigned,
    # 7 days of food logged close to target, several exercise sessions) --
    # the "everything is working as intended" demo member. Leo gets a goal
    # and a diet plan too, but his logged meals run well over target, which
    # is exactly the case the follow-up panel exists to surface (a member
    # who IS logging, just consistently off their goal) as distinct from
    # someone who simply isn't logging at all (Sara, left blank on purpose).
    if not NutritionGoal.query.filter_by(card_id=mona.card.id).first():
        nutrition_manager.set_goal(mona.card, tara, {
            "daily_calories": "2000", "protein_g": "120", "carbs_g": "200", "fat_g": "65",
        })
        print("Added:          nutrition goal on Mona's card (2000 kcal/day, set by Tara)")

    if not DietPlan.query.filter_by(card_id=mona.card.id).first():
        nutrition_manager.assign_diet_plan(
            mona.card, tara, "High-Protein Lean Muscle Plan — 6 weeks",
            "Target ~2000 kcal/day: 120g protein, 200g carbs, 65g fat. Prioritize a lean "
            "protein source at every meal, whole grains over refined carbs, and a "
            "post-workout shake within 30 minutes of training. Re-assess after 6 weeks.")
        print("Added:          diet plan on Mona's card")

    if not FoodEntry.query.filter_by(card_id=mona.card.id).first():
        meal_set_a = [
            ("breakfast", "Oatmeal with banana & almond butter", 420, 14, 58, 14),
            ("lunch", "Grilled chicken breast, brown rice & broccoli", 620, 52, 60, 16),
            ("dinner", "Salmon, quinoa & roasted vegetables", 580, 42, 45, 22),
            ("snack", "Greek yogurt with berries", 180, 15, 20, 3),
        ]
        meal_set_b = [
            ("breakfast", "Scrambled eggs, whole-wheat toast & avocado", 450, 24, 34, 24),
            ("lunch", "Turkey & hummus wrap with side salad", 540, 38, 52, 18),
            ("dinner", "Lean beef stir-fry with brown rice", 610, 46, 58, 20),
            ("snack", "Protein shake", 150, 25, 8, 2),
        ]
        for days_ago in range(7):
            meal_set = meal_set_a if days_ago % 2 == 0 else meal_set_b
            log_date = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            for meal, food_name, calories, protein, carbs, fat in meal_set:
                nutrition_manager.log_food(mona.card, {
                    "date": log_date, "meal": meal, "food_name": food_name,
                    "calories": calories, "protein_g": protein, "carbs_g": carbs, "fat_g": fat,
                })
        print("Added:          7 days of food diary entries on Mona's card")

    if not ExerciseLog.query.filter_by(card_id=mona.card.id).first():
        exercises = [
            (0, "Running", 30, 300, "Easy pace, 5K loop"),
            (1, "Strength training", 45, 250, "Upper body — push day"),
            (3, "Running", 35, 320, ""),
            (5, "Strength training", 50, 280, "Lower body — leg day"),
        ]
        for days_ago, activity, duration, kcal, notes in exercises:
            log_date = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            nutrition_manager.log_exercise(mona.card, {
                "date": log_date, "activity": activity, "duration_minutes": duration,
                "calories_burned": kcal, "notes": notes,
            })
        print("Added:          4 exercise log entries on Mona's card")

    if not NutritionGoal.query.filter_by(card_id=leo.card.id).first():
        nutrition_manager.set_goal(leo.card, tara, {
            "daily_calories": "2200", "protein_g": "140", "carbs_g": "220", "fat_g": "70",
        })
        print("Added:          nutrition goal on Leo's card (2200 kcal/day, set by Tara)")

    if not DietPlan.query.filter_by(card_id=leo.card.id).first():
        nutrition_manager.assign_diet_plan(
            leo.card, tara, "Calorie Deficit Reset — 4 weeks",
            "Logged intake has been running well above target. Cut fast food and "
            "late-night snacking, swap fries/pizza for a lean-protein-plus-vegetables "
            "plate at lunch and dinner, and keep dessert to twice a week. Check back in "
            "with Tara after 2 weeks.")
        print("Added:          diet plan on Leo's card")

    if not FoodEntry.query.filter_by(card_id=leo.card.id).first():
        heavy_meals = [
            ("lunch", "Double cheeseburger with fries", 1150, 45, 90, 60),
            ("dinner", "Pepperoni pizza (half pie)", 1400, 55, 140, 65),
            ("snack", "Ice cream", 350, 5, 45, 15),
        ]
        for days_ago in (0, 2, 4):
            log_date = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            for meal, food_name, calories, protein, carbs, fat in heavy_meals:
                nutrition_manager.log_food(leo.card, {
                    "date": log_date, "meal": meal, "food_name": food_name,
                    "calories": calories, "protein_g": protein, "carbs_g": carbs, "fat_g": fat,
                })
        print("Added:          3 days of over-target food diary entries on Leo's card")
        print("                                       (drives the 'Nutrition follow-up' dashboard demo)")

    print("\nDone! Go to http://localhost:5000 and log in as any of:\n")
    print("  admin@gymflow.local  / Admin123!   (administrator — created automatically)")
    print(f"  tara@gymflow.local   / {DEMO_PASSWORD}   (trainer)")
    print(f"  rita@gymflow.local   / {DEMO_PASSWORD}   (receptionist)")
    print(f"  mona@example.com     / {DEMO_PASSWORD}   (member — note, workout plan, booking, nutrition goal,")
    print("                                       diet plan, a week of food diary + exercise log, Section 4.11)")
    print(f"  leo@example.com      / {DEMO_PASSWORD}   (member — booked into a class; also has a nutrition goal")
    print("                                       + diet plan, logged consistently OVER target)")
    print(f"  sara@example.com     / {DEMO_PASSWORD}   (member — Bronze Rewards Tier, progressing toward Silver)")
    print(f"  ivan@example.com     / {DEMO_PASSWORD}   (member — 3 marked no-shows; log in as tara/admin and check")
    print("                                       the dashboard's 'At-risk members' panel, Section 4.8)")
    print(f"  Loyalty demo: Mona has Silver points + a reserved invoice reward and pending invoice;")
    print(f"                Leo has Gold points + two paid invoices (the second gets the repeat-purchase discount);")
    print(f"                Sara shows Bronze progress. Visit /loyalty/ as a member and /admin/loyalty/ as admin.")
    print("\n  Log in as tara@gymflow.local or admin@gymflow.local and check the dashboard's")
    print("  'Nutrition follow-up' panel (Section 4.11) to see Mona (on-target) vs. Leo (over-target).")
