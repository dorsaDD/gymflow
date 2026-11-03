"""RiskModel — Section 4.8: a scikit-learn classifier that estimates the
probability a member will NOT show up (no-show) to a class they have
confirmed a booking for, trained on the same attendance history that
WaitlistManager's reliability_score() (Section 4.7) is built from.

Design notes, stated plainly rather than oversold (the report's own
second-revision philosophy: every claim here should be checkable against
the code, not just asserted):

* Ground truth. The only real label GymFlow has is Booking.attended,
  set by staff on the Section 4.7 roster page after a class happens. There
  is no synthetic or invented data path here -- every training row is a
  booking someone actually marked attended or no-show.

* Leave-one-out features. Naively using a member's full attended/no-show
  ratio as a feature for predicting one of THEIR OWN bookings would leak
  that booking's own label into its own feature (the row would partly be
  explaining itself). Each training row instead computes the member's
  reliability from every OTHER marked booking of theirs, excluding the row
  being trained on. A live prediction on an unmarked, upcoming booking
  needs no such exclusion -- an upcoming booking has attended=None, so it
  was never going to be counted in its own feature anyway.

* Small-sample honesty. A single-developer coursework app's seeded history
  is tens of rows, not thousands. This module trains a real, unmodified
  scikit-learn LogisticRegression on whatever history actually exists, and
  reports how many samples it used and (when there are enough for a
  meaningful split) a held-out accuracy -- it does not manufacture a bigger
  number by describing the *class of technique* as more validated than the
  *amount of data* supports. Below MIN_TRAINING_SAMPLES it declines to
  predict at all rather than return a number dressed up as ML that is
  really just noise.

* No caching / no pickle file. At this data scale (dozens of rows) training
  from scratch on every call is milliseconds, so there is no persisted
  model file to go stale or drift from the database -- the risk score
  shown is always freshly trained on the current attendance history. This
  would be worth revisiting (see Section 11 Roadmap) if GymFlow's history
  ever grew into the thousands of bookings.
"""
from datetime import datetime

from gymflow.models import Booking, User

MIN_TRAINING_SAMPLES = 6  # below this, no split is meaningful; decline to predict
FEATURE_NAMES = (
    "prior_reliability",    # attended-ratio over the member's OTHER marked bookings (0..1, 0.5 if none)
    "prior_marked_count",   # how many other bookings of theirs have been marked at all
    "prior_no_show_count",  # raw count of prior no-shows (repeat offenders stand out even at a high ratio)
    "tenure_days",          # how long the member has held an account, at the time of this booking
)


def _member_prior_stats(member_id: int, exclude_booking_id: int):
    """(reliability, marked_count, no_show_count) over a member's marked
    bookings, excluding one specific booking id (see leave-one-out note
    above). Filtered in Python, not SQL, to stay portable across the real
    SQLAlchemy engine and the project's offline test shim.
    """
    marked = [
        b for b in Booking.query.filter_by(member_id=member_id).all()
        if b.attended is not None and b.id != exclude_booking_id
    ]
    if not marked:
        return 0.5, 0, 0
    attended = sum(1 for b in marked if b.attended)
    no_shows = len(marked) - attended
    return attended / len(marked), len(marked), no_shows


def _tenure_days(member: User, at_time: datetime) -> float:
    if not member.created_at:
        return 0.0
    return max((at_time - member.created_at).total_seconds() / 86400.0, 0.0)


def _features_for_booking(booking: Booking) -> list:
    reliability, marked_count, no_shows = _member_prior_stats(booking.member_id, booking.id)
    return [
        reliability,
        float(marked_count),
        float(no_shows),
        _tenure_days(booking.member, booking.created_at),
    ]


def _training_rows():
    """Every marked booking in the system, as (features, label) pairs.
    label = 1 means "no-show" (the thing being predicted), 0 means attended.
    """
    rows = []
    for booking in Booking.query.all():
        if booking.attended is None:
            continue
        rows.append((_features_for_booking(booking), 0 if booking.attended else 1))
    return rows


# Candidate algorithms compared on every training run once there is enough
# data for a meaningful held-out split (see train() below). Comparing two
# genuinely different model families -- a linear model vs. an ensemble of
# trees -- rather than just tuning one is the point: it lets the app pick
# whichever one actually generalises better on THIS gym's real data, and
# say why, instead of assuming one algorithm is always the right choice.
_CANDIDATES = (
    ("Logistic Regression", "logreg"),
    ("Random Forest", "random_forest"),
)


def _build_pipeline(kind: str):
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    if kind == "random_forest":
        # Small, shallow forest on purpose: this trains on dozens of rows,
        # not thousands, so a large/deep forest would just memorise noise.
        clf = RandomForestClassifier(n_estimators=100, max_depth=4, random_state=42)
    else:
        clf = LogisticRegression()
    return Pipeline([("scale", StandardScaler()), ("clf", clf)])


def _evaluate(kind: str, X_train, X_test, y_train, y_test) -> dict:
    from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score

    probe = _build_pipeline(kind)
    probe.fit(X_train, y_train)
    preds = probe.predict(X_test)
    return {
        "accuracy": accuracy_score(y_test, preds),
        # zero_division=0: with this little data a candidate can end up
        # predicting the same class for every held-out row, which would
        # otherwise raise a warning/undefined-metric instead of just
        # reporting the (legitimately bad) score of 0.
        "precision": precision_score(y_test, preds, zero_division=0),
        "recall": recall_score(y_test, preds, zero_division=0),
        "f1": f1_score(y_test, preds, zero_division=0),
    }


def train():
    """Trains and compares LogisticRegression vs. RandomForestClassifier on
    the current attendance history, picks whichever scores higher on
    held-out F1 (tie-broken by accuracy) as the model actually used for
    live predictions, and reports the full comparison so the choice is
    checkable rather than asserted.

    Returns a dict:
      {"model": Pipeline|None, "model_name": str|None, "n_samples": int,
       "n_no_show": int, "held_out_accuracy": float|None,
       "comparison": [{"name", "accuracy", "precision", "recall", "f1"}]|None,
       "note": str}
    `model` is None (and `note` explains why) when there is not enough
    marked history yet -- callers must handle that, not assume a model.
    """
    rows = _training_rows()
    n_samples = len(rows)
    n_no_show = sum(label for _, label in rows)

    if n_samples < MIN_TRAINING_SAMPLES or n_no_show == 0 or n_no_show == n_samples:
        return {
            "model": None, "model_name": None, "n_samples": n_samples, "n_no_show": n_no_show,
            "held_out_accuracy": None, "comparison": None,
            "note": (
                f"Only {n_samples} marked booking(s) so far (need at least "
                f"{MIN_TRAINING_SAMPLES}, with both attended and no-show examples) "
                "-- mark attendance on more classes (Section 4.7 roster page) to "
                "train a real model instead of seeing this message."
            ),
        }

    X = [f for f, _ in rows]
    y = [label for _, label in rows]

    held_out_accuracy = None
    comparison = None
    chosen_kind, chosen_name = "logreg", "Logistic Regression"

    # Only bother with a held-out split (and therefore a comparison) once
    # there is enough data that the split itself is meaningful -- a
    # handful of rows would make the reported metrics pure noise from
    # which four test rows happened to fall out. A STRATIFIED split also
    # needs at least 2 examples of the rarer outcome (no-show or attended,
    # whichever is scarcer): with only 1, sklearn cannot put an example of
    # it in both the train and the test side and raises ValueError. This is
    # a real, plausible situation here (e.g. a gym with 12 marked bookings
    # but only a single no-show so far), not a contrived edge case, so it
    # is checked explicitly rather than left to crash the comparison (and,
    # with it, the dashboard/roster pages that call train()).
    minority_count = min(n_no_show, n_samples - n_no_show)
    if n_samples >= 12 and minority_count >= 2:
        from sklearn.model_selection import train_test_split

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.25, random_state=42, stratify=y
        )
        comparison = []
        best_key = None
        for name, kind in _CANDIDATES:
            metrics = _evaluate(kind, X_train, X_test, y_train, y_test)
            comparison.append({"name": name, "kind": kind, **metrics})
            key = (metrics["f1"], metrics["accuracy"])
            if best_key is None or key > best_key:
                best_key = key
                chosen_kind, chosen_name = kind, name
        held_out_accuracy = next(c["accuracy"] for c in comparison if c["kind"] == chosen_kind)

    model = _build_pipeline(chosen_kind)
    model.fit(X, y)  # final model refit on ALL rows -- the split above was only to measure it

    note = f"Trained on {n_samples} marked bookings ({n_no_show} no-shows, {n_samples - n_no_show} attended)."
    if comparison is not None:
        note += (
            f" Compared {len(comparison)} algorithms on a held-out 25% split; "
            f"chose {chosen_name} (F1={next(c['f1'] for c in comparison if c['kind'] == chosen_kind):.0%}, "
            f"accuracy={held_out_accuracy:.0%})."
        )
    elif n_samples >= 12:
        note += (
            f" Too few examples of the rarer outcome yet ({minority_count}, need at least 2) for a "
            "meaningful held-out split/comparison; metrics not reported."
        )
    else:
        note += " Too few samples yet for a meaningful held-out split/comparison; metrics not reported."

    return {
        "model": model, "model_name": chosen_name, "n_samples": n_samples, "n_no_show": n_no_show,
        "held_out_accuracy": held_out_accuracy, "comparison": comparison, "note": note,
    }


def predict_with(trained: dict, booking: Booking):
    """Probability (0..1) that `booking` ends in a no-show, using an
    already-trained result from train() -- pass the SAME `trained` dict to
    every booking you're scoring in one request instead of calling train()
    per booking, which would otherwise refit the model from scratch (cheap,
    but pointlessly repeated) for every single prediction. Returns None if
    `trained["model"]` is None (not enough history yet).
    """
    if trained["model"] is None:
        return None
    features = _features_for_booking(booking)
    return float(trained["model"].predict_proba([features])[0][1])


def predict_no_show_risk(booking: Booking):
    """Convenience one-shot version of predict_with() for callers scoring a
    single booking: trains fresh and predicts in one call. Prefer
    train() + predict_with() when scoring more than one booking at a time.
    """
    return predict_with(train(), booking)
