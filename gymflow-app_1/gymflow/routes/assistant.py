"""Section 4.9 — AI Agent: a chat page backed by a real LLM (gymflow.ai_agent),
grounded in the asking user's own GymFlow data, reachable by any logged-in
role. Voice input/output lives entirely in templates/assistant.html via the
browser's Web Speech API; these routes only ever see and return text.
"""
from flask import Blueprint, jsonify, render_template, request, session

from gymflow import ai_agent
from gymflow.permissions import current_user, login_required

assistant_bp = Blueprint("assistant", __name__, url_prefix="/assistant")

SESSION_KEY = "assistant_history"
# Kept well under Flask's default signed-cookie session size (~4KB) once
# combined with ai_agent.MAX_HISTORY_TURNS -- see that module's comment.
MAX_MESSAGE_LENGTH = 500


@assistant_bp.route("/")
@login_required
def chat_page():
    return render_template("assistant.html", history=session.get(SESSION_KEY, []))


@assistant_bp.route("/message", methods=["POST"])
@login_required
def send_message():
    user = current_user()
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message cannot be empty."}), 400
    if len(message) > MAX_MESSAGE_LENGTH:
        return jsonify({"error": f"Message is too long (max {MAX_MESSAGE_LENGTH} characters)."}), 400

    history = session.get(SESSION_KEY, [])
    reply = ai_agent.ask_agent(user, message, history)

    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    session[SESSION_KEY] = history[-(ai_agent.MAX_HISTORY_TURNS * 2):]

    return jsonify({"reply": reply})


@assistant_bp.route("/reset", methods=["POST"])
@login_required
def reset_chat():
    session.pop(SESSION_KEY, None)
    return jsonify({"ok": True})
