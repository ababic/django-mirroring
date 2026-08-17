# Task runner: https://github.com/casey/just
# Requires: `uv` and `just`.

help:
    just --list --list-prefix 'just '

clean-pyc:
    find . -name '*.pyc' -exec rm -f {} +
    find . -name '*.pyo' -exec rm -f {} +
    find . -name '*~' -exec rm -f {} +

install: clean-pyc
    uv sync --dev

lint:
    uv run ruff format --check .
    uv run ruff check .

format:
    uv run ruff check . --fix
    uv run ruff format .

test:
    uv run pytest

test-lowest-deps:
    #!/usr/bin/env bash
    set -euo pipefail
    lowest_python=$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["requires-python"].removeprefix(">=").strip())')
    uv run --isolated --python "$lowest_python" --resolution lowest-direct pytest

test-highest-deps:
    uv run --isolated --with 'Django' pytest

coverage:
    uv run pytest --cov mirroring --cov-report=term-missing
    uv run coverage html

check:
    uv run ./testmanage.py check
    uv run ./testmanage.py makemigrations --check --noinput
