"""End-to-end rewards/pricing checks against the REAL Flask-SQLAlchemy +
SQLite stack (not the in-memory shim), through the Flask test client.

Covers: upgrading an older database file, the readable rule language,
membership-tier (Bronze/Silver/Gold) offers, free-class credits in the real
booking flow, tier discounts in the real payment flow, refunds, limits,
pausing, roles, member data isolation and CSRF.

Run from the project folder:  python3 _devtools/test_loyalty_e2e.py
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore", category=DeprecationWarning)
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)

from sqlalchemy.schema import CreateTable  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from gymflow import create_app, loyalty_engine  # noqa: E402
from gymflow.config import Config  # noqa: E402
from gymflow.extensions import db  # noqa: E402
from gymflow.models import (  # noqa: E402
    Booking, Card, FitnessClass, Invoice, LoyaltyAccount, PointsLedgerEntry,
    PricingPolicy, Reward, RewardRedemption, User,
)

failures = []
checks = 0


def check(label, condition, extra=""):
    global checks
    checks += 1
    print(f"[{'OK  ' if condition else 'FAIL'}] {label} {extra}")
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------------------
# 1. Build an OLD database file: tables as they were before the rewards
#    feature (no loyalty tables, no newer columns), with some existing data.
# ---------------------------------------------------------------------------
print("== Upgrading an older database file ==")
fd, database_path = tempfile.mkstemp(prefix="gymflow-e2e-", suffix=".sqlite")
os.close(fd)
os.unlink(database_path)

NEW_TABLES = {"loyalty_tiers", "loyalty_accounts", "pricing_policies", "policy_applications",
              "policy_audit", "points_ledger", "rewards", "reward_redemptions"}
NEW_COLUMNS = {"cards": {"card_color"}, "invoices": {"discount_amount", "paid_amount", "category"},
               "bookings": {"reward_redemption_id"}, "fitness_classes": {"category"}}

from sqlalchemy.dialects import sqlite as sqlite_dialect  # noqa: E402

connection = sqlite3.connect(database_path)
for table in db.metadata.sorted_tables:
    if table.name in NEW_TABLES:
        continue
    ddl = str(CreateTable(table).compile(dialect=sqlite_dialect.dialect())).strip()
    removed = NEW_COLUMNS.get(table.name, set())
    head, body = ddl.split("(", 1)
    body = body.rsplit(")", 1)[0]
    parts = [part.strip() for part in body.split(", \n")]
    parts = [part for part in parts
             if part.split(" ")[0] not in removed
             and not any(part.startswith(f"FOREIGN KEY({column})") for column in removed)]
    connection.execute(head + "(\n  " + ",\n  ".join(parts) + "\n)")
pw = generate_password_hash("Testpass123!")
now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
connection.executescript(f"""
INSERT INTO users (id, role, name, surname, email, password_hash, must_change_password, notifications_enabled, created_at)
VALUES (1, 'administrator', 'Old', 'Admin', 'admin@old.test', '{pw}', 0, 1, '{now}'),
       (2, 'member', 'Gina', 'Gold', 'gina@old.test', '{pw}', 0, 1, '{now}'),
       (3, 'member', 'Otto', 'Other', 'otto@old.test', '{pw}', 0, 1, '{now}'),
       (4, 'trainer', 'Tom', 'Trainer', 'tom@old.test', '{pw}', 0, 1, '{now}'),
       (5, 'receptionist', 'Rae', 'Desk', 'rae@old.test', '{pw}', 0, 1, '{now}');
INSERT INTO cards (id, user_id, member_code, birthdate) VALUES (1, 2, 'GF-OLD-1', '1980-05-01'), (2, 3, 'GF-OLD-2', '1999-05-01');
INSERT INTO invoices (id, card_id, issued_by_id, description, amount, status, issued_at)
VALUES (1, 1, 1, 'Old membership fee', 60.0, 'paid', '{now}');
""")
connection.commit()
old_columns = {row[1] for row in connection.execute("PRAGMA table_info(invoices)")}
connection.close()
check("the simulated old database really lacks the newer columns",
      "category" not in old_columns and "discount_amount" not in old_columns)


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"
    UPLOAD_FOLDER = tempfile.mkdtemp(prefix="gymflow-e2e-uploads-")


try:
    app = create_app(TestConfig)
    started = True
except Exception as exc:  # noqa: BLE001 -- this is exactly what the check is about
    started = False
    print("start-up error:", repr(exc))
check("the app starts on an old database file (additive migration)", started)
if not started:
    raise SystemExit(1)

connection = sqlite3.connect(database_path)
invoice_columns = {row[1] for row in connection.execute("PRAGMA table_info(invoices)")}
class_columns = {row[1] for row in connection.execute("PRAGMA table_info(fitness_classes)")}
booking_columns = {row[1] for row in connection.execute("PRAGMA table_info(bookings)")}
old_invoice = connection.execute("SELECT description, amount, discount_amount, category FROM invoices").fetchone()
connection.close()
check("missing columns were added", {"category", "discount_amount", "paid_amount"} <= invoice_columns
      and "category" in class_columns and "reward_redemption_id" in booking_columns)
check("existing rows are kept (no data loss)", old_invoice[0] == "Old membership fee" and old_invoice[1] == 60.0
      and old_invoice[2] == 0)

ctx = app.app_context()
ctx.push()
check("default Bronze/Silver/Gold tiers are seeded for the existing admin",
      sorted(t.name for t in loyalty_engine.LoyaltyTier.query.all()) == ["Bronze", "Gold", "Silver"])
db.session.remove()

client = app.test_client()


def login(email):
    client.get("/logout")
    return client.post("/login", data={"email": email, "password": "Testpass123!"})


def page(url):
    return client.get(url, follow_redirects=True).get_data(as_text=True)


login("gina@old.test")
for url in ("/members/dashboard", "/members/1", "/loyalty/", "/classes/", "/billing/1"):
    response = client.get(url)
    check(f"member page {url} works on the upgraded database", response.status_code == 200, response.status_code)

# ---------------------------------------------------------------------------
# 2. Administrator configures offers through the UI (no code changes).
# ---------------------------------------------------------------------------
print("\n== Administrator creates rewards and rules in the UI ==")
login("admin@old.test")
response = client.post("/admin/loyalty/rewards", data={
    "name": "Gold monthly free class", "reward_type": "free_class", "points_cost": "300",
    "target": "class_booking", "minimum_tier": "Gold", "active": "1"}, follow_redirects=True)
gold_reward = Reward.query.filter_by(name="Gold monthly free class").first()
check("admin creates a Gold free-class reward", gold_reward is not None and gold_reward.minimum_tier == "Gold")
limited = client.post("/admin/loyalty/rewards", data={
    "name": "One-off voucher", "reward_type": "fixed_discount", "points_cost": "50", "value": "5",
    "target": "invoice", "max_redemptions": "1", "active": "1"})
voucher = Reward.query.filter_by(name="One-off voucher").first()


def create_policy(name, event, rule, status="active", **extra):
    data = {"name": name, "event_type": event, "definition": rule, "status": status, "priority": "50"}
    data.update(extra)
    return client.post("/admin/loyalty/policies", data=data, follow_redirects=True).get_data(as_text=True)


create_policy("Gold free class each month", "member_activity",
              f'WHEN member.tier == "Gold" THEN grant_reward({gold_reward.id})')
gold_policy = PricingPolicy.query.filter_by(name="Gold free class each month").first()
check("readable rule is saved as validated JSON (not as code)", gold_policy is not None and json.loads(gold_policy.definition) == {
    "conditions": [{"fact": "member.tier", "op": "eq", "value": "Gold"}],
    "action": {"type": "grant_reward", "reward_id": gold_reward.id}})
create_policy("Yoga or mobility bonus", "class_attended",
              'WHEN event == "class_attended"\nAND (class.category == "yoga" OR class.category == "mobility")\nTHEN award_points(20)')
create_policy("Gold renewal discount", "invoice_paid",
              'WHEN member.tier == "Gold" THEN apply_discount(percent=10, target="membership")', priority="5")
create_policy("Points for payments", "invoice_paid", "THEN award_points(25)")
created = {p.name for p in PricingPolicy.query.all()}
check("all four offers were created from the UI", {"Gold free class each month", "Yoga or mobility bonus",
                                                    "Gold renewal discount", "Points for payments"} <= created)

before = PricingPolicy.query.count()
for bad_rule, label in [
    ('THEN __import__("os").system("x")', "Python injection"),
    ("WHEN member.salary > 1 THEN award_points(5)", "unknown fact"),
    ("WHEN member.age >= 18 THEN apply_discount(percent=90)", "discount over the cap"),
    ("WHEN class.category == \"yoga\"; DROP TABLE users THEN award_points(1)", "SQL-like text"),
    ("WHEN class.category == \"boxing\" THEN award_points(1)", "invalid category value"),
]:
    html = create_policy(f"Bad {label}", "class_attended", bad_rule)
    check(f"unsafe/invalid rule rejected with a message: {label}", "flash danger" in html)
check("no invalid policy was stored", PricingPolicy.query.count() == before)

manage_html = page("/admin/loyalty/")
check("admin page shows the readable rule and plain-English summary",
      'WHEN event == &#34;member_activity&#34;' in manage_html or 'WHEN event == "member_activity"' in manage_html)
check("admin page lists the rule reference with class.category", "class.category" in manage_html)

# ---------------------------------------------------------------------------
# 3. Member tier shows on the card; monthly Gold perk granted exactly once.
# ---------------------------------------------------------------------------
print("\n== Membership tier on the card + Gold monthly free class ==")
gina = User.query.filter_by(email="gina@old.test").first()
account = loyalty_engine.get_account(gina.card)
account.lifetime_points = 1300
account.balance = 100
db.session.commit()
login("gina@old.test")
dashboard = page("/members/dashboard")
check("member dashboard shows 'Gold Rewards Tier'", "Gold Rewards Tier" in dashboard)
card_html = page(f"/members/{gina.card.id}")
check("membership card shows the rewards tier", "Gold Rewards Tier" in card_html and "Membership level" in card_html)
page("/members/dashboard")  # refresh / retry in the same month
credits = RewardRedemption.query.filter_by(account_id=account.id, reward_id=gold_reward.id).all()
check("Gold perk granted once per month even after several dashboard visits", len(credits) == 1
      and credits[0].status == "reserved" and credits[0].policy_id == gold_policy.id)
wallet = page("/loyalty/")
check("wallet shows 'Ready to use' and 'Offers for you'", "Ready to use" in wallet and "Offers for you" in wallet
      and "Gold monthly free class" in wallet)

otto = User.query.filter_by(email="otto@old.test").first()
login("otto@old.test")
page("/members/dashboard")
otto_account = loyalty_engine.get_account(otto.card, create=False)
check("a non-Gold member does not get the Gold perk",
      otto_account is None
      or not RewardRedemption.query.filter_by(account_id=otto_account.id, reward_id=gold_reward.id).first())

# Next month: the same rule gives a new credit.
loyalty_engine.apply_event("member_activity", f"member:{gina.id}:2099-01", gina)
db.session.commit()
check("the Gold perk renews in a new month",
      RewardRedemption.query.filter_by(account_id=account.id, reward_id=gold_reward.id).count() == 2)

# ---------------------------------------------------------------------------
# 4. Free class used in the real booking flow; cannot be used twice.
# ---------------------------------------------------------------------------
print("\n== Free class credit in the booking flow ==")
login("tom@old.test")
future = (datetime.utcnow() + timedelta(days=2)).replace(second=0, microsecond=0)
client.post("/classes/new", data={"name": "Sunrise Yoga", "schedule": future.strftime("%Y-%m-%dT%H:%M"),
                                  "room": "Studio Y", "capacity": "5", "category": "yoga"})
client.post("/classes/new", data={"name": "Power Hour", "schedule": (future + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"),
                                  "room": "Studio Y", "capacity": "5", "category": "strength"})
bad = client.post("/classes/new", data={"name": "Mystery", "schedule": (future + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M"),
                                        "room": "Studio Y", "capacity": "5", "category": "boxing"}, follow_redirects=True)
yoga = FitnessClass.query.filter_by(name="Sunrise Yoga").first()
power = FitnessClass.query.filter_by(name="Power Hour").first()
check("trainer creates classes with a category", yoga is not None and yoga.category == "yoga" and power.category == "strength")
check("unknown class category is rejected", FitnessClass.query.filter_by(name="Mystery").first() is None)

credit = RewardRedemption.query.filter_by(account_id=account.id, reward_id=gold_reward.id, status="reserved").first()
login("gina@old.test")
check("class list offers the free class credit", "Optional class credit" in page("/classes/"))
client.post(f"/classes/{yoga.id}/book", data={"reward_redemption_id": str(credit.id)})
booking = Booking.query.filter_by(class_id=yoga.id, member_id=gina.id).first()
db.session.refresh(credit)
check("booking uses the free class credit", booking is not None and booking.reward_redemption_id == credit.id
      and credit.status == "used")
client.post(f"/classes/{power.id}/book", data={"reward_redemption_id": str(credit.id)})
check("the same credit cannot be used for a second class",
      Booking.query.filter_by(class_id=power.id, member_id=gina.id).first() is None)

login("otto@old.test")
other_credit = RewardRedemption.query.filter_by(account_id=account.id, status="reserved").first()
client.post(f"/classes/{power.id}/book", data={"reward_redemption_id": str(other_credit.id)})
check("another member cannot use Gina's credit",
      Booking.query.filter_by(class_id=power.id, member_id=otto.id).first() is None)

# ---------------------------------------------------------------------------
# 5. Attendance points with class category; idempotent; reversible.
# ---------------------------------------------------------------------------
print("\n== Attendance points by class category ==")
login("tom@old.test")
client.post(f"/classes/bookings/{booking.id}/attendance", data={"attended": "1"})
client.post(f"/classes/bookings/{booking.id}/attendance", data={"attended": "1"})
yoga_entries = [e for e in PointsLedgerEntry.query.filter_by(account_id=account.id).all()
                if e.event_key.endswith(f"attendance:{booking.id}")]
check("yoga attendance earns the category bonus exactly once (retry safe)",
      len(yoga_entries) == 1 and yoga_entries[0].points == 20)

# ---------------------------------------------------------------------------
# 6. Tier discount in the real payment flow; category-targeted; refund.
# ---------------------------------------------------------------------------
print("\n== Gold membership discount in the payment flow ==")
login("rae@old.test")
client.post(f"/billing/{gina.card.id}/invoices", data={"description": "Membership renewal", "amount": "100",
                                                     "category": "membership"})
client.post(f"/billing/{gina.card.id}/invoices", data={"description": "10-class pack", "amount": "50",
                                                     "category": "class_pack"})
renewal = Invoice.query.filter_by(description="Membership renewal").first()
pack = Invoice.query.filter_by(description="10-class pack").first()
check("receptionist issues invoices with a category", renewal.category == "membership" and pack.category == "class_pack")
login("gina@old.test")
card_data = {"card_number": "4242424242424242", "expiry": "12/30", "cvv": "123"}
balance_before = loyalty_engine.get_account(gina.card).balance
client.post(f"/billing/invoices/{renewal.id}/pay", data=card_data)
client.post(f"/billing/invoices/{pack.id}/pay", data=card_data)
db.session.expire_all()
renewal, pack = Invoice.query.get(renewal.id), Invoice.query.get(pack.id)
check("Gold member gets 10% off the membership renewal", renewal.status == "paid"
      and renewal.discount_amount == 10.0 and renewal.paid_amount == 90.0, f"{renewal.status} {renewal.discount_amount}")
check("the membership-only discount does not apply to a class pack", pack.status == "paid" and pack.discount_amount == 0)
account = loyalty_engine.get_account(gina.card)
check("each paid invoice earns 25 points", account.balance == balance_before + 50)
response = client.post(f"/billing/invoices/{renewal.id}/pay", data=card_data, follow_redirects=True)
check("a paid invoice cannot be paid (or rewarded) twice", loyalty_engine.get_account(gina.card).balance == balance_before + 50)

login("rae@old.test")
client.post(f"/billing/invoices/{renewal.id}/refund")
account = loyalty_engine.get_account(gina.card)
check("refund reverses the purchase points but keeps lifetime points (tier stays)",
      account.balance == balance_before + 25 and account.lifetime_points >= 1300
      and PointsLedgerEntry.query.filter_by(event_key=f"refund:invoice:{renewal.id}").first() is not None)

# ---------------------------------------------------------------------------
# 7. Pausing, limits, preview, permissions, CSRF.
# ---------------------------------------------------------------------------
print("\n== Pause, limits, preview, roles, CSRF ==")
login("admin@old.test")
client.post(f"/admin/loyalty/policies/{gold_policy.id}/status", data={"status": "paused"})
count_before = RewardRedemption.query.filter_by(reward_id=gold_reward.id).count()
loyalty_engine.apply_event("member_activity", f"member:{gina.id}:2099-02", gina)
db.session.commit()
check("a paused offer gives nothing", RewardRedemption.query.filter_by(reward_id=gold_reward.id).count() == count_before)
client.post(f"/admin/loyalty/policies/{gold_policy.id}/status", data={"status": "active"})
check("re-activated offer is active again", PricingPolicy.query.get(gold_policy.id).status == "active")

yoga_policy = PricingPolicy.query.filter_by(name="Yoga or mobility bonus").first()
html = client.post(f"/admin/loyalty/policies/{yoga_policy.id}/preview",
                   data={"member_id": str(otto.id), "class_category": "mobility"}, follow_redirects=True).get_data(as_text=True)
check("preview: mobility class matches the yoga-or-mobility rule", "matches" in html)
html = client.post(f"/admin/loyalty/policies/{yoga_policy.id}/preview",
                   data={"member_id": str(otto.id), "class_category": "strength"}, follow_redirects=True).get_data(as_text=True)
check("preview: strength class does not match", "does not match" in html)

ended = PricingPolicy.query.filter_by(name="Points for payments").first()
ended.ends_at = datetime.utcnow() - timedelta(minutes=1)
db.session.commit()
page("/admin/loyalty/")
check("a policy past its end date is shown as expired", PricingPolicy.query.get(ended.id).status == "expired")

# Total redemption limit counts ALL members (was counting only one member).
for member in (gina, otto):
    acc = loyalty_engine.get_account(member.card)
    acc.balance += 100
db.session.commit()
login("gina@old.test")
client.post(f"/loyalty/redeem/{voucher.id}")
login("otto@old.test")
client.post(f"/loyalty/redeem/{voucher.id}")
check("a reward limited to 1 total redemption can only be redeemed once overall",
      RewardRedemption.query.filter_by(reward_id=voucher.id).count() == 1)

for email in ("otto@old.test", "tom@old.test", "rae@old.test"):
    login(email)
    check(f"{email.split('@')[0]} cannot open the policy admin page", client.get("/admin/loyalty/").status_code == 403)
    check(f"{email.split('@')[0]} cannot adjust points",
          client.post("/admin/loyalty/points/adjust", data={"member_id": str(otto.id), "delta": "500", "reason": "x"}).status_code == 403)
login("otto@old.test")
check("member cannot open another member's card", client.get(f"/members/{gina.card.id}").status_code in (403, 404))

past_class = FitnessClass(name="Yesterday Spin", trainer_id=User.query.filter_by(email="tom@old.test").first().id,
                          schedule=datetime.utcnow() - timedelta(days=1), room="Old", capacity=5, category="spin")
db.session.add(past_class)
db.session.flush()
past_booking = Booking(class_id=past_class.id, member_id=otto.id, status="confirmed", attended=True)
db.session.add(past_booking)
db.session.commit()
login("otto@old.test")
check("a past class is not listed as upcoming on the dashboard", "Yesterday Spin" not in page("/members/dashboard"))
client.post(f"/classes/bookings/{past_booking.id}/cancel")
db.session.expire_all()
check("a class that already took place cannot be cancelled", Booking.query.get(past_booking.id).status == "confirmed")

login("gina@old.test")
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("GYMFLOW_LLM_BASE_URL", None)
reply = client.post("/assistant/message", json={"message": "How many points do I have?"}).get_json()["reply"]
check("assistant (offline mode) answers rewards questions from the member's own data",
      "Gold Rewards Tier" in reply and "Offline mode" in reply)
reply = client.post("/assistant/message", json={"message": "what do I owe?"}).get_json()["reply"]
check("assistant (offline mode) answers billing questions", "balance" in reply.lower())
login("rae@old.test")
reply = client.post("/assistant/message", json={"message": "any outstanding invoices?"}).get_json()["reply"]
check("receptionist assistant is grounded in billing follow-up", "outstanding" in reply.lower())

print("\n== Offer preview at payment, paused rewards, tier rename, programme overview ==")
login("rae@old.test")
client.post(f"/billing/{gina.card.id}/invoices", data={"description": "Annual membership", "amount": "200",
                                                     "category": "membership"})
annual = Invoice.query.filter_by(description="Annual membership").first()
login("gina@old.test")
billing_html = page(f"/billing/{gina.card.id}")
check("member sees the automatic offer before paying", "Offer: −$20.00 at payment" in billing_html
      and "Pay $180.00" in billing_html)
client.post(f"/billing/invoices/{annual.id}/pay", data=card_data)
db.session.expire_all()
check("the amount actually charged matches the preview", Invoice.query.get(annual.id).paid_amount == 180.0)

login("admin@old.test")
client.post(f"/admin/loyalty/rewards/{gold_reward.id}/status", data={"active": "0"})
login("gina@old.test")
check("a paused reward is not offered when booking", "Optional class credit" not in page("/classes/"))
check("the wallet marks a paused reward as not available", "Not available right now" in page("/loyalty/"))
login("admin@old.test")
client.post(f"/admin/loyalty/rewards/{gold_reward.id}/status", data={"active": "1"})

gold_tier = loyalty_engine.LoyaltyTier.query.filter_by(name="Gold").first()
client.post("/admin/loyalty/tiers", data={"tier_id": str(gold_tier.id), "name": "Platinum",
                                          "threshold": str(gold_tier.threshold), "sort_order": "2",
                                          "benefits": "Top tier", "active": "1"})
db.session.expire_all()
renamed_rule = json.loads(PricingPolicy.query.get(gold_policy.id).definition)
check("renaming a tier updates the offers that use it",
      renamed_rule["conditions"][0]["value"] == "Platinum"
      and Reward.query.get(gold_reward.id).minimum_tier == "Platinum")
before = RewardRedemption.query.filter_by(reward_id=gold_reward.id).count()
loyalty_engine.apply_event("member_activity", f"member:{gina.id}:2099-03", gina)
db.session.commit()
check("the renamed tier's monthly perk keeps working",
      RewardRedemption.query.filter_by(reward_id=gold_reward.id).count() == before + 1)
client.post("/admin/loyalty/tiers", data={"tier_id": str(gold_tier.id), "name": "Gold",
                                          "threshold": str(gold_tier.threshold), "sort_order": "2",
                                          "benefits": "Top tier", "active": "1"})
admin_html = page("/admin/loyalty/")
check("admin page shows the programme overview", "Programme overview" in admin_html and "Points earned" in admin_html)
check("members list shows each member's rewards tier", "tier-chip" in page("/members/"))
login("gina@old.test")
wallet = page("/loyalty/")
check("wallet history is one timeline with rewards and points", "Points and rewards history" in wallet
      and "Reward used" in wallet)

app.config["CSRF_FORCE_ENABLED"] = True
login_page = client.get("/login")
login("admin@old.test")  # login itself is CSRF-checked too; use a token
token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").get_data(as_text=True))
client.post("/login", data={"email": "admin@old.test", "password": "Testpass123!",
                            "csrf_token": token.group(1) if token else ""})
response = client.post("/admin/loyalty/policies", data={"name": "No token", "event_type": "class_attended",
                                                         "definition": "THEN award_points(1)", "status": "active"})
check("policy changes without a CSRF token are refused", response.status_code == 400
      and PricingPolicy.query.filter_by(name="No token").first() is None, response.status_code)
app.config["CSRF_FORCE_ENABLED"] = False

ctx.pop()
with app.app_context():
    db.session.remove()
    app.extensions["sqlalchemy"].engine.dispose()
os.unlink(database_path)

print(f"\n{checks} checks run, {len(failures)} failed.")
if failures:
    for item in failures:
        print("  FAILED:", item)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
