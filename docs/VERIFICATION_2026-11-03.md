# Verification — GymFlow, 3 November 2026

Source commit: `251c984c1bfdd2aa4021a8296a5e79e7d6252885` (application under `gymflow-app_1/`). This record concerns the newly uploaded source, not the earlier ZIP. Application code was not changed.

Environment: Linux, Python 3.12.14; isolated environment with `requirements.txt` installed. Direct pins: Flask 3.0.3, Flask-SQLAlchemy 3.1.1, Werkzeug 3.0.4, python-dotenv 1.0.1, scikit-learn 1.7.2, openai 1.54.4, httpx 0.27.2. Resolved SQLAlchemy 2.1.6. Transitive versions are not fully locked.

## Executed source scripts

Each script ran from the application root with that root on `PYTHONPATH`; all exited 0.

### test_smoke

```text
289 checks run, 0 failed.
```

### test_unit

```text
55 checks run, 0 failed.
```

### test_loyalty

```text
68 checks run, 0 failed.
```

### test_loyalty_e2e

```text
68 checks run, 0 failed.
```

### test_loyalty_concurrency

```text
outcomes=['redeemed', 'rejected']; successful=1; redemptions=1; balance=20
CONCURRENT_REDEMPTION_CHECK PASS
```

The first three scripts use an in-memory ORM shim. The E2E script uses real SQLite and a synthetic old-schema fixture, driving admin/member operations, rule validation, tier perks, booking, invoice discounts/refunds, permissions, offline assistant and CSRF through Flask's test client. It is not a browser test. The fixture removes selected newer columns; it does not remove both `photo_filename` and `attended`, so recovery of those historical failures is not directly established by its passing result.

## Additional real-SQLite quota probe

Created a fresh disposable SQLite app, two member accounts with 10 points each, and one active fixed-discount reward costing 1 point with `max_redemptions=1`. Redeemed sequentially for each member; rolled back the rejected operation; counted all reward redemptions.

```text
GLOBAL_CAP ['accepted', 'rejected'] redemptions= 1
CURRENT_SOURCE_GLOBAL_CAP PASS
```

Expected and observed: one accepted, one rejected, one redemption total. `redeem_reward()` now counts non-reversed uses across all accounts for the total limit. The prior archive's per-account/global-cap bug is not a known current failure. This probe does not establish every possible cross-account concurrent quota behavior.

## Boundaries

No source fixes were made. No real customer database was used. No browser screenshot/visual validation, live AI request, payment gateway, SMTP delivery, Docker startup/failover, seed-script execution or load test was performed. Those outcomes are **نیازمند بررسی**. No proof of full FR-19/NFR-5 acceptance or all coursework requirements was available.

The uploaded application source is preserved; this change adds only a root README and this verification record. Older source-import results are superseded by this commit-specific review.
