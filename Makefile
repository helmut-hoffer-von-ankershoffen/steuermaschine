# The single entry point for CI and release: standard tools only (uv, ruff, mypy, pyright, pytest, gitleaks, commitizen).
.PHONY: install lint format typecheck test secrets audit build check release clean

# The core with mypy's default rules; the codes below fail on today's code (debt: drop one when its findings are fixed).
# The repository's [tool.mypy] (strict) stays the debt measure of bin/debt.py, so the gate ignores it.
MYPY_DEBT := arg-type assignment attr-defined empty-body index operator union-attr var-annotated annotation-unchecked

install:
	uv sync --all-packages --locked

# ruff format is not in the gate yet: most files predate it, and a mass reformat would collide with open branches.
lint:
	uv run ruff check src examples tests

format:  # apply ruff format (not yet enforced; see lint)
	uv run ruff format src examples tests

typecheck:
	uv run mypy --config-file= --python-version 3.12 $(MYPY_DEBT:%=--disable-error-code %) src/steuermaschine
	uv run pyright

# Coverage floor of the gate (line+branch, core+heinzel plugin, as CI measures it today); [tool.coverage] fail_under
# = 90 stays the target that bin/check reports as debt.
COV_FLOOR := 82

# Typer forces colored help when GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set; the help tests read plain text.
test:
	env -u GITHUB_ACTIONS -u FORCE_COLOR -u PY_COLORS uv run pytest --cov --cov-report=term --cov-report=xml --cov-fail-under=$(COV_FLOOR)

secrets:
	gitleaks git --redact --no-banner .
	gitleaks dir --redact --no-banner .

audit:
	uv audit --locked --preview-features audit-command

build:
	rm -rf dist && uv build --package steuermaschine --out-dir dist

check: lint typecheck test secrets

release:  # bump from Conventional Commits, write CHANGELOG.md, commit, tag v*; then: git push --follow-tags
	uv run cz bump --yes

clean:
	rm -rf dist .coverage coverage.xml .pytest_cache .mypy_cache .ruff_cache
