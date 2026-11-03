# Import verification — 3 November 2026

Source: `GymFlow-loyalty-policy-engine-fixed.zip`, with application code and existing tests unchanged. This record is a point-in-time local verification, not CI or production certification.

## Environment

Linux, Python 3.12.14, isolated virtual environment. Installed `requirements.txt` successfully. Direct pinned dependencies: Flask 3.0.3, Flask-SQLAlchemy 3.1.1, Werkzeug 3.0.4, python-dotenv 1.0.1, scikit-learn 1.7.2, openai 1.54.4, httpx 0.27.2. Resolved SQLAlchemy: 2.1.6. Transitive dependencies are not locked by the source requirements.

## Existing scripts

Ran scripts from the project root with that root on `PYTHONPATH` (the README's `python -m` commands also provide this import context). All five scripts exited 0. The first four use the project's ORM substitute and/or fake services; they must not be described as a full real-database integration suite.

### test_smoke

```text
288 checks run, 0 failed.
```

### test_unit

```text
44 checks run, 0 failed.
```

### test_loyalty

```text
40 checks run, 0 failed.
```

### test_ai_provider

```text
9/9 checks passed; no live API request made.
```

### test_loyalty_concurrency

```text
outcomes=['redeemed', 'rejected']; successful=1; redemptions=1; balance=20
CONCURRENT_REDEMPTION_CHECK PASS
```

## Real SQLite setup and page checks

Set `GYMFLOW_DATABASE_URL` to an absolute SQLite file outside the source tree. Ran `python seed_demo_data.py` twice against that same fresh database; both exited 0. Created authenticated Flask test-client sessions for the seeded admin and three demo members. This checks application responses, not browser appearance or the real login journey.

```text
TIERS [('Bronze', 0), ('Silver', 500), ('Gold', 1200)]
PAGE admin@gymflow.local /members/dashboard 200
PAGE admin@gymflow.local /admin/loyalty/ 200
PAGE mona@example.com /members/dashboard 200
PAGE mona@example.com /loyalty/ 200
PAGE mona@example.com /billing/ 200
TIER mona@example.com Silver 400 650
PAGE leo@example.com /members/dashboard 200
PAGE leo@example.com /loyalty/ 200
PAGE leo@example.com /billing/ 200
TIER leo@example.com Gold 1250 1250
PAGE sara@example.com /members/dashboard 200
PAGE sara@example.com /loyalty/ 200
PAGE sara@example.com /billing/ 200
TIER sara@example.com Bronze 120 120
HEALTH 200
CAP_REDEMPTION mona@example.com accepted
CAP_REDEMPTION leo@example.com accepted
GLOBAL_CAP_RESULT 2 redemptions for limit=1; expected <=1
```

The `GLOBAL_CAP_RESULT` line above is a **failed business invariant**, even though the exploratory script itself exited 0. The expected total is at most one redemption, not two.

## Confirmed defect: cross-member global redemption quota

`gymflow/loyalty_engine.py:redeem_reward()` creates `existing` by filtering `RewardRedemption` on both `account_id` and `reward_id`. The subsequent `max_redemptions` comparison therefore counts only the current account's redemptions. The policy-grant path uses a different query; this finding concerns manual member redemption.

Reproduction on disposable demo data:

1. Create an active invoice reward with type `fixed_discount`, value 1, point cost 1 and `max_redemptions=1`.
2. Use two different funded members (the seeded Mona and Leo).
3. Call `redeem_reward(member, reward)` once for each member.
4. Count `RewardRedemption` rows for that reward across all accounts.
5. Observed: both accepted, count 2. Expected: second rejected, count 1.

This defect is documented, not repaired by the import. The passing existing concurrency test instead verifies that two concurrent redemptions cannot overspend one wallet.

## Other verification boundaries

- All 39 imported Python files passed AST syntax parsing.
- No live AI, SMTP or payment service was called.
- No personal/legacy database was available; migration of the owner's old database remains **نیازمند بررسی**.
- No browser visual check, Docker run, load test or PWA installation was performed: **نیازمند بررسی**.
- No current acceptance evidence establishes the historical FR-19/NFR-5 labels: **نیازمند بررسی**.
- Source scanning found no obvious private-key or API-token patterns in the selected source. This is not a comprehensive secret/security audit.
- Omitted from public import: `outputs/` environment/screenshots and two `.docx` reports. Runtime databases, uploads, caches and virtual environments are excluded.
