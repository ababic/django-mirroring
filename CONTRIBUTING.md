# Contributing guidelines

Thank you for your interest in this project. Contributions that align with the
roadmap (mirror refresh, restore safety, selective media sync) are welcome.

## Installation

```sh
git clone https://github.com/ababic/django-mirroring
cd django-mirroring
```

We use [just](https://github.com/casey/just) as a task runner and
[uv](https://docs.astral.sh/uv/) to manage Python dependencies.

```sh
just install
just test
```

## Quality assurance

```sh
just help              # List recipes
just install           # Install Python deps with uv
just lint              # Ruff format check + lint
just format            # Ruff autofix + format
just test              # pytest
just test-lowest-deps  # Lowest direct dependency resolution
just test-highest-deps # Latest Django on an isolated env
just coverage          # pytest with coverage
just check             # Django system check + migration check
```

## Writing tests

Test modules live in `tests/`. The nested Django settings module is
`mirroring.test.settings` (no Wagtail). Prefer `@pytest.mark.unit` for fast
isolated cases and `@pytest.mark.django_db` when the ORM is required.

## Continuous integration

On every push and pull request, GitHub Actions:

- Runs Ruff lint/format checks
- Runs the suite with coverage and Django system/migration checks
- Runs lowest- and highest-dependency isolated jobs
- Runs a Python 3.12–3.14 compatibility matrix

Creating a GitHub release publishes to PyPI via [trusted publishing](https://docs.pypi.org/trusted-publishers/)
(OIDC — no long-lived API token required). Configure:

1. A GitHub Actions environment named `pypi` on this repository
2. A trusted publisher on PyPI for project `django-mirroring`:

| Field | Value |
|-------|-------|
| Owner | `ababic` |
| Repository | `django-mirroring` |
| Workflow | `publish.yml` |
| Environment | `pypi` |

Optional: keep a project-scoped `PYPI_PUBLISH_TOKEN` environment secret as an
emergency fallback only — the workflow does not use it by default.

## Code review

Open a pull request with a short summary of intent and how to verify the change.
Include unit tests for behaviour changes.

## Releases

On `main`:

1. Update the version in `pyproject.toml` and `src/mirroring/__init__.py`.
2. Update [CHANGELOG.md](CHANGELOG.md).
3. Commit, tag, and push (`git tag -a v0.2.0 -m "Release v0.2.0" && git push --tags`).
4. Create a GitHub release from the tag so the publish workflow can run
   (PyPI trusted publisher must match the table above).
