"""
Trains a lightweight RandomForestClassifier that scores pan-India transport
corridors for landslide/disruption risk from 5 terrain + monsoon features,
and exports it for backend/risk_engine.py to load at API startup.

Prefers ml/real_training_data.csv when present - 640 rows built by
ml/build_real_training_data.py from NASA's Global Landslide Catalog (2,389
real recorded pan-India landslides), with real historical rainfall and soil
moisture (Open-Meteo archive) and real terrain gradient (Open-Topo-Data
SRTM30m) per row. Falls back to a synthesized dataset (hand-tuned risk
formula + noise) only if that file doesn't exist yet - a placeholder for
bootstrapping before real data was available, not the default anymore.

Run directly:
    cd backend && python ml/train_risk_model.py
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
N_SAMPLES = 6000
REAL_DATA_PATH = Path(__file__).parent / "real_training_data.csv"

FEATURE_NAMES = [
    "rainfall_mm_24h",
    "elevation_gradient_pct",
    "soil_saturation_idx",
    "historical_incident_rate",
    "monsoon_month",
]

# 0: SAFE, 1: MODERATE, 2: HIGH_LANDSLIDE_RISK
RISK_LABELS = {0: "SAFE", 1: "MODERATE", 2: "HIGH_LANDSLIDE_RISK"}

MODEL_PATH = Path(__file__).parent / "risk_model.joblib"


def _synthesize_dataset(n: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Generates n synthetic corridor samples with a hand-tuned ground-truth
    risk score, thresholded into the 3 risk classes."""
    monsoon_month = rng.binomial(1, 0.35, size=n).astype(float)

    # Rainfall is heavier and more variable during India's Jun-Sep monsoon.
    rainfall_mm_24h = np.where(
        monsoon_month == 1,
        rng.gamma(shape=4.0, scale=45.0, size=n),
        rng.gamma(shape=2.0, scale=18.0, size=n),
    )
    rainfall_mm_24h = np.clip(rainfall_mm_24h, 0, 400)

    elevation_gradient_pct = np.clip(rng.normal(22, 14, size=n), 0, 65)

    # Soil saturation tracks rainfall with noise rather than a pure function of it.
    soil_saturation_idx = np.clip(
        0.15 + 0.55 * (rainfall_mm_24h / 250.0) + rng.normal(0, 0.12, size=n), 0, 1
    )

    historical_incident_rate = np.clip(rng.exponential(1.1, size=n), 0, 8)

    # Ground-truth risk score: rainfall + slope dominate landslide risk in NE
    # India's hill districts, with saturation/history/season as secondary
    # compounding factors. Thresholded into 3 classes after adding noise.
    score = (
        0.32 * (rainfall_mm_24h / 400.0)
        + 0.28 * (elevation_gradient_pct / 65.0)
        + 0.20 * soil_saturation_idx
        + 0.12 * (historical_incident_rate / 8.0)
        + 0.08 * monsoon_month
        + rng.normal(0, 0.05, size=n)
    )
    labels = np.digitize(score, bins=[0.35, 0.6]).astype(int)

    X = np.column_stack(
        [rainfall_mm_24h, elevation_gradient_pct, soil_saturation_idx, historical_incident_rate, monsoon_month]
    )
    return X, labels


def _load_real_dataset(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
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
    years = grid_cells = None
    if REAL_DATA_PATH.exists():
        X, y, years, grid_cells = _load_real_dataset(REAL_DATA_PATH)
        print(f"Training on {len(X)} real rows from {REAL_DATA_PATH.name} (NASA Global Landslide Catalog + live rainfall/elevation)")
    else:
        rng = np.random.default_rng(RANDOM_SEED)
        X, y = _synthesize_dataset(N_SAMPLES, rng)
        print(f"{REAL_DATA_PATH.name} not found - falling back to {N_SAMPLES} synthesized rows (run build_real_training_data.py first for real data)")

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=8,
        min_samples_leaf=5,
        random_state=RANDOM_SEED,
        class_weight="balanced",
    )

    # 5-fold stratified cross-validation: every row gets used for both
    # training and (out-of-fold) validation across the 5 folds, instead of
    # one lucky/unlucky 80/20 split - a more defensible accuracy estimate on
    # a dataset this small (640 rows), and every fold keeps the same class
    # ratio (stratified) so the rare Moderate class isn't starved in any fold.
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=RANDOM_SEED)
    y_pred = cross_val_predict(model, X, y, cv=cv)
    print(f"5-fold cross-validated accuracy: {(y_pred == y).mean():.4f}")
    print(classification_report(y, y_pred, target_names=[RISK_LABELS[i] for i in sorted(RISK_LABELS)]))

    if years is not None:
        print("Grouped holdout checks (stricter than shuffled K-fold - catches leakage from correlated samples):")
        _report_group_holdout(model, X, y, years, "grouped by year")
        _report_group_holdout(model, X, y, grid_cells, "grouped by ~1deg grid cell")

    # Final deployed model is fit on the full dataset - cross-validation above
    # is purely for an honest accuracy estimate, not what gets saved.
    model.fit(X, y)
    joblib.dump({"model": model, "feature_names": FEATURE_NAMES, "risk_labels": RISK_LABELS}, MODEL_PATH)
    print(f"Saved model to {MODEL_PATH}")


if __name__ == "__main__":
    main()
