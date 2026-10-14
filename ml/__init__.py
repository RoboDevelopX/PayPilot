"""PayPilot's classical ML layer: late-payment risk and payout anomaly detection.

Trained only on SYNTHETIC data (see ml/MODEL_CARD.md). The app treats both
models as advisory: they can raise an approval level or change a reminder's
timing and tone, never lower a level or move money on their own.
"""
