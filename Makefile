.PHONY: help install check lint format typecheck test fix clean

help: ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## uv sync (install all deps including dev)
	uv sync

check: lint typecheck test ## full inner loop (CI runs this)

fix: ## autofix lint + format, then run full check
	uv run ruff check --fix src tests
	uv run ruff format src tests
	$(MAKE) check

lint: ## ruff check (no fix) + format --check
	uv run ruff check src tests
	uv run ruff format --check src tests

format: ## ruff format (writes)
	uv run ruff format src tests

typecheck: ## basedpyright strict
	uv run basedpyright src tests

test: ## pytest
	uv run pytest

clean: ## remove caches
	rm -rf .pytest_cache .ruff_cache .basedpyright .hypothesis .coverage htmlcov dist build
	find . -type d -name __pycache__ -exec rm -rf {} +
