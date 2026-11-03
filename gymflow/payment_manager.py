"""PaymentManager — Section 4.12: Payment & Billing.

A real deployment would hand the actual charge off to a 3rd-party payment
processor (Stripe, etc. — see the "Payment Gateway" node this would add to
the deployment diagram, Section 3.8). For this coursework prototype,
_charge() below stands in for that network hop, the same "mocked external
service" approach already used for MailManager (mail_manager.py) and the
LLM provider (ai_agent.py) — no real money ever moves, and no card details
are ever actually transmitted anywhere.

It deliberately imitates how real payment processors' TEST MODE works
(e.g. Stripe's published test card numbers) rather than just always
succeeding, so the validation and decline paths are both real and
demonstrable: a card number is checked with the actual Luhn checksum
algorithm real card numbers satisfy, and a specific published-style test
number is always declined regardless of everything else about the request
being valid (see the "Demo test cards" hint on the billing page). This
also means a live demo is fully repeatable — the same test card number
always produces the same result.

Section 4.10 (fault tolerance): the mocked network hop is wrapped in
gymflow.resilience's retry-with-backoff + circuit breaker, exactly like
mail delivery and the LLM call — a transient processor blip is retried
automatically, and repeated failures trip a circuit breaker for the
"payment" service so GymFlow fails fast instead of hammering a processor
that is genuinely down. Set GYMFLOW_SIMULATE_PAYMENT_OUTAGE=1 to force
every charge attempt to fail, the same demo pattern as
GYMFLOW_SIMULATE_MAIL_OUTAGE.
"""
import os
import re
from datetime import date as date_cls
from datetime import datetime

from gymflow.card_manager import ValidationError
from gymflow.extensions import db
from gymflow.mail_manager import send_email
from gymflow.models import Card, Invoice, User
from gymflow import resilience
from gymflow import loyalty_engine
from gymflow.models import RewardRedemption

CARD_NUMBER_RE = re.compile(r"^\d{13,19}$")
EXPIRY_RE = re.compile(r"^(0[1-9]|1[0-2])/(\d{2})$")
CVV_RE = re.compile(r"^\d{3,4}$")

# A published-style test card that is always declined regardless of
# everything else being valid, the same idea as Stripe's own test cards
# (e.g. 4000 0000 0000 0002 = "card declined"). See the module docstring.
DECLINE_SUFFIX = "0002"


def _luhn_valid(card_number: str) -> bool:
    total = 0
    for i, digit in enumerate(reversed([int(d) for d in card_number])):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _card_brand(card_number: str) -> str:
    if card_number.startswith("4"):
        return "Visa"
    if card_number[:2] in {"51", "52", "53", "54", "55"}:
        return "Mastercard"
    if card_number[:2] in {"34", "37"}:
        return "American Express"
    return "Card"


def _parse_amount(raw) -> float:
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        raise ValidationError("Amount must be a number.")
    if amount <= 0:
        raise ValidationError("Amount must be greater than zero.")
    return round(amount, 2)


# ---------------------------------------------------------------------------
# Issuing (staff-service)
# ---------------------------------------------------------------------------
def create_invoice(card: Card, issuer: User, data: dict) -> Invoice:
    description = (data.get("description") or "").strip()
    if not description:
        raise ValidationError("A description is required.")
    amount = _parse_amount(data.get("amount"))

    due_date = None
    due_raw = (data.get("due_date") or "").strip()
    if due_raw:
        try:
            due_date = datetime.strptime(due_raw, "%Y-%m-%d").date()
        except ValueError:
            raise ValidationError("Invalid due date (expected YYYY-MM-DD).")

    invoice = Invoice(
        card_id=card.id, issued_by_id=issuer.id, description=description,
        amount=amount, due_date=due_date,
    )
    db.session.add(invoice)
    db.session.commit()
    if card.owner.notifications_enabled:
        due_note = f" Due {invoice.due_date.isoformat()}." if invoice.due_date else ""
        send_email(
            card.owner.email, "A new invoice was issued to your account",
            f"{issuer.full_name} issued a new invoice: '{invoice.description}' "
            f"for ${invoice.amount:.2f}.{due_note}",
        )
    return invoice


# ---------------------------------------------------------------------------
# The mocked processor call itself
# ---------------------------------------------------------------------------
def _charge(card_number: str, amount: float) -> bool:
    """Stands in for the actual network call to a 3rd-party payment
    processor. Isolated here (like mail_manager._deliver and
    ai_agent._call_llm) so tests/demos can force it to fail without a real
    gateway involved — see the module docstring.

    Returns True if the processor approved the charge, False if it
    declined it. A decline is a normal, successful response from a real
    processor's point of view (it just says "no") -- NOT the same thing as
    the processor being unreachable, so it must not raise. Raising here
    would make pay_invoke()'s resilience.call_with_retry() treat a single
    declined card as a transient failure worth retrying, and three
    "failed" attempts would trip the circuit breaker for every OTHER
    member's payment too, over one person's expired card. Only a genuine
    connectivity problem (GYMFLOW_SIMULATE_PAYMENT_OUTAGE) raises.
    """
    if os.environ.get("GYMFLOW_SIMULATE_PAYMENT_OUTAGE") == "1":
        raise ConnectionError("simulated 3rd-party payment processor outage")
    if card_number.endswith(DECLINE_SUFFIX):
        return False
    print(f"[PaymentGateway] charged {_card_brand(card_number)} ...{card_number[-4:]} : ${amount:.2f}")
    return True


# ---------------------------------------------------------------------------
# Paying (self-service)
# ---------------------------------------------------------------------------
def pay_invoice(invoice: Invoice, data: dict) -> Invoice:
    query = Invoice.query.filter_by(id=invoice.id)
    if hasattr(query, "with_for_update"):
        query = query.with_for_update()
    invoice = query.first() or invoice
    if invoice.status == "paid":
        raise ValidationError("This invoice has already been paid.")
    if invoice.status == "refunded":
        raise ValidationError("This invoice was refunded and can no longer be paid.")

    card_number = re.sub(r"\D", "", data.get("card_number") or "")
    expiry = (data.get("expiry") or "").strip()
    cvv = (data.get("cvv") or "").strip()

    if not CARD_NUMBER_RE.match(card_number):
        raise ValidationError("Card number must be 13-19 digits.")
    if not _luhn_valid(card_number):
        raise ValidationError("That card number isn't valid (failed the checksum).")
    expiry_match = EXPIRY_RE.match(expiry)
    if not expiry_match:
        raise ValidationError("Expiry must be in MM/YY format.")
    exp_month, exp_year = int(expiry_match.group(1)), 2000 + int(expiry_match.group(2))
    today = date_cls.today()
    if (exp_year, exp_month) < (today.year, today.month):
        raise ValidationError("That card has expired.")
    if not CVV_RE.match(cvv):
        raise ValidationError("CVV must be 3 or 4 digits.")

    redemption = None
    redemption_id = (data.get("reward_redemption_id") or "").strip()
    if redemption_id:
        try:
            redemption_id = int(redemption_id)
        except ValueError:
            raise ValidationError("Choose a valid reward redemption.")
        redemption = RewardRedemption.query.get(redemption_id)
        redemption = loyalty_engine.validate_redemption_for_member(redemption, invoice.card.owner, "invoice")
        if redemption.reward.reward_type not in {"percent_discount", "fixed_discount"}:
            raise ValidationError("That reward cannot be applied to an invoice.")

    discount, discount_policy = loyalty_engine._get_policy_discount(invoice, redemption)
    charge_amount = round(max(0.01, invoice.amount - discount), 2)
    try:
        approved = resilience.call_with_retry(
            lambda: _charge(card_number, charge_amount), service="payment", backoff_seconds=0.05,
        )
    except resilience.CircuitOpenError as exc:
        invoice.last_error = f"CircuitOpenError: {exc}"
        db.session.commit()
        raise ValidationError("The payment processor is temporarily unavailable after repeated errors. "
                               "Please try again in a minute.")
    except Exception as exc:  # noqa: BLE001 -- any other processor/network failure must degrade, not 500
        invoice.last_error = f"{type(exc).__name__}: {exc}"
        db.session.commit()
        raise ValidationError("The payment couldn't be processed right now. Please try again shortly.")

    if not approved:
        invoice.last_error = "Card declined (insufficient funds)."
        db.session.commit()
        raise ValidationError("Card declined (insufficient funds).")

    invoice.status = "paid"
    invoice.paid_at = datetime.utcnow()
    invoice.payment_method_last4 = card_number[-4:]
    invoice.last_error = None
    loyalty_engine.apply_invoice_payment(invoice, discount, discount_policy,
                                         redemption=redemption, paid_amount=charge_amount)
    db.session.commit()
    if invoice.card.owner.notifications_enabled:
        send_email(
            invoice.card.owner.email, "Payment received",
            f"We received your payment of ${charge_amount:.2f} for '{invoice.description}'. "
            f"Card ending {invoice.payment_method_last4}.",
        )
    return invoice


# ---------------------------------------------------------------------------
# Refunding (staff-service)
# ---------------------------------------------------------------------------
def refund_invoice(invoice: Invoice, refunder: User) -> Invoice:
    query = Invoice.query.filter_by(id=invoice.id)
    if hasattr(query, "with_for_update"):
        query = query.with_for_update()
    invoice = query.first() or invoice
    if invoice.status != "paid":
        raise ValidationError("Only a paid invoice can be refunded.")
    invoice.status = "refunded"
    invoice.refunded_at = datetime.utcnow()
    invoice.refunded_by_id = refunder.id
    loyalty_engine.reverse_invoice_points(invoice, refunder)
    redemptions = RewardRedemption.query.filter_by(invoice_id=invoice.id).all()
    for redemption in redemptions:
        if redemption.status == "used":
            account = redemption.account
            key = f"refund:redemption:{redemption.id}"
            if not loyalty_engine.PointsLedgerEntry.query.filter_by(event_key=key).first():
                account.balance += redemption.reward.points_cost
                account.updated_at = datetime.utcnow()
                db.session.add(loyalty_engine.PointsLedgerEntry(
                    account_id=account.id, event_key=key, entry_type="adjusted",
                    points=redemption.reward.points_cost, qualifying_points=0,
                    note=f"Points restored for refunded invoice #{invoice.id}", created_by_id=refunder.id))
            redemption.status = "reversed"
    db.session.commit()
    if invoice.card.owner.notifications_enabled:
        send_email(
            invoice.card.owner.email, "Payment refunded",
            f"Your payment of ${invoice.amount:.2f} for '{invoice.description}' has been "
            f"refunded by {refunder.full_name}.",
        )
    return invoice


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def outstanding_balance(card: Card) -> float:
    return round(sum(i.amount for i in card.invoices if i.status == "pending"), 2)


def billing_followup(limit: int = 5):
    """Staff dashboard panel data (Section 4.12): members with an
    outstanding balance, largest first, and flagged if any pending invoice
    is past its due date -- mirrors _nutrition_followup()'s "who most needs
    attention" ranking (Section 4.11) but for unpaid invoices.
    """
    rows = []
    today = date_cls.today()
    for card in Card.query.all():
        balance = outstanding_balance(card)
        if balance <= 0:
            continue
        overdue = any(
            i.status == "pending" and i.due_date is not None and i.due_date < today
            for i in card.invoices
        )
        rows.append({"card": card, "balance": balance, "overdue": overdue})
    rows.sort(key=lambda r: (not r["overdue"], -r["balance"]))
    return rows[:limit]
