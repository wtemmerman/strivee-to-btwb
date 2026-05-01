.PHONY: install dev-install test test-cov test-watch lint format start help setup-python

help:
	@echo "Available Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make setup-python   - Ensure correct Python version is installed via uv"
	@echo "  make install        - Install production dependencies with uv"
	@echo "  make dev-install    - Install development and production dependencies with uv"
	@echo ""
	@echo "Development:"
	@echo "  make start          - Start the service locally"
	@echo "  make test           - Run tests"
	@echo "  make test-cov       - Run tests with coverage report"
	@echo "  make test-watch     - Run tests in watch mode (requires pytest-watch)"
	@echo ""
	@echo "Quality:"
	@echo "  make lint           - Run linting checks"
	@echo "  make format         - Format code"
	@echo ""
	@echo "Cleaning:"
	@echo "  make clean          - Remove __pycache__ and .pyc files"
	@echo ""

setup-python:
	@command -v uv >/dev/null 2>&1 || { echo "uv is not installed. Please install it from https://docs.astral.sh/uv/"; exit 1; }
	uv python pin $(shell cat .python-version)
	uv python install

install: setup-python
	uv sync --all-extras

dev-install: setup-python
	uv sync --extra dev

start:
	uv run strivee-btwb

test:
	uv run pytest tests/ -v

test-cov:
	uv run pytest tests/ --cov=strivee_btwb --cov-report=html --cov-report=term-missing
	@echo "Coverage report generated: htmlcov/index.html"

test-watch:
	uv run pytest-watch tests/ -- -v

lint:
	@echo "Running ruff lint..."
	uv run ruff check src tests

format:
	@echo "Formatting with ruff..."
	uv run ruff format src tests
	@echo "Organizing imports with ruff..."
	uv run ruff check --select I --fix src tests

clean:
	find . -type d -name "__pycache__" -exec rm -r {} +
	find . -type f -name "*.pyc" -delete

.DEFAULT_GOAL := help
