"""Authentication routes — 3.5.2 Authentication, 3.5.5 Password recovery,
3.5.6 Password change. See seq_authentication.png / act_authentication.png.
"""
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from gymflow import card_manager, security
from gymflow.card_manager import ValidationError, change_password, recover_password
from gymflow.models import User
from gymflow.permissions import current_user, login_required

auth_bp = Blueprint("auth", __name__)

# A fixed, never-used hash checked when the submitted email does not match
# any account, so an unknown email takes about as long to reject as a known
# one with a wrong password. Without this, an attacker can tell which
# emails are registered members just by noticing that real emails take a
# few milliseconds longer to reject (check_password_hash actually runs)
# than made-up ones (it would otherwise be skipped entirely) -- a timing
# side-channel, not a hypothetical one.
_DUMMY_HASH = generate_password_hash("not-a-real-account-timing-equaliser-only")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("members.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        # Login rate limiting (security pass, Second Revision): too many
        # wrong passwords for this email in a row and further attempts are
        # refused for a while, regardless of whether this one would have
        # been correct -- see gymflow/security.py for the sliding-window
        # lockout algorithm.
        remaining = security.lockout_remaining_seconds(email)
        if remaining is not None:
            minutes, seconds = divmod(remaining, 60)
            wait = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
            flash(f"Too many failed attempts for this account. Try again in {wait}.", "danger")
            return render_template("login.html")

        # isLogged() / verifyCredentials() from the Authentication sequence diagram
        user = User.query.filter_by(email=email).first()
        if user:
            password_ok = user.check_password(password)
        else:
            check_password_hash(_DUMMY_HASH, password)  # equalise timing; see _DUMMY_HASH above
            password_ok = False

        if password_ok:
            security.clear_failed_logins(email)
            session["user_id"] = user.id
            session["role"] = user.role
            flash(f"Welcome back, {user.name}.", "success")
            if user.must_change_password:
                flash("For your security, please set a new password.", "warning")
                return redirect(url_for("auth.change_password_view"))
            return redirect(url_for("members.dashboard"))

        security.register_failed_login(email)
        flash("Invalid email or password.", "danger")
    return render_template("login.html")


# ---------------------------------------------------------------------------
# Public self-service sign-up (extends 3.5.3): anyone can create their own
# member account and start booking classes immediately, without a
# receptionist registering them first.
# ---------------------------------------------------------------------------
@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("user_id"):
        return redirect(url_for("members.dashboard"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if password != confirm:
            flash("The two passwords do not match.", "danger")
            return render_template("signup.html")
        try:
            user = card_manager.self_register_member(request.form, password)
            session["user_id"] = user.id
            session["role"] = user.role
            flash(f"Welcome to GymFlow, {user.name}! Your card ({user.card.member_code}) is ready — "
                  "browse classes and book your first session below.", "success")
            return redirect(url_for("members.dashboard"))
        except ValidationError as exc:
            flash(str(exc), "danger")
    return render_template("signup.html")


@auth_bp.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/password-recovery", methods=["GET", "POST"])
def password_recovery():
    if request.method == "POST":
        email = request.form.get("email", "")
        recover_password(email)  # always behaves the same whether or not the address exists
        flash("If that address is registered, a temporary password has been emailed to it. "
              "(Check the Mail Outbox to see it for this demo.)", "success")
        return redirect(url_for("auth.login"))
    return render_template("password_recovery.html")


@auth_bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password_view():
    user = current_user()
    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if new_password != confirm:
            flash("The two passwords do not match.", "danger")
        else:
            try:
                change_password(user, new_password)
                flash("Password changed successfully.", "success")
                return redirect(url_for("members.dashboard"))
            except ValidationError as exc:
                flash(str(exc), "danger")
    return render_template("change_password.html")
