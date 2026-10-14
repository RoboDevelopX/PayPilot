"""Train the late-payment risk model on the SYNTHETIC invoices.

Chronological split by issue date (see ml.features.time_split):
    fit          first 70%  fit both candidates
    calibration  next 10%   pick the candidate by ROC-AUC, then calibrate
    test         last 20%   untouched here; ml/evaluate.py reports on it
Candidates: scaled logistic regression and a small histogram gradient-boosted
tree model. Calibration is sigmoid (Platt): isotonic regression tends to
overfit with fewer than about 1,000 calibration rows and this window has 500.
Every candidate/calibration pair is kept in the artifact, so the report shows
the combinations that were not picked too.

Run: python -m ml.train_risk
"""

from __future__ import annotations

import hashlib

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from ml.features import RISK_FEATURES, risk_frame, time_split
from ml.generate_data import INVOICES_CSV, SEED
from ml.predict import ARTIFACTS_DIR, RISK_ARTIFACT

CALIBRATION = "sigmoid"
CALIBRATION_METHODS = ("sigmoid", "isotonic")


def candidates() -> dict:
    return {
        "logistic_regression": make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000)),
        "gradient_boosting": HistGradientBoostingClassifier(
            max_iter=200,
            learning_rate=0.05,
            max_leaf_nodes=15,
            l2_regularization=1.0,
            random_state=SEED,
        ),
    }


def matrix(frame: pd.DataFrame) -> np.ndarray:
    return frame[list(RISK_FEATURES)].to_numpy(dtype=float)


def train(frame: pd.DataFrame, data_sha256: str = "") -> dict:
    fit, cal, test = time_split(frame)
    fitted, val_auc, calibrated = {}, {}, {}
    for name, model in candidates().items():
        model.fit(matrix(fit), fit["late"])
        val_auc[name] = float(roc_auc_score(cal["late"], model.predict_proba(matrix(cal))[:, 1]))
        fitted[name] = model
        calibrated[name] = {
            method: CalibratedClassifierCV(FrozenEstimator(model), method=method).fit(
                matrix(cal), cal["late"]
            )
            for method in CALIBRATION_METHODS
        }
    chosen = max(val_auc, key=val_auc.get)
    return {
        "kind": "late_payment_risk",
        "synthetic": True,
        "features": list(RISK_FEATURES),
        "chosen": chosen,
        "calibration": CALIBRATION,
        "model": calibrated[chosen][CALIBRATION],  # calibrated probabilities for the app
        "base": fitted[chosen],  # smooth uncalibrated model, used for explanations
        "candidates": calibrated,
        "validation_roc_auc": val_auc,
        "reference": {f: float(np.median(fit[f])) for f in RISK_FEATURES},
        "train_base_rate": float(fit["late"].mean()),
        "split": {
            "fit_rows": len(fit),
            "calibration_rows": len(cal),
            "test_rows": len(test),
            "test_from": str(test["issued_at"].iloc[0]) if len(test) else None,
        },
        "data_sha256": data_sha256,
        "sklearn_version": sklearn.__version__,
        "seed": SEED,
    }


def main() -> None:
    raw = INVOICES_CSV.read_bytes()
    frame = risk_frame(pd.read_csv(INVOICES_CSV))
    artifact = train(frame, hashlib.sha256(raw).hexdigest())
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, RISK_ARTIFACT, compress=3)
    print(
        f"late-payment risk: chose {artifact['chosen']} "
        f"(validation ROC-AUC {artifact['validation_roc_auc']}), saved {RISK_ARTIFACT.name} "
        f"({RISK_ARTIFACT.stat().st_size / 1024:.0f} KB)"
    )


if __name__ == "__main__":
    main()
