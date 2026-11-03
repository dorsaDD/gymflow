"""SQLAlchemy models for GymFlow.

Class and field names deliberately mirror the class diagram in Section 3.6 of
the accompanying report (GymFlow_Software_Engineering_Coursework.docx), so
that the code can be read side by side with the design documentation:

    User            -> RegisteredUser / Member (Role enumeration = role column)
    Card            -> Card
    Attachment      -> Attachment
    ChangeHistory   -> ChangeHistory
    ProgressNote    -> "Progress Notes on Card" use case (3.5.12)
    WorkoutPlan     -> "Workout Plan Assignment" use case (3.5.13)
    FitnessClass    -> FitnessClass
    Booking         -> Booking
    MailOutbox      -> MailManager's output (mocked; see mail_manager.py)

Card locking (the CardLock class in the diagram) is implemented as two columns
directly on Card (locked_by_id / locked_at) rather than a separate table, to
keep the schema simple; card_manager.acquire_lock()/release_lock() implement
the same isLocked()/lock()/unlock() semantics shown in the sequence diagram
for "Card Modification" (Section 3.7.7).
"""
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from gymflow.extensions import db

ROLES = ("member", "trainer", "receptionist", "administrator")


class User(db.Model):
    """A registered user (Section 3.4.2 actor hierarchy)."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(20), nullable=False)  # Role «enumeration»
    name = db.Column(db.String(80), nullable=False)
    surname = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    notifications_enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # NOTE: Card has *two* foreign keys into users.id (user_id and the
    # CardLock's locked_by_id), so the join condition is ambiguous unless
    # explicitly pinned to user_id here.
    card = db.relationship("Card", backref="owner", uselist=False,
                            cascade="all, delete-orphan", foreign_keys="Card.user_id")

    # -- password handling (never store or compare plain text) --------------
    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)

    @property
    def full_name(self) -> str:
        return f"{self.name} {self.surname}"

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"


class Card(db.Model):
    """Membership Card — Section 3.6 class diagram."""

    __tablename__ = "cards"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    member_code = db.Column(db.String(20), unique=True, nullable=False)
    birthdate = db.Column(db.Date, nullable=True)
    sex = db.Column(db.String(10), nullable=True)
    address = db.Column(db.String(200), nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    # Filename under UPLOAD_FOLDER (never served directly from /static -- see
    # the security note on Config.UPLOAD_FOLDER) of the member's profile
    # photo, shown on the physical-card-style view. See
    # card_manager.set_photo()/remove_photo() and members.view_photo().
    photo_filename = db.Column(db.String(255), nullable=True)
    # The member's chosen color theme for the digital card display (one of
    # card_manager.CARD_COLOR_PRESETS). Null means "no choice made yet" --
    # the UI then falls back to the default "sunset" gradient, so this is
    # purely cosmetic and never blocks anything if it's unset.
    card_color = db.Column(db.String(20), nullable=True)

    # CardLock fields (mutual-exclusion mechanism, see act_card_modification.png)
    locked_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    locked_at = db.Column(db.DateTime, nullable=True)

    attachments = db.relationship("Attachment", backref="card", cascade="all, delete-orphan")
    history = db.relationship(
        "ChangeHistory", backref="card", cascade="all, delete-orphan",
        order_by="desc(ChangeHistory.changed_at)",
    )
    notes = db.relationship(
        "ProgressNote", backref="card", cascade="all, delete-orphan",
        order_by="desc(ProgressNote.created_at)",
    )
    workout_plans = db.relationship(
        "WorkoutPlan", backref="card", cascade="all, delete-orphan",
        order_by="desc(WorkoutPlan.created_at)",
    )
    # Section 4.11 -- Diet & Nutrition tracking
    diet_plans = db.relationship(
        "DietPlan", backref="card", cascade="all, delete-orphan",
        order_by="desc(DietPlan.created_at)",
    )
    nutrition_goal = db.relationship(
        "NutritionGoal", backref="card", uselist=False, cascade="all, delete-orphan",
    )
    food_entries = db.relationship(
        "FoodEntry", backref="card", cascade="all, delete-orphan",
        order_by="desc(FoodEntry.logged_at)",
    )
    exercise_logs = db.relationship(
        "ExerciseLog", backref="card", cascade="all, delete-orphan",
        order_by="desc(ExerciseLog.logged_at)",
    )
    # Section 4.12 -- Payment & Billing
    invoices = db.relationship(
        "Invoice", backref="card", cascade="all, delete-orphan",
        order_by="desc(Invoice.issued_at)",
    )

    def __repr__(self):
        return f"<Card {self.member_code}>"


class Attachment(db.Model):
    """Document attached to a card (medical certificates, etc.) — 3.5.9."""

    __tablename__ = "attachments"

    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("cards.id"), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    uploaded_by = db.relationship("User")


class ChangeHistory(db.Model):
    """Revision history entry — satisfies the "Revision History" use case."""

    __tablename__ = "change_history"

    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("cards.id"), nullable=False)
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    field = db.Column(db.String(60), nullable=False)
    old_value = db.Column(db.String(300), nullable=True)
    new_value = db.Column(db.String(300), nullable=True)
    changed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    changed_by = db.relationship("User")


class ProgressNote(db.Model):
    """Progress note on a card — Section 3.5.12."""

    __tablename__ = "progress_notes"

    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("cards.id"), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    author = db.relationship("User")


class WorkoutPlan(db.Model):
    """Workout plan assigned to a member's card — Section 3.5.13 / the
    WorkoutPlan class box in the Section 3.6 class diagram. Distinct from a
    ProgressNote in that it is a titled artefact (title + structured
    exercise/session details) rather than a free-text note.
    """

    __tablename__ = "workout_plans"

    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("cards.id"), nullable=False)
    trainer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    details = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    trainer = db.relationship("User")


class DietPlan(db.Model):
    """Diet plan assigned to a member's card — Section 4.11 (Diet & Nutrition
    tracking), the same shape and workflow as WorkoutPlan above but for
    nutrition guidance (e.g. "aim for 3 protein-forward meals + 1 snack,
    avoid X"). A titled artefact from a trainer, distinct from the member's
    own day-to-day FoodEntry diary below.
    """

    __tablename__ = "diet_plans"

    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("cards.id"), nullable=False)
    trainer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    details = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    trainer = db.relationship("User")


class NutritionGoal(db.Model):
    """One row per card: the member's daily calorie/macro target, either
    self-set or set by their trainer. Section 4.11. Consulted by
    nutrition_manager.daily_summary() to show progress-toward-goal, and by
    the AI agent's member context so it can talk about whether today is on
    track.
    """

    __tablename__ = "nutrition_goals"

    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("cards.id"), unique=True, nullable=False)
    daily_calories = db.Column(db.Integer, nullable=False)
    protein_g = db.Column(db.Integer, nullable=False)
    carbs_g = db.Column(db.Integer, nullable=False)
    fat_g = db.Column(db.Integer, nullable=False)
    set_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    set_by = db.relationship("User")


class FoodEntry(db.Model):
    """One food diary entry (Section 4.11). Calories/macros are entered by
    whoever logs the meal (member or, for staff coaching a member directly,
    a trainer) -- there's no external nutrition database integration here,
    same "mocked 3rd party kept honest and simple" spirit as MailManager.
    """

    __tablename__ = "food_entries"

    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("cards.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    meal = db.Column(db.String(20), nullable=False)  # breakfast | lunch | dinner | snack
    food_name = db.Column(db.String(120), nullable=False)
    calories = db.Column(db.Integer, nullable=False)
    protein_g = db.Column(db.Float, default=0.0, nullable=False)
    carbs_g = db.Column(db.Float, default=0.0, nullable=False)
    fat_g = db.Column(db.Float, default=0.0, nullable=False)
    logged_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ExerciseLog(db.Model):
    """One self-reported exercise session (Section 4.11). Deliberately
    separate from Booking (a scheduled GymFlow class) and WorkoutPlan (a
    trainer-authored plan) -- this is the member logging what they actually
    did each day, including activity outside GymFlow classes entirely, the
    way a real fitness-tracking app would.
    """

    __tablename__ = "exercise_logs"

    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("cards.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    activity = db.Column(db.String(80), nullable=False)
    duration_minutes = db.Column(db.Integer, nullable=False)
    calories_burned = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.String(300), nullable=True)
    logged_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Invoice(db.Model):
    """A billing charge against a member's card — Section 4.12 Payment &
    Billing. Mirrors WorkoutPlan/DietPlan (staff issues it against a
    member's card); the member then pays it through a mocked card charge
    (see payment_manager.py) rather than a real payment gateway, the same
    "mocked 3rd-party service" approach already used for MailManager and
    kept fault-tolerant the same way (gymflow.resilience) as Section 4.10.

    Two separate foreign keys into users.id (issued_by_id, refunded_by_id)
    make the join between invoices and users ambiguous, so each
    relationship below pins its own FK explicitly -- the same situation,
    and the same fix, as User.card's foreign_keys="Card.user_id" above.
    """

    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("cards.id"), nullable=False)
    issued_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    # Loyalty/promotion discounts are recorded separately from the issued
    # invoice face value so receipts and refunds remain auditable.
    discount_amount = db.Column(db.Float, default=0.0, nullable=False)
    paid_amount = db.Column(db.Float, nullable=True)
    # pending -> paid (successful mock charge) | pending -> refunded is not
    # possible -- only a PAID invoice can be refunded. A failed charge
    # attempt leaves the invoice pending with last_error set so the member
    # can simply retry, the same "don't lose the underlying record" idea as
    # MailOutbox.status.
    status = db.Column(db.String(20), default="pending", nullable=False)
    issued_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    due_date = db.Column(db.Date, nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)
    payment_method_last4 = db.Column(db.String(4), nullable=True)
    last_error = db.Column(db.String(200), nullable=True)
    refunded_at = db.Column(db.DateTime, nullable=True)
    refunded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    issued_by = db.relationship("User", foreign_keys="Invoice.issued_by_id")
    refunded_by = db.relationship("User", foreign_keys="Invoice.refunded_by_id")


class FitnessClass(db.Model):
    """A bookable fitness class — Section 3.5.11 / class diagram."""

    __tablename__ = "fitness_classes"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    trainer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    schedule = db.Column(db.DateTime, nullable=False)
    room = db.Column(db.String(60), nullable=False)
    capacity = db.Column(db.Integer, nullable=False)

    trainer = db.relationship("User")
    bookings = db.relationship("Booking", backref="fitness_class", cascade="all, delete-orphan")
    waitlist_entries = db.relationship("Waitlist", backref="fitness_class", cascade="all, delete-orphan")

    def confirmed_bookings(self):
        return [b for b in self.bookings if b.status == "confirmed"]

    def seats_left(self) -> int:
        return self.capacity - len(self.confirmed_bookings())

    def check_availability(self) -> bool:
        """checkAvailability() — see the "Class Booking" sequence diagram."""
        return self.seats_left() > 0


class Booking(db.Model):
    """A member's reservation for a fitness class."""

    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    class_id = db.Column(db.Integer, db.ForeignKey("fitness_classes.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(20), default="confirmed", nullable=False)  # confirmed | cancelled
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # Attendance outcome for a confirmed booking, recorded by staff after the
    # class takes place: None = not yet marked, True = attended, False =
    # booked but did not show up. This is the ground-truth label the
    # no-show/churn risk model (Section 4.8) is trained on, and it also
    # feeds WaitlistManager's reliability score (Section 4.7).
    attended = db.Column(db.Boolean, nullable=True)
    reward_redemption_id = db.Column(db.Integer, db.ForeignKey("reward_redemptions.id"), nullable=True)

    member = db.relationship("User")


class LoyaltyTier(db.Model):
    """Configurable rewards tier. Kept separate from achievement badges."""

    __tablename__ = "loyalty_tiers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), unique=True, nullable=False)
    threshold = db.Column(db.Integer, unique=True, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    benefits = db.Column(db.String(300), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)


class LoyaltyAccount(db.Model):
    """Member rewards wallet; lifetime qualifying points never decrease."""

    __tablename__ = "loyalty_accounts"

    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("cards.id"), unique=True, nullable=False)
    balance = db.Column(db.Integer, nullable=False, default=0)
    lifetime_points = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    card = db.relationship("Card")
    entries = db.relationship("PointsLedgerEntry", backref="account", order_by="desc(PointsLedgerEntry.created_at)")


class PricingPolicy(db.Model):
    """A policy stores validated JSON DSL data; no policy text is executed."""

    __tablename__ = "pricing_policies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(300), nullable=True)
    event_type = db.Column(db.String(30), nullable=False)
    definition = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft")
    priority = db.Column(db.Integer, nullable=False, default=100)
    starts_at = db.Column(db.DateTime, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    max_uses_per_member = db.Column(db.Integer, nullable=True)
    max_uses_total = db.Column(db.Integer, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    created_by = db.relationship("User", foreign_keys="PricingPolicy.created_by_id")
    updated_by = db.relationship("User", foreign_keys="PricingPolicy.updated_by_id")


class PolicyApplication(db.Model):
    """Idempotency and audit record for a policy applied to an event."""

    __tablename__ = "policy_applications"

    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("pricing_policies.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    event_key = db.Column(db.String(120), nullable=False, unique=True)
    event_type = db.Column(db.String(30), nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)
    points_awarded = db.Column(db.Integer, nullable=False, default=0)
    discount_amount = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    policy = db.relationship("PricingPolicy")
    member = db.relationship("User")


class PolicyAudit(db.Model):
    """Append-only snapshot of administrator policy changes."""

    __tablename__ = "policy_audit"

    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("pricing_policies.id"), nullable=False)
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.String(20), nullable=False)
    snapshot = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    changed_by = db.relationship("User")
    policy = db.relationship("PricingPolicy")


class PointsLedgerEntry(db.Model):
    """Immutable signed point movement; every event has an idempotency key."""

    __tablename__ = "points_ledger"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("loyalty_accounts.id"), nullable=False)
    event_key = db.Column(db.String(160), nullable=False, unique=True)
    entry_type = db.Column(db.String(20), nullable=False)  # earned, redeemed, reversed, adjusted
    points = db.Column(db.Integer, nullable=False)  # signed delta to spendable balance
    qualifying_points = db.Column(db.Integer, nullable=False, default=0)
    note = db.Column(db.String(240), nullable=False)
    policy_id = db.Column(db.Integer, db.ForeignKey("pricing_policies.id"), nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    policy = db.relationship("PricingPolicy")
    created_by = db.relationship("User")


class Reward(db.Model):
    """Member-redeemable discount or free-class credit."""

    __tablename__ = "rewards"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(300), nullable=True)
    reward_type = db.Column(db.String(24), nullable=False)  # percent_discount, fixed_discount, free_class
    points_cost = db.Column(db.Integer, nullable=False)
    value = db.Column(db.Float, nullable=True)
    target = db.Column(db.String(24), nullable=False, default="invoice")  # invoice | class_booking
    minimum_tier = db.Column(db.String(40), nullable=True)
    max_redemptions = db.Column(db.Integer, nullable=True)
    max_per_member = db.Column(db.Integer, nullable=True)
    starts_at = db.Column(db.DateTime, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    created_by = db.relationship("User")


class RewardRedemption(db.Model):
    """A points-funded reward; reserved until consumed by a current flow."""

    __tablename__ = "reward_redemptions"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("loyalty_accounts.id"), nullable=False)
    reward_id = db.Column(db.Integer, db.ForeignKey("rewards.id"), nullable=False)
    policy_id = db.Column(db.Integer, db.ForeignKey("pricing_policies.id"), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="reserved")  # reserved, used, reversed
    redeemed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)

    account = db.relationship("LoyaltyAccount")
    reward = db.relationship("Reward")
    policy = db.relationship("PricingPolicy")


class Waitlist(db.Model):
    """A member queued for a fitness class that was full when they tried to
    book it (Section 4.7, WaitlistManager). Kept as its own table rather
    than a Booking status so that a waitlist entry never counts toward
    FitnessClass.seats_left() -- only a genuine confirmed Booking occupies a
    seat.
    """

    __tablename__ = "waitlist"

    id = db.Column(db.Integer, primary_key=True)
    class_id = db.Column(db.Integer, db.ForeignKey("fitness_classes.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # waiting -> promoted (got a confirmed Booking) | left (member withdrew) | expired (class started)
    status = db.Column(db.String(20), default="waiting", nullable=False)

    # `fitness_class` is deliberately NOT declared here: it is supplied by
    # FitnessClass.waitlist_entries' backref above, exactly like
    # Booking.fitness_class is supplied by FitnessClass.bookings' backref --
    # keeping every FitnessClass <-> child relationship declared on the
    # FitnessClass side is what lets the "many" side's FK (class_id here)
    # be found automatically instead of relying on a name-matching guess.
    member = db.relationship("User")

    def __repr__(self):
        return f"<Waitlist member={self.member_id} class={self.class_id} status={self.status}>"


class MailOutbox(db.Model):
    """Every email GymFlow "sends" is recorded here instead of hitting a real
    SMTP server, so the whole notification flow (Sections 3.5.5, 3.5.8,
    3.5.10, 3.5.11) can be demonstrated and inspected without any external
    dependency. See mail_manager.py and the /outbox route.

    `status`/`last_error` support Section 4.10 (fault tolerance): delivery to
    the simulated 3rd-party relay can fail, so a row is not always "sent" --
    see mail_manager.send_email()/retry_delivery().
    """

    __tablename__ = "mail_outbox"

    id = db.Column(db.Integer, primary_key=True)
    to_email = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # "sent" (delivered to the relay) or "queued" (every retry failed; an
    # administrator can ask GymFlow to try again from /outbox).
    status = db.Column(db.String(20), default="sent", nullable=False)
    last_error = db.Column(db.String(200), nullable=True)
