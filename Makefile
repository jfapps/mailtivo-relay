# Mailtivo-Relay developer Makefile. Run `make help` for the menu.
.PHONY: help install dev test lint typecheck security check tailwind \
        migrate shell run worker docker-up docker-down docker-logs

PYTHON ?= python
PIP    ?= pip

help: ## Show this help.
	@grep -hE '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | sort \
	| awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime + dev requirements.
	$(PIP) install -r requirements-dev.txt

tailwind: ## Rebuild the production Tailwind bundle.
	tailwindcss -i mailtivo_relay/static_src/input.css -o mailtivo_relay/static/css/app.css --minify

migrate: ## Apply pending migrations.
	$(PYTHON) manage.py migrate

shell: ## Open a Django shell.
	$(PYTHON) manage.py shell

run: ## Run the dev server on :8000.
	$(PYTHON) manage.py runserver 0.0.0.0:8000

worker: ## Run the Q2 cluster worker.
	$(PYTHON) manage.py qcluster

test: ## Run the pytest suite.
	$(PYTHON) -m pytest apps

lint: ## Lint with ruff.
	ruff check apps mailtivo_relay

typecheck: ## Type-check with mypy.
	mypy apps mailtivo_relay

security: ## Run bandit + pip-audit.
	bandit -q -r apps mailtivo_relay
	pip-audit -r requirements.txt --strict

check: lint typecheck test ## Run lint, types, and tests.

docker-up: ## Bring the docker-compose stack up in the background.
	docker compose up -d --build

docker-down: ## Tear the docker-compose stack down (preserves volumes).
	docker compose down

docker-logs: ## Tail logs from the docker-compose stack.
	docker compose logs -f --tail=200
