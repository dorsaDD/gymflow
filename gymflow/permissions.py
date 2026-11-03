"""PermissionsManager — role-based access control (see class diagram).

Implements getPermissions(role) / the “verifyAccessRights()” checks that
appear throughout the activity diagrams in Section 3.5.
"""
from functools import wraps

from flask import abort, flash, redirect, session, url_for

# What each role is allowed to do. Mirrors the actor/use-case associations in
# the use case diagram (Figure 1).
PERMISSIONS = {
    "member": {
        "view_own_card", "change_password", "recover_password",
        "browse_classes", "book_class", "cancel_own_booking",
        "join_waitlist", "leave_own_waitlist",
        "manage_own_notifications", "manage_own_photo", "manage_own_diet",
        "manage_own_payments", "view_own_rewards", "redeem_own_rewards",
    },
    "trainer": {
        "view_own_card", "change_password", "recover_password",
        "view_any_card", "modify_card", "upload_attachment",
        "add_progress_note", "remove_own_note",
        "assign_workout_plan", "remove_own_workout_plan",
        "manage_classes", "browse_classes", "mark_attendance",
        "manage_own_notifications", "manage_member_diet", "view_model_insights",
    },
    "receptionist": {
        "view_own_card", "change_password", "recover_password",
        "view_any_card", "modify_card", "upload_attachment",
        "register_member", "remove_own_note",
        "manage_own_notifications", "manage_billing",
    },
    "administrator": {
        "view_own_card", "change_password", "recover_password",
        "view_any_card", "modify_card", "upload_attachment",
        "register_member", "add_progress_note", "remove_any_note",
        "assign_workout_plan", "remove_any_workout_plan",
        "manage_classes", "browse_classes",
        "manage_staff", "deregister_user", "view_history",
        "manage_own_notifications", "view_outbox", "mark_attendance",
        "manage_member_diet", "manage_billing", "view_model_insights",
        "manage_loyalty",
    },
}


def get_permissions(role: str) -> set:
    return PERMISSIONS.get(role, set())


def has_permission(role: str, permission: str) -> bool:
    return permission in get_permissions(role)


def current_user():
    from gymflow.models import User  # local import avoids circular import

    user_id = session.get("user_id")
    if not user_id:
        return None
    return User.query.get(user_id)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def permission_required(permission: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                flash("Please log in to continue.", "warning")
                return redirect(url_for("auth.login"))
            if not has_permission(user.role, permission):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator
