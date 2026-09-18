"""Guards against the dependency declarations drifting apart.

``pyproject.toml`` says what the project is *compatible* with (">=" ranges);
``requirements.txt`` says what a deployment actually *installs* (exact pins).
Two files describing the same dependency set drift silently, and on an
air-gapped factory box that is discovered at the worst possible moment - so a
test holds them together.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: ``name[extra]==version`` / ``name>=version`` - enough for our own files.
_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?P<extras>\[[^\]]*\])?"
    r"\s*(?P<spec>[=<>!~]=?\s*[^\s;#]+)?"
)


def canonical(name: str) -> str:
    """PEP 503 normalisation: ``Jinja2`` and ``jinja2`` are the same package."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def parse_requirements(path: Path) -> dict[str, dict[str, str]]:
    """Parse a requirements file into ``{canonical_name: {...}}``.

    Comments, blank lines and ``-r`` includes are skipped - includes are
    followed explicitly by the tests that care.
    """
    found: dict[str, dict[str, str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQUIREMENT.match(line)
        assert match, f"could not parse requirement line in {path.name}: {raw!r}"
        found[canonical(match.group("name"))] = {
            "extras": (match.group("extras") or "").strip(),
            "spec": (match.group("spec") or "").strip(),
            "line": line,
        }
    return found


@pytest.fixture(scope="module")
def pyproject() -> dict:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


@pytest.fixture(scope="module")
def runtime_requirements() -> dict[str, dict[str, str]]:
    return parse_requirements(PROJECT_ROOT / "requirements.txt")


class TestFilesExist:
    @pytest.mark.parametrize(
        "name",
        ["requirements.txt", "requirements-dev.txt", "requirements-windows.txt",
         "requirements.lock.txt", "pyproject.toml"],
    )
    def test_present(self, name: str) -> None:
        assert (PROJECT_ROOT / name).is_file(), f"{name} is missing"

    @pytest.mark.parametrize("name", ["setup-venv.sh", "setup-venv.ps1"])
    def test_setup_scripts_present(self, name: str) -> None:
        assert (PROJECT_ROOT / "scripts" / name).is_file()

    def test_shell_script_is_executable(self) -> None:
        import os
        import sys

        if sys.platform == "win32":  # pragma: no cover - POSIX permission bit
            pytest.skip("POSIX permission bits do not apply on Windows")
        script = PROJECT_ROOT / "scripts" / "setup-venv.sh"
        assert os.access(script, os.X_OK), "setup-venv.sh must be executable"


class TestRuntimeRequirements:
    def test_every_pyproject_dependency_is_pinned(self, pyproject, runtime_requirements) -> None:
        declared = {
            canonical(_REQUIREMENT.match(dep).group("name"))  # type: ignore[union-attr]
            for dep in pyproject["project"]["dependencies"]
        }
        missing = declared - set(runtime_requirements)
        assert not missing, (
            f"declared in pyproject.toml but absent from requirements.txt: {sorted(missing)}"
        )

    def test_no_extra_packages_sneak_in(self, pyproject, runtime_requirements) -> None:
        """requirements.txt lists direct dependencies only.

        Transitive packages belong in requirements.lock.txt; letting them creep
        in here makes it impossible to see what the project actually depends on.
        """
        declared = {
            canonical(_REQUIREMENT.match(dep).group("name"))  # type: ignore[union-attr]
            for dep in pyproject["project"]["dependencies"]
        }
        unexpected = set(runtime_requirements) - declared
        assert not unexpected, (
            f"in requirements.txt but not declared in pyproject.toml: {sorted(unexpected)}"
        )

    def test_versions_are_pinned_exactly(self, runtime_requirements) -> None:
        for name, info in runtime_requirements.items():
            assert info["spec"].startswith("=="), (
                f"{name} must be pinned with '==' for reproducible deployments, "
                f"got {info['line']!r}"
            )

    def test_extras_match_pyproject(self, pyproject, runtime_requirements) -> None:
        """uvicorn[standard] in one file and bare uvicorn in the other would
        quietly drop uvloop and httptools from a deployment."""
        for dep in pyproject["project"]["dependencies"]:
            match = _REQUIREMENT.match(dep)
            assert match
            name = canonical(match.group("name"))
            declared_extras = (match.group("extras") or "").strip()
            assert runtime_requirements[name]["extras"] == declared_extras, (
                f"{name}: extras differ between pyproject.toml ({declared_extras!r}) "
                f"and requirements.txt ({runtime_requirements[name]['extras']!r})"
            )

    def test_installed_versions_match_the_pins(self, runtime_requirements) -> None:
        """The environment running the tests must be the pinned one."""
        from importlib.metadata import PackageNotFoundError, version

        mismatched = []
        for name, info in runtime_requirements.items():
            pinned = info["spec"].removeprefix("==").strip()
            try:
                installed = version(name)
            except PackageNotFoundError:  # pragma: no cover - a broken env
                mismatched.append(f"{name}: not installed (pinned {pinned})")
                continue
            if installed != pinned:
                mismatched.append(f"{name}: installed {installed}, pinned {pinned}")
        assert not mismatched, (
            "requirements.txt is out of step with this environment: "
            + "; ".join(mismatched)
        )


class TestDerivedRequirements:
    def test_dev_includes_runtime(self) -> None:
        text = (PROJECT_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        assert "-r requirements.txt" in text

    def test_windows_includes_runtime(self) -> None:
        text = (PROJECT_ROOT / "requirements-windows.txt").read_text(encoding="utf-8")
        assert "-r requirements.txt" in text

    def test_dev_covers_the_dev_extra(self, pyproject) -> None:
        dev = parse_requirements(PROJECT_ROOT / "requirements-dev.txt")
        for dep in pyproject["project"]["optional-dependencies"]["dev"]:
            name = canonical(_REQUIREMENT.match(dep).group("name"))  # type: ignore[union-attr]
            assert name in dev, f"{name} is in the dev extra but not requirements-dev.txt"

    def test_windows_covers_the_windows_extra(self, pyproject) -> None:
        windows = parse_requirements(PROJECT_ROOT / "requirements-windows.txt")
        for dep in pyproject["project"]["optional-dependencies"]["windows"]:
            name = canonical(_REQUIREMENT.match(dep).group("name"))  # type: ignore[union-attr]
            assert name in windows, f"{name} is in the windows extra but not the file"

    def test_pywin32_is_not_in_the_portable_files(self) -> None:
        """pywin32 has no Linux wheel; listing it would break every Linux install."""
        for name in ("requirements.txt", "requirements-dev.txt", "requirements.lock.txt"):
            assert "pywin32" not in parse_requirements(PROJECT_ROOT / name)


class TestLockFile:
    def test_lock_is_a_superset_of_the_direct_pins(self, runtime_requirements) -> None:
        locked = parse_requirements(PROJECT_ROOT / "requirements.lock.txt")
        missing = set(runtime_requirements) - set(locked)
        assert not missing, (
            f"in requirements.txt but missing from the lock file (regenerate it): "
            f"{sorted(missing)}"
        )

    def test_lock_agrees_with_the_direct_pins(self, runtime_requirements) -> None:
        locked = parse_requirements(PROJECT_ROOT / "requirements.lock.txt")
        for name, info in runtime_requirements.items():
            assert locked[name]["spec"] == info["spec"], (
                f"{name}: requirements.txt says {info['spec']}, "
                f"the lock file says {locked[name]['spec']} - regenerate the lock file"
            )

    def test_lock_pins_everything_exactly(self) -> None:
        for name, info in parse_requirements(PROJECT_ROOT / "requirements.lock.txt").items():
            assert info["spec"].startswith("=="), f"{name} is not pinned in the lock file"

    def test_lock_does_not_contain_the_project_itself(self) -> None:
        locked = parse_requirements(PROJECT_ROOT / "requirements.lock.txt")
        assert "snap7-gateway" not in locked, (
            "the lock file must describe dependencies only, not the gateway package"
        )


class TestPythonVersion:
    def test_requires_python_matches_the_documented_floor(self, pyproject) -> None:
        assert pyproject["project"]["requires-python"] == ">=3.13"

    def test_setup_scripts_check_the_same_floor(self) -> None:
        shell = (PROJECT_ROOT / "scripts" / "setup-venv.sh").read_text(encoding="utf-8")
        powershell = (PROJECT_ROOT / "scripts" / "setup-venv.ps1").read_text(encoding="utf-8")
        assert "(3, 13)" in shell
        assert "(3,13)" in powershell or "(3, 13)" in powershell
