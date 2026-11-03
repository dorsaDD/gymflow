"""AIAgentManager — Section 4.9: a real LLM-backed chat assistant, grounded
in the asking user's own GymFlow data rather than left to hallucinate.

Reachable by any logged-in role (Section 4.9 realises: browse own context,
ask natural-language questions) from the chat page in
routes/assistant.py. Voice input/output (dictate a question, have the
reply read aloud) is implemented entirely in the browser via the Web
Speech API in templates/assistant.html -- there is deliberately no
server-side speech code here: the browser already does speech-to-text and
text-to-speech natively in Chrome/Edge, so adding a server-side voice
pipeline (and another paid API) would just be reinventing something the
client already provides for free.

Testing boundary: every actual network call to the LLM provider goes
through the single function _call_llm() at the bottom of this module.
_devtools/test_smoke.py monkeypatches THAT one function to a canned stub
before exercising the assistant routes, so the automated suite has no
dependency on a live API key or network access -- the same reason the
project ships its own offline Flask-SQLAlchemy shim
(_devtools/shim/flask_sqlalchemy.py). Everything above _call_llm() (prompt
construction, context grounding, conversation history) is real code that
runs whether the reply came from the real API or the test stub.
"""
import os
from datetime import date as date_cls
from datetime import datetime

from gymflow.models import Booking, FitnessClass
from gymflow import loyalty_engine, nutrition_manager, payment_manager, resilience

MAX_HISTORY_TURNS = 3  # (user, assistant) turn pairs kept as context per chat
# Kept deliberately small: routes/assistant.py stores this history in the
# Flask session, which by default is a SIGNED CLIENT-SIDE COOKIE capped
# around 4KB by browsers -- unlike everything else GymFlow persists, this
# is genuinely per-browser-session, not a database row. A larger or
# longer-lived transcript would need a real ChatMessage table (noted in
# the Section 11 Roadmap) rather than just raising this number.

SYSTEM_PROMPT_TEMPLATE = """You are the GymFlow Assistant, a helpful in-app assistant for \
GymFlow, a gym and fitness club management system. You are talking to {role_label}.

Answer using ONLY the CONTEXT below plus general knowledge of how GymFlow itself works \
(members book classes, can join a waitlist when a class is full and get auto-promoted when a \
seat frees up, have a membership card, may be assigned workout AND diet plans by a trainer, can \
log their own daily food and exercise in the Diet diary against a calorie/macro goal, and can be \
issued invoices for membership fees which they pay in the Billing section; trainers schedule \
classes, mark attendance after each one, and can follow up on a member's logged nutrition; \
receptionists and administrators issue invoices, take payments' history, and refunds). You can \
discuss the member's own logged diet/exercise data and their own billing/invoice status from \
CONTEXT --- e.g. how their calories today compare to their goal, or whether they have an \
outstanding balance --- but you are not a doctor, dietitian, or payment processor: for anything \
beyond what GymFlow itself tracks (medical advice, diagnosing a condition, a specific diet \
prescription) say so plainly and suggest a qualified professional instead of guessing, and you \
cannot take or process a payment yourself (no card details should ever be typed into this chat) \
--- direct the member to the Billing page to actually pay. Be concise and friendly, a few \
sentences at most unless asked for more detail. If asked about something the CONTEXT does not \
cover -- another member's private data, or anything outside GymFlow -- say plainly that you \
don't have that information rather than guessing.

CONTEXT:
{context}"""

ROLE_LABELS = {
    "member": "a GymFlow member", "trainer": "a GymFlow trainer",
    "receptionist": "a GymFlow receptionist", "administrator": "a GymFlow administrator",
}


def _member_context(user) -> str:
    lines = []
    lines.append("- You have a membership card on file." if user.card
                 else "- No membership card on file.")
    # Confirmed bookings can be historical after the class has run. Only pass
    # genuinely future sessions to the model as "upcoming"; attendance is
    # tracked separately and is never inferred from a booking.
    now = datetime.utcnow()
    upcoming = [
        booking for booking in Booking.query.filter_by(
            member_id=user.id, status="confirmed"
        ).all()
        if booking.fitness_class and booking.fitness_class.schedule >= now
    ]
    upcoming.sort(key=lambda booking: booking.fitness_class.schedule)
    upcoming = upcoming[:10]  # keep private context and per-request token use bounded
    if upcoming:
        lines.append("- Upcoming bookings:")
        for b in upcoming:
            lines.append(
                f"  - '{b.fitness_class.name}' on "
                f"{b.fitness_class.schedule.strftime('%Y-%m-%d %H:%M')} ({b.fitness_class.room})"
            )
    else:
        lines.append("- No upcoming bookings.")
    if user.card:
        account = loyalty_engine.get_account(user.card, create=False)
        if account:
            tier = loyalty_engine.current_tier(account)
            tier_label = f"{tier.name} Rewards Tier" if tier else "No rewards tier yet"
            lines.append(
                f"- Rewards: {account.balance} spendable points, "
                f"{account.lifetime_points} lifetime qualifying points; {tier_label}."
            )
        else:
            lines.append("- Rewards: no points account has been created yet.")
        # Section 4.11: ground the assistant in the member's own logged
        # diet/exercise data too, so "how am I doing on calories today" is
        # answered from real numbers instead of guessed.
        summary = nutrition_manager.daily_summary(user.card, date_cls.today())
        goal = summary["goal"]
        if goal:
            line = f"- Today's nutrition: {summary['consumed']['calories']}/{goal.daily_calories} kcal logged"
            if summary["percent_of_goal"] is not None:
                line += f" ({summary['percent_of_goal']}% of goal)"
            lines.append(line)
        elif summary["entries"]:
            lines.append(f"- Today's nutrition: {summary['consumed']['calories']} kcal logged (no daily goal set)")
        else:
            lines.append("- No food logged today, and no daily nutrition goal set.")
        if user.card.diet_plans:
            lines.append(f"- Active diet plan: '{user.card.diet_plans[0].title}'")
        # Section 4.12: ground the assistant in the member's own real
        # billing status too, so "do I owe anything" is answered from the
        # actual invoice data instead of guessed.
        balance = payment_manager.outstanding_balance(user.card)
        if balance:
            lines.append(f"- Outstanding balance: ${balance:.2f} (see the Billing page to pay)")
        else:
            lines.append("- No outstanding balance.")
    return "\n".join(lines)


def _trainer_context(user) -> str:
    classes = FitnessClass.query.filter_by(trainer_id=user.id).all()
    upcoming = sorted(
        (c for c in classes if c.schedule >= datetime.utcnow()), key=lambda c: c.schedule
    )[:10]
    if not upcoming:
        return "- You have no upcoming classes scheduled."
    lines = ["- Classes you teach (upcoming):"]
    for c in upcoming:
        lines.append(
            f"  - '{c.name}' on {c.schedule.strftime('%Y-%m-%d %H:%M')} ({c.room}), "
            f"{len(c.confirmed_bookings())}/{c.capacity} booked"
        )
    return "\n".join(lines)


def _context_for(user) -> str:
    if user.role == "member":
        return _member_context(user)
    if user.role == "trainer":
        return _trainer_context(user)
    return "- (No account-specific context is grounded for this role yet; answer from general GymFlow knowledge only.)"


def ask_agent(user, message: str, history: list) -> str:
    """`history` is a list of {"role": "user"|"assistant", "content": str}
    dicts; the caller owns where it's persisted (routes/assistant.py keeps
    it in the Flask session, per-login-session only -- see that module's
    docstring for why it isn't written to the database). Returns the
    assistant's reply text.
    """
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        role_label=ROLE_LABELS.get(user.role, "a GymFlow user"),
        context=_context_for(user),
    )
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-MAX_HISTORY_TURNS * 2:])
    messages.append({"role": "user", "content": message})
    return _call_llm(messages)


def _call_llm(messages: list) -> str:
    """The one function that talks to an LLM provider.

    Ollama's local server is the default, so using GymFlow does not silently
    send member context to a paid cloud API. OpenAI is available only when an
    administrator explicitly selects ``GYMFLOW_LLM_PROVIDER=openai``.
    Isolated here so it can be monkeypatched in tests -- see the module docstring.

    Section 4.10 (fault tolerance): the request itself goes through
    gymflow.resilience.call_with_retry, so a single dropped connection is
    retried automatically before giving up, and repeated failures trip a
    circuit breaker for the "llm" service so GymFlow stops hammering a
    provider that is genuinely down and fails fast instead. Either way --
    no key configured, every retry failing, or the breaker already open --
    this degrades to a plain-English message instead of a 500.
    """
    provider = os.environ.get("GYMFLOW_LLM_PROVIDER", "ollama").strip().lower()
    if provider not in {"ollama", "openai"}:
        return ("The AI provider setting is invalid. Set GYMFLOW_LLM_PROVIDER to "
                "'ollama' (local) or 'openai' (paid API), then restart GymFlow.")

    api_key = os.environ.get("OPENAI_API_KEY")
    if provider == "openai" and not api_key:
        return ("Paid AI is not configured. Add OPENAI_API_KEY and set "
                "GYMFLOW_LLM_PROVIDER=openai, or switch back to the free local "
                "Ollama provider. See the AI Assistant setup in README.md.")

    # Give a direct setup hint instead of retrying an import failure three
    # times and returning the generic provider/network error. The app can be
    # installed without optional API use, but a live assistant needs the
    # pinned `openai` package from requirements.txt.
    try:
        from openai import OpenAI
    except ImportError:
        return ("The AI assistant package is missing. Activate GymFlow's virtual "
                "environment, run `pip install -r requirements.txt`, then restart GymFlow.")

    def _request():
        if provider == "ollama":
            client = OpenAI(
                base_url=os.environ.get(
                    "GYMFLOW_LLM_BASE_URL", "http://localhost:11434/v1"
                ),
                api_key="ollama",  # local Ollama requires a value and ignores it
                timeout=120.0,  # allow the first CPU inference/model load to finish
            )
            response = client.chat.completions.create(
                model=os.environ.get("GYMFLOW_OLLAMA_MODEL", "qwen2.5:3b"),
                messages=messages,
                max_tokens=400,
                temperature=0.4,
            )
        else:
            client = OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model=os.environ.get("GYMFLOW_LLM_MODEL", "gpt-6-luna"),
                messages=messages,
                max_completion_tokens=400,
                reasoning_effort="none",
            )
        content = response.choices[0].message.content
        return content or "I couldn't create a reply just now. Please try again."

    try:
        return resilience.call_with_retry(_request, service="llm", backoff_seconds=0.3)
    except resilience.CircuitOpenError:
        if provider == "ollama":
            return ("The local AI model is unavailable. Start Ollama, then run "
                    "`ollama pull qwen2.5:3b` once to download GymFlow's default model.")
        return ("The AI assistant is temporarily unavailable after repeated errors -- "
                "please try again in a minute.")
    except Exception as exc:  # noqa: BLE001 -- any provider/network failure must degrade, not 500
        if provider == "ollama":
            return ("GymFlow couldn't reach its local AI model. Make sure Ollama is "
                    "running and the `qwen2.5:3b` model is installed. This local mode "
                    "does not use a paid OpenAI API key.")
        return (f"Sorry, I couldn't reach the AI assistant right now ({type(exc).__name__}). "
                "Please try again in a moment.")
