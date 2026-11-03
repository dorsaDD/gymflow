"""End-to-end smoke test for GymFlow, run inside a sandbox that has no
package-index network access (so it uses the local shim in
_devtools/shim/flask_sqlalchemy.py instead of the real package — see that
file's docstring). Exercises every use case through Flask's test client.
Run with:  python3 _devtools/test_smoke.py
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "shim"))  # fake flask_sqlalchemy first
sys.path.insert(0, APP_ROOT)

os.environ.setdefault("GYMFLOW_TEST_DB", "1")

failures = []
checks = 0


def check(label, condition, extra=""):
    global checks
    checks += 1
    status = "OK  " if condition else "FAIL"
    print(f"[{status}] {label} {extra}")
    if not condition:
        failures.append(label)


from gymflow import create_app  # noqa: E402
from gymflow.models import MailOutbox  # noqa: E402

app = create_app()
app.testing = True
client = app.test_client()


def login(email, password):
    return client.post("/login", data={"email": email, "password": password},
                        follow_redirects=True)


def logout():
    return client.get("/logout", follow_redirects=True)


# ---------------------------------------------------------------------------
print("\n== Auth: default admin seed + login ==")
r = client.get("/", follow_redirects=True)
check("index redirects to login when anonymous", r.status_code == 200 and b"Log in" in r.data)

r = login("admin@gymflow.local", "Admin123!")
check("admin login succeeds", r.status_code == 200 and b"Dashboard" in r.data or b"Welcome" in r.data,
      f"status={r.status_code}")

r = client.get("/members/")
check("admin can list members (initially empty)", r.status_code == 200)

# ---------------------------------------------------------------------------
print("\n== 3.5.8 Staff registration (trainer + receptionist) ==")
r = client.post("/admin/staff/new/trainer",
                 data={"name": "Tara", "surname": "Trainer", "email": "tara@gymflow.local"},
                 follow_redirects=True)
check("create trainer", r.status_code == 200 and b"Tara" in r.data)

r = client.post("/admin/staff/new/receptionist",
                 data={"name": "Rita", "surname": "Reception", "email": "rita@gymflow.local"},
                 follow_redirects=True)
check("create receptionist", r.status_code == 200 and b"Rita" in r.data)

check("welcome emails landed in outbox", MailOutbox.query.count() >= 2,
      f"count={MailOutbox.query.count()}")

trainer_temp_pw = None
receptionist_temp_pw = None
for m in MailOutbox.query.all():
    if m.to_email == "tara@gymflow.local":
        trainer_temp_pw = m.body.split("Temporary password: ")[1].split("\n")[0].strip()
    if m.to_email == "rita@gymflow.local":
        receptionist_temp_pw = m.body.split("Temporary password: ")[1].split("\n")[0].strip()
check("captured trainer temp password", bool(trainer_temp_pw))
check("captured receptionist temp password", bool(receptionist_temp_pw))

logout()

# ---------------------------------------------------------------------------
print("\n== 3.5.6 Forced password change on first login (receptionist) ==")
r = login("rita@gymflow.local", receptionist_temp_pw)
check("receptionist first login redirects to change-password",
      r.status_code == 200 and b"Change password" in r.data)

r = client.post("/account/password",
                 data={"new_password": "Reception123", "confirm_password": "Reception123"},
                 follow_redirects=True)
check("receptionist sets new password", r.status_code == 200)

# ---------------------------------------------------------------------------
print("\n== 3.5.3 Member registration and card creation (as receptionist) ==")
r = client.post("/members/register", data={
    "name": "Mona", "surname": "Member", "email": "mona@example.com",
    "birthdate": "1995-04-12", "sex": "F", "address": "1 Main St", "phone": "555-1234",
}, follow_redirects=True)
check("member registered, redirected to card", r.status_code == 200 and b"GF-" in r.data)

r = client.get("/members/")
check("new member appears in member list", b"Mona" in r.data)

mona_temp_pw = None
for m in MailOutbox.query.all():
    if m.to_email == "mona@example.com":
        mona_temp_pw = m.body.split("Temporary password: ")[1].split("\n")[0].strip()
check("captured member temp password", bool(mona_temp_pw))

from gymflow.models import Card, User  # noqa: E402
mona_card = Card.query.filter_by(member_code=None).first() if False else None
mona_user = User.query.filter_by(email="mona@example.com").first()
check("member user row exists", mona_user is not None)
check("member has a card", mona_user is not None and mona_user.card is not None)
card_id = mona_user.card.id if mona_user and mona_user.card else None

logout()

# ---------------------------------------------------------------------------
print("\n== Public self-service sign-up ==")
r = client.get("/signup")
check("signup page loads while logged out", r.status_code == 200 and b"Create your account" in r.data)

r = client.post("/signup", data={
    "name": "Leo", "surname": "Lopez", "email": "leo@example.com",
    "birthdate": "1990-09-03", "sex": "M", "address": "22 Oak Ave", "phone": "555-2002",
    "password": "SelfSignup1", "confirm_password": "SelfSignup1",
}, follow_redirects=True)
check("self sign-up creates the account and logs the member straight in",
      r.status_code == 200 and b"Welcome to GymFlow, Leo" in r.data)

leo_user = User.query.filter_by(email="leo@example.com").first()
check("self-registered member row exists with a card", leo_user is not None and leo_user.card is not None)
check("self-registered member does not need a forced password change", leo_user is not None and leo_user.must_change_password is False)

r = client.get("/classes/")
check("newly self-registered member can immediately browse classes", r.status_code == 200)
logout()

r = client.post("/signup", data={
    "name": "Leo2", "surname": "Lopez", "email": "leo@example.com",
    "birthdate": "1990-09-03", "sex": "M", "address": "22 Oak Ave", "phone": "555-2002",
    "password": "SelfSignup1", "confirm_password": "SelfSignup1",
}, follow_redirects=True)
check("sign-up with a duplicate email is rejected", b"already exists" in r.data)

r = client.post("/signup", data={
    "name": "Short", "surname": "Pw", "email": "shortpw@example.com",
    "birthdate": "1990-09-03", "sex": "M", "address": "22 Oak Ave", "phone": "555-2003",
    "password": "abc", "confirm_password": "abc",
}, follow_redirects=True)
check("sign-up with too-short a password is rejected", b"at least 8 characters" in r.data)

r = client.post("/signup", data={
    "name": "Mismatch", "surname": "Pw", "email": "mismatch@example.com",
    "birthdate": "1990-09-03", "sex": "M", "address": "22 Oak Ave", "phone": "555-2004",
    "password": "MismatchPw1", "confirm_password": "DifferentPw1",
}, follow_redirects=True)
check("sign-up with mismatched passwords is rejected", b"do not match" in r.data)

# ---------------------------------------------------------------------------
print("\n== 3.5.7 Card modification with CardLock ==")
login("rita@gymflow.local", "Reception123")
r = client.get(f"/members/{card_id}/edit")
check("receptionist opens edit form (acquires lock)", r.status_code == 200 and b"Edit card" in r.data)
r = client.post(f"/members/{card_id}/edit", data={"address": "22 New Ave", "phone": "555-9999", "sex": "F"},
                 follow_redirects=True)
check("card saved", r.status_code == 200 and b"22 New Ave" in r.data)
r = client.get(f"/members/{card_id}/history")
check("history shows the address/phone change", b"address" in r.data and b"phone" in r.data)
logout()

# ---------------------------------------------------------------------------
print("\n== 3.5.9 Document upload / delete ==")
login("rita@gymflow.local", "Reception123")
data = {"file": (io.BytesIO(b"fake pdf bytes"), "certificate.pdf")}
r = client.post(f"/members/{card_id}/attachments", data=data,
                 content_type="multipart/form-data", follow_redirects=True)
check("attachment uploaded", r.status_code == 200 and b"certificate.pdf" in r.data)

r = client.get(f"/members/{card_id}")
check("card page lists the attachment", b"certificate.pdf" in r.data)

from gymflow.models import Attachment  # noqa: E402
att = Attachment.query.filter_by(card_id=card_id).first()
check("attachment row persisted", att is not None)
r = client.get(f"/members/attachments/{att.id}/download")
check("attachment downloads", r.status_code == 200 and r.data == b"fake pdf bytes")
r = client.post(f"/members/attachments/{att.id}/delete", follow_redirects=True)
check("attachment deleted", r.status_code == 200 and b"Attachment removed" in r.data)
logout()

# ---------------------------------------------------------------------------
print("\n== Membership card: profile photo (physical-card view, self-service + staff) ==")
# mona hasn't set her real password yet at this point in the suite (that
# happens later, in Section 3.5.11) -- she's still on the temp password
# captured at registration.
login("mona@example.com", mona_temp_pw)
r = client.post(f"/members/{card_id}/photo", data={"photo": (io.BytesIO(b"fake-png-bytes-1"), "me.png")},
                 content_type="multipart/form-data", follow_redirects=True)
check("a member can upload their own card photo", r.status_code == 200 and b"Profile photo updated" in r.data)

r = client.get(f"/members/{card_id}")
check("the card page now offers to replace (not upload) the photo", b"Replace photo" in r.data)

r = client.get(f"/members/{card_id}/photo/view")
check("the uploaded photo is served back correctly", r.status_code == 200 and r.data == b"fake-png-bytes-1")

r = client.post(f"/members/{card_id}/photo", data={"photo": (io.BytesIO(b"fake-png-bytes-2"), "me2.png")},
                 content_type="multipart/form-data", follow_redirects=True)
check("uploading again replaces the photo", r.status_code == 200)
r = client.get(f"/members/{card_id}/photo/view")
check("the replaced photo serves the new bytes, not the old ones", r.data == b"fake-png-bytes-2")

r = client.post(f"/members/{card_id}/photo", data={"photo": (io.BytesIO(b"not an image"), "virus.exe")},
                 content_type="multipart/form-data", follow_redirects=True)
check("a non-image extension is rejected", r.status_code == 200 and b"must be a PNG, JPG or WEBP" in r.data)
logout()

login("rita@gymflow.local", "Reception123")
r = client.post(f"/members/{card_id}/photo", data={"photo": (io.BytesIO(b"staff-uploaded"), "staff.jpg")},
                 content_type="multipart/form-data", follow_redirects=True)
check("staff with modify_card can also set a member's photo", r.status_code == 200 and b"Profile photo updated" in r.data)
logout()

login("admin@gymflow.local", "Admin123!")
r = client.post("/members/register", data={
    "name": "Fiona", "surname": "Forbidden", "email": "fiona@example.com",
    "birthdate": "1991-01-01", "sex": "F", "address": "x", "phone": "555-7001",
}, follow_redirects=True)
check("second member (for the cross-member permission check) is registered", r.status_code == 200)
logout()

fiona_temp_pw = None
for m in MailOutbox.query.all():
    if m.to_email == "fiona@example.com":
        fiona_temp_pw = m.body.split("Temporary password: ")[1].split("\n")[0].strip()
check("captured fiona's temp password", bool(fiona_temp_pw))

login("fiona@example.com", fiona_temp_pw)  # logging in fully establishes the session even before the forced password change
r = client.post(f"/members/{card_id}/photo", data={"photo": (io.BytesIO(b"nope"), "nope.jpg")},
                 content_type="multipart/form-data", follow_redirects=False)
check("a member cannot upload a photo to another member's card",
      r.status_code == 403, f"status={r.status_code}")
logout()

login("mona@example.com", mona_temp_pw)
r = client.post(f"/members/{card_id}/photo/delete", follow_redirects=True)
check("the photo can be removed", r.status_code == 200 and b"Profile photo removed" in r.data)
r = client.get(f"/members/{card_id}/photo/view")
check("after removal, viewing the photo 404s", r.status_code == 404)
logout()

# ---------------------------------------------------------------------------
print("\n== 3.5.12 Progress notes (trainer/admin only, not receptionist) ==")
login("rita@gymflow.local", "Reception123")
r = client.post(f"/members/{card_id}/notes", data={"text": "Should be blocked."}, follow_redirects=False)
check("receptionist is forbidden from adding progress notes", r.status_code == 403, f"status={r.status_code}")
logout()

login("tara@gymflow.local", trainer_temp_pw)
r = client.post(f"/members/{card_id}/notes", data={"text": "Great first session."}, follow_redirects=True)
check("progress note added", r.status_code == 200 and b"Great first session" in r.data)
from gymflow.models import ProgressNote  # noqa: E402
note = ProgressNote.query.filter_by(card_id=card_id).first()
check("note row persisted", note is not None)
r = client.post(f"/members/notes/{note.id}/delete", follow_redirects=True)
check("note deleted", r.status_code == 200 and b"Note removed" in r.data)
logout()

# ---------------------------------------------------------------------------
print("\n== 3.5.13 Workout plan assignment (trainer/admin only, not receptionist) ==")
login("rita@gymflow.local", "Reception123")
r = client.post(f"/members/{card_id}/workout-plans",
                 data={"title": "Should be blocked", "details": "n/a"}, follow_redirects=False)
check("receptionist is forbidden from assigning workout plans", r.status_code == 403, f"status={r.status_code}")
logout()

login("tara@gymflow.local", trainer_temp_pw)
r = client.post(f"/members/{card_id}/workout-plans",
                 data={"title": "Week 1 — Strength Foundations",
                       "details": "3x squats, 3x push-ups, 3x rows."}, follow_redirects=True)
check("workout plan assigned", r.status_code == 200 and b"Week 1" in r.data)
check("workout plan notification emailed", MailOutbox.query.filter_by(to_email="mona@example.com")
      .filter(MailOutbox.subject == "A new workout plan was assigned to you").count() == 1)

r = client.post(f"/members/{card_id}/workout-plans",
                 data={"title": "", "details": "n/a"}, follow_redirects=True)
check("workout plan without a title is rejected", b"must have a title" in r.data)

from gymflow.models import WorkoutPlan  # noqa: E402
plan = WorkoutPlan.query.filter_by(card_id=card_id).first()
check("workout plan row persisted", plan is not None)
logout()

# a second trainer (not the plan's assigner) cannot remove it...
login("admin@gymflow.local", "Admin123!")
r = client.post("/admin/staff/new/trainer",
                 data={"name": "Tom", "surname": "Trainer2", "email": "tom@gymflow.local"},
                 follow_redirects=True)
check("second trainer created", r.status_code == 200)
tom_temp_pw = None
for m in MailOutbox.query.filter_by(to_email="tom@gymflow.local").all():
    tom_temp_pw = m.body.split("Temporary password: ")[1].split("\n")[0].strip()
logout()

login("tom@gymflow.local", tom_temp_pw)
r = client.post(f"/members/workout-plans/{plan.id}/delete", follow_redirects=True)
check("non-assigning trainer cannot delete another trainer's workout plan",
      b"Only the assigning trainer" in r.data)
logout()

# ...but the assigning trainer can remove their own plan.
login("tara@gymflow.local", trainer_temp_pw)
r = client.post(f"/members/workout-plans/{plan.id}/delete", follow_redirects=True)
check("assigning trainer removes their own workout plan",
      r.status_code == 200 and b"Workout plan removed" in r.data)
logout()

# ---------------------------------------------------------------------------
print("\n== 3.5.11 Fitness class scheduling and booking ==")
login("tara@gymflow.local", trainer_temp_pw)
client.post("/account/password", data={"new_password": "Trainer123", "confirm_password": "Trainer123"},
            follow_redirects=True)
r = client.post("/classes/new", data={
    "name": "Morning Yoga", "schedule": "2030-01-15T08:00", "room": "Studio A", "capacity": "1",
}, follow_redirects=True)
check("trainer creates a class", r.status_code == 200 and b"Morning Yoga" in r.data)
logout()

login("mona@example.com", mona_temp_pw)
client.post("/account/password", data={"new_password": "Member123", "confirm_password": "Member123"},
            follow_redirects=True)
r = client.get("/classes/")
check("member sees class catalog", b"Morning Yoga" in r.data)

from gymflow.models import FitnessClass  # noqa: E402
fclass = FitnessClass.query.filter_by(name="Morning Yoga").first()
r = client.post(f"/classes/{fclass.id}/book", follow_redirects=True)
check("member books the class", r.status_code == 200 and b"Booked" in r.data)

r = client.get("/members/dashboard")
check("booking shows on member dashboard", b"Morning Yoga" in r.data)

# capacity is 1 -> booking again from a second member should fail; reuse admin path is out of scope,
# so just confirm seat count directly.
check("class now has 0 seats left", fclass.seats_left() == 0, f"seats_left={fclass.seats_left()}")

from gymflow.models import Booking  # noqa: E402
booking = Booking.query.filter_by(class_id=fclass.id, member_id=mona_user.id, status="confirmed").first()
check("booking row persisted", booking is not None)
r = client.post(f"/classes/bookings/{booking.id}/cancel", follow_redirects=True)
check("member cancels booking", r.status_code == 200 and b"cancelled" in r.data.lower())
check("class has a free seat again", fclass.seats_left() == 1)

# ---------------------------------------------------------------------------
print("\n== 3.5.10 Notification settings ==")
r = client.post("/notifications/settings", data={"mode": "none"}, follow_redirects=True)
check("member disables notifications", r.status_code == 200)
check("preference persisted", mona_user.notifications_enabled is False)
logout()

# ---------------------------------------------------------------------------
print("\n== Permission boundaries ==")
login("mona@example.com", "Member123")
r = client.get("/members/")
check("member is forbidden from the members list (403)", r.status_code == 403)
r = client.get("/admin/staff")
check("member is forbidden from staff admin (403)", r.status_code == 403)
r = client.get("/outbox")
check("member is forbidden from mail outbox (403)", r.status_code == 403)
logout()

# ---------------------------------------------------------------------------
print("\n== 3.5.5 Password recovery ==")
r = client.post("/password-recovery", data={"email": "mona@example.com"}, follow_redirects=True)
check("password recovery accepted", r.status_code == 200)
r = client.post("/password-recovery", data={"email": "nobody@nowhere.com"}, follow_redirects=True)
check("password recovery is silent for unknown emails (no leak)", r.status_code == 200)

mona_new_temp_pw = None
for m in MailOutbox.query.filter_by(to_email="mona@example.com").all():
    if m.subject == "GymFlow password recovery":
        mona_new_temp_pw = m.body.split("Your temporary password is: ")[1].split("\n")[0].strip()
check("captured mona's recovered temp password", bool(mona_new_temp_pw))
r = login("mona@example.com", mona_new_temp_pw)
check("member can log in with the recovered temp password", b"Change password" in r.data)
client.post("/account/password", data={"new_password": "Member123", "confirm_password": "Member123"},
            follow_redirects=True)
logout()

# ---------------------------------------------------------------------------
print("\n== 3.5.4 De-registration ==")
login("admin@gymflow.local", "Admin123!")
r = client.get("/admin/staff")
check("admin can see staff list", b"Tara" in r.data and b"Rita" in r.data)
from gymflow.models import User as UserModel  # noqa: E402
rita = UserModel.query.filter_by(email="rita@gymflow.local").first()
r = client.post(f"/admin/users/{rita.id}/deregister", follow_redirects=True)
check("receptionist de-registered", r.status_code == 200 and b"de-registered" in r.data)
r = client.get("/admin/staff")
check("de-registered staff no longer listed", b"Rita" not in r.data)

r = client.get("/outbox")
check("admin can view mail outbox", r.status_code == 200 and b"mona@example.com" in r.data)
logout()

# ---------------------------------------------------------------------------
# Regression: a member_code must never be reissued to a different member,
# even after an earlier member with a higher code has been de-registered
# (a count-based numbering scheme would collide here -- see card_manager.py
# _next_member_code()).
print("\n== Regression: member_code is never reused after a de-registration ==")
# Uses two brand-new throwaway members (not `mona_user`/`card_id`, which the
# later "Edge cases" section still needs) so de-registering one of them here
# can't disturb any other test.
login("admin@gymflow.local", "Admin123!")
r = client.post("/members/register", data={
    "name": "Nora", "surname": "Next", "email": "nora@example.com",
    "birthdate": "1992-02-02", "sex": "F", "address": "x", "phone": "555-3001",
}, follow_redirects=True)
check("first throwaway member registered", r.status_code == 200 and b"GF-" in r.data)
nora_user = User.query.filter_by(email="nora@example.com").first()
logout()

login("admin@gymflow.local", "Admin123!")
r = client.post("/members/register", data={
    "name": "Oscar", "surname": "Over", "email": "oscar@example.com",
    "birthdate": "1991-01-01", "sex": "M", "address": "x", "phone": "555-3004",
}, follow_redirects=True)
check("second throwaway member registered", r.status_code == 200 and b"GF-" in r.data)
oscar_user = User.query.filter_by(email="oscar@example.com").first()
oscar_code = oscar_user.card.member_code
logout()

login("admin@gymflow.local", "Admin123!")
r = client.post(f"/admin/users/{nora_user.id}/deregister", follow_redirects=True)
check("earlier throwaway member (lower code) de-registered", r.status_code == 200 and b"de-registered" in r.data)
logout()

login("admin@gymflow.local", "Admin123!")
r = client.post("/members/register", data={
    "name": "Pia", "surname": "Post", "email": "pia@example.com",
    "birthdate": "1993-03-03", "sex": "F", "address": "x", "phone": "555-3002",
}, follow_redirects=True)
check("third extra member registered", r.status_code == 200 and b"GF-" in r.data)
pia_user = User.query.filter_by(email="pia@example.com").first()
check("new member's code does not collide with a code still in use",
      pia_user.card.member_code != oscar_code,
      f"oscar={oscar_code} pia={pia_user.card.member_code}")
logout()

# ---------------------------------------------------------------------------
# Regression: de-registering a member must free any class seat their
# confirmed booking held, not leave it permanently occupied (FitnessClass.
# seats_left() counts every Booking with status="confirmed" -- see
# card_manager.deregister_user()).
print("\n== Regression: de-registering a member frees their booked class seat ==")
login("admin@gymflow.local", "Admin123!")
r = client.post("/classes/new", data={
    "name": "Full-Capacity Spin", "schedule": "2027-01-01T09:00",
    "room": "Studio 3", "capacity": "1",
}, follow_redirects=True)
check("single-seat class created for the regression check", r.status_code == 200)
logout()

login("admin@gymflow.local", "Admin123!")
r = client.post("/members/register", data={
    "name": "Quinn", "surname": "Quota", "email": "quinn@example.com",
    "birthdate": "1994-04-04", "sex": "M", "address": "x", "phone": "555-3003",
}, follow_redirects=True)
check("member for the seat-release check registered", r.status_code == 200)
logout()

quinn_user = User.query.filter_by(email="quinn@example.com").first()
# Quinn has a forced temp password; fetch it instead of guessing it.
quinn_temp_pw = None
for m in MailOutbox.query.filter_by(to_email="quinn@example.com").all():
    quinn_temp_pw = m.body.split("Temporary password: ")[1].split("\n")[0].strip()
login("quinn@example.com", quinn_temp_pw)
client.post("/account/password", data={"new_password": "Quinn12345", "confirm_password": "Quinn12345"},
            follow_redirects=True)
spin_class = FitnessClass.query.filter_by(name="Full-Capacity Spin").first()
r = client.post(f"/classes/{spin_class.id}/book", follow_redirects=True)
check("quinn books the only seat", r.status_code == 200 and b"Booked" in r.data)
check("class is now full", spin_class.seats_left() == 0)
logout()

login("admin@gymflow.local", "Admin123!")
r = client.post(f"/admin/users/{quinn_user.id}/deregister", follow_redirects=True)
check("quinn de-registered while holding the only booking", r.status_code == 200 and b"de-registered" in r.data)
check("the seat is freed, not permanently lost", spin_class.seats_left() == 1,
      f"seats_left={spin_class.seats_left()}")
logout()

# ---------------------------------------------------------------------------
# 4.7/4.8: WaitlistManager is a priority queue, not plain FIFO -- a member's
# reliability_score() (their attended/no-show track record) outranks arrival
# order. This section proves the ordering by having the LATER joiner (Pia,
# a reliable attender) get promoted ahead of the EARLIER joiner (Wade, a
# habitual no-show) when a seat opens up.
print("\n== 4.7 Waitlist: priority ordering and auto-promotion ==")
login("admin@gymflow.local", "Admin123!")
r = client.post("/members/register", data={
    "name": "Wade", "surname": "Waits", "email": "wade@example.com",
    "birthdate": "1990-05-05", "sex": "M", "address": "x", "phone": "555-4001",
}, follow_redirects=True)
check("wade registered", r.status_code == 200)
wade_user = User.query.filter_by(email="wade@example.com").first()

r = client.post("/classes/new", data={
    "name": "History Spin", "schedule": "2028-01-01T09:00", "room": "Studio 4", "capacity": "2",
}, follow_redirects=True)
check("history-building class created", r.status_code == 200)
r = client.post("/classes/new", data={
    "name": "Waitlist Pilates", "schedule": "2028-01-02T09:00", "room": "Studio 5", "capacity": "1",
}, follow_redirects=True)
check("single-seat waitlist test class created", r.status_code == 200)
logout()


_known_pw = {}  # email -> password, once a member's forced temp password has been changed


def _login_as(email):
    """Logs `email` in, going through the forced first-login password change
    exactly once per member; every later call for the same email just logs
    in directly with the password chosen on that first call.
    """
    if email in _known_pw:
        login(email, _known_pw[email])
        return
    temp_pw = None
    for m in MailOutbox.query.filter_by(to_email=email).all():
        if "Temporary password: " in m.body:
            temp_pw = m.body.split("Temporary password: ")[1].split("\n")[0].strip()
    login(email, temp_pw)
    client.post("/account/password", data={"new_password": "Member123x", "confirm_password": "Member123x"},
                follow_redirects=True)
    _known_pw[email] = "Member123x"


history_class = FitnessClass.query.filter_by(name="History Spin").first()
pilates_class = FitnessClass.query.filter_by(name="Waitlist Pilates").first()

# -- build a track record: Pia attends, Wade no-shows -----------------------
_login_as("pia@example.com")
client.post(f"/classes/{history_class.id}/book", follow_redirects=True)
logout()
_login_as("wade@example.com")
client.post(f"/classes/{history_class.id}/book", follow_redirects=True)
logout()

pia_history_booking = Booking.query.filter_by(class_id=history_class.id, member_id=pia_user.id).first()
wade_history_booking = Booking.query.filter_by(class_id=history_class.id, member_id=wade_user.id).first()

login("admin@gymflow.local", "Admin123!")
r = client.get(f"/classes/{history_class.id}/roster")
check("staff can open a class roster", r.status_code == 200 and b"Pia" in r.data and b"Wade" in r.data)
r = client.post(f"/classes/bookings/{pia_history_booking.id}/attendance", data={"attended": "1"},
                 follow_redirects=True)
check("pia marked as attended", r.status_code == 200 and pia_history_booking.attended is True)
r = client.post(f"/classes/bookings/{wade_history_booking.id}/attendance", data={"attended": "0"},
                 follow_redirects=True)
check("wade marked as a no-show", r.status_code == 200 and wade_history_booking.attended is False)
logout()

from gymflow import class_manager  # noqa: E402
from gymflow.models import Waitlist  # noqa: E402
check("pia's reliability score reflects her attendance", class_manager.reliability_score(pia_user) == 1.0,
      f"score={class_manager.reliability_score(pia_user)}")
check("wade's reliability score reflects his no-show", class_manager.reliability_score(wade_user) == 0.0,
      f"score={class_manager.reliability_score(wade_user)}")

# -- fill the one-seat class, then queue Wade first and Pia second ----------
_login_as("oscar@example.com")
r = client.post(f"/classes/{pilates_class.id}/book", follow_redirects=True)
check("oscar takes the only seat in Waitlist Pilates", r.status_code == 200 and b"Booked" in r.data)
logout()

_login_as("wade@example.com")
r = client.post(f"/classes/{pilates_class.id}/book", follow_redirects=True)
check("wade cannot book a full class", b"full" in r.data.lower())
r = client.post(f"/classes/{pilates_class.id}/waitlist/join", follow_redirects=True)
check("wade (unreliable) joins the waitlist first", r.status_code == 200 and b"waitlist" in r.data.lower())
logout()

_login_as("pia@example.com")
r = client.post(f"/classes/{pilates_class.id}/waitlist/join", follow_redirects=True)
check("pia (reliable) joins the waitlist second", r.status_code == 200)
logout()

check("despite joining later, pia outranks wade by reliability",
      class_manager.waitlist_position(pia_user, pilates_class) == 1 and
      class_manager.waitlist_position(wade_user, pilates_class) == 2,
      f"pia={class_manager.waitlist_position(pia_user, pilates_class)} "
      f"wade={class_manager.waitlist_position(wade_user, pilates_class)}")

# -- free the seat: the priority-ranked member (pia), not the first-in-line
#    member (wade), should be auto-promoted ------------------------------
oscar_booking = Booking.query.filter_by(class_id=pilates_class.id, member_id=oscar_user.id,
                                         status="confirmed").first()
_login_as("oscar@example.com")
r = client.post(f"/classes/bookings/{oscar_booking.id}/cancel", follow_redirects=True)
check("oscar cancels, freeing the seat", r.status_code == 200)
logout()

check("pia was auto-promoted into the freed seat (priority, not FIFO)",
      Booking.query.filter_by(class_id=pilates_class.id, member_id=pia_user.id, status="confirmed").first()
      is not None)
check("wade is still waiting, correctly skipped over", pilates_class.seats_left() == 0)
wade_entry = Waitlist.query.filter_by(class_id=pilates_class.id, member_id=wade_user.id).first()
check("wade's waitlist entry is still 'waiting', not promoted", wade_entry.status == "waiting")

# -- leaving a waitlist ------------------------------------------------------
_login_as("wade@example.com")
r = client.post(f"/classes/waitlist/{wade_entry.id}/leave", follow_redirects=True)
check("wade can leave the waitlist himself", r.status_code == 200)
logout()
check("wade's entry is marked 'left'", Waitlist.query.get(wade_entry.id).status == "left")

# ---------------------------------------------------------------------------
# 4.8: the no-show risk model. Only 2 marked bookings exist so far (pia
# attended, wade no-showed, from the waitlist section above) -- below
# risk_model.MIN_TRAINING_SAMPLES, so the model must decline to predict
# rather than return a number built on almost nothing.
print("\n== 4.8 No-show risk model: declines to predict on too little data ==")
from gymflow import risk_model  # noqa: E402
early_result = risk_model.train()
check("model refuses to train on only 2 marked bookings", early_result["model"] is None,
      f"n_samples={early_result['n_samples']}")
check("refusal note explains why", "Only" in early_result["note"] or "need at least" in early_result["note"])
check("predict_no_show_risk() returns None, not a fake number, when untrained",
      risk_model.predict_no_show_risk(pia_history_booking) is None)

# -- build enough real history for a genuine model: two reliable members
#    (pia, rex) who always attend, two unreliable ones (wade, nia) who
#    always no-show, each booking into three more classes -----------------
print("\n== 4.8 No-show risk model: trains once there is enough marked history ==")
login("admin@gymflow.local", "Admin123!")
r = client.post("/members/register", data={
    "name": "Rex", "surname": "Reliable", "email": "rex@example.com",
    "birthdate": "1988-08-08", "sex": "M", "address": "x", "phone": "555-5001",
}, follow_redirects=True)
check("rex (reliable member) registered", r.status_code == 200)
r = client.post("/members/register", data={
    "name": "Nia", "surname": "NoShow", "email": "nia@example.com",
    "birthdate": "1989-09-09", "sex": "F", "address": "x", "phone": "555-5002",
}, follow_redirects=True)
check("nia (unreliable member) registered", r.status_code == 200)
rex_user = User.query.filter_by(email="rex@example.com").first()
nia_user = User.query.filter_by(email="nia@example.com").first()

extra_history_classes = []
for i in range(2, 5):  # History Spin (index 1) already exists; add 2, 3, 4
    r = client.post("/classes/new", data={
        "name": f"History Spin {i}", "schedule": f"2028-01-0{i}T09:00",
        "room": "Studio 4", "capacity": "4",
    }, follow_redirects=True)
    check(f"history-building class {i} created", r.status_code == 200)
    extra_history_classes.append(FitnessClass.query.filter_by(name=f"History Spin {i}").first())
logout()

for cls in extra_history_classes:
    for email in ("pia@example.com", "wade@example.com", "rex@example.com", "nia@example.com"):
        _login_as(email)
        client.post(f"/classes/{cls.id}/book", follow_redirects=True)
        logout()

login("admin@gymflow.local", "Admin123!")
reliable_emails = {"pia@example.com", "rex@example.com"}
for cls in extra_history_classes:
    for email in ("pia@example.com", "wade@example.com", "rex@example.com", "nia@example.com"):
        member = User.query.filter_by(email=email).first()
        booking = Booking.query.filter_by(class_id=cls.id, member_id=member.id, status="confirmed").first()
        client.post(f"/classes/bookings/{booking.id}/attendance",
                    data={"attended": "1" if email in reliable_emails else "0"}, follow_redirects=True)
logout()

trained = risk_model.train()
check("model now trains on the accumulated history", trained["model"] is not None,
      f"n_samples={trained['n_samples']}")
check("sample count matches every marked booking (2 from the waitlist section + 4 members x 3 new classes)",
      trained["n_samples"] == 14, f"n_samples={trained['n_samples']}")
check("held-out accuracy is reported once there are enough rows to split (n>=12)",
      trained["held_out_accuracy"] is not None, f"accuracy={trained['held_out_accuracy']}")

# Book one more, still-unmarked class for a reliable and an unreliable
# member each, and check the model scores the unreliable one riskier --
# this is the directional correctness check: not an exact number (that
# would be over-claiming precision from ~14 rows), just that the model
# learned the right DIRECTION from real, honest labels.
login("admin@gymflow.local", "Admin123!")
r = client.post("/classes/new", data={
    "name": "Future Spin", "schedule": "2029-01-01T09:00", "room": "Studio 4", "capacity": "4",
}, follow_redirects=True)
check("a genuinely future, unmarked class is created", r.status_code == 200)
future_class = FitnessClass.query.filter_by(name="Future Spin").first()
logout()

_login_as("pia@example.com")
client.post(f"/classes/{future_class.id}/book", follow_redirects=True)
logout()
_login_as("nia@example.com")
client.post(f"/classes/{future_class.id}/book", follow_redirects=True)
logout()

pia_future_booking = Booking.query.filter_by(class_id=future_class.id, member_id=pia_user.id).first()
nia_future_booking = Booking.query.filter_by(class_id=future_class.id, member_id=nia_user.id).first()
pia_risk = risk_model.predict_no_show_risk(pia_future_booking)
nia_risk = risk_model.predict_no_show_risk(nia_future_booking)
check("both predictions are valid probabilities",
      pia_risk is not None and nia_risk is not None and 0.0 <= pia_risk <= 1.0 and 0.0 <= nia_risk <= 1.0,
      f"pia={pia_risk} nia={nia_risk}")
check("the model scores the habitual no-show (nia) riskier than the reliable attender (pia)",
      nia_risk > pia_risk, f"pia={pia_risk} nia={nia_risk}")

login("admin@gymflow.local", "Admin123!")
r = client.get(f"/classes/{future_class.id}/roster")
check("roster page renders no-show risk badges for unmarked bookings",
      r.status_code == 200 and b"No-show risk" in r.data)
r = client.get("/members/dashboard")
check("admin dashboard renders the at-risk members panel",
      r.status_code == 200 and b"At-risk members" in r.data and b"Nia" in r.data)
logout()

# ---------------------------------------------------------------------------
# Sprint 15: deeper algorithm/ML (professor follow-up asks for more
# complexity beyond the original four extensions) -- past-date rejection,
# the risk-model algorithm comparison + its insights page, a class
# recommendation engine (item-based collaborative filtering), and a
# scheduling-suggestion algorithm (conflict detection + demand ranking).
print("\n== Sprint 15: past-date class creation is rejected (closed defect) ==")
login("tara@gymflow.local", "Trainer123")
r = client.post("/classes/new", data={
    "name": "Time Traveler Spin", "schedule": "2000-01-01T09:00", "room": "Studio 4", "capacity": "5",
}, follow_redirects=True)
check("a past schedule is rejected with a clear message",
      r.status_code == 200 and b"cannot be in the past" in r.data)
check("no class was actually created for the rejected past date",
      FitnessClass.query.filter_by(name="Time Traveler Spin").first() is None)
logout()

print("\n== Sprint 15: no-show model compares two algorithms and picks one ==")
fresh_trained = risk_model.train()
check("comparison is populated once there's enough marked history (n>=12)",
      fresh_trained["comparison"] is not None and len(fresh_trained["comparison"]) == len(risk_model._CANDIDATES),
      f"comparison={fresh_trained['comparison']}")
check("a model_name was chosen from among the compared candidates",
      fresh_trained["model_name"] in {c["name"] for c in (fresh_trained["comparison"] or [])},
      f"model_name={fresh_trained['model_name']}")
for c in fresh_trained["comparison"] or []:
    check(f"{c['name']} reports all four metrics in [0,1]",
          all(0.0 <= c[k] <= 1.0 for k in ("accuracy", "precision", "recall", "f1")), f"{c}")

login("tara@gymflow.local", "Trainer123")
r = client.get("/classes/model-insights")
check("trainer can open the model insights page", r.status_code == 200 and b"Algorithm comparison" in r.data)
check("the insights page names the chosen algorithm", fresh_trained["model_name"].encode() in r.data)
logout()

login("mona@example.com", "Member123")
r = client.get("/classes/model-insights", follow_redirects=True)
check("a member cannot open the model insights page", r.status_code == 403)
logout()

# rita was already de-registered earlier in this file (Section 3.5.4
# regression checks), so a fresh receptionist account is needed here rather
# than reusing her stale login.
login("admin@gymflow.local", "Admin123!")
r = client.post("/admin/staff/new/receptionist",
                 data={"name": "Remy", "surname": "Reception2", "email": "remy@gymflow.local"},
                 follow_redirects=True)
check("a fresh receptionist (remy) is created for the permission-boundary check", r.status_code == 200)
remy_temp_pw = None
for m in MailOutbox.query.filter_by(to_email="remy@gymflow.local").all():
    if "Temporary password: " in m.body:
        remy_temp_pw = m.body.split("Temporary password: ")[1].split("\n")[0].strip()
logout()
login("remy@gymflow.local", remy_temp_pw)
client.post("/account/password", data={"new_password": "Remy1234", "confirm_password": "Remy1234"},
            follow_redirects=True)
r = client.get("/classes/model-insights", follow_redirects=True)
check("a receptionist (no view_model_insights) cannot open the model insights page either",
      r.status_code == 403)
logout()

print("\n== Sprint 15: class recommendation engine (item-based collaborative filtering) ==")
login("admin@gymflow.local", "Admin123!")
r = client.post("/members/register", data={
    "name": "Uma", "surname": "Umbrella", "email": "uma@example.com",
    "birthdate": "1992-02-02", "sex": "F", "address": "x", "phone": "555-6001",
}, follow_redirects=True)
check("uma registered", r.status_code == 200)
r = client.post("/members/register", data={
    "name": "Vic", "surname": "Victor", "email": "vic@example.com",
    "birthdate": "1993-03-03", "sex": "M", "address": "x", "phone": "555-6002",
}, follow_redirects=True)
check("vic registered", r.status_code == 200)
r = client.post("/classes/new", data={
    "name": "Rec Test A", "schedule": "2030-05-01T09:00", "room": "Rec Room A", "capacity": "5",
}, follow_redirects=True)
check("Rec Test A created", r.status_code == 200)
r = client.post("/classes/new", data={
    "name": "Rec Test B", "schedule": "2030-05-02T09:00", "room": "Rec Room B", "capacity": "5",
}, follow_redirects=True)
check("Rec Test B created", r.status_code == 200)
logout()

rec_a = FitnessClass.query.filter_by(name="Rec Test A").first()
rec_b = FitnessClass.query.filter_by(name="Rec Test B").first()

_login_as("uma@example.com")
client.post(f"/classes/{rec_a.id}/book", follow_redirects=True)
client.post(f"/classes/{rec_b.id}/book", follow_redirects=True)
logout()
_login_as("vic@example.com")
client.post(f"/classes/{rec_a.id}/book", follow_redirects=True)
logout()

from gymflow import recommendation  # noqa: E402
vic_user = User.query.filter_by(email="vic@example.com").first()
rec_result = recommendation.recommend_for(vic_user)
check("vic (booked only A) gets B recommended, via uma linking A and B",
      any(c.id == rec_b.id for c in rec_result["recommendations"]), f"{rec_result}")

login("admin@gymflow.local", "Admin123!")
r = client.post("/members/register", data={
    "name": "Zoe", "surname": "Zero", "email": "zoe@example.com",
    "birthdate": "1994-04-04", "sex": "F", "address": "x", "phone": "555-6003",
}, follow_redirects=True)
check("zoe (no booking history) registered", r.status_code == 200)
zoe_user = User.query.filter_by(email="zoe@example.com").first()
logout()
zero_history_result = recommendation.recommend_for(zoe_user)
check("a member with zero booking history gets an honest empty-state reason, not a crash",
      zero_history_result["recommendations"] == [] and "first class" in zero_history_result["reason"],
      f"{zero_history_result}")

_login_as("vic@example.com")
r = client.get("/members/dashboard")
check("vic's dashboard renders the 'Recommended for you' panel with Rec Test B",
      r.status_code == 200 and b"Recommended for you" in r.data and b"Rec Test B" in r.data)
logout()

print("\n== Sprint 15: scheduling suggestion algorithm (conflicts + demand ranking) ==")
from datetime import datetime as _dt, timedelta as _td  # noqa: E402
_conflict_dt = (_dt.utcnow() + _td(days=2)).replace(hour=18, minute=0, second=0, microsecond=0)
if _conflict_dt <= _dt.utcnow():
    _conflict_dt += _td(days=1)
_conflict_str = _conflict_dt.strftime("%Y-%m-%dT%H:%M")

login("tara@gymflow.local", "Trainer123")
r = client.post("/classes/new", data={
    "name": "Suggest Conflict Class", "schedule": _conflict_str, "room": "Suggest Test Room", "capacity": "5",
}, follow_redirects=True)
check("the conflict-source class is created", r.status_code == 200)

r = client.get("/classes/suggest-slots?room=" + "Suggest%20Test%20Room")
suggest_data = r.get_json()
check("suggest-slots responds with a JSON slot list", r.status_code == 200 and "slots" in suggest_data,
      f"{suggest_data}")
check("the exact conflicting trainer+room slot is never suggested back",
      all(s["value"] != _conflict_str for s in suggest_data["slots"]), f"{suggest_data['slots']}")
check("at least one conflict-free alternative slot is still suggested",
      len(suggest_data["slots"]) > 0, f"{suggest_data['slots']}")
logout()

login("remy@gymflow.local", "Remy1234")
r = client.get("/classes/suggest-slots?room=Suggest%20Test%20Room", follow_redirects=True)
check("a receptionist (no manage_classes) cannot use the scheduling suggestion endpoint",
      r.status_code == 403)
logout()

# ---------------------------------------------------------------------------
# 4.9 AI Agent. No OPENAI_API_KEY is set in this sandbox (and shouldn't need
# to be, for an offline test run) -- first prove the real _call_llm()
# degrades to a plain-English "not configured" message rather than
# crashing, THEN monkeypatch _call_llm() to a stub so the rest of the
# section (routing, permissions, session history, context grounding) is
# exercised without any network access or API key. See ai_agent.py's module
# docstring for why _call_llm() is the one function this is safe to patch.
print("\n== 4.9 AI Agent: degrades gracefully with no API key configured ==")
from gymflow import ai_agent  # noqa: E402
login("mona@example.com", "Member123")
r = client.get("/assistant/")
check("assistant chat page loads", r.status_code == 200 and b"GymFlow Assistant" in r.data)
r = client.post("/assistant/message", json={"message": "What classes do I have coming up?"})
check("unconfigured assistant returns a friendly explanation, not a crash",
      r.status_code == 200 and b"OPENAI_API_KEY" in r.data)
logout()

print("\n== 4.9 AI Agent: routing, permissions, grounding and history (stubbed LLM) ==")
_captured_llm_calls = []


def _stub_llm(messages):
    _captured_llm_calls.append(messages)
    return f"STUB REPLY to: {messages[-1]['content']}"


ai_agent._call_llm = _stub_llm  # monkeypatch: see module docstring

r = client.get("/assistant/", follow_redirects=True)
check("anonymous user is redirected away from the assistant", b"Log in" in r.data)

# mona's earlier "Morning Yoga" booking (Section covering waitlist/cancel above) was
# cancelled again as part of that section, so book something fresh here rather than
# relying on state left over from an earlier section -- this way the grounding check
# below is true regardless of what happens earlier in the file.
login("tara@gymflow.local", "Trainer123")
r = client.post("/classes/new", data={
    "name": "Assistant Grounding Class", "schedule": "2031-02-01T09:00", "room": "Studio A",
    "capacity": "5",
}, follow_redirects=True)
check("class for the grounding check is created", r.status_code == 200)
grounding_class = FitnessClass.query.filter_by(name="Assistant Grounding Class").first()
logout()

login("mona@example.com", "Member123")
r = client.post(f"/classes/{grounding_class.id}/book", follow_redirects=True)
check("mona books the grounding class", r.status_code == 200)

r = client.post("/assistant/message", json={"message": "hello there"})
check("stubbed assistant responds without any network call",
      r.status_code == 200 and r.get_json()["reply"] == "STUB REPLY to: hello there")
check("the system prompt grounds the assistant in the member's OWN data (their booking)",
      any("Assistant Grounding Class" in m["content"] for m in _captured_llm_calls[-1] if m["role"] == "system"),
      "expected the member's real upcoming booking to appear in the LLM system prompt")

r = client.post("/assistant/message", json={"message": ""})
check("an empty message is rejected with 400, not sent to the LLM", r.status_code == 400)

r = client.post("/assistant/message", json={"message": "second question"})
check("second reply also succeeds", r.status_code == 200)
check("conversation history carries the first exchange into the second call's context",
      any(m.get("content") == "hello there" for m in _captured_llm_calls[-1]),
      "expected the earlier user turn to appear in the messages sent for the follow-up")

r = client.post("/assistant/reset")
check("chat can be reset", r.status_code == 200)
r = client.get("/assistant/")
check("reset chat page shows no prior messages", b"No messages yet" in r.data)
logout()

login("tara@gymflow.local", "Trainer123")
r = client.post("/assistant/message", json={"message": "what am I teaching?"})
check("trainer can also use the assistant", r.status_code == 200)
check("the system prompt grounds a TRAINER in the classes THEY teach, not a member's bookings",
      any("classes you teach" in m["content"].lower() for m in _captured_llm_calls[-1] if m["role"] == "system"),
      "expected trainer-specific context, not member context")
logout()

# ---------------------------------------------------------------------------
# 4.10 Fault tolerance / fallback. Answers the professor's feedback about
# resilience directly: a failing 3rd-party service (mail relay, LLM
# provider) or an outright bug should never take the whole page down with
# it -- see gymflow/resilience.py, mail_manager.py and the 500 handler in
# gymflow/__init__.py for the three layers this exercises.
print("\n== 4.10 Fault tolerance: retry-with-backoff and circuit breaker (resilience.py) ==")
from gymflow import resilience  # noqa: E402
resilience.reset_all_breakers()

_attempts = {"n": 0}


def _flaky_twice_then_ok():
    _attempts["n"] += 1
    if _attempts["n"] < 3:
        raise RuntimeError("transient failure")
    return "ok"


result = resilience.call_with_retry(_flaky_twice_then_ok, service="unit-retry", backoff_seconds=0.01)
check("call_with_retry recovers from transient failures and returns the eventual result",
      result == "ok" and _attempts["n"] == 3, f"attempts={_attempts['n']}")


def _always_fails():
    raise RuntimeError("permanently broken")


threw = False
try:
    resilience.call_with_retry(_always_fails, service="unit-breaker", max_attempts=1,
                                failure_threshold=2, backoff_seconds=0.01)
except RuntimeError:
    threw = True
check("a call that never succeeds still raises once its own attempts are exhausted", threw)

threw_again = False
try:
    resilience.call_with_retry(_always_fails, service="unit-breaker", max_attempts=1,
                                failure_threshold=2, backoff_seconds=0.01)
except RuntimeError:
    threw_again = True
check("a second consecutive failure trips the circuit breaker open", threw_again)

_never_should_run = {"n": 0}


def _counts_if_called():
    _never_should_run["n"] += 1
    raise RuntimeError("should never actually run while the breaker is open")


skipped = False
try:
    resilience.call_with_retry(_counts_if_called, service="unit-breaker", max_attempts=1,
                                failure_threshold=2, backoff_seconds=0.01)
except resilience.CircuitOpenError:
    skipped = True
check("once OPEN, the breaker short-circuits further calls without attempting them at all",
      skipped and _never_should_run["n"] == 0, f"attempts_made={_never_should_run['n']}")

breaker = resilience.get_breaker("unit-breaker")
breaker._opened_at -= 999  # stand-in for the real cool-down window having elapsed
check("the breaker reports half-open once its cool-down has elapsed", breaker.state == "half-open")

recovered = resilience.call_with_retry(lambda: "recovered", service="unit-breaker", max_attempts=1)
check("a successful half-open probe closes the breaker again",
      recovered == "recovered" and resilience.get_breaker("unit-breaker").state == "closed")

resilience.reset_all_breakers()

print("\n== 4.10 Fault tolerance: simulated mail relay outage -> auto-retry -> circuit breaker -> manual fallback ==")
login("admin@gymflow.local", "Admin123!")
r = client.post("/members/register", data={
    "name": "Fay", "surname": "Faultless", "email": "fay@example.com",
    "birthdate": "1990-01-01", "sex": "F", "address": "x", "phone": "555-6001",
}, follow_redirects=True)
check("member for the fault-tolerance check is registered", r.status_code == 200)
logout()

os.environ["GYMFLOW_SIMULATE_MAIL_OUTAGE"] = "1"

r = client.post("/password-recovery", data={"email": "fay@example.com"}, follow_redirects=True)
check("the core action still succeeds even though the notification email is failing to send",
      r.status_code == 200)

queued = MailOutbox.query.filter_by(to_email="fay@example.com").order_by(MailOutbox.id.desc()).first()
check("the failed delivery is recorded as queued, not silently dropped",
      queued is not None and queued.status == "queued", f"status={queued.status if queued else None}")
check("the queued row records why delivery failed",
      queued is not None and bool(queued.last_error) and "ConnectionError" in queued.last_error,
      f"last_error={queued.last_error if queued else None}")

r = client.post("/password-recovery", data={"email": "fay@example.com"}, follow_redirects=True)
queued2 = MailOutbox.query.filter_by(to_email="fay@example.com").order_by(MailOutbox.id.desc()).first()
check("after 3 straight failures the breaker trips; further sends fail fast without touching the relay",
      queued2 is not None and bool(queued2.last_error) and "CircuitOpenError" in queued2.last_error,
      f"last_error={queued2.last_error if queued2 else None}")

os.environ.pop("GYMFLOW_SIMULATE_MAIL_OUTAGE", None)
resilience.reset_all_breakers()  # stand-in for the real cool-down window elapsing before a retry

login("admin@gymflow.local", "Admin123!")
r = client.get("/outbox")
check("the outbox page surfaces the queued message with a manual retry option",
      r.status_code == 200 and b"Queued" in r.data and b"Retry delivery" in r.data)

r = client.post(f"/outbox/{queued.id}/retry", follow_redirects=True)
check("an administrator can manually retry a queued message once the relay is back",
      r.status_code == 200 and b"Delivered" in r.data)
retried = MailOutbox.query.get(queued.id)
check("the retried message is now marked sent, with the error cleared",
      retried.status == "sent" and retried.last_error is None)
logout()

print("\n== 4.10 Fault tolerance: an unhandled error in a page falls back to a friendly error page ==")
login("tara@gymflow.local", "Trainer123")
r = client.post("/classes/new", data={
    "name": "Fault Test Class", "schedule": "2031-03-01T09:00", "room": "Studio A", "capacity": "5",
}, follow_redirects=True)
check("class for the crash-fallback check is created", r.status_code == 200)
fault_class = FitnessClass.query.filter_by(name="Fault Test Class").first()
logout()

login("mona@example.com", "Member123")
_original_book_class = class_manager.book_class


def _boom(*_a, **_kw):
    raise RuntimeError("simulated unexpected failure")


class_manager.book_class = _boom
app.config["PROPAGATE_EXCEPTIONS"] = False  # match real (non-testing) behaviour: let the 500 handler run
r = client.post(f"/classes/{fault_class.id}/book", follow_redirects=False)
check("an unhandled exception in a page returns 500 instead of crashing the process",
      r.status_code == 500)
check("...and falls back to the friendly error page, not a raw stack trace",
      b"Something went wrong" in r.data)
app.config["PROPAGATE_EXCEPTIONS"] = None  # restore the default for the rest of the suite

class_manager.book_class = _original_book_class
r = client.post(f"/classes/{fault_class.id}/book", follow_redirects=True)
check("after the underlying bug is fixed, the same page works normally again",
      r.status_code == 200 and b"Booked" in r.data)
logout()

print("\n== 4.10 Fault tolerance: /health endpoint and per-instance identification ==")
# The other half of Section 4.10 -- several GymFlow instances behind an
# nginx load balancer -- is a local Docker Compose demo (docker-compose.yml,
# nginx/nginx.conf, docs/FAULT_TOLERANCE_DEMO.md) that can't run inside this
# offline single-process test suite. What's testable here is the two things
# that demo depends on: a health endpoint the load balancer can poll, and
# every response being tagged with which instance served it.
r = client.get("/health")
check("the health endpoint responds 200 when the database is reachable", r.status_code == 200)
health_json = r.get_json()
check("the health endpoint reports status ok",
      health_json is not None and health_json.get("status") == "ok", f"body={health_json}")
check("every response carries an X-Served-By header (which instance answered)",
      "X-Served-By" in r.headers, f"headers={dict(r.headers)}")

# ---------------------------------------------------------------------------
# 4.11 Diet & Nutrition tracking -- a full MyFitnessPal-style food/exercise
# diary, calorie/macro goals, trainer-assigned diet plans, and real-data
# adherence follow-up (see gymflow/nutrition_manager.py + routes/diet.py).
# Also verifies the Section 4.9 AI agent is grounded in this data.
print("\n== 4.11 Diet & Nutrition: permission boundaries ==")
from datetime import date as date_cls  # noqa: E402
from gymflow import nutrition_manager  # noqa: E402
from gymflow.models import DietPlan, FoodEntry, NutritionGoal  # noqa: E402

# rita was de-registered earlier (Section 3.5.4 regression checks), so a
# fresh receptionist is needed here -- mirrors the "second trainer" (tom)
# pattern used earlier in the file for the same reason.
login("admin@gymflow.local", "Admin123!")
r = client.post("/admin/staff/new/receptionist",
                 data={"name": "Roxy", "surname": "Reception2", "email": "roxy@gymflow.local"},
                 follow_redirects=True)
check("second receptionist created for diet permission checks", r.status_code == 200)
roxy_temp_pw = None
for m in MailOutbox.query.filter_by(to_email="roxy@gymflow.local").all():
    roxy_temp_pw = m.body.split("Temporary password: ")[1].split("\n")[0].strip()
check("captured second receptionist's temp password", bool(roxy_temp_pw))
logout()

r = client.get("/diet/", follow_redirects=True)
check("anonymous user is redirected away from the diet diary", b"Log in" in r.data)

login("mona@example.com", "Member123")
r = client.get("/diet/")
check("member can open their own diet diary", r.status_code == 200 and b"Diet diary" in r.data)

r = client.get(f"/diet/{leo_user.card.id}")
check("member is blocked from viewing another member's diary", r.status_code == 403)
logout()

login("roxy@gymflow.local", roxy_temp_pw)
r = client.get(f"/diet/{card_id}")
check("receptionist (lacks manage_member_diet) is blocked from a member's diary", r.status_code == 403)
logout()

print("\n== 4.11 Diet & Nutrition: food + exercise diary (self-service) ==")
login("mona@example.com", "Member123")
today_str = date_cls.today().isoformat()

r = client.post(f"/diet/{card_id}/food", data={
    "date": today_str, "meal": "lunch", "food_name": "Grilled chicken bowl",
    "calories": "650", "protein_g": "48", "carbs_g": "55", "fat_g": "18",
}, follow_redirects=True)
check("member logs a food entry against their own card", r.status_code == 200 and b"Food logged." in r.data)

r = client.post(f"/diet/{card_id}/food", data={
    "date": today_str, "meal": "lunch", "food_name": "", "calories": "300",
}, follow_redirects=True)
check("a food entry with no name is rejected", b"required" in r.data)

r = client.post(f"/diet/{card_id}/exercise", data={
    "date": today_str, "activity": "Cycling", "duration_minutes": "40", "calories_burned": "350",
}, follow_redirects=True)
check("member logs an exercise entry against their own card",
      r.status_code == 200 and b"Exercise logged." in r.data)

r = client.get("/diet/")
check("the logged food entry appears in the diary", b"Grilled chicken bowl" in r.data)
check("the logged exercise entry appears in the diary", b"Cycling" in r.data)

logged_entry = FoodEntry.query.filter_by(card_id=card_id, food_name="Grilled chicken bowl").first()
check("food entry persisted with the right macros",
      logged_entry is not None and logged_entry.calories == 650 and logged_entry.protein_g == 48,
      f"entry={logged_entry}")

r = client.get(f"/diet/{card_id}?date=not-a-date")
check("an invalid date query param falls back to today rather than erroring", r.status_code == 200)

print("\n== 4.11 Diet & Nutrition: daily goal + progress calculation ==")
r = client.post(f"/diet/{card_id}/goal", data={
    "date": today_str, "daily_calories": "2000", "protein_g": "120", "carbs_g": "200", "fat_g": "65",
}, follow_redirects=True)
check("member sets their own daily nutrition goal", r.status_code == 200 and b"Nutrition goal saved." in r.data)

goal = NutritionGoal.query.filter_by(card_id=card_id).first()
check("nutrition goal persisted", goal is not None and goal.daily_calories == 2000, f"goal={goal}")

mona_card = Card.query.get(card_id)
summary = nutrition_manager.daily_summary(mona_card, date_cls.today())
check("daily_summary totals today's food and exercise correctly",
      summary["consumed"]["calories"] == 650 and summary["burned"] == 350
      and summary["net_calories"] == 300, f"summary={summary}")
check("daily_summary computes percent-of-goal the same way the manager does",
      summary["percent_of_goal"] == round(650 / 2000 * 100), f"percent_of_goal={summary['percent_of_goal']}")

# a throwaway second entry, logged and then deleted, to check the delete path
# and that totals correctly drop back down afterwards.
r = client.post(f"/diet/{card_id}/food", data={
    "date": today_str, "meal": "snack", "food_name": "Delete Me Snack", "calories": "100",
}, follow_redirects=True)
check("second (throwaway) food entry logged", r.status_code == 200)
throwaway = FoodEntry.query.filter_by(card_id=card_id, food_name="Delete Me Snack").first()
summary = nutrition_manager.daily_summary(mona_card, date_cls.today())
check("totals include the throwaway entry before it's deleted", summary["consumed"]["calories"] == 750)

r = client.post(f"/diet/food/{throwaway.id}/delete?date={today_str}", follow_redirects=True)
check("member can delete their own food entry", r.status_code == 200 and b"Entry removed." in r.data)
check("deleted food entry no longer exists", FoodEntry.query.get(throwaway.id) is None)
summary = nutrition_manager.daily_summary(mona_card, date_cls.today())
check("totals drop back down after the deletion", summary["consumed"]["calories"] == 650)

r = client.post(f"/diet/{leo_user.card.id}/food", data={
    "date": today_str, "meal": "snack", "food_name": "Sneaky snack", "calories": "100",
}, follow_redirects=True)
check("member cannot log food onto another member's card", r.status_code == 403)

r = client.get("/members/dashboard")
check("member dashboard shows a 'Today's nutrition' panel with the real logged total",
      r.status_code == 200 and b"nutrition" in r.data.lower() and b"650" in r.data)
logout()

print("\n== 4.11 Diet & Nutrition: trainer/admin follow-up + diet plan assignment ==")
login("admin@gymflow.local", "Admin123!")
r = client.post(f"/diet/{leo_user.card.id}/goal", data={
    "date": today_str, "daily_calories": "2200", "protein_g": "140", "carbs_g": "220", "fat_g": "70",
}, follow_redirects=True)
check("administrator can set a member's nutrition goal too (manage_member_diet)",
      r.status_code == 200 and b"Nutrition goal saved." in r.data)
logout()

login("tara@gymflow.local", "Trainer123")
r = client.get(f"/diet/{card_id}")
check("trainer can view a member's diary (manage_member_diet)",
      r.status_code == 200 and b"Diet diary" in r.data)

r = client.get("/members/dashboard")
check("trainer dashboard shows the 'Nutrition follow-up' panel", b"Nutrition follow-up" in r.data)
check("a member who hasn't logged any food shows the 'not logging' badge (Leo)",
      b"not logging" in r.data.lower())
check("a member with logged food appears with their own percent-of-goal badge (Mona)",
      b"Mona" in r.data)

tara_user = User.query.filter_by(email="tara@gymflow.local").first()
r = client.post(f"/diet/{card_id}/plan", data={
    "title": "Trainer Test Plan", "details": "High protein, moderate carb, 3 meals + 1 snack.",
}, follow_redirects=True)
check("trainer assigns a diet plan to a member", r.status_code == 200 and b"Diet plan assigned." in r.data)

diet_plan = DietPlan.query.filter_by(card_id=card_id, title="Trainer Test Plan").first()
check("diet plan persisted with the assigning trainer recorded",
      diet_plan is not None and diet_plan.trainer_id == tara_user.id, f"plan={diet_plan}")
# Mona had her own notification preference turned off earlier (Section
# 3.5.10), so no email is expected for HER assignment -- assign a second
# plan to Leo (who still has notifications on) to verify the email path.
r = client.post(f"/diet/{leo_user.card.id}/plan", data={
    "title": "Leo Notification Check Plan", "details": "Verifies the diet-plan email path.",
}, follow_redirects=True)
check("trainer assigns a diet plan to Leo too", r.status_code == 200 and b"Diet plan assigned." in r.data)
check("diet plan assignment emails the member when notifications are enabled",
      MailOutbox.query.filter_by(to_email="leo@example.com")
      .filter(MailOutbox.subject == "A new diet plan was assigned to you").count() == 1)
logout()

login("roxy@gymflow.local", roxy_temp_pw)
r = client.post(f"/diet/{card_id}/plan", data={"title": "x", "details": "y"}, follow_redirects=True)
check("receptionist cannot assign a diet plan (lacks manage_member_diet)", r.status_code == 403)
logout()

print("\n== 4.11 Diet & Nutrition: AI agent is grounded in the member's real logged data ==")
login("mona@example.com", "Member123")
r = client.post("/assistant/message", json={"message": "how am I doing on my calories today?"})
check("assistant answers a nutrition question using the stub (no network call)", r.status_code == 200)
system_msg = next(m["content"] for m in _captured_llm_calls[-1] if m["role"] == "system")
check("the system prompt grounds the assistant in the member's OWN logged calories vs. their real goal",
      "Today's nutrition: 650/2000 kcal logged" in system_msg, f"system_msg={system_msg!r}")
check("the system prompt also mentions the member's active diet plan",
      "Active diet plan: 'Trainer Test Plan'" in system_msg, f"system_msg={system_msg!r}")
logout()

print("\n== 4.11 Diet & Nutrition: diet plan removal authorization (mirrors workout plans) ==")
login("tom@gymflow.local", tom_temp_pw)
r = client.post(f"/diet/plan/{diet_plan.id}/delete", follow_redirects=True)
check("non-assigning trainer cannot delete another trainer's diet plan",
      b"Only the assigning trainer" in r.data)
logout()

login("tara@gymflow.local", "Trainer123")
r = client.post(f"/diet/plan/{diet_plan.id}/delete", follow_redirects=True)
check("assigning trainer removes their own diet plan",
      r.status_code == 200 and b"Diet plan removed." in r.data)
logout()

# ---------------------------------------------------------------------------
# 4.12 Payment & Billing -- a mocked payment processor (no real money ever
# moves) sitting behind the SAME retry + circuit breaker infrastructure as
# Section 4.10, so a "flaky payment gateway" degrades the same way a flaky
# mail relay or LLM provider does (see gymflow/payment_manager.py).
print("\n== 4.12 Payment & Billing: permission boundaries ==")
from gymflow.models import Invoice  # noqa: E402
from gymflow import payment_manager  # noqa: E402

r = client.get("/billing/", follow_redirects=True)
check("anonymous user is redirected away from billing", b"Log in" in r.data)

login("mona@example.com", "Member123")
r = client.get("/billing/", follow_redirects=True)
check("member's own billing page opens (redirected from /billing/ to their card)",
      r.status_code == 200 and b"Invoice history" in r.data)

r = client.get(f"/billing/{leo_user.card.id}")
check("member is blocked from viewing another member's billing", r.status_code == 403)
logout()

login("tom@gymflow.local", tom_temp_pw)
r = client.get(f"/billing/{card_id}")
check("trainer (no manage_billing) is blocked from a member's billing", r.status_code == 403)
logout()

login("roxy@gymflow.local", roxy_temp_pw)
r = client.get(f"/billing/{card_id}")
check("receptionist (manage_billing) can view a member's billing", r.status_code == 200)
logout()

print("\n== 4.12 Payment & Billing: issuing invoices (staff-service) + validation ==")
login("admin@gymflow.local", "Admin123!")
r = client.post(f"/billing/{card_id}/invoices", data={
    "description": "September membership fee", "amount": "49.99",
}, follow_redirects=True)
check("administrator issues an invoice to a member", r.status_code == 200 and b"Invoice issued." in r.data)

mona_invoice = Invoice.query.filter_by(card_id=card_id, description="September membership fee").first()
check("invoice persisted with the right amount, pending status and issuer",
      mona_invoice is not None and mona_invoice.amount == 49.99 and mona_invoice.status == "pending"
      and mona_invoice.issued_by_id == User.query.filter_by(email="admin@gymflow.local").first().id,
      f"invoice={mona_invoice}")
check("mona has notifications off (Section 3.5.10), so issuing HER invoice sends no email",
      MailOutbox.query.filter_by(to_email="mona@example.com")
      .filter(MailOutbox.subject == "A new invoice was issued to your account").count() == 0)

r = client.post(f"/billing/{card_id}/invoices", data={"description": "", "amount": "10"},
                 follow_redirects=True)
check("an invoice with no description is rejected", b"required" in r.data)

r = client.post(f"/billing/{card_id}/invoices", data={"description": "Bad amount", "amount": "-5"},
                 follow_redirects=True)
check("a negative amount is rejected", b"greater than zero" in r.data)

r = client.post(f"/billing/{card_id}/invoices", data={"description": "Bad amount", "amount": "not-a-number"},
                 follow_redirects=True)
check("a non-numeric amount is rejected", b"must be a number" in r.data)
logout()

login("mona@example.com", "Member123")
r = client.post(f"/billing/{card_id}/invoices", data={"description": "Self-issued", "amount": "5"},
                 follow_redirects=True)
check("a member cannot issue an invoice, even on their own card", r.status_code == 403)

r = client.get(f"/billing/invoices/{mona_invoice.id}/receipt")
check("a receipt isn't available for a still-pending invoice", r.status_code == 404)

mona_card = Card.query.get(card_id)
check("outstanding_balance reflects the pending invoice",
      payment_manager.outstanding_balance(mona_card) == 49.99,
      f"balance={payment_manager.outstanding_balance(mona_card)}")
logout()

print("\n== 4.12 Payment & Billing: paying an invoice (self-service, mocked card charge) ==")
login("mona@example.com", "Member123")


def _pay(card_number, expiry="12/30", cvv="123"):
    return client.post(f"/billing/invoices/{mona_invoice.id}/pay", data={
        "card_number": card_number, "expiry": expiry, "cvv": cvv,
    }, follow_redirects=True)


r = _pay("123")
check("a too-short card number is rejected", b"13-19 digits" in r.data)

r = _pay("4242424242424241")  # last digit tampered -- fails the Luhn checksum
check("a card number that fails the Luhn checksum is rejected", b"checksum" in r.data)

r = _pay("4242424242424242", expiry="13/99")
check("an invalid expiry format is rejected", b"MM/YY" in r.data)

r = _pay("4242424242424242", expiry="01/20")
check("an expired card is rejected", b"expired" in r.data)

r = _pay("4242424242424242", cvv="12")
check("a CVV that isn't 3-4 digits is rejected", b"CVV" in r.data)

r = _pay("4000000000000002")  # published-style always-declines test card
check("a declined test card fails gracefully with the processor's reason",
      b"declined" in r.data.lower())
declined_check = Invoice.query.get(mona_invoice.id)
check("the decline is recorded on the invoice, which stays pending (not lost)",
      declined_check.status == "pending" and declined_check.last_error
      and "declined" in declined_check.last_error.lower())
logout()

login("leo@example.com", "SelfSignup1")
r = client.post(f"/billing/invoices/{mona_invoice.id}/pay", data={
    "card_number": "4242424242424242", "expiry": "12/30", "cvv": "123",
}, follow_redirects=True)
check("a different member cannot pay someone else's invoice", r.status_code == 403)
logout()

login("roxy@gymflow.local", roxy_temp_pw)
r = client.post(f"/billing/invoices/{mona_invoice.id}/pay", data={
    "card_number": "4242424242424242", "expiry": "12/30", "cvv": "123",
}, follow_redirects=True)
check("staff cannot pay on a member's behalf (self-service only, by design)", r.status_code == 403)
logout()

login("mona@example.com", "Member123")
r = _pay("4242424242424242")  # published-style always-succeeds test card
check("a valid test card succeeds and lands on the receipt page",
      r.status_code == 200 and b"Receipt" in r.data)
paid_check = Invoice.query.get(mona_invoice.id)
check("the invoice is now marked paid with the card's last 4 digits recorded and no error",
      paid_check.status == "paid" and paid_check.payment_method_last4 == "4242"
      and paid_check.paid_at is not None and paid_check.last_error is None,
      f"invoice={paid_check}")

r = _pay("4242424242424242")
check("paying an already-paid invoice is rejected", b"already been paid" in r.data)

r = client.get(f"/billing/invoices/{mona_invoice.id}/receipt")
check("the invoice owner can view their own receipt", r.status_code == 200 and b"49.99" in r.data)
logout()

login("leo@example.com", "SelfSignup1")
r = client.get(f"/billing/invoices/{mona_invoice.id}/receipt")
check("an unrelated member cannot view someone else's receipt", r.status_code == 403)
logout()

login("roxy@gymflow.local", roxy_temp_pw)
r = client.get(f"/billing/invoices/{mona_invoice.id}/receipt")
check("staff can view any member's receipt", r.status_code == 200)

print("\n== 4.12 Payment & Billing: refunds (staff-service) ==")
r = client.post(f"/billing/invoices/{mona_invoice.id}/refund", follow_redirects=True)
check("receptionist refunds a paid invoice", r.status_code == 200 and b"Invoice refunded." in r.data)
refunded_check = Invoice.query.get(mona_invoice.id)
roxy_user = User.query.filter_by(email="roxy@gymflow.local").first()
check("the refund is recorded with who refunded it and when",
      refunded_check.status == "refunded" and refunded_check.refunded_by_id == roxy_user.id
      and refunded_check.refunded_at is not None, f"invoice={refunded_check}")

r = client.post(f"/billing/invoices/{mona_invoice.id}/refund", follow_redirects=True)
check("an already-refunded invoice cannot be refunded again",
      b"Only a paid invoice can be refunded" in r.data)

r = client.get(f"/billing/invoices/{mona_invoice.id}/receipt")
check("the receipt now shows the refunded status", b"refunded" in r.data.lower())
logout()

login("mona@example.com", "Member123")
r = client.post(f"/billing/invoices/{mona_invoice.id}/refund", follow_redirects=True)
check("a member cannot refund their own invoice", r.status_code == 403)
logout()

login("tom@gymflow.local", tom_temp_pw)
r = client.post(f"/billing/invoices/{mona_invoice.id}/refund", follow_redirects=True)
check("a trainer (no manage_billing) cannot refund an invoice either", r.status_code == 403)
logout()

check("mona's balance is back to zero after the refund (a refunded invoice isn't outstanding)",
      payment_manager.outstanding_balance(Card.query.get(card_id)) == 0)

print("\n== 4.12 Payment & Billing: staff follow-up across members + AI agent grounding ==")
past_due = date_cls.fromordinal(date_cls.today().toordinal() - 5).isoformat()
login("roxy@gymflow.local", roxy_temp_pw)
r = client.post(f"/billing/{leo_user.card.id}/invoices", data={
    "description": "October membership fee", "amount": "35.00", "due_date": past_due,
}, follow_redirects=True)
check("receptionist issues an (overdue) invoice to Leo", r.status_code == 200 and b"Invoice issued." in r.data)
check("Leo still has notifications on, so issuing HIS invoice does email him",
      MailOutbox.query.filter_by(to_email="leo@example.com")
      .filter(MailOutbox.subject == "A new invoice was issued to your account").count() == 1)

followup = payment_manager.billing_followup()
leo_row = next((row for row in followup if row["card"].id == leo_user.card.id), None)
check("billing_followup surfaces Leo with his real outstanding balance, flagged overdue",
      leo_row is not None and leo_row["balance"] == 35.0 and leo_row["overdue"] is True,
      f"followup={followup}")
mona_row = next((row for row in followup if row["card"].id == card_id), None)
check("mona (refunded, zero balance) does not show up in the follow-up list", mona_row is None)

r = client.get("/billing/all")
check("the staff 'all invoices' overview lists every invoice",
      r.status_code == 200 and b"October membership fee" in r.data)

r = client.get("/members/dashboard")
check("the staff dashboard shows the 'Outstanding payments' panel with Leo's overdue balance",
      b"Outstanding payments" in r.data and b"Leo" in r.data and b"overdue" in r.data.lower())
logout()

login("mona@example.com", "Member123")
r = client.get("/billing/all", follow_redirects=True)
check("a member cannot reach the staff 'all invoices' overview", b"permission" in r.data.lower())
logout()

login("leo@example.com", "SelfSignup1")
r = client.get("/members/dashboard")
check("Leo's own dashboard shows his real outstanding balance",
      b"Your balance" in r.data and b"35.00" in r.data)

r = client.post("/assistant/message", json={"message": "do I owe anything on my account?"})
check("assistant answers a billing question using the stub (no network call)", r.status_code == 200)
system_msg = next(m["content"] for m in _captured_llm_calls[-1] if m["role"] == "system")
check("the system prompt grounds the assistant in the member's OWN real outstanding balance",
      "Outstanding balance: $35.00" in system_msg, f"system_msg={system_msg!r}")
logout()

print("\n== 4.12 Payment & Billing: simulated processor outage -> retry -> circuit breaker (reuses Section 4.10) ==")
resilience.reset_all_breakers()
login("admin@gymflow.local", "Admin123!")
r = client.post(f"/billing/{card_id}/invoices", data={"description": "Outage test fee", "amount": "10.00"},
                 follow_redirects=True)
check("invoice for the outage check is issued", r.status_code == 200)
outage_invoice = Invoice.query.filter_by(card_id=card_id, description="Outage test fee").first()
logout()

os.environ["GYMFLOW_SIMULATE_PAYMENT_OUTAGE"] = "1"
login("mona@example.com", "Member123")
r = client.post(f"/billing/invoices/{outage_invoice.id}/pay", data={
    "card_number": "4242424242424242", "expiry": "12/30", "cvv": "123",
}, follow_redirects=True)
check("a payment attempt during a simulated processor outage fails gracefully, not with a 500",
      r.status_code == 200 and b"couldn" in r.data.lower())
outage_check = Invoice.query.get(outage_invoice.id)
check("the failed attempt is recorded on the invoice instead of silently lost",
      outage_check.status == "pending" and bool(outage_check.last_error))

r = client.post(f"/billing/invoices/{outage_invoice.id}/pay", data={
    "card_number": "4242424242424242", "expiry": "12/30", "cvv": "123",
}, follow_redirects=True)
check("after repeated failures the circuit breaker trips; the very next attempt fails fast",
      b"temporarily unavailable" in r.data.lower())

os.environ.pop("GYMFLOW_SIMULATE_PAYMENT_OUTAGE", None)
resilience.reset_all_breakers()

r = client.post(f"/billing/invoices/{outage_invoice.id}/pay", data={
    "card_number": "4242424242424242", "expiry": "12/30", "cvv": "123",
}, follow_redirects=True)
check("once the processor is back, the very same invoice pays normally",
      r.status_code == 200 and b"Receipt" in r.data)
logout()

# ---------------------------------------------------------------------------
print("\n== Edge cases: validation, locking, 404s ==")
login("tara@gymflow.local", "Trainer123")
r = client.get("/members/99999")
check("unknown card returns 404", r.status_code == 404)
logout()

login("admin@gymflow.local", "Admin123!")
r = client.post("/admin/staff/new/trainer",
                 data={"name": "Second", "surname": "Trainer", "email": "tara@gymflow.local"},
                 follow_redirects=True)
check("duplicate email is rejected on staff creation", b"already exists" in r.data)

r = client.post("/members/register", data={
    "name": "Bad", "surname": "Email", "email": "not-an-email",
    "birthdate": "1990-01-01", "sex": "F", "address": "x", "phone": "1",
}, follow_redirects=True)
check("malformed email is rejected on member registration", b"not well-formed" in r.data)
logout()

# CardLock: two different staff cannot edit the same card at once.
login("tara@gymflow.local", "Trainer123")
r = client.get(f"/members/{card_id}/edit")
check("trainer acquires the card lock", r.status_code == 200 and b"Edit card" in r.data)
logout()

login("admin@gymflow.local", "Admin123!")
r = client.get(f"/members/{card_id}/edit", follow_redirects=True)
check("second staff member is blocked by the live lock", b"currently being edited" in r.data)
logout()

login("tara@gymflow.local", "Trainer123")
r = client.post(f"/members/{card_id}/edit/cancel", follow_redirects=True)
check("lock holder releases the lock", r.status_code == 200 and b"lock was released" in r.data)
logout()

login("admin@gymflow.local", "Admin123!")
r = client.get(f"/members/{card_id}/edit")
check("lock is free again after release", r.status_code == 200 and b"Edit card" in r.data)
client.post(f"/members/{card_id}/edit/cancel", follow_redirects=True)
logout()

# Empty progress note should be rejected.
login("tara@gymflow.local", "Trainer123")
r = client.post(f"/members/{card_id}/notes", data={"text": "   "}, follow_redirects=True)
check("empty progress note is rejected", b"cannot be empty" in r.data)
logout()

# ---------------------------------------------------------------------------
print("\n== PWA: manifest, service worker, offline fallback, icons ==")
r = client.get("/static/manifest.json")
check("manifest.json is served", r.status_code == 200 and b'"short_name": "GymFlow"' in r.data)
r = client.get("/sw.js")
check("sw.js is served from root scope", r.status_code == 200
      and r.headers.get("Service-Worker-Allowed") == "/"
      and "javascript" in (r.content_type or ""))
r = client.get("/offline")
check("offline fallback page renders", r.status_code == 200 and b"offline" in r.data.lower())
for icon in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png", "favicon.ico"):
    r = client.get(f"/static/icons/{icon}")
    check(f"icon asset {icon} is served", r.status_code == 200 and len(r.data) > 0)

login("admin@gymflow.local", "Admin123!")
r = client.get("/members/dashboard")
check("manifest link + SW registration script present on a logged-in page",
      b'rel="manifest"' in r.data and b"serviceWorker" in r.data)
logout()

# ---------------------------------------------------------------------------
print(f"\n{checks} checks run, {len(failures)} failed.")
if failures:
    print("FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
