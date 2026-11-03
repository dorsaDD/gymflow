"""Payment & Billing routes — Section 4.12. A member's own invoices + a
mocked card payment form (self-service) plus a receptionist/administrator
view for issuing invoices, browsing every member's billing, and refunds
(staff-service) -- mirrors the self-vs-staff split in routes/diet.py.
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from gymflow import payment_manager
from gymflow.card_manager import ValidationError
from gymflow.models import Card, Invoice, RewardRedemption
from gymflow.permissions import current_user, has_permission, login_required

billing_bp = Blueprint("billing", __name__, url_prefix="/billing")


def _get_card_or_404(card_id):
    card = Card.query.get(card_id)
    if not card:
        abort(404)
    return card


def _can_view(user, card) -> bool:
    return user.id == card.user_id or has_permission(user.role, "manage_billing")


def _can_pay(user, card) -> bool:
    return user.id == card.user_id and has_permission(user.role, "manage_own_payments")


@billing_bp.route("/")
@login_required
def own_invoices():
    user = current_user()
    if not user.card:
        abort(404)
    return redirect(url_for("billing.view_invoices", card_id=user.card.id))


@billing_bp.route("/<int:card_id>")
@login_required
def view_invoices(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    if not _can_view(user, card):
        abort(403)
    own_rewards = []
    if user.id == card.user_id and user.role == "member":
        from gymflow import loyalty_engine
        account = loyalty_engine.get_account(card, create=False)
        if account:
            own_rewards = [item for item in RewardRedemption.query.filter_by(account_id=account.id, status="reserved").all()
                           if item.reward.target == "invoice"]
    return render_template(
        "billing.html", card=card, user=user,
        balance=payment_manager.outstanding_balance(card),
        can_pay=_can_pay(user, card),
        can_manage=has_permission(user.role, "manage_billing"),
        own_rewards=own_rewards,
    )


@billing_bp.route("/<int:card_id>/invoices", methods=["POST"])
@login_required
def issue_invoice(card_id):
    user = current_user()
    card = _get_card_or_404(card_id)
    if not has_permission(user.role, "manage_billing"):
        abort(403)
    try:
        payment_manager.create_invoice(card, user, request.form)
        flash("Invoice issued.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("billing.view_invoices", card_id=card.id))


@billing_bp.route("/invoices/<int:invoice_id>/pay", methods=["POST"])
@login_required
def pay(invoice_id):
    user = current_user()
    invoice = Invoice.query.get_or_404(invoice_id)
    card = invoice.card
    if not _can_pay(user, card):
        abort(403)
    try:
        payment_manager.pay_invoice(invoice, request.form)
        flash(f"Payment successful. Amount paid: ${invoice.paid_amount:.2f}.", "success")
        return redirect(url_for("billing.receipt", invoice_id=invoice.id))
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("billing.view_invoices", card_id=card.id))


@billing_bp.route("/invoices/<int:invoice_id>/refund", methods=["POST"])
@login_required
def refund(invoice_id):
    user = current_user()
    invoice = Invoice.query.get_or_404(invoice_id)
    card = invoice.card
    if not has_permission(user.role, "manage_billing"):
        abort(403)
    try:
        payment_manager.refund_invoice(invoice, user)
        flash("Invoice refunded.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("billing.view_invoices", card_id=card.id))


@billing_bp.route("/invoices/<int:invoice_id>/receipt")
@login_required
def receipt(invoice_id):
    user = current_user()
    invoice = Invoice.query.get_or_404(invoice_id)
    card = invoice.card
    if not _can_view(user, card):
        abort(403)
    if invoice.status == "pending":
        abort(404)
    return render_template("receipt.html", invoice=invoice, card=card)


@billing_bp.route("/all")
@login_required
def all_invoices():
    user = current_user()
    if not has_permission(user.role, "manage_billing"):
        abort(403)
    invoices = Invoice.query.order_by(Invoice.issued_at.desc()).all()
    return render_template("billing_all.html", invoices=invoices)
