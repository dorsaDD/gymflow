"""MemberSegmentation -- unsupervised learning (K-Means clustering) that
groups members by how they actually use the gym, so staff can see who the
champions are and who is drifting away, and so the pricing engine can target
offers at a segment (fact `member.segment`, e.g. a win-back offer for
"At risk" members).

Method
------
1. Features per member (an RFM model -- Recency, Frequency, Monetary -- the
   standard customer-segmentation features in marketing analytics, plus two
   gym-specific ones), all computed from real database rows:
     recency_days     days since their last attended class (tenure if never)
     frequency_60d    classes attended in the last 60 days
     monetary_90d     money actually paid in the last 90 days
     tenure_days      days since they joined
     reliability      attended / marked bookings (0.5 if nothing marked)
2. Standardise (StandardScaler) so euros and days are comparable.
3. Fit K-Means for every k in 2..min(5, n-1) and keep the k with the best
   silhouette score (how well separated the clusters are, -1..1) -- the
   number of groups is chosen by the data, not hard-coded. Inertia is kept
   too for an elbow chart.
4. Name each cluster from its centroid in the original units with simple,
   documented rules (see _label), so a segment name always matches what its
   members actually do.
5. Project to 2-D with PCA purely for the scatter chart on the insights page.

Deterministic (random_state=42) and cached until the underlying data
changes, because the pricing engine may ask for a member's segment many
times per request.
"""
from datetime import datetime, timedelta

from gymflow.models import Booking, Invoice, User

SEGMENTS = ("Champions", "Loyal regulars", "New members", "Occasional", "At risk")
SEGMENT_DESCRIPTIONS = {
    "Champions": "Train very often and recently — the gym's most engaged members.",
    "Loyal regulars": "Steady, reliable attendance.",
    "New members": "Joined recently; still forming a habit.",
    "Occasional": "Come now and then; room to grow.",
    "At risk": "Long time since their last class or many no-shows — likely to churn.",
}
FEATURES = ("recency_days", "frequency_60d", "monetary_90d", "tenure_days", "reliability")
MIN_MEMBERS = 4
MAX_K = 5

_CACHE = {"key": None, "result": None}


def _member_features(now):
    rows = []
    bookings_by_member = {}
    for booking in Booking.query.all():
        bookings_by_member.setdefault(booking.member_id, []).append(booking)
    for user in User.query.filter_by(role="member").all():
        if user.card is None:
            continue
        mine = bookings_by_member.get(user.id, [])
        attended = [b for b in mine if b.attended is True and b.fitness_class]
        marked = [b for b in mine if b.attended is not None]
        tenure = max((now - user.created_at).days, 0) if user.created_at else 0
        last = max((b.fitness_class.schedule for b in attended), default=None)
        recency = max((now - last).days, 0) if last else tenure
        frequency = sum(1 for b in attended if b.fitness_class.schedule >= now - timedelta(days=60))
        monetary = 0.0
        for invoice in Invoice.query.filter_by(card_id=user.card.id).all():
            if invoice.status == "paid" and invoice.paid_at and invoice.paid_at >= now - timedelta(days=90):
                monetary += invoice.paid_amount if invoice.paid_amount is not None else invoice.amount
        reliability = (sum(1 for b in marked if b.attended) / len(marked)) if marked else 0.5
        rows.append((user, [float(recency), float(frequency), round(monetary, 2), float(tenure), reliability]))
    return rows


def _label(centroid):
    recency, frequency, _monetary, tenure, reliability = centroid
    if tenure < 45:
        return "New members"
    if recency >= 21 or reliability < 0.5:
        return "At risk"
    if frequency >= 12:
        return "Champions"
    if frequency >= 5:
        return "Loyal regulars"
    return "Occasional"


def _signature():
    from flask import current_app, has_app_context
    database = current_app.config.get("SQLALCHEMY_DATABASE_URI") if has_app_context() else None
    bookings = tuple(sorted((b.id, b.member_id, b.attended) for b in Booking.query.all()))
    invoices = tuple(sorted((i.id, i.status) for i in Invoice.query.all()))
    members = tuple(sorted(u.id for u in User.query.filter_by(role="member").all()))
    return (database, datetime.utcnow().date(), hash((bookings, invoices, members)))


def segment_members():
    """Returns {"ok": bool, "note": str, "k": int, "silhouette": float,
    "k_scores": [(k, silhouette, inertia)], "members": [{user, segment,
    features, x, y}], "segments": [{name, size, means, description}]}.
    """
    key = _signature()
    if _CACHE["key"] == key:
        return _CACHE["result"]
    result = _segment_uncached()
    _CACHE["key"], _CACHE["result"] = key, result
    return result


def _segment_uncached():
    now = datetime.utcnow()
    rows = _member_features(now)
    if len(rows) < MIN_MEMBERS:
        return {"ok": False, "note": f"Need at least {MIN_MEMBERS} members with a card to find segments "
                                     f"(currently {len(rows)}).", "members": [], "segments": [],
                "k": None, "silhouette": None, "k_scores": []}

    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler

    X = np.array([features for _, features in rows])
    scaled = StandardScaler().fit_transform(X)
    k_scores, best = [], None
    for k in range(2, min(MAX_K, len(rows) - 1) + 1):
        model = KMeans(n_clusters=k, n_init=10, random_state=42).fit(scaled)
        if len(set(model.labels_)) < 2:
            continue
        score = float(silhouette_score(scaled, model.labels_))
        k_scores.append((k, score, float(model.inertia_)))
        if best is None or score > best[1]:
            best = (k, score, model)
    if best is None:
        return {"ok": False, "note": "Members are too similar to split into groups yet.", "members": [],
                "segments": [], "k": None, "silhouette": None, "k_scores": k_scores}

    k, silhouette, model = best
    labels = model.labels_
    names = {}
    for cluster in range(k):
        names[cluster] = _label(X[labels == cluster].mean(axis=0))
    pca = PCA(n_components=2, random_state=42)
    points = pca.fit_transform(scaled)
    explained = float(sum(pca.explained_variance_ratio_))
    xs, ys = points[:, 0], points[:, 1]
    span_x = (xs.max() - xs.min()) or 1.0
    span_y = (ys.max() - ys.min()) or 1.0

    members = []
    for (user, features), cluster, (x, y) in zip(rows, labels, points):
        members.append({"user": user, "segment": names[int(cluster)], "cluster": int(cluster),
                        "features": dict(zip(FEATURES, features)), "x": float(x), "y": float(y),
                        "px": float((x - xs.min()) / span_x), "py": float((y - ys.min()) / span_y),
                        "css": names[int(cluster)].lower().replace(" ", "-")})
    segments = []
    for name in SEGMENTS:
        group = [m for m in members if m["segment"] == name]
        if not group:
            continue
        means = {f: round(sum(m["features"][f] for m in group) / len(group), 1) for f in FEATURES}
        segments.append({"name": name, "size": len(group), "means": means, "css": name.lower().replace(" ", "-"),
                         "description": SEGMENT_DESCRIPTIONS[name],
                         "members": sorted(group, key=lambda m: m["user"].surname)})
    note = (f"K-Means on {len(rows)} members and {len(FEATURES)} features; k={k} chosen automatically "
            f"by the best silhouette score ({silhouette:.2f}) among k=2..{k_scores[-1][0]}.")
    if len(segments) < k:
        note += f" {k} clusters were found; clusters with the same profile share one segment name."
    return {"ok": True, "note": note, "k": k, "silhouette": silhouette, "k_scores": k_scores,
            "members": members, "segments": segments, "pca_explained": explained}


def segment_of(user):
    """The segment name for one member, or None if segmentation isn't possible yet."""
    result = segment_members()
    if not result["ok"]:
        return None
    return next((m["segment"] for m in result["members"] if m["user"].id == user.id), None)
