"""
Trains a lightweight RandomForestClassifier that scores a location for
flood risk from 5 rainfall/terrain/history features, and exports it for
backend/risk_engine.py to load at API startup.

Uses ml/flood_training_data.csv - 238 rows built by
ml/build_flood_training_data.py from the Dartmouth Flood Observatory's
recorded South Asia flood events (1985-2010) plus real historical rainfall
(Open-Meteo archive), river discharge (Open-Meteo flood API), and elevation
(Open-Topo-Data).

Run directly:
    cd backend && python ml/train_flood_risk_model.py
"""
from __future__ import annotations

import csv
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report
from sklearn.model_selection import GroupKFold, StratifiedKFold, cross_val_predict

RANDOM_SEED = 42
DATA_PATH = Path(__file__).parent / "flood_training_data.csv"
MODEL_PATH = Path(__file__).parent / "flood_risk_model.joblib"

FEATURE_NAMES = [
    "rainfall_mm_72h",
    "river_discharge_m3s",
    "elevation_m",
    "historical_flood_density",
    "monsoon_month",
]

# 0: SAFE, 1: MODERATE, 2: HIGH_FLOOD_RISK
RISK_LABELS = {0: "SAFE", 1: "MODERATE", 2: "HIGH_FLOOD_RISK"}


def _load_dataset(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    X = np.array([[float(r[name]) for name in FEATURE_NAMES] for r in rows])
    y = np.array([int(r["label"]) for r in rows])
    years = np.array([int(r["year"]) for r in rows])
    grid_cells = np.array([r["grid_cell"] for r in rows])
    return X, y, years, grid_cells


def _report_group_holdout(model: RandomForestClassifier, X: np.ndarray, y: np.ndarray, groups: np.ndarray, label: str) -> None:
    """See train_earthquake_risk_model.py's version of this - a stricter
    check than shuffled StratifiedKFold, since no fold sees a year (or grid
    cell) it was partly trained on."""
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
        raise SystemExit(f"{DATA_PATH.name} not found - run ml/build_flood_training_data.py first")

    X, y, years, grid_cells = _load_dataset(DATA_PATH)
    print(f"Training on {len(X)} real rows from {DATA_PATH.name} (Dartmouth Flood Observatory + live rainfall/discharge/elevation)")

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=8,
        min_samples_leaf=5,
        random_state=RANDOM_SEED,
        class_weight="balanced",
    )

    # 5-fold stratified CV, same rationale as the other 3 hazard models - an
    # honest accuracy estimate on a dataset this small (238 rows), with
    # every fold keeping the same class ratio.
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=RANDOM_SEED)
    y_pred = cross_val_predict(model, X, y, cv=cv)
    print(f"5-fold cross-validated accuracy: {(y_pred == y).mean():.4f}")
    print(classification_report(y, y_pred, target_names=[RISK_LABELS[i] for i in sorted(RISK_LABELS)]))

    print("\nGrouped holdout checks (stricter than shuffled K-fold - catches leakage from correlated samples):")
    _report_group_holdout(model, X, y, years, "grouped by year")
    _report_group_holdout(model, X, y, grid_cells, "grouped by ~1deg grid cell")

    model.fit(X, y)
    joblib.dump({"model": model, "feature_names": FEATURE_NAMES, "risk_labels": RISK_LABELS}, MODEL_PATH)
    print(f"Saved model to {MODEL_PATH}")


if __name__ == "__main__":
    main()
