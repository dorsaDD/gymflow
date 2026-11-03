# Pricing, loyalty, and incentive policies

GymFlow stores promotion definitions as validated JSON data and interprets a
small allow-listed rule format. It never executes policy text. A new offer
using supported facts and actions is created from **Pricing & Rewards** in
the administrator navigation; no Python change is needed for each offer.

## Where the feature connects

- `gymflow/loyalty_engine.py` validates and evaluates rule definitions,
  computes tiers, manages the points ledger, and handles reward redemption.
- `gymflow/routes/loyalty.py` provides the member wallet and admin screens.
- `gymflow/routes/billing.py` and `payment_manager.py` apply invoice discounts
  only during successful payment. A failed/declined payment does not consume a
  reward or earn purchase points.
- `gymflow/routes/classes.py` and `class_manager.py` award attendance points
  only after staff mark a confirmed booking attended. Correcting an attendance
  to a no-show appends a reversal.
- Models in `gymflow/models.py`: `LoyaltyTier`, `LoyaltyAccount`,
  `PointsLedgerEntry`, `PricingPolicy`, `PolicyApplication`, `PolicyAudit`,
  `Reward`, and `RewardRedemption`.
- Member page: `/loyalty/`. Admin page: `/admin/loyalty/`.

The rewards tier uses lifetime qualifying points. Redemption, invoice refunds,
and attendance corrections append signed ledger entries and affect spendable
balance; they do not reduce lifetime qualifying points. Point expiry is not
implemented; points do not expire in this version. “Active days” means days
since the GymFlow user account was created because there is no separate
membership start/end model in the current schema.

The existing billing model has generic invoices rather than separate
membership subscriptions or product types. Therefore `invoice_paid` policies
apply to successful paid invoices in the current billing flow; GymFlow cannot
distinguish a membership renewal from another invoice charge yet. The booking
flow remains free and has no payment step.

## Rule format (DSL)

Each policy definition must be a JSON object with exactly `conditions` and
`action`. Conditions are a list combined with AND; an empty list matches every
eligible event. Every condition has exactly `fact`, `op`, and `value`. No
nesting, functions, expressions, code, or free-form identifiers are allowed.

Example: 10 points for each valid class attendance:

```json
{
  "conditions": [],
  "action": {"type": "award_points", "points": 10}
}
```

Example: 50 points when the member reaches eight attended classes in the
current calendar month:

```json
{
  "conditions": [
    {"fact": "member.attended_classes_month", "op": "gte", "value": 8}
  ],
  "action": {"type": "award_points", "points": 50}
}
```

Example: a 5% discount for members' second paid invoice within 30 days of
their first:

```json
{
  "conditions": [
    {"fact": "member.purchase_count", "op": "eq", "value": 1},
    {"fact": "member.days_since_first_purchase", "op": "lte", "value": 30}
  ],
  "action": {"type": "discount_percent", "percent": 5}
}
```

For an active-member milestone, choose the `member_activity` trigger and use
`member.active_days` in the conditions. The check runs when the member visits
their dashboard after the configured age is reached; the stable event key
prevents a repeated award. GymFlow has no background scheduler in this app.

To give a free class or another preconfigured reward after a condition, create
the reward first, then use its displayed ID:

```json
{
  "conditions": [
    {"fact": "member.age", "op": "gte", "value": 45},
    {"fact": "member.age", "op": "lte", "value": 60}
  ],
  "action": {"type": "grant_reward", "reward_id": 3}
}
```

Policy-level `max_uses_per_member: 1` makes that an at-most-once offer per
member. Rewards themselves can also have a global redemption limit and a
minimum rewards tier.

### Supported facts and operators

| Fact | Meaning |
|---|---|
| `member.age` | Age from the member card birthdate; unknown birthdate never matches. |
| `member.tier` | Current configurable Rewards Tier name, based on lifetime points. |
| `member.active_days` | Days since GymFlow account creation. |
| `member.attended_classes` | Lifetime count of staff-confirmed attended bookings. |
| `member.attended_classes_month` | Attended bookings whose class schedule is in the current calendar month. |
| `member.attended_classes_30d` | Attended bookings scheduled in the last 30 days. |
| `member.spending_total` | Earlier successful invoice payments, net of recorded discounts. |
| `member.spending_total_including_invoice` | Earlier paid total plus this invoice's expected/net amount. |
| `member.purchase_count` | Successful invoices before the current invoice event. |
| `member.days_since_first_purchase` | Days since the first successful invoice, before the current invoice event. |
| `invoice.amount` | Face value of the current invoice; available for `invoice_paid`. |

Operators: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, and `in`. Numeric facts use
numeric values; text facts use strings. `in` accepts a short JSON list.

Actions: `award_points` (positive whole points, up to 100,000),
`discount_percent` (up to 50%), `discount_fixed` (positive amount up to
$100,000, only for paid invoices), and `grant_reward` (a valid existing
reward ID). Percentage/fixed discounts are only valid for `invoice_paid`.

Unknown facts, fields, operators, actions, malformed JSON, invalid values,
over-limit discounts, and unsupported fact/event combinations fail validation
with a form message. The interpreter performs only explicit comparisons from
the whitelist; it does not use `eval`, `exec`, SQL fragments, or user code.

## Admin workflow

1. Open **Pricing & Rewards** as an administrator.
2. Add or edit rewards tiers and thresholds. Seed examples are Bronze (0),
   Silver (500), and Gold (1,200); these are editable sample values.
3. Add rewards such as a percentage/fixed invoice discount or free-class
   credit. Choose point cost, optional tier eligibility, per-member and
   overall redemption caps, and active dates.
4. Add a policy, choose its event, enter its JSON definition, set priority,
   dates, and optional per-member/global use limits. Save as draft first if
   desired.
5. Use **Test this policy** to preview its conditions for a member; for
   invoice discounts, enter an invoice amount to see the estimated value.
6. Set status to Active. Active rules can be paused or explicitly expired;
   start/end dates also make a policy ineligible outside its active window.
   Edits, activation, pausing, and expiry create `PolicyAudit` snapshots.

Policy and reward start/end inputs are entered as UTC date-times.

For `grant_reward`, create a reward and use its listed reward ID in the rule.
This grants a reserved reward credit without deducting points; the policy
limits and reward cap provide the usage controls. Administrators can also
adjust a member's spendable balance with a signed, reason-required adjustment.
Those manual changes are ledger entries and do not change lifetime qualifying
points.

## Ready-to-show demo data

On a fresh or development database, run `python seed_demo_data.py` as described
in the README. It creates six active sample policies, three rewards, populated
ledger histories, and a pending invoice for Mona. The mock processor pays two
sample invoices for Leo so the second-purchase discount and paid-invoice points
are immediately visible. No real payment is made. Stable event keys make the
seed idempotent, and existing admin edits to demo policies/rewards are left
alone.

- `mona@example.com` / `Demo123!`: Silver Rewards Tier, a reserved 10% invoice
  discount, a policy-granted free class credit, and a pending invoice to show
  the reward in the existing billing form.
- `leo@example.com` / `Demo123!`: Gold Rewards Tier and two paid invoices; the
  second invoice has the repeat-purchase discount in its receipt/history.
- `sara@example.com` / `Demo123!`: Bronze Rewards Tier with visible progress
  toward Silver.
- `admin@gymflow.local` / `Admin123!`: use **Pricing & Rewards** or
  `/admin/loyalty/` to show policies, tiers, rewards, and policy audit rows.

The seed's sample point top-ups are explicitly labeled as demo historical
campaign points in each member's ledger. They are fixture data for showing the
wallet and tier UI, not a production award policy.

## Tiers, points, and redemption

`LoyaltyAccount.balance` is the current spendable balance. `lifetime_points`
is the cumulative qualifying total used to find the current rewards tier and
progress to the next threshold. `PointsLedgerEntry` is the audit source of
truth for earns, redemptions, reversals, and adjustments; it stores a signed
balance delta and an event/policy reference. `PolicyApplication` gives policy
usage counts and idempotency keys. Repeated processing of one attendance or
invoice event cannot create a second award. If a refund reverses more earned
points than remain in the wallet, the internal balance can be negative; the
member sees zero spendable points until later earnings or adjustments cover
that reversal.

Members can see their wallet, progress, rewards, and history at **Rewards**.
Invoice rewards are selected in the existing invoice payment form and are
consumed only after a successful charge. If an invoice is refunded, invoice
purchase points are reversed and a used invoice reward's points are restored,
with ledger records. Attendance corrections reverse attendance awards.

Free-class rewards are reserved on redemption and attached to the next class
booking selected by the member; cancelling that booking restores the credit to
reserved status. If an already attended booking is cancelled or corrected to a
no-show, the related attendance award is reversed once. The current GymFlow booking flow has no class price or
checkout, so this credit is tracked as an incentive/usage entitlement and
does not reduce a monetary charge. No shopping cart or class price has been
invented.

## Conflicts and audit behavior

- Invoice discounts do not stack. The first matching discount policy by
  ascending priority number (ties by policy ID) wins; lower-priority rules are
  considered if earlier matching rules are point/reward actions instead.
- A member-selected invoice discount reward is used only when no policy
  discount matched. The effective discount is capped at 50% of invoice face
  value. Fixed amounts use the project's existing USD-denominated invoice
  model and are capped by the same percentage ceiling.
- Point awards from different eligible policies may coexist. Policy limits
  count previously recorded applications, and each event/policy pair has an
  idempotency key.
- Failed payments do not create invoice-paid policy applications or point
  awards. Refunds append reversal entries. A cancelled booking earns no points;
  if a free-class credit was attached, it returns to reserved. Cancelling or
  correcting an already attended booking reverses its attendance award.
- The existing waitlist ranking remains based on attendance reliability and
  join time. Rewards do not affect waitlist priority.

## Database and deployment

New tables are created through the application's existing `db.create_all()`
startup path. `_migrate_schema()` adds only the new invoice discount/payment
amount and booking reward-reference columns to existing databases; it does not
delete, recreate, or reset a database. The new account, policy, tier, reward,
ledger, redemption, application, and audit tables are additive. Back up the
SQLite database before deployment as usual. No external service or new Python
dependency is required.

## Tests

Run from the project directory:

```bash
python3 _devtools/test_smoke.py
python3 _devtools/test_unit.py
python3 _devtools/test_loyalty.py
```

The loyalty tests cover safe DSL validation/evaluation, tier thresholds and
progress, attendance and invoice event idempotency, invoice refunds, reward
eligibility/redemption, and member/admin authorization through the current
project's test shim. The existing suites continue to cover class booking,
waitlist, billing, and staff permissions.
