# Quality gates. `make check` is what CI runs and what to run before committing.
#
# Everything here is OFFLINE and needs no API key. The two scripts that touch
# the network — scripts/verify_contract.py and `corpus run --capture-raw` — are
# run deliberately, by hand, and are never part of this.

PY := .venv/bin/python
RUFF := .venv/bin/ruff
MYPY := .venv/bin/mypy

.PHONY: help check lint format fmt types test coverage secrets install clean contract

help:
	@echo "make install   create .venv and install the package plus dev tools"
	@echo "make check     lint + format check + types + secrets + tests  (the gate)"
	@echo "make lint      ruff check"
	@echo "make fmt       ruff format (rewrites files)"
	@echo "make types     mypy --strict over corpus/"
	@echo "make test      pytest"
	@echo "make coverage  pytest with a coverage report on the money/history paths"
	@echo "make secrets   scripts/check_secrets.sh"
	@echo ""
	@echo "make contract  LIVE provider check, ~\$$0.01. Never run by CI."

install:
	uv venv
	uv pip install -e . pytest pytest-cov ruff mypy

# The gate. Ordered cheapest-first so an obvious failure surfaces in seconds
# rather than after the full test run.
check: lint format types secrets test
	@echo ""
	@echo "all checks passed"

lint:
	$(RUFF) check corpus tests scripts

format:
	$(RUFF) format --check corpus tests scripts

fmt:
	$(RUFF) format corpus tests scripts

types:
	$(MYPY)

secrets:
	bash scripts/check_secrets.sh

test:
	$(PY) -m pytest -q

# Coverage is targeted rather than global: budget, ingest, client, and hydrate
# are the paths that handle money and history, and they are the ones worth a
# hard threshold. Coverage of the rendering code matters much less.
coverage:
	$(PY) -m pytest -q \
		--cov=corpus.budget --cov=corpus.x.ingest \
		--cov=corpus.x.client --cov=corpus.x.hydrate \
		--cov-report=term-missing --cov-report=json:coverage.json
	$(PY) scripts/check_coverage.py coverage.json

# Not part of `check`, and deliberately so: this spends real money.
contract:
	$(PY) scripts/verify_contract.py

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.json
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
