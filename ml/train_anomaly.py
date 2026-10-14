"""Train the payout anomaly detector on the SYNTHETIC payouts.

Isolation Forest is unsupervised: it is fitted without the anomaly labels and
learns what ordinary payouts look like. The flag threshold is a review budget,
not a label-tuned number: the score that flags the top 3% of the holdout
window. The injected labels are used only in ml/evaluate.py, on the test window.

Run: python -m ml.train_anomaly
"""

from __future__ import annotations

import hashlib

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import IsolationForest

from ml.features import ANOMALY_FEATURES, anomaly_frame, time_split
from ml.generate_data import PAYOUTS_CSV, SEED
from ml.predict import ANOMALY_ARTIFACT, ARTIFACTS_DIR

REVIEW_BUDGET = 0.03  # flag about 3 in 100 payouts for a careful look


def matrix(frame: pd.DataFrame) -> np.ndarray:
    return frame[list(ANOMALY_FEATURES)].to_numpy(dtype=float)


def train(frame: pd.DataFrame, data_sha256: str = "") -> dict:
    fit, holdout, test = time_split(frame)
    model = IsolationForest(n_estimators=200, max_samples=256, random_state=SEED)
    model.fit(matrix(fit))
    holdout_scores = -model.score_samples(matrix(holdout))
    return {
        "kind": "payout_anomaly",
        "synthetic": True,
        "features": list(ANOMALY_FEATURES),
        "model": model,
        "threshold": float(np.quantile(holdout_scores, 1 - REVIEW_BUDGET)),
        "review_budget": REVIEW_BUDGET,
        # sorted training values per feature, to say which feature is most unusual
        "tails": {f: np.sort(fit[f].to_numpy(dtype=float)) for f in ANOMALY_FEATURES},
        "split": {
            "fit_rows": len(fit),
            "threshold_rows": len(holdout),
            "test_rows": len(test),
        },
        "data_sha256": data_sha256,
        "sklearn_version": sklearn.__version__,
        "seed": SEED,
    }


def load_frame() -> pd.DataFrame:
    payouts = pd.read_csv(PAYOUTS_CSV, keep_default_na=False)
    return anomaly_frame(payouts)


def main() -> None:
    artifact = train(load_frame(), hashlib.sha256(PAYOUTS_CSV.read_bytes()).hexdigest())
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, ANOMALY_ARTIFACT, compress=3)
    print(
        f"payout anomaly: threshold {artifact['threshold']:.3f} (top {REVIEW_BUDGET:.0%} of the "
        f"holdout window), saved {ANOMALY_ARTIFACT.name} "
        f"({ANOMALY_ARTIFACT.stat().st_size / 1024:.0f} KB)"
    )


if __name__ == "__main__":
    main()
