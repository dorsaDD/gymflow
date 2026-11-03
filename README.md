# GymFlow

Gym and fitness-club management coursework application built with Flask, Flask-SQLAlchemy, SQLite and Jinja2. **The application lives in `gymflow-app_1/`.**

## Verified scope

Reviewed **3 November 2026**, against commit [`251c984`](https://github.com/dorsaDD/gymflow/commit/251c984c1bfdd2aa4021a8296a5e79e7d6252885). That commit uploaded the application to `main`; the repository is no longer README-only. This documentation update preserves those application files.

**نیازمند بررسی — Needs verification** marks a claim that the checked source and observed results do not establish. Full course-requirement completion, production readiness and whether this is the owner's latest local copy remain **نیازمند بررسی**. Test results for an earlier ZIP are not evidence about this commit.

## Existing capabilities

Paths in this table are relative to `gymflow-app_1/gymflow/`.

| Area | Present implementation | Source evidence |
|---|---|---|
| Accounts and permissions | Login, signup, password recovery/change; member, trainer, receptionist, administrator | `routes/auth.py`, `card_manager.py`, `permissions.py` |
| Membership cards | Registration, edits/locking, photos, attachments, notes, workout plans, history; rewards tier on card | `models.py`, `card_manager.py`, `routes/members.py`, `templates/card_detail.html` |
| Classes | Scheduling, categories, bookings/cancellations, waitlist, confirmed attendance, free-class credit selection | `class_manager.py`, `routes/classes.py` |
| Nutrition | Plans, food/exercise logs, goals and follow-up | `nutrition_manager.py`, `routes/diet.py` |
| Insights | Achievement badges, recommendations, attendance-risk modelling, K-Means member segmentation | `achievements.py`, `recommendation.py`, `risk_model.py`, `segmentation.py` |
| Billing | Categorized invoices, mock card payments, discounts and refunds | `payment_manager.py`, `routes/billing.py`, `models.py` |
| Notifications | Preferences and mock mail outbox | `mail_manager.py`, `routes/notifications.py`, `routes/outbox.py` |
| Loyalty | Configurable rules, tiers, points ledger, rewards, policy preview and audit history | `loyalty_engine.py`, `routes/loyalty.py`, `models.py` |
| Assistant | Answers from current-user data offline; optional OpenAI-compatible model adapter | `ai_agent.py`, `routes/assistant.py` |
| PWA / deployment demo | Manifest, icons, service worker, health endpoint; Docker/Gunicorn/nginx files | `static/`, `__init__.py`; deployment files in app root |

Payments and mail are mocked; no live money transfer or SMTP delivery is established. Real-world ML accuracy, live AI response quality, PWA installation and deployment resilience are **نیازمند بررسی**.

## Repository structure

| Path | Purpose |
|---|---|
| `gymflow-app_1/run.py` | Development entry point |
| `gymflow-app_1/requirements.txt` | Direct Python dependency pins |
| `gymflow-app_1/gymflow/__init__.py` | App factory, blueprints, database creation, additive migration and seed |
| `gymflow-app_1/gymflow/config.py`, `extensions.py`, `models.py` | Configuration, ORM and models |
| `gymflow-app_1/gymflow/routes/`, `templates/`, `static/` | Routes, server-rendered pages and assets |
| `gymflow-app_1/gymflow/loyalty_engine.py` | Rule parser/interpreter, wallets, tiers and rewards |
| `gymflow-app_1/gymflow/*_manager.py` | Membership, classes, nutrition, mail and mock-payment logic |
| `gymflow-app_1/_devtools/` | Five existing test scripts, icon tools and an ORM shim |
| `gymflow-app_1/seed_demo_data.py` | Sample accounts and activity through application functions |
| `gymflow-app_1/docs/` | Existing design documents |
| `gymflow-app_1/Dockerfile`, `docker-compose.yml`, `nginx/` | Deployment demo (paths relative to app root after the first entry) |
| `gymflow-app_1/outputs/`, `GymFlow_Extensions_Summary.docx` | Uploaded screenshots and report (report is in app root) |
| `docs/VERIFICATION_2026-11-03.md` | Verification of the exact source commit |

## Run locally

Checked with **Python 3.12.14** and the dependencies in `requirements.txt`. Other Python versions are **نیازمند بررسی**.

```bash
git clone https://github.com/dorsaDD/gymflow.git
cd gymflow/gymflow-app_1
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m flask --app run:app run --host 127.0.0.1 --port 5000
```

On Windows PowerShell use `py -3 -m venv .venv` and `.\.venv\Scripts\Activate.ps1` for environment creation/activation. Open <http://127.0.0.1:5000>.

The existing `python run.py` is also an entry point, but enables debug mode and listens on all interfaces. The command above binds locally. Production hosting is **نیازمند بررسی**.

Startup creates `gymflow.db` next to `run.py`, unless `GYMFLOW_DATABASE_URL` overrides it. Uploads go under `instance/uploads/`. Initial admin: **`admin@gymflow.local` / `Admin123!`**. These are demo credentials. Change the password and set a unique `GYMFLOW_SECRET_KEY` before a shared deployment.

The source uses `load_dotenv()`. An optional `.env` next to `run.py` can contain `GYMFLOW_SECRET_KEY` and `GYMFLOW_DATABASE_URL`; do not commit secrets, local databases, uploads or virtual environments. This uploaded snapshot contains no `.env.example` or `.gitignore`.

A fresh database seeds the administrator and tier defaults. To populate a separate/disposable demo database, from the app directory run:

```bash
python seed_demo_data.py
```

The script contains staff/member accounts, class history, invoices, rewards and readable policies. Sample passwords: `Demo123!` for demo staff/members, `Admin123!` for the seeded admin. Intended demo accounts include Mona/Silver (`mona@example.com`), Leo/Gold (`leo@example.com`) and Sara/Bronze (`sara@example.com`). The seed script itself was not executed in this verification: its complete output and rerun behavior are **نیازمند بررسی**.

Admin loyalty page: `/admin/loyalty/` (**Pricing & Rewards**). Member wallet: `/loyalty/`; invoices: `/billing/`. Back up an existing database before upgrading; do not delete personal data to bypass schema errors. The nested older README's folder name `gymflow-app` differs from the actual `gymflow-app_1`; use the path above.

## Loyalty policy engine

Administrators can create/edit offers, preview conditions, set dates, priorities and usage limits, and activate/pause/expire policies through the UI. `PolicyAudit` records changes. Definitions are stored as validated JSON and evaluated by an allow-listed interpreter; the human-readable rule parser builds that structure without executing user Python.

| Event | Application behavior |
|---|---|
| `class_attended` | Points/rewards after staff confirms attendance; booking alone earns no points |
| `invoice_paid` | Discounts and points/rewards in the successful mock-payment flow |
| `member_activity` | Dashboard-triggered monthly activity key; configurable monthly perks or one-time milestones |

Example readable rule for a `class_attended` policy:

```text
WHEN (class.category == "yoga" OR class.category == "mobility")
THEN award_points(20)
```

Actions include `award_points`, percentage/fixed `apply_discount`, and `grant_reward` / `grant_free_class` with an existing reward ID. JSON actions are `award_points`, `discount_percent`, `discount_fixed`, `grant_reward`. Supported comparisons include equality, inequality, ordered numeric comparisons and `IN`; conditions support AND and limited OR groups.

Facts include member age/tier, days since account creation, attendance, paid-invoice history, membership-purchase count, invoice/class category and member segment. Supported categories and facts are allow-listed in `loyalty_engine.py` and `models.py`.

Offers built from supported facts/actions do not require Python changes. Adding new facts, actions or events still requires implementation changes. A separate supplier/plugin framework, or compliance with the professor's complete “no base-code changes” requirement, is **نیازمند بررسی**.

Invoice discounts do not stack: the first matching discount policy by ascending priority then policy ID wins; a selected reward discount is used if no policy discount matched. Effective discounts are capped at 50%. Ledger records track earns, spends and reversals. Policy/event keys prevent duplicate awards in the checked flows. Refunds and attendance corrections have reversal paths.

### Bronze / Silver / Gold

| Default tier | Minimum lifetime qualifying points |
|---|---:|
| Bronze | 0 |
| Silver | 500 |
| Gold | 1,200 |

Defaults come from `_seed_loyalty_tiers()` in `gymflow/__init__.py`; an administrator can edit them. Tiers use `LoyaltyAccount.lifetime_points`, while spendable points use `balance`. Redemption and reversals do not lower lifetime tier points. Rewards tiers appear on the card, dashboard and wallet and are separate from achievement badges.

Tier benefit text is descriptive. Actual benefits come from policies/rewards: the demo source includes Gold monthly free-class and tier-based membership-renewal discounts. Renewal eligibility is inferred from prior paid membership-category invoices; a full subscription start/end lifecycle is absent. `member.active_days` is account age. Points do not expire. Free-class credits are tracked entitlements; ordinary class booking has no monetary checkout.

## Tests — observed results

From `gymflow-app_1/`, with dependencies installed:

```bash
python _devtools/test_smoke.py
python _devtools/test_unit.py
python _devtools/test_loyalty.py
python _devtools/test_loyalty_e2e.py
python _devtools/test_loyalty_concurrency.py
```

| Script | Result on 3 November 2026 | Scope |
|---|---:|---|
| `test_smoke.py` | 289 checks, 0 failed | Flask test client + in-memory ORM shim |
| `test_unit.py` | 55 checks, 0 failed | Unit/security/segmentation checks; shim-backed app |
| `test_loyalty.py` | 68 checks, 0 failed | Rules, rewards, roles; ORM shim |
| `test_loyalty_e2e.py` | 68 checks, 0 failed | Real Flask-SQLAlchemy + SQLite; synthetic old schema, admin/member flows, tiers/perks, discounts/refunds, offline assistant, CSRF |
| `test_loyalty_concurrency.py` | PASS | Real SQLite, one accepted redemption and one rejected, final balance 20 |

That is **480 assertions in four scripts plus the separate concurrency check**, not 480 browser tests. An additional real-SQLite cross-member global-cap probe also passed (one accepted, second rejected). The earlier archive's global-cap defect was **not reproduced in this commit**; the relevant query now counts across accounts.

No CI workflow is present. Test coverage percentage, full browser testing, live AI, Docker failover and sustained load are **نیازمند بررسی**. Details: [verification record](docs/VERIFICATION_2026-11-03.md).

## Known issues and verification gaps

| Item | Evidence / status |
|---|---|
| Historical missing-column errors (`cards.photo_filename`, `bookings.attended`) | Current `_migrate_schema()` generically adds missing model columns. The synthetic old-schema suite passes, but upgrading the owner's actual old database is **نیازمند بررسی**; it was not available. The fixture does not specifically remove both of these columns. |
| Migration scope | Additive columns/tables are implemented. General rename/type/constraint migration guarantees are **نیازمند بررسی**. |
| Existing documentation mismatch | Nested README uses a different folder name and describes eight demo members, while the seed source defines fourteen. Follow actual paths/source. |
| Default configuration | Hardcoded demo administrator password, fallback secret, debug development server and demo Docker secret exist. Production hardening is **نیازمند بررسی**. |
| Deprecation warnings | Tests emit SQLAlchemy `Query.get()` legacy warnings; observed scripts still exit 0. |
| Billing / membership | Mock payments, float monetary amounts, no full subscription lifecycle. |
| FR-19 / NFR-5 and overall completion | Status against historical requirement labels is **نیازمند بررسی**; test success alone does not establish full coursework compliance. |
| API / deployment | Live provider availability, browser/PWA installation, HTTPS deployment and alternative databases are **نیازمند بررسی**. |
| Licensing | No license file is present; licensing terms are **نیازمند بررسی**. |

No functional failure occurred in the executed scripts or targeted global-cap probe. That is not a claim that the application has no other defects.

## Optional assistant configuration

With no key or model endpoint configured, the assistant answers supported questions directly from the user's GymFlow data. Offline reward/billing answers are exercised by `test_loyalty_e2e.py`.

The current adapter reads `OPENAI_API_KEY`, optional `GYMFLOW_LLM_BASE_URL`, `GYMFLOW_LLM_MODEL` and `GYMFLOW_LLM_TIMEOUT`. Providing a key can enable paid API requests. A local OpenAI-compatible endpoint can be configured instead; install/run that model separately. The adapter falls back to offline answers on an unavailable provider. No live model was called during verification; no AI configuration is needed for loyalty.

## Existing design documents

- [Pricing and loyalty](gymflow-app_1/docs/PRICING_AND_LOYALTY.md)
- [Security](gymflow-app_1/docs/SECURITY.md)
- [Algorithms and ML](gymflow-app_1/docs/ALGORITHMS_AND_ML.md)
- [Fault-tolerance demonstration](gymflow-app_1/docs/FAULT_TOLERANCE_DEMO.md)

These documents include design intent. Claims beyond the checked code and observed results remain **نیازمند بررسی**.
