.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck js-lint test test-cov coverage-floors ci build clean

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## Install dev dependencies
	uv sync --group dev

lint:  ## Run linter + format check
	uv run ruff check src/ tests/ scripts/
	uv run ruff format --check src/ tests/ scripts/

format:  ## Format code
	uv run ruff format src/ tests/ scripts/
	uv run ruff check --fix src/ tests/ scripts/

typecheck:  ## Run type checker
	uv run mypy src/filigree/

js-lint:  ## Run dashboard JavaScript lint + format checks
	npm run lint
	npm run format:check

test:  ## Run tests
	uv run pytest

test-cov:  ## Run tests with coverage
	uv run pytest --cov --cov-report=term-missing --cov-report=json:coverage.json --cov-fail-under=85

coverage-floors:  ## Enforce per-surface coverage floors
	uv run python scripts/check_coverage_floors.py coverage.json

ci: lint typecheck js-lint test-cov coverage-floors  ## Run full CI locally (with coverage)

build:  ## Build sdist and wheel
	uv build

clean:  ## Remove build artifacts
	rm -rf dist/ build/ *.egg-info .mypy_cache .ruff_cache .pytest_cache .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
