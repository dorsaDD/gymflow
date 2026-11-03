"""Restricted, data-driven rewards and promotion policies.

Policy definitions are JSON data, never executable source. Administrators can
write a rule either as that JSON or in a small readable rule language, e.g.

    WHEN event == "class_attended"
    AND member.tier == "Gold"
    AND (class.category == "yoga" OR class.category == "mobility")
    THEN award_points(20)

The text is tokenized and parsed by the hand-written parser below into the
same JSON structure; nothing is ever passed to eval()/exec(). See
docs/PRICING_AND_LOYALTY.md for the supported facts, operators and actions.
"""
import json
import math
import re
import uuid
from datetime import datetime, timedelta

from gymflow.card_manager import ValidationError
from gymflow.extensions import db
from gymflow.models import (
    CLASS_CATEGORIES, INVOICE_CATEGORIES,
    Booking, Card, Invoice, LoyaltyAccount, LoyaltyTier, PointsLedgerEntry,
    PolicyApplication, PricingPolicy, Reward, RewardRedemption, User,
)

EVENTS = {"class_attended", "invoice_paid", "member_activity"}
EVENT_LABELS = {
    "class_attended": "When staff records that you attended a class",
    "invoice_paid": "When you pay an invoice",
    "member_activity": "Checked once a month when you open your dashboard",
}
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
    "member.membership_purchases": "number",
    "member.days_since_first_purchase": "number",
    "member.days_since_last_purchase": "number",
    "invoice.amount": "number",
    "invoice.category": "string",
    "class.category": "string",
    "member.segment": "string",
}
# Facts that only exist for one trigger event.
FACT_EVENTS = {
    "invoice.amount": {"invoice_paid"},
    "invoice.category": {"invoice_paid"},
    "class.category": {"class_attended"},
}
# String facts whose values must come from a fixed list.
from gymflow.segmentation import SEGMENTS as _SEGMENTS  # noqa: E402

FACT_CHOICES = {
    "invoice.category": set(INVOICE_CATEGORIES),
    "class.category": set(CLASS_CATEGORIES),
    "member.segment": set(_SEGMENTS),
}
FACT_LABELS = {
    "member.age": "your age",
    "member.tier": "your rewards tier",
    "member.active_days": "days since you joined",
    "member.attended_classes": "classes you attended",
    "member.attended_classes_month": "classes attended this month",
    "member.attended_classes_30d": "classes attended in the last 30 days",
    "member.spending_total": "your total paid spending ($)",
    "member.spending_total_including_invoice": "your total spending including this invoice ($)",
    "member.purchase_count": "your previous paid purchases",
    "member.membership_purchases": "your previous membership purchases",
    "member.days_since_first_purchase": "days since your first purchase",
    "member.days_since_last_purchase": "days since your last purchase",
    "invoice.amount": "the invoice amount ($)",
    "invoice.category": "what the invoice is for",
    "class.category": "the class category",
    "member.segment": "your member group (from the K-Means segmentation)",
}
OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "in"}
OP_SYMBOLS = {"eq": "==", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "in": "IN"}
OP_WORDS = {"eq": "is", "ne": "is not", "gt": "is more than", "gte": "is at least",
            "lt": "is less than", "lte": "is at most", "in": "is one of"}
ACTION_TYPES = {"award_points", "discount_percent", "discount_fixed", "grant_reward"}
MAX_DISCOUNT_PERCENT = 50.0
MAX_CONDITIONS = 8


def _finite_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _validate_condition(event_type, condition):
    if not isinstance(condition, dict) or set(condition) != {"fact", "op", "value"}:
        raise ValidationError("Each condition needs exactly fact, op, and value.")
    fact, op, value = condition["fact"], condition["op"], condition["value"]
    if not isinstance(fact, str) or fact not in FACT_TYPES:
        raise ValidationError(f"'{fact}' is not an available GymFlow policy fact.")
    if fact in FACT_EVENTS and event_type not in FACT_EVENTS[fact]:
        allowed = ", ".join(sorted(FACT_EVENTS[fact]))
        raise ValidationError(f"{fact} is only available for the {allowed} trigger.")
    if not isinstance(op, str) or op not in OPERATORS:
        raise ValidationError(f"'{op}' is not a supported comparison operator.")
    kind = FACT_TYPES[fact]
    values = value if op == "in" else [value]
    if op == "in" and (not isinstance(value, list) or not value or len(value) > 20):
        raise ValidationError("The 'in' operator needs a non-empty list of up to 20 values.")
    for item in values:
        if kind == "number":
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not _finite_number(item):
                raise ValidationError(f"'{fact}' needs a finite numeric value.")
        else:
            if not isinstance(item, str) or not item or len(item) > 80:
                raise ValidationError(f"'{fact}' needs a short text value.")
            if fact in FACT_CHOICES and item not in FACT_CHOICES[fact]:
                choices = ", ".join(sorted(FACT_CHOICES[fact]))
                raise ValidationError(f"'{item}' is not a valid value for {fact}. Use one of: {choices}.")
    if op in {"gt", "gte", "lt", "lte"} and kind != "number":
        raise ValidationError("Greater-than and less-than comparisons require a numeric fact.")


def validate_definition(event_type, raw):
    """Parse and validate the deliberately small policy document format.

    conditions: list (AND) of comparisons, or {"any": [comparisons]} groups
    (OR inside the group). One level of grouping only.
    """
    if not isinstance(event_type, str) or event_type not in EVENTS:
        raise ValidationError("Choose class_attended, invoice_paid, or member_activity.")
    try:
        definition = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        raise ValidationError("Policy rules must be valid JSON or use the WHEN ... THEN ... rule format.")
    if not isinstance(definition, dict) or set(definition) != {"conditions", "action"}:
        raise ValidationError("A rule must contain only 'conditions' and 'action'.")
    conditions = definition.get("conditions")
    action = definition.get("action")
    if not isinstance(conditions, list) or len(conditions) > MAX_CONDITIONS:
        raise ValidationError(f"Conditions must be a list of at most {MAX_CONDITIONS} checks.")
    for condition in conditions:
        if isinstance(condition, dict) and set(condition) == {"any"}:
            group = condition["any"]
            if not isinstance(group, list) or not 2 <= len(group) <= MAX_CONDITIONS:
                raise ValidationError(f"An OR group needs between 2 and {MAX_CONDITIONS} comparisons.")
            for item in group:
                _validate_condition(event_type, item)
        else:
            _validate_condition(event_type, condition)
    if not isinstance(action, dict) or "type" not in action:
        raise ValidationError("Choose a supported action.")
    kind = action.get("type")
    if not isinstance(kind, str) or kind not in ACTION_TYPES:
        raise ValidationError("Supported actions are award_points, discount_percent, discount_fixed, and grant_reward.")
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
    if kind in {"discount_fixed", "discount_percent"} and event_type != "invoice_paid":
        raise ValidationError("Discounts can only apply to the invoice_paid trigger (a payment).")
    return definition


# ---------------------------------------------------------------------------
# Readable rule language: WHEN <condition> AND <condition> THEN <action>
# A tiny hand-written tokenizer + recursive-descent parser. It only produces
# the JSON structure above, which validate_definition() then checks. No
# user text is ever evaluated as code.
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>-?\d+(?:\.\d+)?)
  | (?P<str>"[^"\\\n]*"|'[^'\\\n]*')
  | (?P<op>==|!=|>=|<=|>|<)
  | (?P<punct>[()\[\],=])
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)
""", re.VERBOSE)
_KEYWORDS = {"WHEN", "IF", "AND", "OR", "THEN", "IN"}
_SYMBOL_OPS = {"==": "eq", "!=": "ne", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}
MAX_RULE_TEXT = 2000


def _tokenize(text):
    tokens, pos = [], 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if not match:
            raise ValidationError(f"Unexpected character '{text[pos]}' in the rule (position {pos + 1}).")
        pos = match.end()
        kind = match.lastgroup
        value = match.group()
        if kind == "ws":
            continue
        if kind == "num":
            value = float(value) if "." in value else int(value)
        elif kind == "str":
            value = value[1:-1]
        elif kind == "word" and value.upper() in _KEYWORDS:
            kind, value = "kw", value.upper()
        tokens.append((kind, value))
    return tokens


class _RuleParser:
    def __init__(self, text, event_type):
        if len(text) > MAX_RULE_TEXT:
            raise ValidationError("The rule is too long.")
        self.tokens = _tokenize(text)
        self.pos = 0
        self.event_type = event_type

    def peek(self, offset=0):
        index = self.pos + offset
        return self.tokens[index] if index < len(self.tokens) else (None, None)

    def take(self, kind=None, value=None, message=None):
        token = self.peek()
        if token[0] is None or (kind and token[0] != kind) or (value is not None and token[1] != value):
            found = "the end of the rule" if token[0] is None else f"'{token[1]}'"
            raise ValidationError(message or f"Expected {value or kind} but found {found}.")
        self.pos += 1
        return token

    def at(self, kind, value=None):
        token = self.peek()
        return token[0] == kind and (value is None or token[1] == value)

    def parse(self):
        conditions = []
        if self.at("kw", "WHEN") or self.at("kw", "IF"):
            self.pos += 1
            self.parse_term(conditions)
            while self.at("kw", "AND"):
                self.pos += 1
                self.parse_term(conditions)
        if self.at("kw", "THEN"):
            self.pos += 1
        elif conditions:
            self.take("kw", "THEN", "Expected THEN before the action (e.g. THEN award_points(10)).")
        action, extra = self.parse_action()
        if self.peek()[0] is not None:
            raise ValidationError(f"Unexpected '{self.peek()[1]}' after the action.")
        conditions.extend(extra)
        return {"conditions": conditions, "action": action}

    def parse_term(self, conditions):
        if self.at("punct", "("):
            self.pos += 1
            group = [self.parse_comparison()]
            while self.at("kw", "OR"):
                self.pos += 1
                group.append(self.parse_comparison())
            self.take("punct", ")", "Missing ')' to close the OR group.")
            group = [item for item in group if item is not None]
            if len(group) == 1:
                conditions.append(group[0])
            elif group:
                conditions.append({"any": group})
            return
        if self.at("kw", "OR"):
            raise ValidationError("Put OR choices inside brackets, e.g. (member.tier == \"Gold\" OR member.tier == \"Silver\").")
        condition = self.parse_comparison()
        if condition is not None:
            conditions.append(condition)

    def parse_literal(self):
        kind, value = self.peek()
        if kind in {"num", "str"}:
            self.pos += 1
            return value
        if kind == "word":  # allow bare words for simple values, e.g. Gold, yoga
            self.pos += 1
            return value
        raise ValidationError("Expected a number or a quoted text value.")

    def parse_comparison(self):
        _, fact = self.take("word", message="Expected a fact name such as member.tier.")
        if self.at("kw", "IN"):
            self.pos += 1
            self.take("punct", "[", "Expected '[' after IN.")
            values = [self.parse_literal()]
            while self.at("punct", ","):
                self.pos += 1
                values.append(self.parse_literal())
            self.take("punct", "]", "Expected ']' to close the IN list.")
            op, value = "in", values
        else:
            _, symbol = self.take("op", message=f"Expected a comparison (==, !=, >, >=, <, <=) after {fact}.")
            op, value = _SYMBOL_OPS[symbol], self.parse_literal()
        if fact == "event":
            if op != "eq" or value != self.event_type:
                raise ValidationError(f"The rule says event {value!r} but the selected trigger is {self.event_type!r}.")
            return None
        return {"fact": fact, "op": op, "value": value}

    def parse_action(self):
        _, name = self.take("word", message="Expected an action such as award_points(10).")
        self.take("punct", "(", f"Expected '(' after {name}.")
        args, kwargs = [], {}
        while not self.at("punct", ")"):
            if self.peek(0)[0] == "word" and self.peek(1) == ("punct", "="):
                key = self.take("word")[1]
                self.pos += 1
                kwargs[key] = self.parse_literal()
            else:
                args.append(self.parse_literal())
            if not self.at("punct", ","):
                break
            self.pos += 1
        self.take("punct", ")", f"Missing ')' after the {name} arguments.")
        return _build_action(name, args, kwargs)


def _single(name, args, kwargs, key):
    if args and kwargs.get(key) is not None:
        raise ValidationError(f"{name}: give the value once.")
    if len(args) > 1 or set(kwargs) - {key}:
        raise ValidationError(f"{name} takes one value ({key}).")
    value = args[0] if args else kwargs.get(key)
    if value is None:
        raise ValidationError(f"{name} needs a value, e.g. {name}(10).")
    return value


def _build_action(name, args, kwargs):
    """Map the readable action names to the JSON action types."""
    extra = []
    if name == "award_points":
        return {"type": "award_points", "points": _single(name, args, kwargs, "points")}, extra
    if name == "discount_percent":
        return {"type": "discount_percent", "percent": _single(name, args, kwargs, "percent")}, extra
    if name == "discount_fixed":
        return {"type": "discount_fixed", "amount": _single(name, args, kwargs, "amount")}, extra
    if name in {"grant_reward", "grant_free_class"}:
        return {"type": "grant_reward", "reward_id": _single(name, args, kwargs, "reward_id")}, extra
    if name == "apply_discount":
        if args or set(kwargs) - {"percent", "amount", "target"} or ("percent" in kwargs) == ("amount" in kwargs):
            raise ValidationError("apply_discount needs percent=... or amount=..., and optionally target=\"membership\".")
        target = kwargs.get("target")
        if target is not None:
            if target not in INVOICE_CATEGORIES and target != "invoice":
                raise ValidationError(f"apply_discount target must be one of: invoice, {', '.join(INVOICE_CATEGORIES)}.")
            if target != "invoice":
                extra.append({"fact": "invoice.category", "op": "eq", "value": target})
        if "percent" in kwargs:
            return {"type": "discount_percent", "percent": kwargs["percent"]}, extra
        return {"type": "discount_fixed", "amount": kwargs["amount"]}, extra
    raise ValidationError(f"'{name}' is not a supported action. Use award_points, apply_discount, "
                          "discount_percent, discount_fixed, or grant_reward.")


def parse_rule_text(text, event_type):
    """Readable rule text -> validated JSON definition."""
    if event_type not in EVENTS:
        raise ValidationError("Choose class_attended, invoice_paid, or member_activity.")
    definition = _RuleParser(text or "", event_type).parse()
    return validate_definition(event_type, definition)


def parse_definition_input(raw, event_type):
    """Accept either the JSON form or the readable WHEN ... THEN ... form."""
    raw = (raw or "").strip()
    if not raw:
        raise ValidationError("Write a rule, e.g. THEN award_points(10).")
    if raw.startswith("{"):
        return validate_definition(event_type, raw)
    return parse_rule_text(raw, event_type)


def _literal_text(value):
    if isinstance(value, list):
        return "[" + ", ".join(_literal_text(item) for item in value) + "]"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _comparison_text(condition):
    op = condition["op"]
    return f"{condition['fact']} {OP_SYMBOLS[op]} {_literal_text(condition['value'])}"


def rule_to_text(event_type, definition):
    """JSON definition -> readable rule text (round-trips through the parser)."""
    if isinstance(definition, str):
        definition = json.loads(definition)
    lines = [f"WHEN event == {json.dumps(event_type)}"]
    for condition in definition["conditions"]:
        if "any" in condition:
            text = "(" + " OR ".join(_comparison_text(item) for item in condition["any"]) + ")"
        else:
            text = _comparison_text(condition)
        lines.append(f"AND {text}")
    action = definition["action"]
    value = {"award_points": "points", "discount_percent": "percent",
             "discount_fixed": "amount", "grant_reward": "reward_id"}[action["type"]]
    lines.append(f"THEN {action['type']}({_literal_text(action[value])})")
    return "\n".join(lines)


def _condition_words(condition):
    value = condition["value"]
    shown = ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
    if isinstance(value, float) and value.is_integer():
        shown = str(int(value))
    return f"{FACT_LABELS.get(condition['fact'], condition['fact'])} {OP_WORDS[condition['op']]} {shown}"


def describe_action(action):
    kind = action["type"]
    if kind == "award_points":
        return f"earn {action['points']} points"
    if kind == "discount_percent":
        return f"get {action['percent']:g}% off the invoice"
    if kind == "discount_fixed":
        return f"get ${action['amount']:.2f} off the invoice"
    reward = Reward.query.get(action.get("reward_id"))
    return f"receive '{reward.name}'" if reward else "receive a reward"


def describe_conditions(definition):
    parts = []
    for condition in definition["conditions"]:
        if "any" in condition:
            parts.append("(" + " or ".join(_condition_words(item) for item in condition["any"]) + ")")
        else:
            parts.append(_condition_words(condition))
    return parts


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


def _uses_segment(policies):
    return any("member.segment" in (p.definition or "") for p in policies)


def _context(member, event_type, invoice=None, now=None, booking=None, include_segment=True):
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
    last_purchase = max(paid_dates) if paid_dates else None

    def paid_value(item):
        return item.paid_amount if item.paid_amount is not None else item.amount

    spending = round(sum(paid_value(item) for item in paid), 2)
    current_invoice_value = 0
    if event_type == "invoice_paid" and invoice is not None:
        current = getattr(invoice, "paid_amount", None)
        current_invoice_value = current if current is not None else invoice.amount
    tier = current_tier(account)
    facts = {
        "member.age": _age(card, now),
        "member.tier": tier.name if tier else "Unranked",
        "member.active_days": max(0, (now - member.created_at).days) if member.created_at else 0,
        "member.attended_classes": len(attended),
        "member.attended_classes_month": len(month_attended),
        "member.attended_classes_30d": len(attended_30d),
        "member.spending_total": spending,
        "member.spending_total_including_invoice": round(spending + current_invoice_value, 2),
        "member.purchase_count": len(paid),
        "member.membership_purchases": sum(1 for item in paid if (item.category or "other") == "membership"),
        "member.days_since_first_purchase": max(0, (now - first_purchase).days) if first_purchase else 0,
        "member.days_since_last_purchase": max(0, (now - last_purchase).days) if last_purchase else None,
        "invoice.amount": invoice.amount if invoice is not None else None,
        "invoice.category": (getattr(invoice, "category", None) or "other") if invoice is not None else None,
        "class.category": ((booking.fitness_class.category or "general")
                           if booking is not None and booking.fitness_class else None),
        "member.segment": None,
    }
    if include_segment:
        from gymflow.segmentation import segment_of
        facts["member.segment"] = segment_of(member)
    return facts


def _compare(condition, facts):
    """Returns True/False, or None when the fact is unknown in this context."""
    left, op, right = facts.get(condition["fact"]), condition["op"], condition["value"]
    if left is None:
        return None
    try:
        return {
            "eq": lambda: left == right, "ne": lambda: left != right,
            "gt": lambda: left > right, "gte": lambda: left >= right,
            "lt": lambda: left < right, "lte": lambda: left <= right,
            "in": lambda: left in right,
        }[op]()
    except (TypeError, KeyError):
        return False


def _evaluate(definition, facts):
    """Three-valued AND/OR evaluation: True, False, or None (unknown)."""
    unknown = False
    for condition in definition["conditions"]:
        if "any" in condition:
            results = [_compare(item, facts) for item in condition["any"]]
            result = True if any(r is True for r in results) else (None if None in results else False)
        else:
            result = _compare(condition, facts)
        if result is False:
            return False
        if result is None:
            unknown = True
    return None if unknown else True


def matches(definition, facts):
    """Evaluate only parsed comparisons with AND / OR-groups; no dynamic code.
    An unknown fact never matches."""
    return _evaluate(definition, facts) is True


def validate_policy_fields(data):
    """Validate and normalize common policy form fields."""
    name = (data.get("name") or "").strip()
    if not name or len(name) > 100:
        raise ValidationError("Policy name is required (maximum 100 characters).")
    event_type = data.get("event_type", "")
    if event_type not in EVENTS:
        raise ValidationError("Choose a trigger event.")
    definition = parse_definition_input(data.get("definition", ""), event_type)
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
    policies = _eligible_policies("invoice_paid")
    facts = _context(member, "invoice_paid", invoice, include_segment=_uses_segment(policies))
    for policy in policies:
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


def preview_policy(policy, member, invoice_amount=None, invoice_category=None, class_category=None):
    """Return a non-mutating eligibility preview for an administrator.

    For invoice_paid the admin supplies a sample amount/category; for
    class_attended a sample class category. Nothing is written."""
    definition = json.loads(policy.definition)
    pseudo_invoice = pseudo_booking = None
    if policy.event_type == "invoice_paid":
        class PreviewInvoice:
            id = None
            amount = float(invoice_amount) if invoice_amount is not None else 0.0
            paid_amount = None
            category = invoice_category if invoice_category in INVOICE_CATEGORIES else "other"
        pseudo_invoice = PreviewInvoice()
    if policy.event_type == "class_attended":
        class PreviewClass:
            schedule = datetime.utcnow()
            category = class_category if class_category in CLASS_CATEGORIES else "general"

        class PreviewBooking:
            fitness_class = PreviewClass()
        pseudo_booking = PreviewBooking()
    facts = _context(member, policy.event_type, pseudo_invoice, booking=pseudo_booking)
    matched = matches(definition, facts)
    within_limits = _within_limits(policy, member)
    action = definition["action"]
    result = dict(matched=matched, within_limits=within_limits, action=action, facts=facts,
                  outcome=describe_action(action))
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
    policies = _eligible_policies(event_type)
    if not policies:
        return []
    facts = _context(member, event_type, invoice, booking=booking, include_segment=_uses_segment(policies))
    applied = []
    for policy in policies:
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
            reward_redemptions = [row for row in RewardRedemption.query.filter_by(reward_id=reward.id).all()
                                  if row.status != "reversed"]
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
    # Total limit counts every member's redemptions; the personal limit
    # counts only this account. Reversed (refunded) redemptions are not
    # counted, so a refund gives the slot back.
    all_active = [row for row in RewardRedemption.query.filter_by(reward_id=reward.id).all()
                  if row.status != "reversed"]
    existing = [row for row in all_active if row.account_id == account.id]
    if reward.max_redemptions is not None and len(all_active) >= reward.max_redemptions:
        raise ValidationError("This reward has reached its redemption limit.")
    if reward.max_per_member is not None and len(existing) >= reward.max_per_member:
        raise ValidationError("You have already reached your personal redemption limit for this reward.")
    key = f"redeem:{account.id}:{reward.id}:{uuid.uuid4().hex}"
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


def offers_for_member(member):
    """Active policies a member can see, in plain language, with whether
    they qualify right now. Read-only: nothing is awarded here."""
    if not member or not member.card:
        return []
    policies = _eligible_policies_readonly()
    facts = _context(member, "member_activity", include_segment=_uses_segment(policies))
    rows = []
    for policy in policies:
        definition = json.loads(policy.definition)
        state = _evaluate(definition, facts)
        if not _within_limits(policy, member):
            status = "used"
        elif state is True:
            status = "eligible"
        elif state is None:
            status = "depends"
        else:
            status = "not_yet"
        rows.append({"policy": policy, "when": EVENT_LABELS[policy.event_type],
                     "conditions": describe_conditions(definition),
                     "outcome": describe_action(definition["action"]), "status": status})
    order = {"eligible": 0, "depends": 1, "not_yet": 2, "used": 3}
    rows.sort(key=lambda row: (order[row["status"]], row["policy"].priority, row["policy"].id))
    return rows


def _eligible_policies_readonly(now=None):
    now = now or datetime.utcnow()
    return sorted([p for p in PricingPolicy.query.filter_by(status="active").all()
                   if (p.starts_at is None or p.starts_at <= now)
                   and (p.ends_at is None or p.ends_at >= now)], key=lambda p: (p.priority, p.id))


def tier_perks(account):
    """Rewards that the member's current tier unlocks (minimum_tier set),
    so the card and wallet can show 'what Gold gets you'."""
    tier = current_tier(account)
    if tier is None:
        return []
    now = datetime.utcnow()
    return [reward for reward in Reward.query.filter_by(active=True).all()
            if reward.minimum_tier and reward_is_eligible(account, reward)
            and (reward.starts_at is None or reward.starts_at <= now)
            and (reward.ends_at is None or reward.ends_at >= now)]


def tier_css(tier):
    """CSS modifier for a tier chip; unknown custom tier names get a neutral style."""
    name = (tier.name if tier else "").lower()
    return name if name in {"bronze", "silver", "gold", "platinum"} else "custom"



def upcoming_offers(limit=5):
    """Active offers whose start date is still in the future ('coming soon')."""
    now = datetime.utcnow()
    rows = [p for p in PricingPolicy.query.filter_by(status="active").all()
            if p.starts_at and p.starts_at > now and (p.ends_at is None or p.ends_at > now)]
    rows.sort(key=lambda p: p.starts_at)
    result = []
    for policy in rows[:limit]:
        definition = json.loads(policy.definition)
        result.append({"policy": policy, "when": EVENT_LABELS[policy.event_type],
                       "conditions": describe_conditions(definition),
                       "outcome": describe_action(definition["action"])})
    return result


def invoice_offer_preview(invoice):
    """What the automatic offers would do if this pending invoice were paid
    now: (discount amount, policy or None). Read-only."""
    if invoice.status != "pending":
        return 0.0, None
    return _get_policy_discount(invoice, None)


def rename_tier_references(old_name, new_name, administrator):
    """Keep offers and rewards working when an administrator renames a tier:
    rules comparing member.tier to the old name, and rewards with that
    minimum tier, are updated (each policy change is audited)."""
    if not old_name or old_name == new_name:
        return 0
    changed = 0
    for policy in PricingPolicy.query.all():
        definition = json.loads(policy.definition)
        touched = False

        def fix(condition):
            nonlocal touched
            if condition.get("fact") != "member.tier":
                return
            value = condition["value"]
            if isinstance(value, list) and old_name in value:
                condition["value"] = [new_name if item == old_name else item for item in value]
                touched = True
            elif value == old_name:
                condition["value"] = new_name
                touched = True

        for condition in definition["conditions"]:
            if "any" in condition:
                for item in condition["any"]:
                    fix(item)
            else:
                fix(condition)
        if touched:
            policy.definition = json.dumps(definition)
            policy.updated_by_id = administrator.id
            policy.updated_at = datetime.utcnow()
            from gymflow.models import PolicyAudit
            db.session.add(PolicyAudit(policy_id=policy.id, changed_by_id=administrator.id, action="updated",
                                       snapshot=json.dumps({"name": policy.name, "definition": definition,
                                                            "reason": f"Tier renamed from {old_name} to {new_name}"})))
            changed += 1
    for reward in Reward.query.filter_by(minimum_tier=old_name).all():
        reward.minimum_tier = new_name
        changed += 1
    return changed


def programme_overview(days=30):
    """Small KPI summary for the administrator page."""
    since = datetime.utcnow() - timedelta(days=days)
    accounts = LoyaltyAccount.query.all()
    tiers = sorted(LoyaltyTier.query.filter_by(active=True).all(), key=lambda t: t.threshold)
    per_tier = {tier.name: 0 for tier in tiers}
    for account in accounts:
        tier = current_tier(account)
        if tier:
            per_tier[tier.name] = per_tier.get(tier.name, 0) + 1
    entries = [e for e in PointsLedgerEntry.query.all() if e.created_at >= since]
    applications = [a for a in PolicyApplication.query.all() if a.created_at >= since]
    uses = {}
    for application in applications:
        uses[application.policy_id] = uses.get(application.policy_id, 0) + 1
    top = sorted(uses.items(), key=lambda item: -item[1])[:3]
    top_policies = [(PricingPolicy.query.get(pid), count) for pid, count in top]
    return {
        "days": days,
        "members_per_tier": per_tier,
        "points_earned": sum(e.points for e in entries if e.points > 0 and e.entry_type == "earned"),
        "points_redeemed": -sum(e.points for e in entries if e.entry_type == "redeemed"),
        "discounts_total": round(sum(a.discount_amount or 0 for a in applications), 2),
        "rewards_ready": RewardRedemption.query.filter_by(status="reserved").count(),
        "top_policies": [(p, c) for p, c in top_policies if p is not None],
    }


def redemption_usable(redemption):
    """True if a reserved reward can be used right now (reward still active,
    in its date window, and the member still meets its tier requirement)."""
    if redemption is None or redemption.status != "reserved":
        return False
    reward, now = redemption.reward, datetime.utcnow()
    return bool(reward.active and (reward.starts_at is None or reward.starts_at <= now)
                and (reward.ends_at is None or reward.ends_at >= now)
                and reward_is_eligible(redemption.account, reward))
