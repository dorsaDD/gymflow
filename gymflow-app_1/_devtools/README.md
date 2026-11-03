# _devtools — not part of the application

This folder was used only to smoke-test GymFlow while it was being written,
inside a sandbox that has no access to the Python package index (pip/apt
installs of new packages are blocked there). It is **not a dependency of
the app** and you do not need it to run GymFlow normally — just follow the
"Getting started" section of the top-level README with the real
`requirements.txt`.

- `shim/flask_sqlalchemy.py` — a small, purpose-built, in-memory
  stand-in for the real Flask-SQLAlchemy package, implementing only the
  subset of its API that `gymflow/models.py` and `gymflow/routes/*.py`
  actually use (columns, relationships/backrefs, `filter`/`filter_by`/
  `order_by`/`join`/`get`/`get_or_404`, cascading `delete-orphan`). It
  deliberately cuts corners that would be unsafe in a general-purpose ORM
  but are fine for driving this app's own test scenarios.
- `test_smoke.py` — exercises the whole application through Flask's test
  client: every use case in the report (auth, staff/member registration,
  card modification with CardLock, attachments, progress notes, class
  booking, notifications, password recovery, de-registration), plus role
  permission boundaries, the lock-conflict path, and input validation.
- `test_unit.py` — focused checks for scheduling, recommendations, risk-model
  selection, and security logic.
- `test_loyalty.py` — policy DSL validation, the readable WHEN/THEN rule
  language, tiers, idempotent attendance and payment events, refunds,
  rewards, UI routes, and role isolation.
- `test_loyalty_e2e.py` — uses the REAL Flask-SQLAlchemy + SQLite: upgrades an
  old database file, then drives the admin offer UI, tier perks (Gold monthly
  free class), free-class booking, tier discounts, refunds, limits, roles and
  CSRF through the test client.
- `test_loyalty_concurrency.py` — real SQLite race: two simultaneous
  redemptions cannot spend the same points twice.

To re-run it (from `gymflow-app/`):

```bash
python3 _devtools/test_smoke.py
python3 _devtools/test_unit.py
python3 _devtools/test_loyalty.py
python3 _devtools/test_loyalty_e2e.py
python3 _devtools/test_loyalty_concurrency.py
```

It prints one `[OK]`/`[FAIL]` line per check and exits non-zero if
anything fails. On a machine with normal internet access you can ignore
this folder entirely and just `pip install -r requirements.txt` — the real
Flask-SQLAlchemy will be used automatically since it's a normal PyPI
package.
