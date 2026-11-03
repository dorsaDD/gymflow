"""Real SQLite concurrency check for competing reward redemptions.

Requires the normal app dependencies (Flask-SQLAlchemy), not the test shim.
Run: python3 _devtools/test_loyalty_concurrency.py
"""
import os
import tempfile
import threading

from gymflow import create_app, loyalty_engine
from gymflow.config import Config
from gymflow.extensions import db
from gymflow.models import Card, LoyaltyAccount, PointsLedgerEntry, Reward, RewardRedemption, User


fd, database_path = tempfile.mkstemp(prefix="gymflow-loyalty-", suffix=".sqlite")
os.close(fd)


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"
    UPLOAD_FOLDER = tempfile.mkdtemp(prefix="gymflow-uploads-")


app = create_app(TestConfig)
with app.app_context():
    admin = User.query.filter_by(role="administrator").first()
    member = User(role="member", name="Concurrent", surname="Member", email="concurrent@example.test")
    member.set_password("Testpass123!")
    db.session.add(member)
    db.session.flush()
    card = Card(user_id=member.id, member_code="GF-CONCURRENT")
    db.session.add(card)
    db.session.flush()
    account = LoyaltyAccount(card_id=card.id, balance=100, lifetime_points=100)
    reward_a = Reward(name="Concurrent A", reward_type="fixed_discount", points_cost=80,
                      value=5, target="invoice", active=True, created_by_id=admin.id)
    reward_b = Reward(name="Concurrent B", reward_type="fixed_discount", points_cost=80,
                      value=5, target="invoice", active=True, created_by_id=admin.id)
    db.session.add_all([account, reward_a, reward_b])
    db.session.commit()
    member_id, card_id = member.id, card.id
    reward_ids = [reward_a.id, reward_b.id]

barrier = threading.Barrier(2)
original_get_account = loyalty_engine.get_account


def synchronized_get_account(card, create=True, for_update=False):
    account = original_get_account(card, create=create, for_update=for_update)
    if for_update:
        # Force both workers to have read the same starting balance before
        # either attempts to debit it.
        barrier.wait(timeout=10)
    return account


loyalty_engine.get_account = synchronized_get_account
outcomes = []
outcome_lock = threading.Lock()


def redeem(reward_id):
    try:
        with app.app_context():
            member = User.query.get(member_id)
            reward = Reward.query.get(reward_id)
            loyalty_engine.redeem_reward(member, reward)
            result = "redeemed"
    except loyalty_engine.ValidationError:
        result = "rejected"
    except Exception as exc:  # the test must surface unexpected persistence failures
        result = f"unexpected:{type(exc).__name__}"
    with outcome_lock:
        outcomes.append(result)


threads = [threading.Thread(target=redeem, args=(reward_id,)) for reward_id in reward_ids]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=20)

with app.app_context():
    final_balance = LoyaltyAccount.query.filter_by(card_id=card_id).first().balance
    redemptions = RewardRedemption.query.filter_by(account_id=LoyaltyAccount.query.filter_by(card_id=card_id).first().id).all()
    ledger = PointsLedgerEntry.query.filter_by(account_id=LoyaltyAccount.query.filter_by(card_id=card_id).first().id).all()
    successful = sum(1 for item in outcomes if item == "redeemed")
    passed = (not any(item.startswith("unexpected:") for item in outcomes)
              and successful == 1 and len(redemptions) == 1 and final_balance == 20
              and sum(item.points for item in ledger) == -80)
    print(f"outcomes={outcomes}; successful={successful}; redemptions={len(redemptions)}; balance={final_balance}")
    print("CONCURRENT_REDEMPTION_CHECK", "PASS" if passed else "FAIL")
    if not passed:
        raise SystemExit(1)

loyalty_engine.get_account = original_get_account
with app.app_context():
    db.session.remove()
    app.extensions["sqlalchemy"].engine.dispose()
os.unlink(database_path)
