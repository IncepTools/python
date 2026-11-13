# inceptools — development tasks.
#
# `make ci` runs what CI runs, in the same order. If it passes locally it
# should pass there; if it does not, that difference is a bug in this file.

UV ?= uv
RUN := $(UV) run
PKG := src/inceptools
ALL := src tests examples

.DEFAULT_GOAL := help
.PHONY: help install test test-fast test-matrix cov lint format format-check types check ci \
        docs docs-links docs-serve workflows build clean audit outdated \
        example-minimal example-todo example-worker routes pre-commit

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create the environment with every extra
	$(UV) sync --all-extras

test: ## Run the test suite
	$(RUN) pytest

test-fast: ## Stop at the first failure
	$(RUN) pytest -x -q

test-matrix: ## Run the suite on every supported Python
	@for v in 3.11 3.12 3.13 3.14 3.15; do \
		printf "python %s -> " "$$v"; \
		$(UV) run --python $$v --all-extras --isolated pytest 2>&1 | tail -1; \
	done

cov: ## Run tests with a coverage report
	$(RUN) pytest --cov=inceptools --cov-report=term-missing --cov-report=xml

lint: ## Check style and common mistakes
	$(RUN) ruff check $(ALL)

format: ## Format the code in place
	$(RUN) ruff format $(ALL)
	$(RUN) ruff check $(ALL) --fix

format-check: ## Fail if anything is unformatted
	$(RUN) ruff format --check $(ALL)

types: ## Type-check the package
	$(RUN) mypy

docs-links: ## Fail on a broken relative link in the docs
	@$(RUN) python scripts/check_links.py

docs: ## Build the documentation site, strictly
	$(RUN) --group docs mkdocs build --strict

docs-serve: ## Serve the documentation site with live reload
	$(RUN) --group docs mkdocs serve

workflows: ## Lint the GitHub Actions workflows
	@$(RUN) python scripts/check_workflows.py

check: lint format-check types workflows ## Every static check

ci: check test docs-links ## Everything CI runs, in order
	@echo "ok"

build: ## Build the wheel and sdist
	$(UV) build

audit: ## Report known vulnerabilities in the locked dependencies
	$(RUN) --with pip-audit pip-audit

outdated: ## Show dependencies with newer releases
	$(UV) tree --outdated 2>/dev/null || $(UV) pip list --outdated

routes: ## Print the route table of the todo example
	PYTHONPATH=examples/todo $(RUN) python -m inceptools routes main:app

example-minimal: ## Serve examples/minimal on :8080
	$(RUN) python examples/minimal/main.py

example-todo: ## Serve examples/todo on :8080
	$(RUN) python examples/todo/main.py

example-worker: ## Serve examples/worker on :8081
	$(RUN) python examples/worker/main.py

pre-commit: ## Install the git hooks
	$(RUN) --with pre-commit pre-commit install

clean: ## Remove build and cache artefacts
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -path ./.venv -prune -o -name '__pycache__' -type d -print0 | xargs -0 rm -rf
	find . -path ./.venv -prune -o -name '*.egg-info' -type d -print0 | xargs -0 rm -rf
