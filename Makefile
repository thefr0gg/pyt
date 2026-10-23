.PHONY: dev test test-sabr typecheck lint format upload \
        clean clean-build clean-pyc install help

dev:
	pip install poetry
	poetry install --with dev

test:
	poetry run pytest tests/ -v

test-sabr:
	poetry run pytest tests/test_sabr_proto.py tests/test_sabr_ump.py \
		tests/test_sabr_session.py tests/test_sabr_live.py -v

typecheck:
	poetry run mypy pyt/ --ignore-missing-imports

lint:
	poetry run black --check pyt/ tests/

format:
	poetry run black pyt/ tests/


clean: clean-build clean-pyc

clean-build:
	rm -fr build/
	rm -fr dist/
	rm -fr .eggs/
	find . -name '*.egg-info' -exec rm -fr {} +
	find . -name '*.egg' -exec rm -f {} +
	find . -name '*.DS_Store' -exec rm -f {} +

clean-pyc:
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '*~' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +

install: clean
	pip install -e .

help:
	@echo "dev         - install all dependencies"
	@echo "test        - run the full test suite"
	@echo "test-sabr   - run just the SABR/UMP test files"
	@echo "typecheck   - run mypy"
	@echo "lint        - check formatting with black (no changes written)"
	@echo "format      - reformat pyt/ and tests/ with black"
	@echo "upload      - build and publish to PyPI"
	@echo "clean       - remove build and cache artifacts"
	@echo "install     - install the package in editable mode"
