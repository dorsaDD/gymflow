"""Restricted, data-driven rewards and promotion policies.

Policy definitions are JSON data, never executable source. See
docs/PRICING_AND_LOYALTY.md for the supported facts and operators.
"""
import json
import math
import uuid
from datetime import datetime, timedelta

from gymflow.card_manager import ValidationError
from gymflow.extensions import db
from gymflow.models import (
    Booking, Card, Invoice, LoyaltyAccount, LoyaltyTier, PointsLedgerEntry,
    PolicyApplication, PricingPolicy, Reward, RewardRedemption, User,
)

EVENTS = {"class_attended", "invoice_paid", "member_activity"}
FACT_TYPES = {
    "member.age": "number",
    "member.tier": "string",
    "member.active_days": "number",
    "member.attended_classes": "number",
    "member.attended_classes_month": "number",
    "member.attended_classes_30d": "number",
    "member.spending_total": "number",
    "member.spending_total_including_invoice": "number",
    "member.purchase_count": "number",
    "member.days_since_first_purchase": "number",
    "invoice.amount": "number",
}
OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "in"}
ACTION_TYPES = {"award_points", "discount_percent", "discount_fixed", "grant_reward"}
MAX_DISCOUNT_PERCENT = 50.0


def _finite_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def validate_definition(event_type, raw):
    """Parse and validate the deliberately small policy document format."""
    if not isinstance(event_type, str) or event_type not in EVENTS:
        raise ValidationError("Choose class_attended, invoice_paid, or member_activity.")
    try:
        definition = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        raise ValidationError("Policy rules must be valid JSON.")
    if not isinstance(definition, dict) or set(definition) != {"conditions", "action"}:
        raise ValidationError("A rule must contain only 'conditions' and 'action'.")
    conditions = definition.get("conditions")
    action = definition.get("action")
    if not isinstance(conditions, list) or len(conditions) > 8:
        raise ValidationError("Conditions must be a list of at most 8 checks.")
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) != {"fact", "op", "value"}:
            raise ValidationError("Each condition needs exactly fact, op, and value.")
        fact, op, value = condition["fact"], condition["op"], condition["value"]
        if not isinstance(fact, str) or fact not in FACT_TYPES:
            raise ValidationError(f"'{fact}' is not an available GymFlow policy fact.")
        if fact == "invoice.amount" and event_type != "invoice_paid":
            raise ValidationError("invoice.amount is only available for the invoice_paid event.")
        if not isinstance(op, str) or op not in OPERATORS:
            raise ValidationError(f"'{op}' is not a supported comparison operator.")
        if op == "in":
            if not isinstance(value, list) or not value or len(value) > 20:
                raise ValidationError("The 'in' operator needs a non-empty list of up to 20 values.")
            expected = (int, float) if FACT_TYPES[fact] == "number" else (str,)
            if any(isinstance(item, bool) or not isinstance(item, expected) for item in value):
                raise ValidationError(f"Values in the '{fact}' list must match that fact's type.")
            if FACT_TYPES[fact] == "number" and any(not _finite_number(item) for item in value):
                raise ValidationError(f"Values in the '{fact}' list must be finite numbers.")
        elif FACT_TYPES[fact] == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"'{fact}' needs a numeric value.")
            if not _finite_number(value):
                raise ValidationError(f"'{fact}' needs a finite numeric value.")
        elif not isinstance(value, str) or len(value) > 80:
            raise ValidationError(f"'{fact}' needs a short text value.")
        if op in {"gt", "gte", "lt", "lte"} and FACT_TYPES[fact] != "number":
            raise ValidationError("Greater-than and less-than comparisons require a numeric fact.")
    if not isinstance(action, dict) or "type" not in action:
        raise ValidationError("Choose a supported action.")
    kind = action.get("type")
    if not isinstance(kind, str) or kind not in ACTION_TYPES:
        raise ValidationError("Supported actions are award_points, discount_percent, and discount_fixed.")
    required = {"award_points": {"type", "points"},
                "discount_percent": {"type", "percent"},
                "discount_fixed": {"type", "amount"},
                "grant_reward": {"type", "reward_id"}}[kind]
    if set(action) != required:
        raise ValidationError(f"The {kind} action has missing or unsupported fields.")
    if kind == "grant_reward":
        reward_id = action.get("reward_id")
        if isinstance(reward_id, bool) or not isinstance(reward_id, int) or reward_id <= 0:
            raise ValidationError("grant_reward needs a positive reward_id from the Rewards list.")
        reward = Reward.query.get(reward_id)
        if not reward:
            raise ValidationError("grant_reward refers to a reward that does not exist.")
        return definition
    value_key = {"award_points": "points", "discount_percent": "percent", "discount_fixed": "amount"}[kind]
    value = action[value_key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite_number(value) or value <= 0:
        raise ValidationError(f"{value_key.capitalize()} must be a positive number.")
    if kind == "award_points" and (int(value) != value or value > 100000):
        raise ValidationError("Points must be a whole number between 1 and 100,000.")
    if kind == "discount_percent" and value > MAX_DISCOUNT_PERCENT:
        raise ValidationError(f"Percentage discounts cannot exceed {MAX_DISCOUNT_PERCENT:g}%.")
    if kind == "discount_fixed" and value > 100000:
        raise ValidationError("Fixed discount is above the supported limit.")
    if kind == "discount_fixed" and event_type != "invoice_paid":
        raise ValidationError("Fixed discounts can only apply to a paid invoice.")
    if kind == "discount_percent" and event_type != "invoice_paid":
        raise ValidationError("Percentage discounts can only apply to a paid invoice.")
    return definition


def _age(card, now):
    if not card or not card.birthdate:
        return None
    return now.year - card.birthdate.year - ((now.month, now.day) < (card.birthdate.month, card.birthdate.day))


def get_account(card, create=True, for_update=False):
    if not card:
        return None
    query = LoyaltyAccount.query.filter_by(card_id=card.id)
    if for_update and hasattr(query, "with_for_update"):
        query = query.with_for_update()
    account = query.first()
    if account is None and create:
        account = LoyaltyAccount(card_id=card.id, balance=0, lifetime_points=0)
        db.session.add(account)
        db.session.flush()
    return account


def current_tier(account):
    tiers = LoyaltyTier.query.filter_by(active=True).all()
    tiers.sort(key=lambda item: item.threshold)
    earned = account.lifetime_points if account else 0
    eligible = [tier for tier in tiers if tier.threshold <= earned]
    return eligible[-1] if eligible else None


def tier_progress(account):
    tiers = LoyaltyTier.query.filter_by(active=True).all()
    tiers.sort(key=lambda item: item.threshold)
    earned = account.lifetime_points if account else 0
    current = current_tier(account)
    next_tier = next((tier for tier in tiers if tier.threshold > earned), None)
    if next_tier is None:
        return {"current": current, "next": None, "remaining": 0, "percent": 100}
    lower = current.threshold if current else 0
    span = max(1, next_tier.threshold - lower)
    progress = max(0, min(span, earned - lower))
    return {"current": current, "next": next_tier,
            "remaining": max(0, next_tier.threshold - earned),
            "percent": int(100 * progress / span)}


def _context(member, event_type, invoice=None, now=None, booking=None):
    now = now or datetime.utcnow()
    card = Card.query.filter_by(user_id=member.id).first()
    account = get_account(card, create=False)
    bookings = Booking.query.filter_by(member_id=member.id).all()
    attended = [booking for booking in bookings if booking.attended is True]
    month_anchor = booking.fitness_class.schedule if booking and booking.fitness_class else now
    month_attended = [item for item in attended
                      if item.fitness_class and item.fitness_class.schedule.year == month_anchor.year
                      and item.fitness_class.schedule.month == month_anchor.month]
    recent_cutoff = now - timedelta(days=30)
    attended_30d = [booking for booking in attended
                    if booking.fitness_class and booking.fitness_class.schedule >= recent_cutoff]
    invoices = Invoice.query.filter_by(card_id=card.id).all() if card else []
    # For invoice_paid, these are prior purchases before the current event;
    # this lets a configured `purchase_count == 1` rule target a second buy.
    paid = [item for item in invoices if item.status == "paid"
            and (invoice is None or item.id != getattr(invoice, "id", None))]
    paid_dates = [item.paid_at for item in paid if item.paid_at]
    first_purchase = min(paid_dates) if paid_dates else None
    facts = {
        "member.age": _age(card, now),
        "member.tier": (current_tier(account).name if current_tier(account) else "Unranked"),
        "member.active_days": max(0, (now - member.created_at).days) if member.created_at else 0,
        "member.attended_classes": len(attended),
        "member.attended_classes_month": len(month_attended),
        "member.attended_classes_30d": len(attended_30d),
        "member.spending_total": round(sum(item.paid_amount if item.paid_amount is not None else item.amount for item in paid), 2),
        "member.spending_total_including_invoice": round(sum(item.paid_amount if item.paid_amount is not None else item.amount for item in paid)
                                                          + ((getattr(invoice, "paid_amount", None) if getattr(invoice, "paid_amount", None) is not None else invoice.amount)
                                                             if event_type == "invoice_paid" and invoice else 0), 2),
        "member.purchase_count": len(paid),
        "member.days_since_first_purchase": max(0, (now - first_purchase).days) if first_purchase else 0,
        "invoice.amount": invoice.amount if invoice else 0,
    }
    return facts


def matches(definition, facts):
    """Evaluate only parsed comparisons and boolean AND; no dynamic code."""
    for condition in definition["conditions"]:
        left, op, right = facts.get(condition["fact"]), condition["op"], condition["value"]
        if left is None:
            return False
        try:
            matched = {
                "eq": lambda: left == right, "ne": lambda: left != right,
                "gt": lambda: left > right, "gte": lambda: left >= right,
                "lt": lambda: left < right, "lte": lambda: left <= right,
                "in": lambda: left in right,
            }[op]()
        except (TypeError, KeyError):
            return False
        if not matched:
            return False
    return True


def validate_policy_fields(data):
    """Validate and normalize common policy form fields."""
    name = (data.get("name") or "").strip()
    if not name or len(name) > 100:
        raise ValidationError("Policy name is required (maximum 100 characters).")
    event_type = data.get("event_type", "")
    definition = validate_definition(event_type, data.get("definition", ""))
    def optional_positive(field, label):
        raw = (data.get(field) or "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            raise ValidationError(f"{label} must be a whole number.")
        if value < 1:
            raise ValidationError(f"{label} must be greater than zero.")
        return value
    try:
        priority = int(data.get("priority") or 100)
    except ValueError:
        raise ValidationError("Priority must be a whole number.")
    return {"name": name, "description": (data.get("description") or "").strip()[:300],
            "event_type": event_type, "definition": json.dumps(definition),
            "priority": priority,
            "max_uses_per_member": optional_positive("max_uses_per_member", "Member limit"),
            "max_uses_total": optional_positive("max_uses_total", "Total limit")}


def _eligible_policies(event_type, now=None):
    now = now or datetime.utcnow()
    query = PricingPolicy.query.filter_by(event_type=event_type, status="active")
    if hasattr(query, "with_for_update"):
        query = query.with_for_update()
    policies = query.all()
    return sorted([p for p in policies
                   if (p.starts_at is None or p.starts_at <= now)
                   and (p.ends_at is None or p.ends_at >= now)], key=lambda p: (p.priority, p.id))


def _within_limits(policy, member):
    applications = PolicyApplication.query.filter_by(policy_id=policy.id).all()
    if policy.max_uses_total is not None and len(applications) >= policy.max_uses_total:
        return False
    if policy.max_uses_per_member is not None and sum(1 for row in applications if row.member_id == member.id) >= policy.max_uses_per_member:
        return False
    return True


def _get_policy_discount(invoice, reward_redemption=None):
    member = invoice.card.owner
    facts = _context(member, "invoice_paid", invoice)
    for policy in _eligible_policies("invoice_paid"):
        if not _within_limits(policy, member) or not matches(json.loads(policy.definition), facts):
            continue
        action = json.loads(policy.definition)["action"]
        base = max(0.0, float(invoice.amount))
        if action["type"] == "discount_percent":
            return round(min(base * action["percent"] / 100, base * MAX_DISCOUNT_PERCENT / 100), 2), policy
        if action["type"] == "discount_fixed":
            return round(min(base * MAX_DISCOUNT_PERCENT / 100, action["amount"]), 2), policy
    # One discount per invoice: the highest-priority matching policy wins;
    # member-selected rewards are used only when no policy discount applies.
    if reward_redemption and reward_redemption.status == "reserved":
        reward = reward_redemption.reward
        if reward.target == "invoice" and reward.reward_type in {"percent_discount", "fixed_discount"}:
            base = max(0.0, float(invoice.amount))
            if reward.reward_type == "percent_discount":
                return round(min(base * reward.value / 100, base * MAX_DISCOUNT_PERCENT / 100), 2), None
            return round(min(base * MAX_DISCOUNT_PERCENT / 100, reward.value), 2), None
    return 0.0, None


def invoice_discount_preview(invoice, redemption=None):
    return _get_policy_discount(invoice, redemption)[0]


def preview_policy(policy, member, invoice_amount=None):
    """Return a non-mutating eligibility preview for an administrator."""
    definition = json.loads(policy.definition)
    pseudo_invoice = None
    if policy.event_type == "invoice_paid" and invoice_amount is not None:
        class PreviewInvoice:
            amount = float(invoice_amount)
        pseudo_invoice = PreviewInvoice()
    facts = _context(member, policy.event_type, pseudo_invoice)
    matched = matches(definition, facts)
    action = definition["action"]
    result = dict(matched=matched, action=action, facts=facts)
    if matched and action["type"] in {"discount_percent", "discount_fixed"}:
        base = max(0.0, float(invoice_amount or 0))
        if action["type"] == "discount_percent":
            result["discount"] = round(min(base * action["percent"] / 100,
                                           base * MAX_DISCOUNT_PERCENT / 100), 2)
        else:
            result["discount"] = round(min(base * MAX_DISCOUNT_PERCENT / 100,
                                           action["amount"]), 2)
    return result


def apply_event(event_type, event_key, member, invoice=None, booking=None):
    """Apply matching point rules once, after the triggering event is valid.

    Invoice-paid processing runs inside payment_manager's final transaction;
    attendance processing runs after staff records an attended booking.
    """
    if event_type not in EVENTS or not member or not member.card:
        return []
    facts = _context(member, event_type, invoice, booking=booking)
    applied = []
    for policy in _eligible_policies(event_type):
        definition = json.loads(policy.definition)
        if not _within_limits(policy, member) or not matches(definition, facts):
            continue
        action = definition["action"]
        if action["type"] not in {"award_points", "grant_reward"}:
            continue
        key = f"policy:{policy.id}:{event_key}"
        if PolicyApplication.query.filter_by(event_key=key).first():
            continue
        account = get_account(member.card, for_update=True)
        points = 0
        if action["type"] == "award_points":
            points = int(action["points"])
            account.balance += points
            account.lifetime_points += points
            account.updated_at = datetime.utcnow()
            db.session.add(PointsLedgerEntry(account_id=account.id, event_key=key, entry_type="earned",
                                            points=points, qualifying_points=points,
                                            note=f"Policy: {policy.name}", policy_id=policy.id))
        else:
            reward_query = Reward.query.filter_by(id=action["reward_id"])
            if hasattr(reward_query, "with_for_update"):
                reward_query = reward_query.with_for_update()
            reward = reward_query.first()
            if not reward or not reward.active or not reward_is_eligible(account, reward):
                continue
            if reward.starts_at and reward.starts_at > datetime.utcnow():
                continue
            if reward.ends_at and reward.ends_at < datetime.utcnow():
                continue
            reward_redemptions = RewardRedemption.query.filter_by(reward_id=reward.id).all()
            if reward.max_redemptions is not None and len(reward_redemptions) >= reward.max_redemptions:
                continue
            if reward.max_per_member is not None and sum(1 for row in reward_redemptions
                                                           if row.account_id == account.id) >= reward.max_per_member:
                continue
            db.session.add(RewardRedemption(account_id=account.id, reward_id=reward.id,
                                            policy_id=policy.id, status="reserved"))
        application = PolicyApplication(policy_id=policy.id, member_id=member.id,
                                        event_key=key, event_type=event_type,
                                        invoice_id=invoice.id if invoice else None,
                                        points_awarded=points)
        db.session.add(application)
        applied.append(application)
    return applied


def apply_invoice_payment(invoice, discount_amount, discount_policy=None, redemption=None, paid_amount=None):
    """Persist discount audit, consume redemption, award event points/reverse on refund separately."""
    member = invoice.card.owner
    invoice.discount_amount = discount_amount
    invoice.paid_amount = paid_amount if paid_amount is not None else max(0.0, invoice.amount - discount_amount)
    if discount_policy:
        key = f"policy:{discount_policy.id}:invoice:{invoice.id}"
        if not PolicyApplication.query.filter_by(event_key=key).first():
            db.session.add(PolicyApplication(policy_id=discount_policy.id, member_id=member.id,
                                            event_key=key, event_type="invoice_paid", invoice_id=invoice.id,
                                            discount_amount=discount_amount))
    if redemption and discount_amount > 0:
        redemption.status = "used"
        redemption.invoice_id = invoice.id
        redemption.used_at = datetime.utcnow()
    apply_event("invoice_paid", f"invoice:{invoice.id}", member, invoice)


def reverse_invoice_points(invoice, refunder):
    """Append a refund reversal; lifetime qualifying points stay unchanged."""
    account = get_account(invoice.card, create=False)
    if not account:
        return
    # Each policy uses its own event key, so locate all source entries by the
    # explicit invoice-paid note/policy applications.
    applications = PolicyApplication.query.filter_by(event_type="invoice_paid", invoice_id=invoice.id).all()
    total = sum(max(0, row.points_awarded) for row in applications)
    if not total:
        return
    reverse_key = f"refund:invoice:{invoice.id}"
    if PointsLedgerEntry.query.filter_by(event_key=reverse_key).first():
        return
    account.balance -= total
    account.updated_at = datetime.utcnow()
    db.session.add(PointsLedgerEntry(account_id=account.id, event_key=reverse_key,
                                    entry_type="reversed", points=-total,
                                    qualifying_points=0, note=f"Refund reversal for invoice #{invoice.id}",
                                    created_by_id=refunder.id))


def reverse_attendance_points(booking):
    """Reverse point awards if staff corrects a recorded attendance to no-show."""
    if not booking.member or not booking.member.card:
        return
    account = get_account(booking.member.card, create=False, for_update=True)
    if not account:
        return
    event_key = f"attendance:{booking.id}"
    applications = PolicyApplication.query.filter_by(event_type="class_attended", member_id=booking.member_id).all()
    total = 0
    for application in applications:
        if application.event_key.endswith(event_key):
            total += max(0, application.points_awarded)
    key = f"reverse:{event_key}"
    if not total or PointsLedgerEntry.query.filter_by(event_key=key).first():
        return
    account.balance -= total
    account.updated_at = datetime.utcnow()
    db.session.add(PointsLedgerEntry(account_id=account.id, event_key=key,
                                    entry_type="reversed", points=-total,
                                    qualifying_points=0, note=f"Attendance correction for booking #{booking.id}"))


def redeem_reward(member, reward):
    if member.role != "member" or not member.card:
        raise ValidationError("Only members with a membership card can redeem rewards.")
    reward_query = Reward.query.filter_by(id=reward.id)
    if hasattr(reward_query, "with_for_update"):
        reward_query = reward_query.with_for_update()
    reward = reward_query.first() or reward
    now = datetime.utcnow()
    if not reward.active or (reward.starts_at and reward.starts_at > now) or (reward.ends_at and reward.ends_at < now):
        raise ValidationError("This reward is not currently available.")
    if reward.reward_type not in {"percent_discount", "fixed_discount", "free_class"}:
        raise ValidationError("This reward type is not supported.")
    if reward.points_cost <= 0:
        raise ValidationError("Reward points cost must be positive.")
    account = get_account(member.card, for_update=True)
    if not reward_is_eligible(account, reward):
        raise ValidationError("Your current rewards tier does not meet this reward's eligibility requirement.")
    # Acquire a database write lock before reading redemption counts. Row locks
    # are honored by PostgreSQL, while SQLite ignores FOR UPDATE. A conditional
    # decrement provides a portable balance guard and serializes SQLite writers.
    # If any later validation fails, the surrounding transaction rolls it back.
    debit_query = LoyaltyAccount.query.filter(
        LoyaltyAccount.id == account.id,
        LoyaltyAccount.balance >= reward.points_cost,
    )
    if hasattr(debit_query, "update"):
        debit = debit_query.update({
            LoyaltyAccount.balance: LoyaltyAccount.balance - reward.points_cost,
            LoyaltyAccount.updated_at: now,
        }, synchronize_session=False)
    else:  # Lightweight legacy test query shim; production uses SQLAlchemy UPDATE.
        debit = int(account.balance >= reward.points_cost)
        if debit:
            account.balance -= reward.points_cost
            account.updated_at = now
    if not debit:
        raise ValidationError("You do not have enough spendable points for this reward.")
    existing = RewardRedemption.query.filter_by(account_id=account.id, reward_id=reward.id).all()
    if reward.max_redemptions is not None and len(existing) >= reward.max_redemptions:
        raise ValidationError("This reward has reached its redemption limit.")
    if reward.max_per_member is not None and len(existing) >= reward.max_per_member:
        raise ValidationError("You have already reached your personal redemption limit for this reward.")
    key = f"redeem:{account.id}:{reward.id}:{len(existing) + 1}"
    redemption = RewardRedemption(account_id=account.id, reward_id=reward.id, status="reserved")
    db.session.add(redemption)
    db.session.add(PointsLedgerEntry(account_id=account.id, event_key=key,
                                    entry_type="redeemed", points=-reward.points_cost,
                                    qualifying_points=0, note=f"Redeemed: {reward.name}"))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise ValidationError("The reward could not be reserved. Refresh your balance and try again.")
    return redemption


def adjust_points(member, delta, reason, administrator):
    """Administrator-only signed wallet correction; never changes lifetime tier points."""
    if member.role != "member" or not member.card:
        raise ValidationError("Choose a member account with a membership card.")
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        raise ValidationError("Point adjustment must be a whole number.")
    reason = (reason or "").strip()
    if delta == 0 or abs(delta) > 100000 or not reason or len(reason) > 240:
        raise ValidationError("Enter a non-zero adjustment up to 100,000 points and a reason.")
    account = get_account(member.card, for_update=True)
    if account.balance + delta < 0:
        raise ValidationError("The adjustment cannot reduce spendable points below zero.")
    account.balance += delta
    account.updated_at = datetime.utcnow()
    db.session.add(PointsLedgerEntry(account_id=account.id, event_key=f"admin-adjust:{uuid.uuid4().hex}",
                                    entry_type="adjusted", points=delta, qualifying_points=0,
                                    note=reason, created_by_id=administrator.id))
    db.session.commit()
    return account


def validate_redemption_for_member(redemption, member, target):
    if not redemption or not member.card or redemption.account.card_id != member.card.id:
        raise ValidationError("That reward redemption does not belong to your account.")
    if redemption.status != "reserved" or redemption.reward.target != target:
        raise ValidationError("That reward is not available for this checkout.")
    if not redemption.reward.active or not reward_is_eligible(redemption.account, redemption.reward):
        raise ValidationError("That reward is no longer active or eligible for your tier.")
    if redemption.reward.starts_at and redemption.reward.starts_at > datetime.utcnow():
        raise ValidationError("That reward is not active yet.")
    if redemption.reward.ends_at and redemption.reward.ends_at < datetime.utcnow():
        raise ValidationError("That reward has expired.")
    return redemption


def validate_reward(data):
    name = (data.get("name") or "").strip()
    kind = data.get("reward_type")
    target = data.get("target") or "invoice"
    try:
        points = int(data.get("points_cost") or "")
    except ValueError:
        raise ValidationError("Point cost must be a whole number.")
    if not name or points <= 0:
        raise ValidationError("Reward name and a positive point cost are required.")
    if kind not in {"percent_discount", "fixed_discount", "free_class"}:
        raise ValidationError("Choose a supported reward type.")
    if target != ("class_booking" if kind == "free_class" else "invoice"):
        raise ValidationError("Discounts apply to invoices and free-class credits apply to bookings.")
    value = None
    if kind in {"percent_discount", "fixed_discount"}:
        try:
            value = round(float(data.get("value") or 0), 2)
        except ValueError:
            raise ValidationError("Discount value must be numeric.")
        if not math.isfinite(value) or value <= 0 or (kind == "percent_discount" and value > MAX_DISCOUNT_PERCENT):
            raise ValidationError("Discount must be positive; percentage discounts are capped at 50%.")
        if kind == "fixed_discount" and value > 100000:
            raise ValidationError("Fixed discount is above the supported limit.")
    try:
        limit = int(data.get("max_redemptions") or "") if data.get("max_redemptions") else None
    except ValueError:
        raise ValidationError("Redemption limit must be a whole number.")
    if limit is not None and limit < 1:
        raise ValidationError("Redemption limit must be greater than zero.")
    try:
        member_limit = int(data.get("max_per_member") or "") if data.get("max_per_member") else None
    except ValueError:
        raise ValidationError("Per-member redemption limit must be a whole number.")
    if member_limit is not None and member_limit < 1:
        raise ValidationError("Per-member redemption limit must be greater than zero.")
    minimum_tier = (data.get("minimum_tier") or "").strip() or None
    if minimum_tier and not any(item.name == minimum_tier and item.active for item in LoyaltyTier.query.all()):
        raise ValidationError("Choose an active rewards tier for the minimum tier requirement.")
    return {"name": name, "description": (data.get("description") or "").strip()[:300],
            "reward_type": kind, "points_cost": points, "value": value,
            "target": target, "max_redemptions": limit, "max_per_member": member_limit,
            "minimum_tier": minimum_tier}


def reward_is_eligible(account, reward):
    if not reward.minimum_tier:
        return True
    current = current_tier(account)
    tiers = LoyaltyTier.query.filter_by(active=True).all()
    required = next((tier for tier in tiers if tier.name == reward.minimum_tier), None)
    return bool(current and required and current.threshold >= required.threshold)


