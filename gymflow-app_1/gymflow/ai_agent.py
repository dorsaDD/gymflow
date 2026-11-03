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
import re
from datetime import date as date_cls
from datetime import datetime

from gymflow.models import Booking, FitnessClass, Invoice, PricingPolicy, RewardRedemption
from gymflow import loyalty_engine, nutrition_manager, payment_manager, resilience

from datetime import timedelta as _timedelta

_ONE_DAY = _timedelta(days=1)

MAX_HISTORY_TURNS = 3  # (user, assistant) turn pairs kept as context per chat
# Kept deliberately small: routes/assistant.py stores this history in the
# Flask session, which by default is a SIGNED CLIENT-SIDE COOKIE capped
# around 4KB by browsers -- unlike everything else GymFlow persists, this
# is genuinely per-browser-session, not a database row. A larger or
# longer-lived transcript would need a real ChatMessage table (noted in
# the Section 11 Roadmap) rather than just raising this number.

SYSTEM_PROMPT_TEMPLATE = """You are the GymFlow Assistant, a helpful in-app assistant for \
GymFlow, a gym and fitness club management system. You are talking to {role_label}, {name}.

Answer using ONLY the CONTEXT below plus general knowledge of how GymFlow itself works \
(members book classes, can join a waitlist when a class is full and get auto-promoted when a \
seat frees up, have a membership card, may be assigned workout AND diet plans by a trainer, can \
log their own daily food and exercise in the Diet diary against a calorie/macro goal, and can be \
issued invoices for membership fees which they pay in the Billing section; trainers schedule \
classes, mark attendance after each one, and can follow up on a member's logged nutrition; \
receptionists and administrators issue invoices, take payments' history, and refunds; members earn \
loyalty points for attended classes and paid invoices, move up Bronze/Silver/Gold Rewards Tiers \
from lifetime points, and redeem points for rewards such as free classes and invoice discounts on \
the Rewards page, while automatic offers apply at payment or attendance time). You can \
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
    lines.append(f"- Membership code: {user.card.member_code}" if user.card
                 else "- No membership card on file.")
    now = datetime.utcnow()
    upcoming = [b for b in Booking.query.filter_by(member_id=user.id, status="confirmed").all()
                if b.fitness_class and b.fitness_class.schedule >= now]
    if upcoming:
        lines.append("- Upcoming bookings:")
        for b in sorted(upcoming, key=lambda b: b.fitness_class.schedule):
            lines.append(
                f"  - '{b.fitness_class.name}' on "
                f"{b.fitness_class.schedule.strftime('%Y-%m-%d %H:%M')} ({b.fitness_class.room})"
            )
    else:
        lines.append("- No upcoming bookings.")
    if user.card:
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
        lines.extend(_rewards_lines(user))
    return "\n".join(lines)


def _rewards_lines(user):
    """Loyalty grounding: tier, points, ready rewards, and offers in plain words."""
    account = loyalty_engine.get_account(user.card, create=False)
    progress = loyalty_engine.tier_progress(account)
    tier = progress["current"]
    lines = [f"- Rewards tier: {tier.name + ' Rewards Tier' if tier else 'none yet'}; "
             f"{max(account.balance, 0) if account else 0} spendable points, "
             f"{account.lifetime_points if account else 0} lifetime points."]
    if progress["next"]:
        lines.append(f"- {progress['remaining']} more lifetime points to reach {progress['next'].name}.")
    if tier and tier.benefits:
        lines.append(f"- {tier.name} benefits: {tier.benefits}")
    ready = (RewardRedemption.query.filter_by(account_id=account.id, status="reserved").all()
             if account else [])
    for item in ready:
        where = "choose it when booking a class" if item.reward.target == "class_booking" else "choose it when paying an invoice"
        lines.append(f"- Reward ready to use: '{item.reward.name}' ({where}).")
    offers = loyalty_engine.offers_for_member(user)
    qualifying = [o for o in offers if o["status"] in ("eligible", "depends")]
    if qualifying:
        lines.append("- Active offers for this member:")
        for o in qualifying[:6]:
            cond = (" if " + " and ".join(o["conditions"])) if o["conditions"] else ""
            lines.append(f"  - {o['policy'].name}: {o['when'].lower()}{cond} → you {o['outcome']}.")
    return lines


def _trainer_context(user) -> str:
    classes = FitnessClass.query.filter_by(trainer_id=user.id).all()
    upcoming = sorted(
        (c for c in classes if c.schedule >= datetime.utcnow()), key=lambda c: c.schedule
    )
    if not upcoming:
        return "- You have no upcoming classes scheduled."
    lines = ["- Classes you teach (upcoming):"]
    for c in upcoming:
        lines.append(
            f"  - '{c.name}' on {c.schedule.strftime('%Y-%m-%d %H:%M')} ({c.room}), "
            f"{len(c.confirmed_bookings())}/{c.capacity} booked"
        )
    return "\n".join(lines)


def _front_desk_context(user) -> str:
    lines = []
    followup = payment_manager.billing_followup(limit=5)
    if followup:
        lines.append("- Members with an outstanding balance (largest first):")
        for row in followup:
            flag = " — OVERDUE" if row["overdue"] else ""
            lines.append(f"  - {row['card'].owner.full_name} ({row['card'].member_code}): ${row['balance']:.2f}{flag}")
    else:
        lines.append("- No member has an outstanding balance.")
    pending = Invoice.query.filter_by(status="pending").count()
    lines.append(f"- Pending invoices in total: {pending}.")
    soon = sorted((c for c in FitnessClass.query.all()
                   if datetime.utcnow() <= c.schedule <= datetime.utcnow().replace(hour=23, minute=59) + _ONE_DAY),
                  key=lambda c: c.schedule)
    if soon:
        lines.append("- Classes today and tomorrow:")
        for c in soon:
            lines.append(f"  - '{c.name}' {c.schedule.strftime('%a %H:%M')} ({c.room}), "
                         f"{len(c.confirmed_bookings())}/{c.capacity} booked, trainer {c.trainer.full_name}")
    if user.role == "administrator":
        active = [p for p in PricingPolicy.query.filter_by(status="active").all()]
        if active:
            lines.append("- Active pricing/loyalty offers: " + "; ".join(p.name for p in active[:12]) + ".")
    return "\n".join(lines)


def _context_for(user) -> str:
    if user.role == "member":
        return _member_context(user)
    if user.role == "trainer":
        return _trainer_context(user)
    if user.role in ("receptionist", "administrator"):
        return _front_desk_context(user)
    return "- (No account-specific context is grounded for this role yet; answer from general GymFlow knowledge only.)"


class LLMUnavailable:
    """Returned by _call_llm() when no model can be used right now; ask_agent()
    then answers from the built-in offline assistant instead of failing."""

    def __init__(self, note):
        self.note = note


def ask_agent(user, message: str, history: list) -> str:
    """`history` is a list of {"role": "user"|"assistant", "content": str}
    dicts; the caller owns where it's persisted (routes/assistant.py keeps
    it in the Flask session, per-login-session only -- see that module's
    docstring for why it isn't written to the database). Returns the
    assistant's reply text.
    """
    context = _context_for(user)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        role_label=ROLE_LABELS.get(user.role, "a GymFlow user"),
        name=user.full_name, context=context,
    )
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-MAX_HISTORY_TURNS * 2:])
    messages.append({"role": "user", "content": message})
    reply = _call_llm(messages)
    if isinstance(reply, LLMUnavailable):
        return offline_answer(user, message, context) + "\n\n(" + reply.note + ")"
    return reply


# ---------------------------------------------------------------------------
# Offline assistant: answers the common questions directly from the same
# grounded CONTEXT when no language model is configured or reachable, so the
# chat is still useful (and demonstrable) without an API key.
# ---------------------------------------------------------------------------
_TOPICS = [
    ("rewards", ("point", "tier", "reward", "offer", "loyal", "free class", "discount", "gold", "silver",
                 "bronze", "redeem", "punti", "sconto", "premi")),
    ("billing", ("owe", "invoice", "bill", "pay", "balance", "overdue", "refund", "fattur", "pagare", "debito")),
    ("nutrition", ("calorie", "kcal", "nutrition", "diet", "food", "eat", "protein", "meal", "cibo", "dieta")),
    ("classes", ("class", "book", "schedule", "upcoming", "teach", "session", "waitlist", "lezion", "corso")),
]


def _pick_lines(context, words):
    lines = context.splitlines()
    picked, keep_children = [], False
    for line in lines:
        is_child = line.startswith("  - ")
        if not is_child:
            keep_children = any(word in line.lower() for word in words)
            if keep_children:
                picked.append(line)
        elif keep_children:
            picked.append(line)
    return picked


def offline_answer(user, message, context):
    text = (message or "").lower()
    topic = next((name for name, words in _TOPICS if any(word in text for word in words)), None)
    keywords = {
        "rewards": ("rewards", "points to reach", "benefits", "reward ready", "offers", "spendable"),
        "billing": ("balance", "invoice", "outstanding", "overdue"),
        "nutrition": ("nutrition", "diet plan", "food"),
        "classes": ("upcoming bookings", "no upcoming", "classes you teach", "no upcoming classes",
                    "classes today"),
    }
    if topic:
        picked = _pick_lines(context, keywords[topic])
        if picked:
            intro = {"rewards": "Here is your rewards summary:", "billing": "Here is the billing information:",
                     "nutrition": "Here is your nutrition summary:", "classes": "Here are the classes:"}[topic]
            if user.role != "member" and topic == "rewards":
                intro = "Here is the rewards information I have:"
            return intro + "\n" + "\n".join(picked)
        return "I don't have any information about that in your account right now."
    words = set(re.findall(r"[a-zà-ù]+", text))
    if words & {"hello", "hi", "ciao", "hey", "help", "salve"} or "what can you" in text:
        return (f"Hi {user.name}! I can tell you about your classes, bookings, rewards points and tier, "
                "offers and free classes, invoices and balance, and your nutrition log. What would you like to know?")
    return ("I can answer questions about your classes, rewards and points, offers, invoices and nutrition. "
            "Here is a quick overview:\n" + context)


def _call_llm(messages: list):
    """The one function that actually talks to the LLM provider. Isolated
    here so it can be monkeypatched in tests -- see the module docstring.

    Provider: any OpenAI-compatible Chat Completions API.
    - OpenAI: set OPENAI_API_KEY (model: GYMFLOW_LLM_MODEL, default gpt-4o-mini).
    - Free local model with Ollama: set GYMFLOW_LLM_BASE_URL=http://localhost:11434/v1
      and GYMFLOW_LLM_MODEL=llama3.2 (no key needed).

    Section 4.10 (fault tolerance): the request goes through
    gymflow.resilience.call_with_retry (retry + circuit breaker). If no
    provider is configured, or it keeps failing, this returns LLMUnavailable
    and ask_agent() answers from the offline assistant instead of a 500.
    """
    base_url = os.environ.get("GYMFLOW_LLM_BASE_URL") or None
    api_key = os.environ.get("OPENAI_API_KEY") or ("local" if base_url else None)
    if not api_key:
        return LLMUnavailable("Offline mode: answered directly from your GymFlow data. For full AI chat, "
                              "add OPENAI_API_KEY (or a local GYMFLOW_LLM_BASE_URL) to the .env file.")

    # A local model (Ollama) on a laptop can need a minute for its first
    # answer while it loads, so it gets a longer timeout and fewer retries.
    timeout = float(os.environ.get("GYMFLOW_LLM_TIMEOUT") or (180 if base_url else 30))
    attempts = 1 if base_url else 3

    def _request():
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
        response = client.chat.completions.create(
            model=os.environ.get("GYMFLOW_LLM_MODEL", "gpt-4o-mini"),
            messages=messages, max_tokens=400, temperature=0.4,
        )
        return response.choices[0].message.content

    try:
        return resilience.call_with_retry(_request, service="llm", backoff_seconds=0.3, max_attempts=attempts)
    except resilience.CircuitOpenError:
        return LLMUnavailable("The AI model is temporarily unavailable after repeated errors, so this answer "
                              "comes from the offline assistant.")
    except Exception as exc:  # noqa: BLE001 -- any provider/network failure must degrade, not 500
        return LLMUnavailable(f"Couldn't reach the AI model ({type(exc).__name__}), so this answer comes "
                              "from the offline assistant.")
