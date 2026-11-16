PY := .venv/bin/python
PYTEST := .venv/bin/pytest

.PHONY: install lint types test-unit test-contract test-int test-java test-ui test-tf test-security test-gpu record check stack-up stack-down stack-logs

install:
	$(PY) -m pip install -q -e ".[dev]"

lint:
	.venv/bin/ruff check aisvc tests install.py
	.venv/bin/ruff format --check aisvc tests install.py

types:
	.venv/bin/mypy

test-unit:
	$(PYTEST) tests/unit -q

test-contract:
	$(PYTEST) tests/contract -q

# Fakes, not containers: fast, and what CI runs. The stack below is
# what talks to something real.
test-int:
	$(PYTEST) tests/integration -q

COMPOSE = docker compose -f deploy/compose/docker-compose.yml

# The CloudStack simulator takes minutes to come up, so --wait is the
# point of these: `stack-up` returns when the API answers, not when the
# container starts.
stack-up:
	$(COMPOSE) up -d --wait

stack-down:
	$(COMPOSE) down -v

stack-logs:
	$(COMPOSE) logs --tail=50

# JDK 17 from Homebrew is keg-only, so point JAVA_HOME at it explicitly.
JAVA_HOME ?= /opt/homebrew/opt/openjdk@17

test-java:
	@test -d "$(HOME)/.m2/repository/org/apache/cloudstack/cloud-api" || \
		{ echo "CloudStack artifacts are not installed; see java-plugin/README.md"; exit 1; }
	cd java-plugin && JAVA_HOME=$(JAVA_HOME) PATH=$(JAVA_HOME)/bin:$$PATH mvn -q test

test-ui:
	cd ui && npm run --silent test:unit
	@echo "Playwright needs a CloudStack; see ui/README.md"
	cd ui && npx playwright test

test-tf:
	cd terraform && go build ./... && go vet ./... && go test ./...

test-security:
	$(PYTEST) tests/security -q

test-gpu:
	$(PYTEST) -m gpu -q --override-ini="addopts="

record:
	@echo "refreshes tests/fixtures/cloudstack against a lab zone; milestone 2"; exit 1

check: lint types test-unit test-contract test-int test-security
