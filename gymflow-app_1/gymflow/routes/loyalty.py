"""Member rewards wallet and administrator pricing/policy management."""
import json
from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from gymflow import loyalty_engine
from gymflow.card_manager import ValidationError
from gymflow.extensions import db
from gymflow.models import (
    LoyaltyAccount, LoyaltyTier, PolicyApplication, PolicyAudit, PricingPolicy, Reward,
    RewardRedemption, User,
)
from gymflow.permissions import current_user, login_required, permission_required

loyalty_bp = Blueprint("loyalty", __name__, url_prefix="/loyalty")
admin_loyalty_bp = Blueprint("admin_loyalty", __name__, url_prefix="/admin/loyalty")


def _date_field(raw, label):
    if not (raw or "").strip():
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ValidationError(f"{label} must use the date and time shown in the form.")


@loyalty_bp.route("/")
@login_required
def dashboard():
    member = current_user()
    if member.role != "member" or not member.card:
        abort(404)
    account = loyalty_engine.get_account(member.card)
    db.session.commit()
    progress = loyalty_engine.tier_progress(account)
    now = datetime.utcnow()
    rewards = [reward for reward in Reward.query.filter_by(active=True).all()
               if (reward.starts_at is None or reward.starts_at <= now)
               and (reward.ends_at is None or reward.ends_at >= now)
               and loyalty_engine.reward_is_eligible(account, reward)]
    redemptions = RewardRedemption.query.filter_by(account_id=account.id).all()
    redemptions.sort(key=lambda item: item.redeemed_at, reverse=True)
    # One timeline, newest first: point movements and reward credits together.
    history = [{"when": e.created_at, "text": e.note, "points": e.points, "kind": e.entry_type.capitalize()}
               for e in account.entries]
    history += [{"when": r.redeemed_at, "text": r.reward.name + (f" (from offer: {r.policy.name})" if r.policy else ""),
                 "points": None, "kind": {"reserved": "Reward ready", "used": "Reward used",
                                          "reversed": "Reward refunded"}.get(r.status, r.status)}
                for r in redemptions]
    history.sort(key=lambda row: row["when"], reverse=True)
    return render_template("loyalty.html", account=account, progress=progress,
                           rewards=rewards, redemptions=redemptions[:20],
                           history=history[:40], history_total=len(history),
                           upcoming=loyalty_engine.upcoming_offers(),
                           offers=loyalty_engine.offers_for_member(member),
                           tier_css=loyalty_engine.tier_css(progress["current"]),
                           ready=[item for item in redemptions if item.status == "reserved"],
                           usable={item.id for item in redemptions if loyalty_engine.redemption_usable(item)})


@loyalty_bp.route("/redeem/<int:reward_id>", methods=["POST"])
@permission_required("redeem_own_rewards")
def redeem(reward_id):
    member = current_user()
    reward = Reward.query.get_or_404(reward_id)
    try:
        loyalty_engine.redeem_reward(member, reward)
        flash(f"'{reward.name}' reserved. Your points were deducted.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("loyalty.dashboard"))


@admin_loyalty_bp.route("/")
@permission_required("manage_loyalty")
def manage():
    policies = PricingPolicy.query.all()
    admin = current_user()
    now = datetime.utcnow()
    expired = False
    for policy in policies:
        if policy.status == "active" and policy.ends_at and policy.ends_at < now:
            policy.status = "expired"
            policy.updated_by_id = admin.id
            policy.updated_at = now
            db.session.add(PolicyAudit(policy_id=policy.id, changed_by_id=admin.id, action="expired",
                                       snapshot=json.dumps({"name": policy.name, "status": "expired",
                                                           "event_type": policy.event_type,
                                                           "definition": json.loads(policy.definition),
                                                           "priority": policy.priority,
                                                           "starts_at": policy.starts_at.isoformat() if policy.starts_at else None,
                                                           "ends_at": policy.ends_at.isoformat()})))
            expired = True
    if expired:
        db.session.commit()
    policies.sort(key=lambda item: (item.priority, item.name.lower()))
    tiers = LoyaltyTier.query.all()
    tiers.sort(key=lambda item: item.threshold)
    rewards = Reward.query.all()
    rewards.sort(key=lambda item: item.name.lower())
    members = User.query.filter_by(role="member").all()
    audits = PolicyAudit.query.all()
    audits.sort(key=lambda item: item.created_at, reverse=True)
    rules = {}
    for policy in policies:
        try:
            definition = json.loads(policy.definition)
            rules[policy.id] = {
                "text": loyalty_engine.rule_to_text(policy.event_type, definition),
                "summary": (loyalty_engine.EVENT_LABELS[policy.event_type]
                            + (", if " + " and ".join(loyalty_engine.describe_conditions(definition))
                               if definition["conditions"] else "")
                            + " → Result: " + loyalty_engine.describe_action(definition["action"]) + "."),
                "uses": PolicyApplication.query.filter_by(policy_id=policy.id).count(),
            }
        except (ValueError, KeyError, TypeError):
            rules[policy.id] = {"text": policy.definition, "summary": "This stored rule could not be read; edit and save it again.", "uses": 0}
    return render_template("loyalty_admin.html", policies=policies, tiers=tiers,
                           rewards=rewards, members=members, audits=audits[:30], rules=rules,
                           fact_types=loyalty_engine.FACT_TYPES, fact_events=loyalty_engine.FACT_EVENTS,
                           fact_labels=loyalty_engine.FACT_LABELS, fact_choices=loyalty_engine.FACT_CHOICES,
                           event_labels=loyalty_engine.EVENT_LABELS,
                           overview=loyalty_engine.programme_overview())


@admin_loyalty_bp.route("/policies", methods=["POST"])
@permission_required("manage_loyalty")
def save_policy():
    admin = current_user()
    policy_id = request.form.get("policy_id", type=int)
    policy = PricingPolicy.query.get(policy_id) if policy_id else None
    if policy_id and policy is None:
        abort(404)
    try:
        values = loyalty_engine.validate_policy_fields(request.form)
        starts_at = _date_field(request.form.get("starts_at"), "Start date")
        ends_at = _date_field(request.form.get("ends_at"), "End date")
        if starts_at and ends_at and ends_at < starts_at:
            raise ValidationError("End date must be after the start date.")
        action = "created" if policy is None else "updated"
        if policy is None:
            policy = PricingPolicy(**values, created_by_id=admin.id, updated_by_id=admin.id)
            db.session.add(policy)
        else:
            for field, value in values.items():
                setattr(policy, field, value)
            policy.updated_by_id = admin.id
        policy.starts_at, policy.ends_at = starts_at, ends_at
        policy.updated_at = datetime.utcnow()
        initial_status = request.form.get("status", "draft")
        if initial_status not in {"draft", "active", "paused", "expired"}:
            raise ValidationError("Choose draft, active, paused, or expired status.")
        if initial_status == "active":
            loyalty_engine.validate_definition(policy.event_type, policy.definition)
        policy.status = initial_status
        db.session.flush()
        db.session.add(PolicyAudit(policy_id=policy.id, changed_by_id=admin.id, action=action,
                                   snapshot=json.dumps({"name": policy.name, "description": policy.description,
                                                       "status": policy.status, "event_type": policy.event_type,
                                                       "definition": json.loads(policy.definition),
                                                       "priority": policy.priority,
                                                       "starts_at": starts_at.isoformat() if starts_at else None,
                                                       "ends_at": ends_at.isoformat() if ends_at else None,
                                                       "max_uses_per_member": policy.max_uses_per_member,
                                                       "max_uses_total": policy.max_uses_total})))
        db.session.commit()
        flash(f"Policy {action}.", "success")
    except ValidationError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("admin_loyalty.manage"))


@admin_loyalty_bp.route("/policies/<int:policy_id>/status", methods=["POST"])
@permission_required("manage_loyalty")
def change_status(policy_id):
    policy = PricingPolicy.query.get_or_404(policy_id)
    admin = current_user()
    status = request.form.get("status")
    if status not in {"active", "paused", "expired"}:
        abort(400)
    try:
        if status == "active":
            loyalty_engine.validate_definition(policy.event_type, policy.definition)
        policy.status = status
        policy.updated_by_id = admin.id
        policy.updated_at = datetime.utcnow()
        db.session.add(PolicyAudit(policy_id=policy.id, changed_by_id=admin.id, action=status,
                                   snapshot=json.dumps({"name": policy.name, "status": status,
                                                       "event_type": policy.event_type,
                                                       "definition": json.loads(policy.definition),
                                                       "priority": policy.priority,
                                                       "starts_at": policy.starts_at.isoformat() if policy.starts_at else None,
                                                       "ends_at": policy.ends_at.isoformat() if policy.ends_at else None,
                                                       "max_uses_per_member": policy.max_uses_per_member,
                                                       "max_uses_total": policy.max_uses_total})))
        db.session.commit()
        flash(f"Policy {status}.", "success")
    except ValidationError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("admin_loyalty.manage"))


@admin_loyalty_bp.route("/policies/<int:policy_id>/preview", methods=["POST"])
@permission_required("manage_loyalty")
def preview(policy_id):
    policy = PricingPolicy.query.get_or_404(policy_id)
    member = User.query.filter_by(id=request.form.get("member_id", type=int), role="member").first()
    if not member:
        flash("Select a valid member for the preview.", "danger")
        return redirect(url_for("admin_loyalty.manage"))
    amount = request.form.get("invoice_amount", type=float)
    try:
        result = loyalty_engine.preview_policy(policy, member, amount,
                                               invoice_category=request.form.get("invoice_category"),
                                               class_category=request.form.get("class_category"))
        if result["matched"]:
            summary = result.get("discount")
            detail = (f" Estimated discount: ${summary:.2f}." if summary is not None
                      else f" Outcome: {result['outcome']}.")
            limit_note = "" if result["within_limits"] else " (But this member has already used up this policy's limit.)"
            flash(f"Preview: {member.full_name} matches.{detail}{limit_note}", "success")
        else:
            flash(f"Preview: {member.full_name} does not match this policy's conditions "
                  f"(tier {result['facts']['member.tier']}, age {result['facts']['member.age']}, "
                  f"{result['facts']['member.attended_classes']} classes attended).", "info")
    except (ValueError, ValidationError) as exc:
        flash(f"Preview unavailable: {exc}", "danger")
    return redirect(url_for("admin_loyalty.manage", _anchor=f"policy-{policy.id}"))


@admin_loyalty_bp.route("/tiers", methods=["POST"])
@permission_required("manage_loyalty")
def save_tier():
    name = (request.form.get("name") or "").strip()
    try:
        threshold = int(request.form.get("threshold") or "")
        order = int(request.form.get("sort_order") or "0")
    except ValueError:
        flash("Tier threshold and order must be whole numbers.", "danger")
        return redirect(url_for("admin_loyalty.manage"))
    if not name or len(name) > 40 or threshold < 0:
        flash("Enter a tier name and a non-negative point threshold.", "danger")
        return redirect(url_for("admin_loyalty.manage"))
    tier_id = request.form.get("tier_id", type=int)
    tier = LoyaltyTier.query.get(tier_id) if tier_id else None
    if tier_id and not tier:
        abort(404)
    if any(item.id != (tier.id if tier else None) and (item.name.lower() == name.lower() or item.threshold == threshold)
           for item in LoyaltyTier.query.all()):
        flash("Tier names and thresholds must be unique.", "danger")
        return redirect(url_for("admin_loyalty.manage"))
    renamed = 0
    if tier is None:
        tier = LoyaltyTier(name=name, threshold=threshold, sort_order=order)
        db.session.add(tier)
    else:
        if tier.name != name:
            renamed = loyalty_engine.rename_tier_references(tier.name, name, current_user())
        tier.name, tier.threshold, tier.sort_order = name, threshold, order
    tier.benefits = (request.form.get("benefits") or "").strip()[:300]
    tier.active = request.form.get("active") == "1"
    db.session.commit()
    note = f" {renamed} offer(s)/reward(s) using the old name were updated." if renamed else ""
    flash("Rewards tier saved." + note, "success")
    return redirect(url_for("admin_loyalty.manage"))


@admin_loyalty_bp.route("/rewards", methods=["POST"])
@permission_required("manage_loyalty")
def save_reward():
    try:
        values = loyalty_engine.validate_reward(request.form)
        reward_id = request.form.get("reward_id", type=int)
        reward = Reward.query.get(reward_id) if reward_id else None
        if reward_id and not reward:
            abort(404)
        starts_at = _date_field(request.form.get("starts_at"), "Start date")
        ends_at = _date_field(request.form.get("ends_at"), "End date")
        if reward is None:
            reward = Reward(**values, created_by_id=current_user().id)
            db.session.add(reward)
        else:
            for field, value in values.items():
                setattr(reward, field, value)
        reward.active = request.form.get("active") == "1"
        reward.starts_at, reward.ends_at = starts_at, ends_at
        if reward.starts_at and reward.ends_at and reward.ends_at < reward.starts_at:
            raise ValidationError("End date must be after the start date.")
        db.session.commit()
        flash("Reward saved.", "success")
    except ValidationError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("admin_loyalty.manage"))


@admin_loyalty_bp.route("/rewards/<int:reward_id>/status", methods=["POST"])
@permission_required("manage_loyalty")
def reward_status(reward_id):
    reward = Reward.query.get_or_404(reward_id)
    reward.active = request.form.get("active") == "1"
    db.session.commit()
    flash("Reward availability updated.", "success")
    return redirect(url_for("admin_loyalty.manage"))


@admin_loyalty_bp.route("/points/adjust", methods=["POST"])
@permission_required("manage_loyalty")
def adjust_points():
    member = User.query.filter_by(id=request.form.get("member_id", type=int), role="member").first()
    if not member:
        flash("Select a valid member.", "danger")
        return redirect(url_for("admin_loyalty.manage"))
    try:
        loyalty_engine.adjust_points(member, request.form.get("delta"), request.form.get("reason"), current_user())
        flash(f"Points adjusted for {member.full_name}. The change is in the points ledger.", "success")
    except ValidationError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("admin_loyalty.manage"))
