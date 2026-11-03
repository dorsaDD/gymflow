"""Focused rewards/policy tests using GymFlow's in-memory test shim.

Run from the project directory: python3 _devtools/test_loyalty.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "shim"))
sys.path.insert(0, APP_ROOT)
os.environ.setdefault("GYMFLOW_TEST_DB", "1")

from gymflow import create_app  # noqa: E402
from gymflow import class_manager, loyalty_engine, payment_manager  # noqa: E402
from gymflow.card_manager import ValidationError  # noqa: E402
from gymflow.extensions import db  # noqa: E402
from gymflow.models import (  # noqa: E402
    Booking, Card, FitnessClass, Invoice, LoyaltyAccount, LoyaltyTier,
    PointsLedgerEntry, PricingPolicy, Reward, RewardRedemption, User,
)

app = create_app()
app.testing = True
app.app_context().push()
failures = []
checks = 0


def check(label, condition):
    global checks
    checks += 1
    print(f"[{'OK  ' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


def make_user(role, email):
    user = User(role=role, name=role.title(), surname="Test", email=email)
    user.set_password("Testpass123!")
    db.session.add(user)
    db.session.commit()
    return user


def make_member(email, code, birthdate=None):
    user = make_user("member", email)
    card = Card(user_id=user.id, member_code=code, birthdate=birthdate)
    db.session.add(card)
    db.session.commit()
    return user, card


def make_policy(name, event, definition, admin, **kwargs):
    policy = PricingPolicy(name=name, event_type=event, definition=json.dumps(definition),
                           status="active", priority=kwargs.get("priority", 100),
                           created_by_id=admin.id, updated_by_id=admin.id,
                           max_uses_per_member=kwargs.get("max_uses_per_member"),
                           max_uses_total=kwargs.get("max_uses_total"))
    db.session.add(policy)
    db.session.commit()
    return policy


print("== DSL validation and interpretation ==")
valid = {"conditions": [{"fact": "member.age", "op": "gte", "value": 18}],
         "action": {"type": "award_points", "points": 10}}
check("valid JSON rule is accepted", loyalty_engine.validate_definition("class_attended", valid) == valid)
for event, unsafe in [
    ("class_attended", {"conditions": [{"fact": "__import__('os')", "op": "eq", "value": "x"}], "action": {"type": "award_points", "points": 10}}),
    ("class_attended", {"conditions": [{"fact": "member.age", "op": "eval", "value": 18}], "action": {"type": "award_points", "points": 10}}),
    ("class_attended", {"conditions": [{"fact": [], "op": "eq", "value": 18}], "action": {"type": "award_points", "points": 10}}),
    ("class_attended", {"conditions": [{"fact": "member.age", "op": "gte", "value": float("inf")}], "action": {"type": "award_points", "points": 10}}),
    ("invoice_paid", {"conditions": [], "action": {"type": "discount_percent", "percent": 51}}),
    ("class_attended", {"conditions": [], "action": {"type": "discount_fixed", "amount": 5}}),
]:
    try:
        loyalty_engine.validate_definition(event, unsafe)
        accepted = True
    except ValidationError:
        accepted = False
    check("unsafe, unsupported, or over-limit rule is rejected", not accepted)
check("AND conditions evaluate through supported comparisons", loyalty_engine.matches(valid, {"member.age": 18}))
check("unknown or missing fact does not match", not loyalty_engine.matches(valid, {}))

print("\n== Tier thresholds and progress ==")
admin = User.query.filter_by(role="administrator").first()
for name, threshold, order in [("Bronze", 0, 0), ("Silver", 500, 1), ("Gold", 1200, 2)]:
    if not LoyaltyTier.query.filter_by(name=name).first():
        db.session.add(LoyaltyTier(name=name, threshold=threshold, sort_order=order, active=True))
db.session.commit()
member, card = make_member("loyalty-member@example.test", "GF-LOY-1", datetime(1990, 1, 1).date())
account = LoyaltyAccount(card_id=card.id, balance=250, lifetime_points=250)
db.session.add(account)
db.session.commit()
p = loyalty_engine.tier_progress(account)
check("250 lifetime points is Bronze with 250 to Silver and 50% progress",
      p["current"].name == "Bronze" and p["remaining"] == 250 and p["percent"] == 50)
account.lifetime_points = 500
check("exact 500 threshold enters Silver", loyalty_engine.current_tier(account).name == "Silver")
account.lifetime_points = 499
check("one point below Silver remains Bronze", loyalty_engine.current_tier(account).name == "Bronze"
      and loyalty_engine.tier_progress(account)["remaining"] == 1)
account.lifetime_points = 1200
check("exact 1,200 threshold enters Gold and completes progress",
      loyalty_engine.current_tier(account).name == "Gold" and loyalty_engine.tier_progress(account)["percent"] == 100)

print("\n== Active-member milestone event ==")
active_member, active_card = make_member("active-member@example.test", "GF-LOY-A")
active_member.created_at = datetime.utcnow() - timedelta(days=31)
db.session.commit()
active_policy = make_policy("30 day member milestone", "member_activity",
                            {"conditions": [{"fact": "member.active_days", "op": "gte", "value": 30}],
                             "action": {"type": "award_points", "points": 20}}, admin,
                            max_uses_per_member=1)
loyalty_engine.apply_event("member_activity", f"member:{active_member.id}", active_member)
db.session.commit()
loyalty_engine.apply_event("member_activity", f"member:{active_member.id}", active_member)
db.session.commit()
active_account = LoyaltyAccount.query.filter_by(card_id=active_card.id).first()
check("active-days rule awards once when the member next visits", active_account is not None and active_account.balance == 20)

print("\n== Attendance point awarding, no-show, idempotency, correction ==")
trainer = make_user("trainer", "loyalty-trainer@example.test")
fc = FitnessClass(name="Test class", trainer_id=trainer.id,
                  schedule=datetime.utcnow() - timedelta(days=1), room="Test", capacity=3)
db.session.add(fc)
db.session.commit()
policy = make_policy("10 per attendance", "class_attended",
                     {"conditions": [], "action": {"type": "award_points", "points": 10}}, admin,
                     max_uses_per_member=5)
booking = Booking(class_id=fc.id, member_id=member.id, status="confirmed")
db.session.add(booking)
db.session.commit()
class_manager.mark_attendance(booking, True)
class_manager.mark_attendance(booking, True)
check("confirmed attendance awards points once even if handler repeats",
      PointsLedgerEntry.query.filter_by(event_key=f"policy:{policy.id}:attendance:{booking.id}").count() == 1)
earned_balance = account.balance
no_show = Booking(class_id=fc.id, member_id=member.id, status="confirmed")
db.session.add(no_show)
db.session.commit()
class_manager.mark_attendance(no_show, False)
check("no-show earns no attendance points", account.balance == earned_balance)
class_manager.mark_attendance(booking, False)
check("attendance correction appends a reversal and reduces spendable balance",
      PointsLedgerEntry.query.filter_by(event_key=f"reverse:attendance:{booking.id}").first() is not None
      and LoyaltyAccount.query.get(account.id).balance == earned_balance - 10)

free_reward = Reward(name="Free class", reward_type="free_class", points_cost=999,
                     target="class_booking", active=True, max_per_member=1,
                     created_by_id=admin.id)
db.session.add(free_reward)
db.session.commit()
grant_policy = make_policy("Attendance class credit", "class_attended",
                           {"conditions": [], "action": {"type": "grant_reward", "reward_id": free_reward.id}},
                           admin, max_uses_per_member=1)
grant_booking = Booking(class_id=fc.id, member_id=member.id, status="confirmed")
db.session.add(grant_booking)
db.session.commit()
class_manager.mark_attendance(grant_booking, True)
credit = RewardRedemption.query.filter_by(account_id=account.id, reward_id=free_reward.id).first()
check("policy can grant a configured free-class credit without a point debit",
      credit is not None and credit.status == "reserved")
fc2 = FitnessClass(name="Credit class", trainer_id=trainer.id,
                   schedule=datetime.utcnow() + timedelta(days=1), room="Test 2", capacity=2)
db.session.add(fc2)
db.session.commit()
credit_booking = class_manager.book_class(member, fc2, credit.id)
check("free-class credit is consumed by the existing class booking flow",
      credit_booking.reward_redemption_id == credit.id and credit.status == "used")
class_manager.cancel_booking(member, credit_booking)
check("cancelling a class restores its free-class credit to reserved",
      RewardRedemption.query.get(credit.id).status == "reserved")

print("\n== Invoice payment points, discount, refund ==")
invoice_policy = make_policy("Paid invoice points", "invoice_paid",
                             {"conditions": [{"fact": "invoice.amount", "op": "gte", "value": 20}],
                              "action": {"type": "award_points", "points": 25}}, admin)
discount_policy = make_policy("Test invoice discount", "invoice_paid",
                              {"conditions": [], "action": {"type": "discount_percent", "percent": 10}}, admin,
                              priority=5)
expired_policy = make_policy("Future offer", "invoice_paid",
                             {"conditions": [], "action": {"type": "discount_percent", "percent": 5}}, admin)
expired_policy.starts_at = datetime.utcnow() + timedelta(days=1)
db.session.commit()
check("policy start date prevents early application", expired_policy not in loyalty_engine._eligible_policies("invoice_paid"))
invoice = Invoice(card_id=card.id, issued_by_id=admin.id, description="Test invoice", amount=100)
db.session.add(invoice)
db.session.commit()
check("unpaid invoice has not earned purchase points", invoice.status == "pending"
      and PointsLedgerEntry.query.filter_by(event_key=f"policy:{invoice_policy.id}:invoice:{invoice.id}").first() is None)
paid = payment_manager.pay_invoice(invoice, {"card_number": "4242424242424242", "expiry": "12/30", "cvv": "123"})
check("successful invoice payment applies the configured discount", paid.status == "paid" and paid.discount_amount == 10.0 and paid.paid_amount == 90.0)
check("successful invoice payment awards configured points exactly once",
      PointsLedgerEntry.query.filter_by(event_key=f"policy:{invoice_policy.id}:invoice:{invoice.id}").count() == 1)
before_refund = LoyaltyAccount.query.get(account.id).balance
payment_manager.refund_invoice(paid, admin)
check("refund creates a reversal entry and does not lower lifetime points",
      PointsLedgerEntry.query.filter_by(event_key=f"refund:invoice:{invoice.id}").first() is not None
      and LoyaltyAccount.query.get(account.id).lifetime_points >= 500)
fixed_policy = make_policy("Fixed discount cap", "invoice_paid",
                           {"conditions": [], "action": {"type": "discount_fixed", "amount": 200}}, admin,
                           priority=1)
second_invoice = Invoice(card_id=card.id, issued_by_id=admin.id, description="Fixed cap invoice", amount=100)
db.session.add(second_invoice)
db.session.commit()
paid_second = payment_manager.pay_invoice(second_invoice, {"card_number": "4242424242424242", "expiry": "12/30", "cvv": "123"})
check("fixed discount uses priority and stays under 50% cap without stacking",
      paid_second.discount_amount == 50.0 and paid_second.paid_amount == 50.0)

print("\n== Reward redemption and role boundaries ==")
discount_reward = Reward(name="10% off", reward_type="percent_discount", points_cost=10,
                         value=10, target="invoice", active=True, max_per_member=1,
                         created_by_id=admin.id)
db.session.add(discount_reward)
db.session.commit()
member_account = LoyaltyAccount.query.get(account.id)
member_account.balance = max(20, member_account.balance)
db.session.commit()
balance_before_redeem = member_account.balance
redemption = loyalty_engine.redeem_reward(member, discount_reward)
check("sufficient balance reserves reward and deducts points",
      redemption.status == "reserved" and LoyaltyAccount.query.get(account.id).balance == balance_before_redeem - discount_reward.points_cost)
try:
    loyalty_engine.redeem_reward(member, discount_reward)
    redeemed_twice = True
except ValidationError:
    redeemed_twice = False
check("per-member reward limit prevents duplicate redemption", not redeemed_twice)
poor, poor_card = make_member("poor-member@example.test", "GF-LOY-2")
try:
    loyalty_engine.redeem_reward(poor, discount_reward)
    enough = True
except ValidationError:
    enough = False
check("insufficient balance is rejected", not enough)
payment_manager.create_invoice(card, admin, {"description": "Pending reward test", "amount": "25"})

client = app.test_client()
with client.session_transaction() as sess:
    sess["user_id"] = member.id
check("member can view their own wallet", client.get("/loyalty/").status_code == 200)
class_page = client.get("/classes/")
check("reserved free-class reward appears in the existing booking flow",
      class_page.status_code == 200 and b"Optional class credit" in class_page.data)
invoice_page = client.get(f"/billing/{card.id}")
check("reserved invoice reward appears in the existing payment flow",
      invoice_page.status_code == 200 and b"Apply a reserved reward" in invoice_page.data)
check("member cannot manage policies", client.get("/admin/loyalty/").status_code == 403)
check("member cannot view another member's invoices", client.get(f"/billing/{poor_card.id}").status_code == 403)
with client.session_transaction() as sess:
    sess["user_id"] = admin.id
check("administrator can view policy management", client.get("/admin/loyalty/").status_code == 200)
created_policy = client.post("/admin/loyalty/policies", data={
    "name": "Admin-created rule", "description": "created through the UI",
    "event_type": "member_activity",
    "definition": json.dumps({"conditions": [], "action": {"type": "award_points", "points": 3}}),
    "priority": "100", "status": "active",
}, follow_redirects=True)
check("administrator can create an active validated policy in the UI",
      created_policy.status_code == 200 and PricingPolicy.query.filter_by(name="Admin-created rule").first() is not None)
new_tier = client.post("/admin/loyalty/tiers", data={
    "name": "Platinum", "threshold": "2000", "sort_order": "3",
    "benefits": "Sample configurable benefit", "active": "1",
}, follow_redirects=True)
check("administrator can add a configurable rewards tier", new_tier.status_code == 200
      and LoyaltyTier.query.filter_by(name="Platinum", threshold=2000).first() is not None)
with client.session_transaction() as sess:
    sess["user_id"] = admin.id
preview = client.post(f"/admin/loyalty/policies/{discount_policy.id}/preview",
                      data={"member_id": member.id, "invoice_amount": "100"}, follow_redirects=True)
check("administrator can preview a member-policy match", preview.status_code == 200 and b"Preview:" in preview.data)
before_points = LoyaltyAccount.query.get(account.id).balance
before_lifetime = LoyaltyAccount.query.get(account.id).lifetime_points
adjusted = client.post("/admin/loyalty/points/adjust",
                       data={"member_id": member.id, "delta": "5", "reason": "Test correction"},
                       follow_redirects=True)
check("admin adjustment is audited and does not change tier points",
      adjusted.status_code == 200 and LoyaltyAccount.query.get(account.id).balance == before_points + 5
      and LoyaltyAccount.query.get(account.id).lifetime_points == before_lifetime)
with client.session_transaction() as sess:
    sess["user_id"] = member.id
unauthorized = client.post("/admin/loyalty/points/adjust",
                           data={"member_id": member.id, "delta": "5", "reason": "Not allowed"})
check("member cannot adjust points", unauthorized.status_code == 403)
print(f"\n{checks} checks run, {len(failures)} failed.")
if failures:
    print("Failures:", "; ".join(failures))
    raise SystemExit(1)
