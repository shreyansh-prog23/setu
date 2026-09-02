"""
Trains a lightweight RandomForestClassifier that scores a location for
earthquake risk from 5 seismic-history features, and exports it for
backend/risk_engine.py to load at API startup.

Uses ml/earthquake_training_data.csv - 1,468 rows built by
ml/build_earthquake_training_data.py from USGS's real earthquake catalog for
India + neighbors (23,612 recorded M>=4.0 events, 1970-2026).

Run directly:
    cd backend && python ml/train_earthquake_risk_model.py
"""
from __future__ import annotations

import csv
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report
from sklearn.model_selection import GroupKFold, StratifiedKFold, cross_val_predict


def _apply_decision_rule(proba: np.ndarray) -> np.ndarray:
    """predicted = HIGH whenever P(HIGH) clears the lowered threshold,
    otherwise the plain argmax between SAFE/MODERATE. Mirrors exactly what
    risk_engine.py must do at serving time with this model's predict_proba
    output - the threshold is saved into the model bundle so serving code
    doesn't have to hardcode it separately."""
    high_wins = proba[:, 2] >= HIGH_RISK_DECISION_THRESHOLD
    fallback = np.argmax(proba[:, :2], axis=1)  # 0 or 1 when HIGH doesn't clear the bar
    return np.where(high_wins, 2, fallback)

RANDOM_SEED = 42
DATA_PATH = Path(__file__).parent / "earthquake_training_data.csv"
MODEL_PATH = Path(__file__).parent / "earthquake_risk_model.joblib"

# Life-safety routing should favor recall over precision on the HIGH class -
# a missed high-risk corridor is worse than an over-cautious one. Plain
# argmax (implicit 0.5 threshold) misses most real HIGH cases (recall 0.44 -
# many M>=5.5 quakes strike areas with low prior activity, a real
# "seismic quiescence" effect, not just noise). A swept threshold check
# (0.30/0.35/0.40/0.45/0.50) showed 0.30 overcorrects - SAFE recall
# collapses to 0.29 (most safe corridors would get needlessly flagged,
# risking alert fatigue) and macro-F1 actually drops below the untuned
# baseline. 0.35 keeps most of the recall gain (0.44 -> 0.70) while SAFE
# recall stays workable (0.51) - the best tradeoff point, not the highest
# possible HIGH recall.
HIGH_RISK_DECISION_THRESHOLD = 0.35

FEATURE_NAMES = [
    "local_seismic_density",
    "max_magnitude_nearby",
    "avg_depth_km",
    "recent_activity_rate_30d",
    "days_since_major_quake",
    "fault_dist_km",
    "seismic_zone_factor",
]

# 0: SAFE, 1: MODERATE, 2: HIGH_EARTHQUAKE_RISK
RISK_LABELS = {0: "SAFE", 1: "MODERATE", 2: "HIGH_EARTHQUAKE_RISK"}


def _load_dataset(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    X = np.array([[float(r[name]) for name in FEATURE_NAMES] for r in rows])
    y = np.array([int(r["label"]) for r in rows])
    years = np.array([int(r["year"]) for r in rows])
    grid_cells = np.array([r["grid_cell"] for r in rows])
    return X, y, years, grid_cells


def _report_group_holdout(model: RandomForestClassifier, X: np.ndarray, y: np.ndarray, groups: np.ndarray, label: str) -> None:
    """A shuffled StratifiedKFold can quietly overstate accuracy when
    correlated samples (an aftershock sequence, or the same location sampled
    twice) land on both sides of a fold. Grouping every fold by year (no
    fold sees a year it partly trained on) or by grid_cell (no fold sees a
    location it partly trained on) is a stricter, more honest check of
    whether the model generalizes rather than partly memorizes."""
    n_groups = len(set(groups))
    n_splits = min(5, n_groups)
    if n_splits < 2:
        print(f"  {label}: skipped (only {n_groups} distinct group(s))")
        return
    cv = GroupKFold(n_splits=n_splits)
    y_pred = cross_val_predict(model, X, y, cv=cv, groups=groups)
    print(f"  {label} ({n_splits}-fold, {n_groups} groups): accuracy={(y_pred == y).mean():.4f}")


def main() -> None:
    if not DATA_PATH.exists():
        raise SystemExit(f"{DATA_PATH.name} not found - run ml/build_earthquake_training_data.py first")

    X, y, years, grid_cells = _load_dataset(DATA_PATH)
    print(f"Training on {len(X)} real rows from {DATA_PATH.name} (USGS earthquake catalog)")

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=8,
        min_samples_leaf=5,
        random_state=RANDOM_SEED,
        class_weight="balanced",
    )

    # 5-fold stratified cross-validation, same rationale as the landslide
    # model: an honest accuracy estimate on a dataset this size, with every
    # fold keeping the same class ratio so the rare HIGH class isn't starved.
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=RANDOM_SEED)
    proba_cv = cross_val_predict(model, X, y, cv=cv, method="predict_proba")
    y_pred_argmax = np.argmax(proba_cv, axis=1)
    y_pred_thresholded = _apply_decision_rule(proba_cv)

    print(f"5-fold cross-validated accuracy (plain argmax):   {(y_pred_argmax == y).mean():.4f}")
    print(f"5-fold cross-validated accuracy (HIGH @ P>={HIGH_RISK_DECISION_THRESHOLD}): {(y_pred_thresholded == y).mean():.4f}")
    print(f"\nWith the {HIGH_RISK_DECISION_THRESHOLD} HIGH-risk threshold (what actually gets served):")
    print(classification_report(y, y_pred_thresholded, target_names=[RISK_LABELS[i] for i in sorted(RISK_LABELS)]))

    print("\nGrouped holdout checks (stricter than shuffled K-fold - catches leakage from correlated samples):")
    _report_group_holdout(model, X, y, years, "grouped by year")
    _report_group_holdout(model, X, y, grid_cells, "grouped by ~1deg grid cell")

    model.fit(X, y)
    joblib.dump({
        "model": model,
        "feature_names": FEATURE_NAMES,
        "risk_labels": RISK_LABELS,
        "high_risk_decision_threshold": HIGH_RISK_DECISION_THRESHOLD,
    }, MODEL_PATH)
    print(f"Saved model to {MODEL_PATH}")


if __name__ == "__main__":
    main()
