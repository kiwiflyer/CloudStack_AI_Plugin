PY := .venv/bin/python
PYTEST := .venv/bin/pytest

.PHONY: install lint types test-unit test-contract test-int test-java test-ui test-tf test-security test-gpu record check

install:
	$(PY) -m pip install -q -e ".[dev]"

lint:
	.venv/bin/ruff check aisvc tests
	.venv/bin/ruff format --check aisvc tests

types:
	.venv/bin/mypy

test-unit:
	$(PYTEST) tests/unit -q

test-contract:
	$(PYTEST) tests/contract -q

test-int:
	$(PYTEST) tests/integration -q

# JDK 17 from Homebrew is keg-only, so point JAVA_HOME at it explicitly.
JAVA_HOME ?= /opt/homebrew/opt/openjdk@17

test-java:
	@test -d "$(HOME)/.m2/repository/org/apache/cloudstack/cloud-api" || \
		{ echo "CloudStack artifacts are not installed; see java-plugin/README.md"; exit 1; }
	cd java-plugin && JAVA_HOME=$(JAVA_HOME) PATH=$(JAVA_HOME)/bin:$$PATH mvn -q test

test-ui:
	@echo "milestone 6: not scaffolded yet"; exit 1

test-tf:
	@echo "milestone 8: not scaffolded yet"; exit 1

test-security:
	$(PYTEST) tests/security -q

test-gpu:
	$(PYTEST) -m gpu -q --override-ini="addopts="

record:
	@echo "refreshes tests/fixtures/cloudstack against a lab zone; milestone 2"; exit 1

check: lint types test-unit test-contract test-int test-security
