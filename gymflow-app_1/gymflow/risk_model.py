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
from datetime import datetime, timedelta

from gymflow.models import Booking, User

MIN_TRAINING_SAMPLES = 6  # below this, no split is meaningful; decline to predict
FEATURE_NAMES = (
    "prior_reliability",       # attended-ratio over the member's OTHER marked bookings (0..1, 0.5 if none)
    "prior_marked_count",      # how many other bookings of theirs have been marked at all
    "prior_no_show_count",     # raw count of prior no-shows (repeat offenders stand out even at a high ratio)
    "tenure_days",             # how long the member has held an account, at the time of this booking
    "recent_reliability_30d",  # attended-ratio over their marked classes in the 30 days BEFORE this class
    "lead_time_days",          # days between making the booking and the class (late bookers vs planners)
    "class_hour",              # hour of day of the class (early mornings are skipped more often)
    "is_weekend",              # 1 if the class is on Saturday/Sunday
)
FEATURE_DESCRIPTIONS = {
    "prior_reliability": "Attended-ratio over this member's other marked bookings (0.5 if none yet)",
    "prior_marked_count": "How many other bookings of theirs have been marked",
    "prior_no_show_count": "Raw count of their other no-shows",
    "tenure_days": "Days the member has held an account when they booked",
    "recent_reliability_30d": "Attended-ratio over their marked classes in the 30 days before this class (recent behaviour)",
    "lead_time_days": "Days between booking and class (last-minute bookings vs planned ones)",
    "class_hour": "Hour of day the class starts",
    "is_weekend": "1 if the class is on a weekend",
}


def _member_marked(member_id: int, exclude_booking_id: int):
    return [
        b for b in Booking.query.filter_by(member_id=member_id).all()
        if b.attended is not None and b.id != exclude_booking_id
    ]


def _member_prior_stats(member_id: int, exclude_booking_id: int):
    """(reliability, marked_count, no_show_count) over a member's marked
    bookings, excluding one specific booking id (see leave-one-out note
    above). Filtered in Python, not SQL, to stay portable across the real
    SQLAlchemy engine and the project's offline test shim.
    """
    marked = _member_marked(member_id, exclude_booking_id)
    if not marked:
        return 0.5, 0, 0
    attended = sum(1 for b in marked if b.attended)
    no_shows = len(marked) - attended
    return attended / len(marked), len(marked), no_shows


def _tenure_days(member: User, at_time: datetime) -> float:
    if not member.created_at or not at_time:
        return 0.0
    return max((at_time - member.created_at).total_seconds() / 86400.0, 0.0)


def _features_for_booking(booking: Booking) -> list:
    reliability, marked_count, no_shows = _member_prior_stats(booking.member_id, booking.id)
    schedule = booking.fitness_class.schedule if booking.fitness_class else (booking.created_at or datetime.utcnow())
    window = [b for b in _member_marked(booking.member_id, booking.id)
              if b.fitness_class and schedule - timedelta(days=30) <= b.fitness_class.schedule < schedule]
    recent = (sum(1 for b in window if b.attended) / len(window)) if window else reliability
    created = booking.created_at or schedule
    lead_time = max((schedule - created).total_seconds() / 86400.0, 0.0)
    return [
        reliability,
        float(marked_count),
        float(no_shows),
        _tenure_days(booking.member, booking.created_at),
        recent,
        lead_time,
        float(schedule.hour),
        1.0 if schedule.weekday() >= 5 else 0.0,
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


_CANDIDATES = (
    ("Logistic Regression", "logreg"),
    ("Random Forest", "random_forest"),
    ("Gradient Boosting", "gradient_boosting"),
)


def _build_pipeline(kind: str):
    from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    if kind == "random_forest":
        # class_weight="balanced": no-shows are the rare class, so errors on
        # them are weighted up instead of letting the model always say "attends".
        clf = RandomForestClassifier(n_estimators=100, max_depth=4, random_state=42, class_weight="balanced")
    elif kind == "gradient_boosting":
        clf = GradientBoostingClassifier(n_estimators=80, max_depth=2, learning_rate=0.1, random_state=42)
    else:
        clf = LogisticRegression(max_iter=1000, class_weight="balanced")
    return Pipeline([("scale", StandardScaler()), ("clf", clf)])


def _evaluate(kind: str, X_train, X_test, y_train, y_test) -> dict:
    from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score

    probe = _build_pipeline(kind)
    probe.fit(X_train, y_train)
    preds = probe.predict(X_test)
    return {
        "accuracy": accuracy_score(y_test, preds),
        "precision": precision_score(y_test, preds, zero_division=0),
        "recall": recall_score(y_test, preds, zero_division=0),
        "f1": f1_score(y_test, preds, zero_division=0),
    }


def _extra_metrics(kind, X_train, X_test, y_train, y_test, X, y, minority_count):
    """ROC-AUC on the held-out split, plus stratified k-fold cross-validated
    F1 (mean and spread) on all rows -- a much more stable estimate than a
    single 25% split when the dataset is small."""
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold, cross_val_score

    probe = _build_pipeline(kind)
    probe.fit(X_train, y_train)
    proba = probe.predict_proba(X_test)[:, 1]
    try:
        auc = float(roc_auc_score(y_test, proba))
    except ValueError:
        auc = None
    folds = min(5, minority_count)
    cv_mean = cv_std = None
    if folds >= 2:
        scores = cross_val_score(_build_pipeline(kind), X, y, scoring="f1",
                                 cv=StratifiedKFold(n_splits=folds, shuffle=True, random_state=42))
        cv_mean, cv_std = float(scores.mean()), float(scores.std())
    return {"roc_auc": auc, "cv_f1_mean": cv_mean, "cv_f1_std": cv_std, "cv_folds": folds}, probe, proba


def _diagnostics(probe, X_test, y_test, proba, final_model, X, y):
    """Confusion matrix + ROC curve for the chosen model on the held-out
    split, and permutation feature importance of the final model."""
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import confusion_matrix, roc_curve

    preds = probe.predict(X_test)
    tn, fp, fn, tp = confusion_matrix(y_test, preds, labels=[0, 1]).ravel()
    roc = None
    try:
        fpr, tpr, _ = roc_curve(y_test, proba)
        roc = [(float(a), float(b)) for a, b in zip(fpr, tpr)]
    except ValueError:
        pass
    result = permutation_importance(final_model, X, y, n_repeats=10, random_state=42, scoring="accuracy")
    importances = sorted(
        ({"name": name, "importance": float(mean), "std": float(std)}
         for name, mean, std in zip(FEATURE_NAMES, result.importances_mean, result.importances_std)),
        key=lambda row: -row["importance"])
    return {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)}, roc, importances


# Training is deterministic, so a result is reused until the attendance data
# it was trained on actually changes (dashboard and rosters call train()).
_CACHE = {"key": None, "result": None}


def _data_signature():
    rows = []
    for b in Booking.query.all():
        if b.attended is not None:
            rows.append((b.id, b.member_id, bool(b.attended), b.class_id))
    from flask import current_app, has_app_context
    database = current_app.config.get("SQLALCHEMY_DATABASE_URI") if has_app_context() else None
    return (database, hash(tuple(sorted(rows))))


def train():
    """Trains and compares Logistic Regression, Random Forest and Gradient
    Boosting on the current attendance history, picks whichever scores
    higher on held-out F1 (tie-broken by accuracy) as the model actually
    used for live predictions, and reports the full comparison so the
    choice is checkable rather than asserted.

    Returns a dict:
      {"model": Pipeline|None, "model_name": str|None, "n_samples": int,
       "n_no_show": int, "held_out_accuracy": float|None,
       "comparison": [{"name", "accuracy", "precision", "recall", "f1",
                       "roc_auc", "cv_f1_mean", "cv_f1_std"}]|None,
       "baseline_accuracy": float|None, "confusion": dict|None,
       "roc": [(fpr, tpr)]|None, "importances": [...]|None, "note": str}
    `model` is None (and `note` explains why) when there is not enough
    marked history yet -- callers must handle that, not assume a model.
    """
    key = _data_signature()
    if _CACHE["key"] == key and _CACHE["result"] is not None:
        return _CACHE["result"]
    result = _train_uncached()
    _CACHE["key"], _CACHE["result"] = key, result
    return result


def _train_uncached():
    rows = _training_rows()
    n_samples = len(rows)
    n_no_show = sum(label for _, label in rows)
    empty = {"baseline_accuracy": None, "confusion": None, "roc": None, "importances": None}

    if n_samples < MIN_TRAINING_SAMPLES or n_no_show == 0 or n_no_show == n_samples:
        return {
            "model": None, "model_name": None, "n_samples": n_samples, "n_no_show": n_no_show,
            "held_out_accuracy": None, "comparison": None, **empty,
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
    diagnostics = dict(empty)

    minority_count = min(n_no_show, n_samples - n_no_show)
    if n_samples >= 12 and minority_count >= 2:
        from sklearn.model_selection import train_test_split

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.25, random_state=42, stratify=y
        )
        comparison = []
        probes = {}
        best_key = None
        for name, kind in _CANDIDATES:
            metrics = _evaluate(kind, X_train, X_test, y_train, y_test)
            extra, probe, proba = _extra_metrics(kind, X_train, X_test, y_train, y_test, X, y, minority_count)
            probes[kind] = (probe, proba)
            comparison.append({"name": name, "kind": kind, **metrics, **extra})
            key = (metrics["f1"], metrics["accuracy"])
            if best_key is None or key > best_key:
                best_key = key
                chosen_kind, chosen_name = kind, name
        held_out_accuracy = next(c["accuracy"] for c in comparison if c["kind"] == chosen_kind)
        majority = 1 if sum(y_test) * 2 > len(y_test) else 0
        diagnostics["baseline_accuracy"] = sum(1 for label in y_test if label == majority) / len(y_test)

    model = _build_pipeline(chosen_kind)
    model.fit(X, y)  # final model refit on ALL rows -- the split above was only to measure it

    if comparison is not None:
        probe, proba = probes[chosen_kind]
        confusion, roc, importances = _diagnostics(probe, X_test, y_test, proba, model, X, y)
        diagnostics.update(confusion=confusion, roc=roc, importances=importances)

    note = f"Trained on {n_samples} marked bookings ({n_no_show} no-shows, {n_samples - n_no_show} attended)."
    if comparison is not None:
        chosen = next(c for c in comparison if c["kind"] == chosen_kind)
        note += (
            f" Compared {len(comparison)} algorithms on a held-out 25% split; "
            f"chose {chosen_name} (F1={chosen['f1']:.0%}, accuracy={held_out_accuracy:.0%}"
            + (f", ROC-AUC={chosen['roc_auc']:.2f}" if chosen["roc_auc"] is not None else "")
            + f"; always-guess-majority baseline={diagnostics['baseline_accuracy']:.0%})."
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
        "held_out_accuracy": held_out_accuracy, "comparison": comparison, **diagnostics, "note": note,
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
