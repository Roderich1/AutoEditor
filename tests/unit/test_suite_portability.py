"""The suite may only import what the CI environment actually installs.

This exists because of a defect it would have caught. Three test modules
imported `click.testing` for a type annotation. It worked locally — `click` was
in the developer environment as a transitive dependency of `faster-whisper`,
pulled in by `uv sync --extra transcription` — and failed on CI, which installs
neither the extra nor anything that depends on click, because typer 0.27 no
longer does.

Nothing in the existing suite could have noticed. A developer environment is
always a superset of the CI one, so "it passes here" says nothing about whether
an import will be there on Ubuntu.

The set to check against is not the *declared* dependencies. `test_gemini_analyzer`
legitimately imports `httpx`, which nothing declares and which is present
because `google-genai` depends on it. What matters is what
`uv sync --frozen --group dev` installs, and `uv.lock` is the authority on that:
the transitive closure of the main dependencies and the dev group, excluding
every extra CI does not ask for.

Markers are deliberately ignored, which over-approximates the closure — a
Windows-only package counts as installed. That direction is safe: the failure
this guards against is an import that is in nobody's closure at all.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
TESTS = REPOSITORY.joinpath("tests")

#: The project's own distribution name in the lockfile.
PROJECT = "content-engine"

#: What `.github/workflows/ci.yml` asks for: the default dependencies plus this
#: group, and no extra. Kept beside the workflow it mirrors.
CI_GROUP = "dev"


def _normalise(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _lock() -> dict[str, dict[str, Any]]:
    payload = tomllib.loads(REPOSITORY.joinpath("uv.lock").read_text(encoding="utf-8"))
    return {_normalise(package["name"]): package for package in payload["package"]}


def _direct_requirements(package: dict[str, Any]) -> set[str]:
    """A package's unconditional dependencies. Extras are not followed."""
    return {_normalise(entry["name"]) for entry in package.get("dependencies", [])}


def _ci_closure() -> set[str]:
    """Every distribution `uv sync --frozen --group dev` would install."""
    packages = _lock()
    project = packages[PROJECT]
    frontier = _direct_requirements(project)
    frontier |= {
        _normalise(entry["name"]) for entry in project.get("dev-dependencies", {}).get(CI_GROUP, [])
    }

    seen: set[str] = set()
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        package = packages.get(name)
        if package is not None:
            frontier |= _direct_requirements(package) - seen
    return seen


def _top_level_imports(source: Path) -> set[str]:
    """Every top-level package a module imports, at module or function scope.

    Deferred imports inside a function count. They fail at call time rather than
    at collection, which is later and harder to read, not safer.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def _providers(module: str) -> set[str]:
    """Which distributions provide this import name."""
    return {_normalise(name) for name in packages_distributions().get(module, [module])}


TEST_MODULES = sorted(TESTS.rglob("test_*.py")) + sorted(TESTS.rglob("conftest.py"))


def test_the_sweep_sees_the_whole_suite() -> None:
    """A sweep that found no files would pass for any suite, including a broken one."""
    assert len(TEST_MODULES) > 20
    assert TESTS.joinpath("conftest.py") in TEST_MODULES


def test_the_closure_is_believable() -> None:
    """Guards the closure itself: an empty or tiny one would accept anything."""
    closure = _ci_closure()
    assert {"pytest", "typer", "pydantic", "google-genai", "httpx"} <= closure
    # The transcription extra is not requested, so neither it nor the packages
    # only it brings in may appear. `click` is the one this file exists for.
    assert "faster-whisper" not in closure
    assert "click" not in closure


@pytest.mark.parametrize("source", TEST_MODULES, ids=lambda path: str(path.name))
def test_every_import_is_available_on_ci(source: Path) -> None:
    closure = _ci_closure()
    local = {"content_engine", "tests"}

    for module in sorted(_top_level_imports(source)):
        if module in local or module in sys.stdlib_module_names:
            continue
        providers = _providers(module)
        assert providers & closure, (
            f"{source.name} imports {module!r}, provided by {sorted(providers)}, which "
            "`uv sync --frozen --group dev` does not install. It is present in this "
            "environment as a transitive dependency of an extra, and will be absent on CI."
        )
