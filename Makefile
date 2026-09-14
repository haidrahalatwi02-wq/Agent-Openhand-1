.PHONY: help install test lint format clean run dev

help:
	@echo "Lead Finder Agent - Available Commands"
	@echo "======================================="
	@echo "  make install    - Install dependencies"
	@echo "  make test       - Run tests"
	@echo "  make lint       - Run linters"
	@echo "  make format     - Format code"
	@echo "  make clean      - Clean up build artifacts"
	@echo "  make run        - Run the agent"
	@echo "  make dev        - Run in development mode"

install:
	pip install -r requirements.txt

pip install -r requirements-dev.txt

test:
	pytest tests/ -v --cov=lead_finder_agent

lint:
	flake8 lead_finder_agent tests
	pylint lead_finder_agent

format:
	black lead_finder_agent tests
	isort lead_finder_agent tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
	find . -type d -name '.pytest_cache' -exec rm -rf {} +
	find . -type d -name '.coverage' -exec rm -rf {} +
	find . -type d -name 'build' -exec rm -rf {} +
	find . -type d -name 'dist' -exec rm -rf {} +
	rm -rf *.egg-info

run:
	python -m lead_finder_agent

dev:
	python -m lead_finder_agent --debug
