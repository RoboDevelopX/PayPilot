# PayPilot developer commands. Run `make setup` once, then `make run`.
PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup run demo test lint fmt seed train smoke invoice paypal-token-reset secrets clean

setup:  ## create the venv, install deps, install the git pre-commit hook
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements-dev.txt
	@test -f .env || cp .env.example .env
	-$(BIN)/pre-commit install
	@echo "Try it now: make demo (no keys needed). For the real sandbox, fill in .env, then: make run"

run:  ## start the app on http://localhost:8000 (real sandbox, keys from .env)
	$(BIN)/uvicorn app.main:create_app --factory --reload --port 8000

demo:  ## DEMO MODE: sample data, recorded PayPal answers when needed, separate demo.db
	DEMO_MODE=1 DATABASE_URL=sqlite:///./demo.db $(BIN)/uvicorn app.main:create_app --factory --port 8000

test:  ## unit + integration tests (live tests skip unless keys are set)
	$(BIN)/pytest

lint:
	$(BIN)/ruff check .
	$(BIN)/black --check .
	$(BIN)/python scripts/check_secrets.py

fmt:
	$(BIN)/ruff check --fix .
	$(BIN)/black .

seed:  ## load realistic demo data into the database
	$(BIN)/python -m scripts.seed

train:  ## regenerate synthetic data, retrain both models, write metrics
	$(BIN)/python -m ml.generate_data
	$(BIN)/python -m ml.train_risk
	$(BIN)/python -m ml.train_anomaly
	$(BIN)/python -m ml.evaluate

invoice:  ## M1 proof: one real sandbox invoice
	$(BIN)/python -m scripts.create_sandbox_invoice

paypal-token-reset:  ## after ticking an app feature: revoke PayPal's cached token, show scopes
	$(BIN)/python -m scripts.paypal_token_reset

smoke:  ## live end-to-end check against the PayPal sandbox (ARGS="--llm --record")
	$(BIN)/python -m scripts.smoke_test $(ARGS)

secrets:
	$(BIN)/python scripts/check_secrets.py

clean:
	rm -rf .pytest_cache .ruff_cache paypilot.db* demo.db*
