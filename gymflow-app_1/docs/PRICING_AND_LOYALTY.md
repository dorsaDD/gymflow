# Pricing, loyalty, and incentive policies

GymFlow lets an administrator create promotions, loyalty rewards, discounts
and point rules from the **Pricing & Rewards** page, without changing Python
code for each offer. Rules are written in a small readable rule language
(or as JSON), checked against a fixed whitelist, stored as validated JSON,
and evaluated by a restricted interpreter. Policy text is never executed.

## Where the feature connects

- `gymflow/loyalty_engine.py` — rule language parser, validation, evaluation,
  tiers, points ledger, reward redemption, member offer list.
- `gymflow/routes/loyalty.py` — member wallet (`/loyalty/`) and admin screens
  (`/admin/loyalty/`).
- `gymflow/payment_manager.py` / `routes/billing.py` — invoice discounts are
  applied only during a successful payment. A declined payment uses no reward
  and earns no points.
- `gymflow/class_manager.py` / `routes/classes.py` — attendance points only
  after staff mark a confirmed booking attended; free-class credits are used
  in the normal booking form.
- `gymflow/routes/members.py` — the membership card and member dashboard show
  the member's Rewards Tier; a dashboard visit runs the monthly
  `member_activity` check (tier perks, time-based bonuses).
- Models (`gymflow/models.py`): `LoyaltyTier`, `LoyaltyAccount`,
  `PointsLedgerEntry`, `PricingPolicy`, `PolicyApplication`, `PolicyAudit`,
  `Reward`, `RewardRedemption`, plus `FitnessClass.category` and
  `Invoice.category`.

## Membership level (Rewards Tier) on the card

Each member's Rewards Tier (Bronze / Silver / Gold by default) is shown:

- on the digital **membership card** (tier chip + spendable points) and a
  "Membership level & rewards" panel under it (tier benefits, progress,
  rewards unlocked by the tier);
- on the member **dashboard** ("Your rewards" panel);
- in the **Rewards wallet**.

Rewards Tiers are labelled "… Rewards Tier" and drawn as pill chips.
Achievement badges are labelled "… Achievement" and keep their own round
style, so "Gold Rewards Tier" and "Gold Achievement" are never confused.

The tier is based on **lifetime qualifying points**. Redeeming points lowers
the spendable balance, not the tier. Points do not expire in this version.
Tier names, thresholds and benefit text are editable by the administrator
(sample values: Bronze 0, Silver 500, Gold 1,200).

## Rule language

```
WHEN <condition>
AND <condition>
THEN <action>
```

- `WHEN event == "class_attended"` as the first line is optional; if written,
  it must match the trigger selected in the form.
- Conditions are joined with `AND`. Use brackets for `OR`:
  `(class.category == "yoga" OR class.category == "mobility")`.
- A rule with no conditions is just `THEN award_points(10)`.
- Comparisons: `==`, `!=`, `>`, `>=`, `<`, `<=`, `IN ["a", "b"]`.
- Text values in quotes.

Actions:

| Action | Meaning |
|---|---|
| `award_points(10)` | Add points (whole number, 1–100,000). |
| `apply_discount(percent=5)` | % off the invoice (max 50%). |
| `apply_discount(amount=10)` | Fixed amount off (capped at 50% of the invoice). |
| `apply_discount(percent=10, target="membership")` | Only for invoices of that type (`membership`, `class_pack`, `personal_training`, `other`). |
| `grant_reward(3)` / `grant_free_class(3)` | Give reward #3 (e.g. a free class credit) without spending points. |

Discounts only work with the `invoice_paid` trigger.

The parser is a hand-written tokenizer + recursive-descent parser that only
builds the JSON structure below. Anything else (`;`, function calls other
than the five actions, unknown facts, wrong value types, discounts over the
cap, facts that don't exist for the trigger) is rejected with a clear
message. There is no `eval`, `exec`, SQL or user code anywhere.

### Stored JSON form

The same rule is stored like this (and can also be typed as JSON):

```json
{
  "conditions": [
    {"fact": "member.tier", "op": "eq", "value": "Gold"},
    {"any": [
      {"fact": "class.category", "op": "eq", "value": "yoga"},
      {"fact": "class.category", "op": "eq", "value": "mobility"}
    ]}
  ],
  "action": {"type": "award_points", "points": 20}
}
```

`any` is an OR group (2–8 comparisons, one level only). Max 8 conditions.

### Triggers

| Trigger | When it runs |
|---|---|
| `class_attended` | Staff marks a confirmed booking as attended. Booking alone earns nothing. |
| `invoice_paid` | A payment succeeds (discounts are calculated here). |
| `member_activity` | When the member opens the dashboard — **at most once per calendar month** per member. Monthly tier perks renew each month; set "max uses per member = 1" for a one-time bonus. |

### Facts

| Fact | Meaning | Trigger |
|---|---|---|
| `member.age` | Age from card birthdate (unknown never matches) | all |
| `member.tier` | Current Rewards Tier name | all |
| `member.active_days` | Days since the account was created | all |
| `member.attended_classes` | Attended classes, lifetime | all |
| `member.attended_classes_month` | Attended classes in the class's calendar month | all |
| `member.attended_classes_30d` | Attended classes in the last 30 days | all |
| `member.spending_total` | Earlier paid invoices (after discounts) | all |
| `member.spending_total_including_invoice` | Same plus the current invoice | all |
| `member.purchase_count` | Earlier paid invoices | all |
| `member.membership_purchases` | Earlier paid **membership** invoices (≥1 = renewal) | all |
| `member.days_since_first_purchase` | Days since first paid invoice | all |
| `member.days_since_last_purchase` | Days since last paid invoice (none = unknown) | all |
| `invoice.amount` | Invoice face value | `invoice_paid` |
| `invoice.category` | `membership`, `class_pack`, `personal_training`, `other` | `invoice_paid` |
| `class.category` | `general`, `yoga`, `mobility`, `strength`, `cardio`, `hiit`, `spin`, `pilates` | `class_attended` |
| `member.segment` | ML segment from K-Means: `Champions`, `Loyal regulars`, `New members`, `Occasional`, `At risk` (see ALGORITHMS_AND_ML.md) | all |

GymFlow does not store location data, so there are no location facts (no
location tracking was added).

### Examples (the professor's list)

```
1. 10 points per attended class            (class_attended)
THEN award_points(10)

2. 50 bonus at 8 classes in one month      (class_attended)
WHEN member.attended_classes_month == 8 THEN award_points(50)

3. Discount on 2nd purchase within 30 days (invoice_paid, max 1 per member)
WHEN member.purchase_count == 1 AND member.days_since_first_purchase <= 30
THEN apply_discount(percent=5)

4. Free class for ages 45–60, once         (class_attended, max 1 per member)
WHEN member.age >= 45 AND member.age <= 60 THEN grant_reward(3)

5. Loyalty bonus after one year            (member_activity, max 1 per member)
WHEN member.active_days >= 365 THEN award_points(100)

6. Offer for one tier                      (invoice_paid)
WHEN member.tier == "Gold" THEN apply_discount(percent=10, target="membership")

Tier perk: Gold gets a free class monthly  (member_activity)
WHEN member.tier == "Gold" THEN grant_reward(3)
```

## Admin workflow

1. **Pricing & Rewards** → edit tiers (name, threshold, benefits, active).
2. Create rewards (percent/fixed invoice discount or free class credit):
   point cost, minimum tier, total limit, per-member limit, dates. Each
   reward shows its ID for `grant_reward(...)`.
3. **Create a new offer**: name, member-facing description, trigger, rule,
   dates, per-member and total limits, priority. Save as **Draft** first.
4. **Test this policy**: pick a member (and a sample invoice amount/type or
   class category) to see if it matches and the estimated discount.
5. **Activate**, **Pause**, or **Expire**. A policy past its end date is
   marked expired automatically. Every change is saved in the policy history
   (who, when, full snapshot).
6. Points adjustment: signed, reason required, recorded in the ledger;
   changes spendable points only.

Members see active offers in plain English under **Offers for you**, marked
"You qualify", "Depends on the class/invoice", "Not yet" or "Already used".

## Conflicts

- **Points** from all matching policies add up.
- **Discounts do not stack**: one discount per invoice. The matching discount
  policy with the lowest priority number wins (ties: lower ID). A member's
  reserved reward is used only if no automatic discount matched.
- Every discount is capped at 50% of the invoice.
- Each policy + event has a unique key, so a retried payment or attendance
  mark never awards twice. Policy limits count recorded applications.
- Refund: purchase points are reversed (ledger entry), a used invoice reward
  is returned as points, lifetime points / tier stay.
- Cancelled booking: a used free-class credit returns to "ready"; attendance
  points for that booking are reversed.
- The waitlist priority (reliability + join time) is unchanged; rewards never
  affect it.

## Sample data

`python seed_demo_data.py` (on a new database) builds a realistic gym through
the app's own functions: the administrator creates tiers, rewards and 14
offers; trainers run 7 weeks of classes and mark attendance (points come from
the live offers); the receptionist issues invoices and a refund; members pay,
redeem points and book with free-class credits. See the README for the
accounts. Offers included:

| Offer | Trigger | Rule |
|---|---|---|
| Class attendance points | class_attended | `THEN award_points(10)` |
| Monthly 8-class milestone | class_attended | `WHEN member.attended_classes_month == 8 THEN award_points(50)` |
| Silver & Gold: extra points for yoga and mobility | class_attended | tier IN Silver/Gold AND category yoga OR mobility → +10 |
| Purchase points | invoice_paid | `THEN award_points(25)` |
| Gold: 15% off membership renewals | invoice_paid | Gold AND membership_purchases ≥ 1 → 15% (membership) |
| Active training reward: 10% off membership | invoice_paid | `member.attended_classes_30d >= 8` → 10% (membership) |
| Silver: 10% off membership renewals | invoice_paid | Silver AND renewal → 10% (membership) |
| Second purchase within 30 days: 5% off | invoice_paid | purchase_count == 1 AND ≤ 30 days → 5%, once |
| Welcome bonus | member_activity | Bronze → +20, once |
| Gold: free class every month | member_activity | Gold → Gold monthly free class |
| 50+ wellness: one free class | member_activity | `member.age >= 50` → free class, once |
| One-year loyalty bonus | member_activity | `member.active_days >= 365` → +100, once |
| Late-summer double points | class_attended | ended campaign (expired) |
| Black Friday weekend: 20% off membership | invoice_paid | scheduled with start/end dates |
| Win-back: 50 points for at-risk members | class_attended | `WHEN member.segment == "At risk" THEN award_points(50)`, once |

Members' starting points come from an "Opening balance from previous stamp
card" ledger entry dated at the programme launch.

## Database and upgrade

New tables are created by `db.create_all()`. `_migrate_schema()` then adds
**any** missing column of an existing table (add-only, never drop or
rewrite), so an older `gymflow.db` keeps its data and starts normally.
Existing invoices become category `other`, existing classes `general`.
No new Python dependency.

## Tests

```bash
python3 _devtools/test_smoke.py              # whole app (shim)
python3 _devtools/test_unit.py               # algorithms (shim)
python3 _devtools/test_loyalty.py            # rules, rule language, tiers, ledger (shim)
python3 _devtools/test_loyalty_e2e.py        # real SQLite: old-DB upgrade + full UI flows
python3 _devtools/test_loyalty_concurrency.py  # real SQLite: double-spend race
```
