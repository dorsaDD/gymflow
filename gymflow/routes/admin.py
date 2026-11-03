"""Administrator routes — 3.5.8 Trainer/Receptionist registration,
3.5.4 De-registration.
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from gymflow import card_manager
from gymflow.card_manager import ValidationError
from gymflow.models import User
from gymflow.permissions import current_user, permission_required

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/staff")
@permission_required("manage_staff")
def staff_list():
    staff = User.query.filter(User.role.in_(["trainer", "receptionist"])).order_by(User.surname).all()
    return render_template("staff_list.html", staff=staff)


@admin_bp.route("/staff/new/<role>", methods=["GET", "POST"])
@permission_required("manage_staff")
def staff_new(role):
    if role not in ("trainer", "receptionist"):
        abort(404)
    if request.method == "POST":
        try:
            user = card_manager.register_staff(request.form, role)
            flash(f"{role.capitalize()} account created for {user.full_name}. "
                  "A welcome email with a temporary password was sent (see Mail Outbox).", "success")
            return redirect(url_for("admin.staff_list"))
        except ValidationError as exc:
            flash(str(exc), "danger")
    return render_template("staff_new.html", role=role)


@admin_bp.route("/users/<int:user_id>/deregister", methods=["POST"])
@permission_required("deregister_user")
def deregister(user_id):
    admin = current_user()
    if user_id == admin.id:
        flash("You cannot de-register your own account.", "danger")
        return redirect(request.referrer or url_for("admin.staff_list"))
    try:
        target = User.query.get_or_404(user_id)
        name, role = target.full_name, target.role
        card_manager.deregister_user(user_id)
        flash(f"{name} ({role}) has been de-registered.", "success")
    except ValidationError as exc:
        flash(str(exc), "danger")
    return redirect(request.referrer or url_for("admin.staff_list"))
