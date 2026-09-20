.PHONY: help install install-dev test test-all lint clean run-example dashboard

help:
	@echo "Lead Finder Agent - common tasks"
	@echo ""
	@echo "  make install      Install the package (runtime deps only)"
	@echo "  make install-dev  Install the package with dev/test dependencies"
	@echo "  make test         Run the offline test suite (no network)"
	@echo "  make test-all     Run all tests including network integration tests"
	@echo "  make run-example  Run a sample offline search and export the results"
	@echo "  make dashboard    Serve the Dashboard / Control Center on 127.0.0.1:8765"
	@echo "  make clean        Remove caches and local build artifacts"

install:
	python -m pip install .

install-dev:
	python -m pip install -e ".[dev]"

test:
	python -m pytest -m "not integration"

test-all:
	python -m pytest

run-example:
	python -m lead_finder_agent search --city Aden --type restaurants --limit 10 --providers sample
	python -m lead_finder_agent list --limit 10
	python -m lead_finder_agent export --format json --output exports/leads.json

dashboard:
	python -m lead_finder_agent dashboard --port 8765

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache build dist *.egg-info .coverage htmlcov
