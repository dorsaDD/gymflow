# GymFlow

Gym/fitness-club management coursework application using Flask, Flask-SQLAlchemy, SQLite and Jinja2.

## Verified scope — 3 November 2026

Before this import, `main` contained only a 44-byte README at commit `056ee68cd6caa089219a208266f84f668adf956c` (21 October 2026), with one branch and no open issues. This branch imports source from `GymFlow-loyalty-policy-engine-fixed.zip` and documents what can actually be checked.

Application source, templates, icons and existing test scripts are unchanged. This import replaces README, expands `.gitignore`, and adds a verification record. Local environment settings, generated screenshots and Word reports are excluded; no user database or uploaded member documents are included.

**نیازمند بررسی — Needs verification** identifies an unverified claim or activity. Whether this archive is the owner's latest local working version is **نیازمند بررسی**. Historical reports and earlier test totals are not current verification evidence.

## Features and source evidence

| Area | Present implementation | Evidence |
|---|---|---|
| Accounts | Login, signup, password recovery/change; member, trainer, receptionist and administrator roles | `routes/auth.py`, `card_manager.py`, `permissions.py` |
| Membership cards | Registration, edits/locking, photos, attachments, progress notes, workout plans, change history | `models.py`, `card_manager.py`, `routes/members.py` |
| Classes | Scheduling, booking/cancellation, waitlist and staff-recorded attendance | `class_manager.py`, `routes/classes.py` |
| Nutrition / insights | Diet plans, food/exercise logs, goals, achievement badges, recommendations, risk-model code | `nutrition_manager.py`, `achievements.py`, `recommendation.py`, `risk_model.py` |
| Billing | Generic invoices, mock card payments, discounts, refunds | `payment_manager.py`, `routes/billing.py` |
| Notifications | Preferences and mock mail outbox | `mail_manager.py`, `routes/notifications.py`, `routes/outbox.py` |
| Loyalty | JSON policies, tiers, points ledger, rewards, admin audit history | `loyalty_engine.py`, `routes/loyalty.py`, `models.py` |
| AI | Ollama-default adapter and explicit cloud-provider option, account-specific context | `ai_agent.py`, `routes/assistant.py` |
| PWA / deployment | Manifest, icons, service worker, health endpoint, Docker/Gunicorn/nginx demo files | `static/`, `__init__.py`, `Dockerfile`, `docker-compose.yml` |

Paths above are relative to `gymflow/` except deployment files. Presence in source is not a production-readiness claim. Real SMTP/payment integration, real-world ML quality, live AI, browser/PWA behavior and container failover are **نیازمند بررسی**.

## Structure

| Path | Purpose |
|---|---|
| `run.py` | Development entry point |
| `requirements.txt`, `.env.example` | Dependencies and sample configuration |
| `gymflow/__init__.py` | App factory, routes, database creation, additive migrations, admin/tier seed |
| `gymflow/config.py`, `extensions.py`, `models.py` | Settings, shared ORM and data models |
| `gymflow/routes/`, `templates/`, `static/` | Blueprints, Jinja pages and browser assets |
| `gymflow/*_manager.py` | Membership, classes, nutrition, mail and mock payments |
| `gymflow/loyalty_engine.py` | Rule interpreter, tiers, ledger and rewards |
| `gymflow/security.py`, `permissions.py`, `resilience.py` | CSRF/login controls, RBAC, retry/circuit-breaker helpers |
| `seed_demo_data.py` | Sample users, classes, invoices, policies, rewards and points |
| `_devtools/test_*.py`, `_devtools/shim/` | Existing checks and in-memory ORM substitute |
| `docs/` | Design documents and current verification record |
| `Dockerfile`, `docker-compose.yml`, `requirements-docker.txt`, `nginx/` | Deployment demonstration |

## Local setup

Tested with Python 3.12 and the pinned direct dependencies in `requirements.txt`. Other Python versions are **نیازمند بررسی**.

```bash
git clone --branch import/gymflow-verified-2026-11-03 https://github.com/dorsaDD/gymflow.git
cd gymflow
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python -m flask --app run:app run --host 127.0.0.1 --port 5000
```

Windows PowerShell equivalents: `py -3 -m venv .venv`, `.\.venv\Scripts\Activate.ps1`, and `Copy-Item .env.example .env`. Open <http://127.0.0.1:5000>.

The existing `python run.py` also works as an entry point, but enables debug mode and binds to all interfaces. The command above binds locally. Production hosting is **نیازمند بررسی**.

Startup creates `gymflow.db` **at the project root**, unless `GYMFLOW_DATABASE_URL` overrides it. Uploads are stored under `instance/uploads/`. Initial administrator: `admin@gymflow.local` / `Admin123!`. These are demo credentials: change the password and set a unique `GYMFLOW_SECRET_KEY` before any shared deployment.

A fresh database contains an admin and sample tiers, but no populated reward catalogue or policies. To populate a disposable demo database:

```bash
python seed_demo_data.py
```

| Demo account | Password | Tier / screen |
|---|---|---|
| `admin@gymflow.local` | `Admin123!` | Pricing & Rewards: `/admin/loyalty/` |
| `mona@example.com` | `Demo123!` | Silver; `/loyalty/`, `/billing/` |
| `leo@example.com` | `Demo123!` | Gold; `/loyalty/`, `/billing/` |
| `sara@example.com` | `Demo123!` | Bronze; `/loyalty/` |

Two seed runs against the same fresh SQLite database completed successfully. Compatibility with the owner's pre-existing database is **نیازمند بررسی**. Back it up before running newer source; do not delete it to bypass schema errors.

## Loyalty policy engine

Administrators configure validated JSON policies in `/admin/loyalty/`: conditions, action, dates, priority, status and usage limits. The UI includes condition previews and an audit history. `PolicyAudit` stores change snapshots.

| Event | Application connection |
|---|---|
| `class_attended` | Staff confirms attendance; later cancellation/no-show corrections can reverse points |
| `invoice_paid` | Successful mock invoice payment; discounts and points/rewards |
| `member_activity` | Evaluation during member dashboard activity, without a background scheduler |

Supported actions: `award_points`, `discount_percent`, `discount_fixed`, `grant_reward`. Supported comparisons: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`. Conditions are ANDed, using allow-listed facts such as age, tier, account age, attendance and invoice history. Policy text is not executed as Python.

Example for a `class_attended` policy:

```json
{
  "conditions": [],
  "action": {"type": "award_points", "points": 10}
}
```

New offers composed from supported facts/actions can be created without Python edits. New facts, event types or behaviors require code changes. A separate supplier/plugin framework, or compliance with the professor's complete “no base-code changes” requirement, is **نیازمند بررسی**. The present implementation is an interpreter integrated into existing managers/routes.

Invoice discounts do not stack. The first matching discount policy by ascending priority then policy ID wins; a selected reward discount is used only if no policy discount matches. Effective discounts are capped at 50%. Points/reversals are recorded in `PointsLedgerEntry`; event/policy keys prevent repeated awards in the tested flows.

### Bronze / Silver / Gold

| Default tier | Minimum lifetime qualifying points |
|---|---:|
| Bronze | 0 |
| Silver | 500 |
| Gold | 1,200 |

These editable defaults are seeded in `_seed_loyalty_tiers()` in `gymflow/__init__.py`. Tier selection uses `LoyaltyAccount.lifetime_points`; spendable points use `balance`. Redemption/reversal can reduce balance without lowering lifetime tier points. The tier `benefits` text does not automatically grant a discount: configure a policy/reward for that.

Rewards tiers are distinct from the bronze/silver/gold achievement badges in `achievements.py`. They are not paid subscription plans. The schema has generic invoices, no membership subscription start/end lifecycle; `member.active_days` means days since account creation. Points do not expire. A free-class credit is tracked as an entitlement; the current booking flow has no price/checkout to discount.

## Test status

From the project root, with the virtual environment activated:

```bash
python -m _devtools.test_smoke
python -m _devtools.test_unit
python -m _devtools.test_loyalty
python -m _devtools.test_ai_provider
python -m _devtools.test_loyalty_concurrency
```

| Existing script | Observed result, 3 November 2026 | Scope |
|---|---:|---|
| `test_smoke.py` | 288 checks, 0 failed | Flask test client, in-memory ORM shim |
| `test_unit.py` | 44 checks, 0 failed | Unit/security checks; shim-backed app |
| `test_loyalty.py` | 40 checks, 0 failed | Policy, rewards, authorization; ORM shim |
| `test_ai_provider.py` | 9/9 passed | Fake model client, no live API request |
| `test_loyalty_concurrency.py` | PASS | Real SQLite: one accepted redemption, one rejected; balance 20 |

Additional real-SQLite checks passed: database creation, two demo-seed runs, admin/member dashboard and loyalty pages, member billing pages and `/health`. Demo tiers matched the table above. Page checks used authenticated Flask test-client sessions, not a browser. Python syntax was parsed successfully for all 39 imported Python files.

**Passing existing scripts does not establish that the app is defect-free.** An extra cross-member test reproduced the global reward-cap defect below. No CI workflow is included. Coverage percentage, full browser end-to-end testing, sustained load and production operation are **نیازمند بررسی**.

See [the verification record](docs/VERIFICATION_2026-11-03.md) for environment, results and the defect reproduction.

## Known defects and limitations

| Item | Evidence and status |
|---|---|
| Global reward redemption cap | **Confirmed defect:** `redeem_reward()` counts prior redemptions by both account and reward when enforcing `max_redemptions`. Two different members redeemed a reward with global limit 1 in real SQLite. Do not rely on that field as a global quota until fixed. The existing concurrency test checks one wallet's balance, not this case. |
| `cards.photo_filename` / `bookings.attended` missing in old databases | `_migrate_schema()` includes additive updates for these fields. Fresh startup passed; compatibility/repair of the owner's old database is **نیازمند بررسی** because that database was not available. |
| Historical FR-19 / NFR-5 status | Current completion against those requirement labels is **نیازمند بررسی**; past reports are not acceptance evidence. |
| Development defaults | Default admin password, fallback secret and `run.py` debug mode are present. Production hardening is **نیازمند بررسی**. |
| Deprecations | `Query.get()` emits SQLAlchemy legacy-API warnings; these did not fail the checks. |
| Membership / payments | No subscription lifecycle; generic invoices and float amounts; payments are mocked. |
| AI | Local Ollama defaults exist; live local/cloud responses and configured cloud model availability are **نیازمند بررسی**. |
| Deployment | Docker failover, PWA installation, HTTPS hosting and other database backends are **نیازمند بررسی**. |

## Configuration and documents

`.env` and then legacy `outputs/env` are loaded without overriding existing environment variables. Only `.env.example` is tracked. Core settings: `GYMFLOW_SECRET_KEY` and `GYMFLOW_DATABASE_URL`. Loyalty does not require an AI provider.

`.env.example` selects Ollama (`qwen2.5:3b`, `http://localhost:11434/v1`). Install/run that local model separately to use the assistant. A stopped/missing server produces setup guidance. A cloud provider requires explicitly setting `GYMFLOW_LLM_PROVIDER=openai` and supplying an API key. No live provider call was made for this verification.

- [Pricing and loyalty](docs/PRICING_AND_LOYALTY.md)
- [Security](docs/SECURITY.md)
- [Algorithms and ML](docs/ALGORITHMS_AND_ML.md)
- [Fault-tolerance demo](docs/FAULT_TOLERANCE_DEMO.md)

Existing documents contain design intent as well as implementation descriptions. Where they conflict with the verification record, the conflicting claim is **نیازمند بررسی**. No license file was included in the archive; licensing terms are **نیازمند بررسی**.
