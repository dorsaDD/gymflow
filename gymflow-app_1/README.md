# GymFlow — reference implementation

GymFlow is a gym / fitness-club membership management system. This Flask
application is the working code companion to the coursework report
**"GymFlow_Software_Engineering_Coursework.docx"**: it implements every use
case described in that report's Chapter 3, and the code is deliberately
named and commented so it can be read side by side with the report's class
diagram, sequence diagrams, and activity diagrams.

## Tech stack

- Python 3 / Flask (application factory + blueprints)
- Flask-SQLAlchemy (ORM) over SQLite (file database, zero setup)
- Werkzeug password hashing, Flask sessions for authentication
- Server-rendered Jinja2 templates, no JS framework
- A mock "Mail Outbox" stands in for a real SMTP server, so every
  notification-driven use case can be demonstrated without external
  dependencies (see `gymflow/mail_manager.py` and the `/outbox` page)

## Getting started

```bash
cd gymflow-app
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Then open **http://localhost:5000**.

On first run the app creates its SQLite database and seeds a default
administrator account:

| Email                  | Password    |
|-------------------------|-------------|
| admin@gymflow.local     | Admin123!   |

Log in as the administrator to create trainer/receptionist accounts
(Section 3.5.8); receptionists can then register members (3.5.3), trainers
can schedule classes (3.5.11), and so on. Every account created after the
seeded admin gets a temporary password emailed to it — since there is no
real mail server, check the **Mail Outbox** page (visible to
administrators) to read those "emails" and forced password changes.

To start over, stop the server and delete `gymflow.db` (in the project folder, next to `run.py`).

## Loading sample data

A fresh install only has the administrator account. To fill the app with a
realistic gym — staff, eight members, seven weeks of classes and attendance,
invoices, payments, a refund, rewards tiers, offers and points — stop the
server, delete `gymflow.db` (if it exists), and run:

```bash
python seed_demo_data.py
```

Everything is created through GymFlow's own functions by the person who
would really do it: the administrator sets up the rewards programme and
offers, trainers Tara and Marco run the classes and mark attendance (points
are earned through the live offers), receptionist Rita issues invoices and a
refund, and members pay with the mock card processor (no real money), redeem
points and book classes with free-class credits. Running the script again is
safe; nothing is duplicated.

All sample accounts use the password `Demo123!` (administrator: `Admin123!`).

| Account | Role | What to show |
|---|---|---|
| `admin@gymflow.local` | Administrator | **Pricing & Rewards**: tiers, rewards, 14 offers written as rules, change history, a scheduled Black Friday campaign and an ended summer campaign |
| `tara@gymflow.local` / `marco@gymflow.local` | Trainers | Class rosters, attendance, at-risk members, nutrition follow-up |
| `rita@gymflow.local` | Receptionist | Billing: issued, paid, refunded and overdue invoices |
| `leo@example.com` | Member, **Gold** | Gold tier on his card, monthly free class ready to use, 15% Gold renewal discount on his latest invoice |
| `mona@example.com` | Member, **Silver** | 10% reward ready for her pending personal-training invoice, nutrition diary |
| `elena@example.com` | Member, 52, Silver | 10% "active training" discount (8+ classes in 30 days), 50+ free class |
| `giorgio@example.com` | Member, 64, Silver | One-year loyalty bonus, used his 50+ free class to book Gentle Mobility |
| `ivan@example.com` | Member, Bronze | At-risk: many no-shows, refunded class pack, overdue invoice, on a waitlist |
| `sara@`, `paolo@`, `chiara@example.com` | Members, Bronze | Newer members; Paolo got the 5% second-purchase discount |

Offers are written in a readable rule language, e.g.
`WHEN member.tier == "Gold" THEN grant_reward(4)`, and can be created and
edited in the app. Details are in
[`docs/PRICING_AND_LOYALTY.md`](docs/PRICING_AND_LOYALTY.md).

## AI assistant

The **Assistant** page answers questions using the logged-in user's own
data: classes and bookings, rewards tier, points, ready rewards and offers,
invoices and balance, nutrition. Staff get billing follow-up and class info.

It works in three ways — choose one by creating a file called `.env` next to
`run.py`:

1. **Offline mode (no setup).** Without any settings the assistant answers
   the common questions directly from the database. Good for a demo.
2. **OpenAI.** Put your key in `.env`:
   ```
   OPENAI_API_KEY=sk-...
   GYMFLOW_LLM_MODEL=gpt-4o-mini
   ```
3. **Free local model with Ollama.** Install Ollama, run `ollama pull llama3.2`,
   then put in `.env`:
   ```
   GYMFLOW_LLM_BASE_URL=http://localhost:11434/v1
   GYMFLOW_LLM_MODEL=llama3.2
   ```

Restart `python run.py` after changing `.env`. If the model can't be reached,
the assistant falls back to offline mode instead of showing an error.

## Installing GymFlow as a phone app (PWA)

GymFlow is a Progressive Web App: it ships a web manifest (`gymflow/static/manifest.json`)
and a service worker (`gymflow/static/sw.js`), so a phone can install it as a
real app icon instead of just bookmarking a page in the browser.

1. Run the server as above and open it **on your phone's browser**, pointed
   at your machine's address on the same network (e.g. `http://192.168.1.23:5000`,
   not `localhost`) — see the note on HTTPS below.
2. **Android (Chrome):** tap the ⋮ menu → "Add to Home screen" / "Install app".
3. **iPhone (Safari):** tap the Share icon → "Add to Home Screen".
4. The GymFlow icon now opens full-screen, with no browser address bar, and
   shows a friendly offline page (instead of a browser error) if the network
   drops mid-use.

**HTTPS note:** browsers only allow service-worker registration (and therefore
installability) over HTTPS, with one exception: `http://localhost` is treated
as secure for local development. Testing straight from `localhost:5000` on
the same machine works out of the box; testing from an actual phone over
plain HTTP on your LAN will load fine as a normal page but the "Add to Home
Screen" / offline behavior needs a real deployment behind HTTPS (e.g. any
free host with a TLS certificate, or a tool like `ngrok` for a quick demo
tunnel).

What the service worker does — and deliberately does not do: GymFlow is a
session/login-driven app, so the service worker does **not** cache member
data, cards, or bookings for offline use (that data would either go stale or
leak between accounts on a shared device). It only caches the static "app
shell" (stylesheet, icons, manifest) for an instant-loading look, and shows
`gymflow/templates/offline.html` when a page request fails with no network —
see `gymflow/static/sw.js` for the full strategy.

## Project layout

```
gymflow-app/
├── run.py                     entry point
├── requirements.txt
└── gymflow/
    ├── __init__.py             app factory, blueprint registration, error handlers
    ├── config.py                Config (DB URI, upload folder, card-lock timeout)
    ├── extensions.py            the shared `db = SQLAlchemy()` instance
    ├── models.py                ORM models — mirrors the Section 3.6 class diagram
    ├── permissions.py           PermissionsManager: roles, RBAC decorators
    ├── card_manager.py          CardManager: members, cards, attachments, notes, staff
    ├── class_manager.py         ClassManager: fitness classes and bookings
    ├── mail_manager.py          MailManager: mock email delivery -> MailOutbox
    ├── loyalty_engine.py        restricted JSON rule evaluator + rewards wallet
    ├── routes/                  Flask blueprints, one per functional area
    │   ├── auth.py               login, logout, password recovery/change
    │   ├── members.py            dashboard, cards, attachments, notes, history
    │   ├── classes.py            class catalog, scheduling, booking
    │   ├── admin.py               staff accounts, de-registration
    │   ├── loyalty.py             member wallet and administrator policy screens
    │   ├── notifications.py      notification settings
    │   └── outbox.py             mail outbox viewer
    ├── templates/                Jinja2 templates (one per screen)
    └── static/
        ├── css/style.css         stylesheet
        ├── manifest.json         PWA web app manifest
        ├── sw.js                 PWA service worker (app-shell cache + offline fallback)
        └── icons/                app icons (generated by _devtools/generate_icons.py)
```

## Pricing, loyalty, and incentive policies

The configurable pricing and rewards engine provides member points, editable
Rewards Tiers, reward redemption, and administrator-managed policies without
per-offer code changes. Open **Rewards** as a member or **Pricing & Rewards**
as an administrator. See [`docs/PRICING_AND_LOYALTY.md`](docs/PRICING_AND_LOYALTY.md)
for the supported JSON DSL, facts/actions, policy limits, refunds, and
existing-flow integration details.

## Use case → code map

Section numbers refer to Chapter 3 of the report.

| Use case (report §) | Route(s) | Business logic |
|---|---|---|
| 3.5.2 Authentication | `POST /login`, `GET /logout` | `auth.py` (verifies via `User.check_password`) |
| 3.5.3 Member registration & card creation | `GET/POST /members/register` | `card_manager.register_member()` |
| Public self-service sign-up (extends 3.5.3) | `GET/POST /signup` | `card_manager.self_register_member()` |
| 3.5.4 De-registration | `POST /admin/users/<id>/deregister` | `card_manager.deregister_user()` |
| 3.5.5 Password recovery | `GET/POST /password-recovery` | `card_manager.recover_password()` |
| 3.5.6 Password change | `GET/POST /account/password` | `card_manager.change_password()` |
| 3.5.7 Card modification (CardLock) | `GET/POST /members/<id>/edit`, `POST /members/<id>/edit/cancel` | `card_manager.is_locked/acquire_lock/release_lock/modify_card()` |
| 3.5.8 Trainer / Receptionist registration | `GET/POST /admin/staff/new/<role>` | `card_manager.register_staff()` |
| 3.5.9 Document upload | `POST /members/<id>/attachments`, `POST /members/attachments/<id>/delete`, `GET /members/attachments/<id>/download` | `card_manager.upload_attachment/remove_attachment()` |
| 3.5.10 Notification settings | `GET/POST /notifications/settings` | `notifications.py` |
| 3.5.11 Class booking | `GET /classes/`, `GET/POST /classes/new`, `POST /classes/<id>/book`, `POST /classes/bookings/<id>/cancel` | `class_manager.create_class/book_class/cancel_booking()` |
| 3.5.12 Progress notes on card | `POST /members/<id>/notes`, `POST /members/notes/<id>/delete` | `card_manager.add_progress_note/remove_progress_note()` |
| 3.5.13 Workout plan assignment | `POST /members/<id>/workout-plans`, `POST /members/workout-plans/<id>/delete` | `card_manager.assign_workout_plan/remove_workout_plan()` |
| Revision history | `GET /members/<id>/history` | `ChangeHistory` rows written by `modify_card()`, `upload_attachment()` |

Role permissions (who can do what) live in a single table:
`gymflow/permissions.py:PERMISSIONS`, mirroring the actor/use-case
associations in the use case diagram (Figure 1 of the report).

## Design notes worth reading alongside the report

- **CardLock (§3.7.7)** is implemented as two columns directly on `Card`
  (`locked_by_id`, `locked_at`) rather than a separate table, with a
  configurable timeout (`CARD_LOCK_TIMEOUT_SECONDS`, default 5 minutes) so a
  crashed browser tab can't permanently lock a card.
- **Mail is mocked** (`MailOutbox` table + console `print`) instead of
  requiring a real SMTP server — see the "Mail Server" node in the
  deployment diagram (§3.8). Every notification the report describes
  (welcome emails, password recovery, card/attachment/note/workout-plan
  updates, booking confirmations) is generated and can be inspected on the
  `/outbox` page.
- **Model and method names intentionally match the class diagram** (e.g.
  `CardManager`, `ClassManager`, `MailManager`, `PermissionsManager`,
  `checkAvailability()` → `FitnessClass.check_availability()`,
  `isLocked()`/`lock()`/`unlock()` → `card_manager.is_locked/acquire_lock/release_lock()`)
  so the report and the code can be cross-referenced directly.

## Testing

This build was smoke-tested end to end (every use case above, plus role
permission boundaries, the CardLock conflict path, and input validation)
using Flask's test client. If you'd like to re-run that suite yourself, see
`_devtools/README.md` for how it's wired up — note that it substitutes a
tiny in-memory stand-in for Flask-SQLAlchemy purely to work around this
authoring sandbox's lack of package-index network access; your own machine
should just use the real dependencies from `requirements.txt` as normal.

For regression and loyalty coverage, run all three scripts from the project
directory:

```bash
python3 _devtools/test_smoke.py
python3 _devtools/test_unit.py
python3 _devtools/test_loyalty.py
```
