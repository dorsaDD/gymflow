"""CardManager — business logic for members, cards, attachments and staff
accounts. Method names intentionally match the class diagram / sequence
diagrams in Section 3.6–3.7 of the report.
"""
import os
import re
import secrets
import string
from datetime import datetime, timedelta

from werkzeug.utils import secure_filename

from gymflow.extensions import db
from gymflow.mail_manager import send_email
from gymflow.models import Attachment, Booking, Card, ChangeHistory, LoyaltyAccount, ProgressNote, User, WorkoutPlan

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Card theme presets a member can pick for their digital membership card
# (Card.card_color). Each key has a matching .membership-card--<key> /
# .color-swatch--<key> gradient defined in static/css/style.css. "sunset" is
# the default brand look, applied whenever card_color is unset.
CARD_COLOR_PRESETS = ("sunset", "ocean", "violet", "lime", "charcoal", "rose")


class ValidationError(Exception):
    pass


class PermissionError(Exception):
    pass


def _generate_temp_password(length=10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _next_member_code() -> str:
    # Derived from the highest existing numeric suffix, not a live COUNT --
    # a COUNT-based scheme reissues an already-used code as soon as any card
    # is deleted (e.g. after a de-registration), because the count drops back
    # down while the higher codes are still in use elsewhere. Taking the max
    # keeps every currently-existing code unique regardless of deletions.
    highest = 0
    for card in Card.query.all():
        try:
            highest = max(highest, int(card.member_code.split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"GF-{highest + 1:05d}"


# ---------------------------------------------------------------------------
# 3.5.3 Member registration and card creation
# ---------------------------------------------------------------------------
def register_member(data: dict) -> User:
    """addCard(c) — verifies the submitted form and persists a new member and
    their Membership Card in one transaction (see the "Registration"
    sequence diagram, 3.7.3).
    """
    required = ["name", "surname", "email", "birthdate", "sex", "address", "phone"]
    for field in required:
        if not data.get(field):
            raise ValidationError(f"'{field}' is required.")
    if not EMAIL_RE.match(data["email"]):
        raise ValidationError("The email address is not well-formed.")
    if User.query.filter_by(email=data["email"].lower()).first():
        raise ValidationError("A user with this email address already exists.")

    temp_password = _generate_temp_password()
    user = User(
        role="member",
        name=data["name"].strip(),
        surname=data["surname"].strip(),
        email=data["email"].strip().lower(),
        must_change_password=True,
    )
    user.set_password(temp_password)
    db.session.add(user)
    db.session.flush()  # obtain user.id before creating the card

    card = Card(
        user_id=user.id,
        member_code=_next_member_code(),
        birthdate=datetime.strptime(data["birthdate"], "%Y-%m-%d").date(),
        sex=data["sex"],
        address=data["address"].strip(),
        phone=data["phone"].strip(),
    )
    db.session.add(card)
    db.session.flush()
    db.session.add(LoyaltyAccount(card_id=card.id, balance=0, lifetime_points=0))
    db.session.commit()

    send_email(
        user.email,
        "Welcome to GymFlow",
        f"Hello {user.name},\n\nYour GymFlow account has been created.\n"
        f"Membership code: {card.member_code}\nTemporary password: {temp_password}\n\n"
        "Please log in and change your password as soon as possible.",
    )
    return user


# ---------------------------------------------------------------------------
# Public self-service sign-up — an extension of 3.5.3 for members who join
# themselves online instead of being registered at the front desk. Same
# validation as register_member(), but the member picks their own password
# up front (no temporary password / forced change), and no staff member is
# involved in creating the account.
# ---------------------------------------------------------------------------
def self_register_member(data: dict, password: str) -> User:
    required = ["name", "surname", "email", "birthdate", "sex", "address", "phone"]
    for field in required:
        if not data.get(field):
            raise ValidationError(f"'{field}' is required.")
    if not EMAIL_RE.match(data["email"]):
        raise ValidationError("The email address is not well-formed.")
    if User.query.filter_by(email=data["email"].lower()).first():
        raise ValidationError("A user with this email address already exists.")
    if not password or len(password) < 8:
        raise ValidationError("Password must be at least 8 characters long.")

    user = User(
        role="member",
        name=data["name"].strip(),
        surname=data["surname"].strip(),
        email=data["email"].strip().lower(),
        must_change_password=False,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.flush()  # obtain user.id before creating the card

    card = Card(
        user_id=user.id,
        member_code=_next_member_code(),
        birthdate=datetime.strptime(data["birthdate"], "%Y-%m-%d").date(),
        sex=data["sex"],
        address=data["address"].strip(),
        phone=data["phone"].strip(),
    )
    db.session.add(card)
    db.session.flush()
    db.session.add(LoyaltyAccount(card_id=card.id, balance=0, lifetime_points=0))
    db.session.commit()

    send_email(
        user.email,
        "Welcome to GymFlow",
        f"Hello {user.name},\n\nYour GymFlow account has been created.\n"
        f"Membership code: {card.member_code}\n\n"
        "You can log in anytime with the email and password you chose at sign-up.",
    )
    return user


# ---------------------------------------------------------------------------
# 3.5.8 Trainer and Receptionist registration
# ---------------------------------------------------------------------------
def register_staff(data: dict, role: str) -> User:
    if role not in ("trainer", "receptionist"):
        raise ValidationError("Invalid staff role.")
    if not data.get("name") or not data.get("surname"):
        raise ValidationError("Name and surname are required.")
    email = (data.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ValidationError("The email address is not well-formed.")
    if User.query.filter_by(email=email).first():
        raise ValidationError("A user with this email address already exists.")

    temp_password = _generate_temp_password()
    user = User(
        role=role, name=data["name"].strip(), surname=data["surname"].strip(),
        email=email, must_change_password=True,
    )
    user.set_password(temp_password)
    db.session.add(user)
    db.session.commit()

    send_email(
        user.email,
        "Your GymFlow staff account",
        f"Hello {user.name},\n\nAn administrator has created a {role} account for you.\n"
        f"Temporary password: {temp_password}\n\nPlease log in and change your password.",
    )
    return user


# ---------------------------------------------------------------------------
# 3.5.4 De-registration
# ---------------------------------------------------------------------------
def deregister_user(user_id: int) -> None:
    # Local import: class_manager imports ValidationError from this module,
    # so importing it at module scope here would be circular.
    from gymflow.class_manager import promote_from_waitlist

    user = User.query.get(user_id)
    if not user:
        raise ValidationError("User not found.")
    if user.role == "administrator":
        raise ValidationError("The administrator account cannot be de-registered.")
    # A departing member's confirmed bookings must be released, not just left
    # behind: FitnessClass.seats_left() counts every Booking with
    # status="confirmed", so an uncancelled booking would permanently occupy
    # a seat in that class even after the member who held it no longer exists.
    freed_classes = []
    for booking in Booking.query.filter_by(member_id=user_id, status="confirmed").all():
        booking.status = "cancelled"
        freed_classes.append(booking.fitness_class)
    db.session.delete(user)  # cascades to Card, attachments, notes, history
    db.session.commit()
    # Each freed seat may have a waitlist behind it (Section 4.7); the
    # highest-priority waiting member is auto-promoted into it.
    for fitness_class in freed_classes:
        promote_from_waitlist(fitness_class)


# ---------------------------------------------------------------------------
# 3.5.5 / 3.5.6 Password recovery and change
# ---------------------------------------------------------------------------
def recover_password(email: str) -> bool:
    user = User.query.filter_by(email=email.strip().lower()).first()
    if not user:
        # Do not reveal whether the address exists — but still return False
        # so the UI can show a generic confirmation either way.
        return False
    temp_password = _generate_temp_password()
    user.set_password(temp_password)
    user.must_change_password = True
    db.session.commit()
    send_email(
        user.email, "GymFlow password recovery",
        f"Hello {user.name},\n\nYour temporary password is: {temp_password}\n"
        "Please log in and change it as soon as possible.",
    )
    return True


def change_password(user: User, new_password: str) -> None:
    if len(new_password) < 8:
        raise ValidationError("Password must be at least 8 characters long.")
    user.set_password(new_password)
    user.must_change_password = False
    db.session.commit()


# ---------------------------------------------------------------------------
# 3.5.7 Card modification (with CardLock mutual exclusion)
# ---------------------------------------------------------------------------
def is_locked(card: Card, by_user_id: int, timeout_seconds: int) -> bool:
    """isLocked() — a lock older than timeout_seconds is considered stale."""
    if not card.locked_by_id or card.locked_by_id == by_user_id:
        return False
    if not card.locked_at:
        return False
    return datetime.utcnow() - card.locked_at < timedelta(seconds=timeout_seconds)


def acquire_lock(card: Card, user_id: int, timeout_seconds: int) -> bool:
    """lock(user) — fails if another user already holds a live lock."""
    if is_locked(card, user_id, timeout_seconds):
        return False
    card.locked_by_id = user_id
    card.locked_at = datetime.utcnow()
    db.session.commit()
    return True


def release_lock(card: Card, user_id: int) -> None:
    """unlock() — only the lock holder (or nobody) may release it."""
    if card.locked_by_id == user_id:
        card.locked_by_id = None
        card.locked_at = None
        db.session.commit()


def modify_card(card: Card, changed_by: User, fields: dict) -> None:
    """setFields(changes) + ChangeHistory logging, per the sequence diagram
    for "Card Modification" (3.7.7).
    """
    editable = {"address", "phone", "sex"}
    for field, new_value in fields.items():
        if field not in editable:
            continue
        old_value = getattr(card, field)
        if str(old_value) == str(new_value):
            continue
        setattr(card, field, new_value)
        db.session.add(ChangeHistory(
            card_id=card.id, changed_by_id=changed_by.id, field=field,
            old_value=str(old_value) if old_value is not None else "",
            new_value=str(new_value),
        ))
    db.session.commit()


# ---------------------------------------------------------------------------
# 3.5.9 Document upload
# ---------------------------------------------------------------------------
def upload_attachment(card: Card, uploader: User, file_storage, upload_folder: str) -> Attachment:
    if not file_storage or not file_storage.filename:
        raise ValidationError("No file was selected.")
    original_name = secure_filename(file_storage.filename)
    if not original_name:
        raise ValidationError("Invalid file name.")
    os.makedirs(upload_folder, exist_ok=True)
    stored_name = f"{secrets.token_hex(8)}_{original_name}"
    file_storage.save(os.path.join(upload_folder, stored_name))

    attachment = Attachment(
        card_id=card.id, stored_filename=stored_name, original_filename=original_name,
        uploaded_by_id=uploader.id,
    )
    db.session.add(attachment)
    db.session.add(ChangeHistory(
        card_id=card.id, changed_by_id=uploader.id, field="attachment",
        old_value="", new_value=f"added {original_name}",
    ))
    db.session.commit()

    if card.owner.notifications_enabled:
        send_email(card.owner.email, "A new document was added to your card",
                   f"{uploader.full_name} attached '{original_name}' to your GymFlow card.")
    return attachment


def remove_attachment(attachment: Attachment, remover: User, upload_folder: str) -> None:
    if remover.role not in ("administrator",) and remover.id != attachment.uploaded_by_id:
        raise PermissionError("Only the uploader or an administrator can remove this file.")
    path = os.path.join(upload_folder, attachment.stored_filename)
    if os.path.exists(path):
        os.remove(path)
    db.session.delete(attachment)
    db.session.commit()


# ---------------------------------------------------------------------------
# Profile photo (physical-card-style membership card view)
# ---------------------------------------------------------------------------
ALLOWED_PHOTO_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}


def set_photo(card: Card, uploader: User, file_storage, upload_folder: str) -> None:
    """Stores/replaces the member's card photo. Kept outside static/ like
    every other upload (see Config.UPLOAD_FOLDER's docstring) and served
    only through members.view_photo(), which enforces the same _can_view()
    check as attachments -- a membership photo is identifiable personal
    data, not something to leave world-readable.
    """
    if not file_storage or not file_storage.filename:
        raise ValidationError("No photo was selected.")
    original_name = secure_filename(file_storage.filename)
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if ext not in ALLOWED_PHOTO_EXTENSIONS:
        raise ValidationError("Photo must be a PNG, JPG or WEBP image.")

    os.makedirs(upload_folder, exist_ok=True)
    stored_name = f"photo_{card.id}_{secrets.token_hex(6)}.{ext}"
    file_storage.save(os.path.join(upload_folder, stored_name))

    old_filename = card.photo_filename
    card.photo_filename = stored_name
    db.session.add(ChangeHistory(
        card_id=card.id, changed_by_id=uploader.id, field="photo",
        old_value="(previous photo)" if old_filename else "(none)", new_value="(new photo)",
    ))
    db.session.commit()

    if old_filename:  # clean up the replaced file only after the new one is safely committed
        old_path = os.path.join(upload_folder, old_filename)
        if os.path.exists(old_path):
            os.remove(old_path)


def set_card_color(card: Card, changed_by: User, color: str) -> None:
    """Sets the member's chosen theme for their digital membership card
    (purely cosmetic -- see Card.card_color's docstring). Logged the same
    way as any other card field change (ChangeHistory), consistent with
    modify_card() above.
    """
    if color not in CARD_COLOR_PRESETS:
        raise ValidationError("Not a recognized card color.")
    old_value = card.card_color or "sunset"
    if old_value == color:
        return
    card.card_color = color
    db.session.add(ChangeHistory(
        card_id=card.id, changed_by_id=changed_by.id, field="card_color",
        old_value=old_value, new_value=color,
    ))
    db.session.commit()


def remove_photo(card: Card, remover: User, upload_folder: str) -> None:
    if not card.photo_filename:
        return
    path = os.path.join(upload_folder, card.photo_filename)
    if os.path.exists(path):
        os.remove(path)
    db.session.add(ChangeHistory(
        card_id=card.id, changed_by_id=remover.id, field="photo",
        old_value="(previous photo)", new_value="(none)",
    ))
    card.photo_filename = None
    db.session.commit()


# ---------------------------------------------------------------------------
# 3.5.12 Progress notes on card
# ---------------------------------------------------------------------------
def add_progress_note(card: Card, author: User, text: str) -> ProgressNote:
    if not text or not text.strip():
        raise ValidationError("The note cannot be empty.")
    note = ProgressNote(card_id=card.id, author_id=author.id, text=text.strip())
    db.session.add(note)
    db.session.commit()
    if card.owner.notifications_enabled:
        send_email(card.owner.email, "A new note was added to your GymFlow card",
                   f"{author.full_name} added a progress note to your card.")
    return note


def remove_progress_note(note: ProgressNote, remover: User) -> None:
    """verifyAuthor() — see act_progress_notes.png."""
    if remover.role != "administrator" and remover.id != note.author_id:
        raise PermissionError("Only the author or an administrator can remove this note.")
    db.session.delete(note)
    db.session.commit()


# ---------------------------------------------------------------------------
# 3.5.13 Workout plan assignment
# ---------------------------------------------------------------------------
def assign_workout_plan(card: Card, trainer: User, title: str, details: str) -> WorkoutPlan:
    """assignWorkoutPlan(id, p) — see act_workout_plan.png / the "Workout Plan
    Assignment" sequence diagram (3.7.8). Validates the plan (mirrors the
    activity diagram's "[incomplete]"/"[complete]" decision), persists it,
    and emails the member a notification via WorkoutPlan.notify().
    """
    if not title or not title.strip():
        raise ValidationError("The plan must have a title.")
    if not details or not details.strip():
        raise ValidationError("The plan must include exercise/session details.")
    plan = WorkoutPlan(
        card_id=card.id, trainer_id=trainer.id,
        title=title.strip(), details=details.strip(),
    )
    db.session.add(plan)
    db.session.commit()
    if card.owner.notifications_enabled:
        send_email(
            card.owner.email, "A new workout plan was assigned to you",
            f"{trainer.full_name} assigned you a new workout plan: '{plan.title}'.\n\n{plan.details}",
        )
    return plan


def remove_workout_plan(plan: WorkoutPlan, remover: User) -> None:
    """verifyAuthor() analogue for workout plans — only the assigning trainer
    or an administrator may remove a plan.
    """
    if remover.role != "administrator" and remover.id != plan.trainer_id:
        raise PermissionError("Only the assigning trainer or an administrator can remove this plan.")
    db.session.delete(plan)
    db.session.commit()
