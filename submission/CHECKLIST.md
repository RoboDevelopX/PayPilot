# PayPilot: requirements checklist

✓ done and verified by tests or a run · ◐ partly done · ✗ not done · ☐ yours to do before submitting

## Still to do before submitting

Deadline: **Thursday Nov 12, 2026, 12:00 pm Pacific** (1:30 am Nov 13 in India), per the [official rules](https://paypalaihackathon.devpost.com/rules).

- ✓ **Live sandbox check.** Passed on 2026-10-14 with US sandbox accounts: `make invoice`, `make smoke ARGS="--llm --record"` (every step PASS) and `pytest -m live`.
- ✓ **Real recordings committed.** `invoice.json` and `payout_get.json` are captures from the US sandbox (emails redacted); `payout_create.json` is still hand-written.
- ✓ **AI tools rule checked.** The rules don't require disclosing AI coding tools; the README lists the AI used inside the product.
- ✓ **Deployed to Render** on 2026-10-14: https://paypilot-ytst.onrender.com. `/healthz` reports the PayPal sandbox, Gemini and both ML models, and a payout from chat works. Using Render also makes the project eligible for Render's sponsor prize ("all eligible submissions that use any Render tools").
- ☐ **Webhook:** create it in the sandbox app, set `PAYPAL_WEBHOOK_ID` on Render, pay a sandbox invoice as the Personal account, and check that PayPilot marks it paid.
- ☐ **Repo About box:** add a one-line description and the Render URL as the website, so judges land on the live app.
- ☐ **Record the video** with [DEMO_SCRIPT.md](DEMO_SCRIPT.md): under 3 minutes, showing the app running, no music you don't own, no unneeded third-party brands. Upload it to **YouTube as Public**.
- ☐ **Write the `[TODO: personal story]` parts** in [DEVPOST.md](DEVPOST.md) and the video opener, and add the video and app links.
- ☐ **Fill in the Devpost form**: paste the sections from DEVPOST.md, the repo URL, the YouTube link, and the testing instructions from DEVPOST.md. Judges must be able to use the app free of charge until Dec 15, 2026, so keep the Render service and its keys in place until then.

## Product

| Requirement | Status | Where |
|---|---|---|
| Chat to invoice (Invoicing v2) with clarifying questions | ✓ | `app/agent/`, `app/services/actions.py`; `tests/test_chat_agent.py`, `tests/test_demo.py` |
| Policy engine: auto-approve limit L | ✓ | `app/policy/engine.py`; boundary tests in `tests/test_policy.py` |
| Daily and monthly caps that hard-block, even with approval | ✓ | same; approval re-check in `tests/test_demo.py` |
| Recipient allowlist | ✓ | same; Rules page |
| Currency allowlist | ✓ | same |
| Readable reason for every decision | ✓ | every `Check` carries a sentence; shown in chat and on approval cards |
| Guardrailed payouts (Payouts API) with an approval queue | ✓ | `app/paypal/payouts.py`, `app/services/approvals.py`; `tests/test_payouts_approvals.py` |
| Single-use approvals | ✓ | atomic claim in `app/services/approvals.py` |
| Follow-ups: webhooks plus polling | ✓ | `app/services/webhooks.py`, `app/services/followups.py`; `tests/test_followups.py` |
| Tone ladder friendly, firm, final, and the remind endpoint | ✓ | `app/services/followups.py`, `app/paypal/invoices.py` |
| Append-only audit trail | ✓ | SQLite triggers in `app/db.py`; `tests/test_db.py` |
| ML: late-payment risk | ✓ | `ml/train_risk.py`, `app/services/ml.py`; `tests/test_ml.py` |
| ML: payout anomaly detection (Isolation Forest) | ✓ | `ml/train_anomaly.py` |
| ML can only raise the approval level | ✓ | `app/policy/engine.py`; tested |
| Dashboard | ✓ | `app/services/dashboard.py`, `app/templates/dashboard.html` |
| Stretch: multi-currency | ◐ | several currencies can be allowed, caps and totals are per currency, no FX conversion |
| Stretch: per-agent budgets | ✗ | not built (first item cut, as planned) |

## Security

| Requirement | Status | Where |
|---|---|---|
| Secrets only in environment variables, `.env.example` | ✓ | `app/config.py` (`SecretStr`), `.env.example` |
| Pre-commit secret check | ✓ | `scripts/check_secrets.py` in `.pre-commit-config.yaml` and CI (a small in-repo scanner rather than gitleaks, so it needs no extra install) |
| Sandbox only, obvious in code and UI | ✓ | base URL validator in `app/config.py`; SANDBOX badge and footer on every page |
| Money as `Decimal`, never float | ✓ | `app/money.py`; integer cents in the database |
| User text and invoice descriptions treated as untrusted | ✓ | the model has no PayPal access; rule changes always need approval; `prompts/system.md` |
| Chat endpoint rate-limited | ✓ | `app/services/ratelimit.py`; spoofed `X-Forwarded-For` tested in `tests/test_ui.py` |
| Optional password for a public deployment | ✓ | `app/auth.py` (`APP_PASSWORD`) |

## Hackathon rules

| Requirement | Status | Where |
|---|---|---|
| Public repo | ✓ | github.com/RoboDevelopX/PayPilot is public |
| MIT LICENSE | ✓ | `LICENSE` |
| All source and run instructions included | ✓ | `README.md`, `Makefile` |
| Clean incremental commits | ✓ | one commit per milestone (M1 to M9) on `paypilot-v2` |
| No copyrighted music or unneeded trademarks in the demo | ☐ | reminder in `DEMO_SCRIPT.md` |
| Demo video under 3 minutes, public on YouTube | ☐ | `DEMO_SCRIPT.md` is 2:30 |
| License visible in the repo's About box | ✓ | GitHub detects MIT from `LICENSE` |
| Working build judges can use, with testing instructions | ✓ | hosted at https://paypilot-ytst.onrender.com; run instructions in `README.md`; testing text in `DEVPOST.md` |
| English materials | ✓ | |

## ML honesty

| Requirement | Status | Where |
|---|---|---|
| Synthetic data labeled as synthetic | ✓ | `ml/MODEL_CARD.md`, report title, UI says "trained on synthetic data" |
| Real evaluation metrics, not cherry-picked | ✓ | `ml/reports/REPORT.md`: every candidate, baselines, bootstrap intervals, recall by anomaly type |
| Seeds set for reproducibility | ✓ | seed 42; `make train` reproduces artifacts byte for byte |
| Artifacts small enough to commit, or one-command rebuild | ✓ | 135 KB and 633 KB in `ml/artifacts/`, rebuilt by `make train` |

## Demo mode

| Requirement | Status | Where |
|---|---|---|
| `DEMO_MODE` flag that seeds realistic data | ✓ | `app/services/seed.py` |
| Falls back to recorded responses if the sandbox is flaky | ✓ | `app/paypal/recorded.py`; `tests/test_demo.py` |
| Clearly labeled in the UI and README | ✓ | banner on every page, `demo-recorded-` debug ids, "Demo script" replies; README section |
| Real sandbox path stays the default | ✓ | `DEMO_MODE=0` by default; recordings only wired in demo mode |
| Recordings captured from the real sandbox | ◐ | `invoice.json` and `payout_get.json` captured 2026-10-14; `payout_create.json` hand-written |

## Stack and tooling

| Requirement | Status |
|---|---|
| Python 3.11+, FastAPI, Pydantic v2, SQLite with SQLAlchemy, httpx | ✓ |
| scikit-learn, LLM tool use (Gemini or Claude) | ✓ |
| Jinja + HTMX UI, responsive, light and dark themes | ✓ |
| Render with `render.yaml` | ✓ (deployed from the blueprint) |
| ruff and black | ✓ (CI and pre-commit) |
| Makefile: setup, run, test, seed, train (plus demo, lint, smoke) | ✓ |
| Fresh clone runs with `make setup && make demo` | ✓ (checked on a clean copy) |

## Submission files

| File | Status |
|---|---|
| [README.md](README.md) and the main [README](../README.md): Mermaid diagram, APIs, AI tools, setup under 10 minutes, demo mode, license | ✓ |
| [DEVPOST.md](DEVPOST.md) with `[TODO: personal story]` placeholders | ✓ (placeholders are yours to fill) |
| [DEMO_SCRIPT.md](DEMO_SCRIPT.md), 2:30 shot list | ✓ |
| [INTERVIEW_NOTES.md](INTERVIEW_NOTES.md): 5 resume bullets, 8 questions, improvements with real data | ✓ |
| This checklist | ✓ |
