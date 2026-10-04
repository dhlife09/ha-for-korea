"""Local validation for CI, HACS, and repository package metadata."""

from __future__ import annotations

import json
import re
import struct
import subprocess
import tomllib
from pathlib import Path
from typing import Any, cast

import yaml
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION_REFS = {
    "actions/checkout": {
        "ref": "3d3c42e5aac5ba805825da76410c181273ba90b1",
        "comment": "source: actions/checkout@v7",
    },
    "actions/setup-python": {
        "ref": "5fda3b95a4ea91299a34e894583c3862153e4b97",
        "comment": "source: actions/setup-python@v7",
    },
    "actions/setup-node": {
        "ref": "820762786026740c76f36085b0efc47a31fe5020",
        "comment": "source: actions/setup-node@v7",
    },
    "hacs/action": {
        "ref": "1ebf01c408f29afcb6406bd431bc98fd8cbb15aa",
        "comment": "source: hacs/action@main",
    },
    "home-assistant/actions/hassfest": {
        "ref": "a7c616ce81ccda50150bf1595786c71b1883fabb",
        "comment": "source: home-assistant/actions/hassfest@master",
    },
}
ALLOWED_FIXTURES = {
    "bill_202607.json",
    "house_customer_list.json",
    "house_main_chart.json",
    "house_power_planner.json",
    "bill_latest.json",
    "customer_list_multiple.json",
    "customer_list_single.json",
    "session_check_success.json",
    "sso_check_success.json",
}
SENSITIVE_FILE_SUFFIXES = (".har", ".jsonl", ".trace.zip")
SENSITIVE_FILE_NAMES = {"secrets.yaml"}
RAW_ARTIFACT_PREFIXES = ("session", "cookies", "login-schema")
FORBIDDEN_KEYS = {
    "access_token",
    "address",
    "api_body",
    "authorization",
    "body",
    "cookie",
    "cookies",
    "email",
    "headers",
    "member_name",
    "name",
    "password",
    "phone",
    "pwdval",
    "raw",
    "refresh_token",
    "token",
}
FORBIDDEN_KEY_PARTS = (
    "address",
    "authorization",
    "cookie",
    "email",
    "header",
    "name",
    "nm",
    "password",
    "phone",
    "pwd",
    "raw",
    "token",
)
FORBIDDEN_VALUE_PARTS = (
    "bearer ",
    "cookie:",
    "eyj",
    "password",
    "secret_canary",
    "set-cookie",
)
SAFE_SYNTHETIC_FIXTURE_KEYS = {"apt_name", "apt_nm", "cust_no", "si_cust_no"}
# 실제 KEPCO 응답 필드명(변경 불가)이지만 값은 YYYYMMDD 날짜라 개인정보가 아니다.
SAFE_DATE_FIXTURE_KEYS = {"dc_user_chg_nm_ymd"}


def load_yaml(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def load_json(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def workflow_on(workflow: dict[str, Any]) -> dict[str, Any]:
    raw_workflow = cast("dict[Any, Any]", workflow)
    on_value = raw_workflow["on"] if "on" in raw_workflow else raw_workflow[True]
    return cast("dict[str, Any]", on_value)


def run_steps(workflow: dict[str, Any], job_name: str) -> list[dict[str, Any]]:
    jobs = cast("dict[str, Any]", workflow["jobs"])
    job = cast("dict[str, Any]", jobs[job_name])
    return cast("list[dict[str, Any]]", job["steps"])


def uses_values(steps: list[dict[str, Any]]) -> list[str]:
    return [cast("str", step["uses"]) for step in steps if "uses" in step]


def run_values(steps: list[dict[str, Any]]) -> list[str]:
    return [cast("str", step["run"]) for step in steps if "run" in step]


def workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def uses_for_action(steps: list[dict[str, Any]], action: str) -> str:
    matches = [value for value in uses_values(steps) if value.startswith(f"{action}@")]
    assert len(matches) == 1
    return matches[0]


def assert_pinned_action(workflow_source: str, steps: list[dict[str, Any]], action: str) -> None:
    expected = ACTION_REFS[action]
    uses = uses_for_action(steps, action)
    assert uses == f"{action}@{expected['ref']}"
    assert f"uses: {uses} # {expected['comment']}" in workflow_source
    ref = uses.rsplit("@", 1)[1]
    assert len(ref) == 40
    assert all(character in "0123456789abcdef" for character in ref)
    assert not ref.startswith("v")
    assert ref not in {"main", "master"}


def tracked_files() -> list[str]:
    return subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


def assert_fixture_value_is_safe(key: str, value: object) -> None:
    normalized_key = key.lower()
    if normalized_key in SAFE_SYNTHETIC_FIXTURE_KEYS:
        assert isinstance(value, str)
        assert value.startswith("TEST_")
        return
    if not isinstance(value, str):
        return
    lowered = value.lower()
    assert "@" not in value
    assert not any(part in lowered for part in FORBIDDEN_VALUE_PARTS)
    assert not any(character.isdigit() for character in value if "phone" in normalized_key)


def scan_fixture(value: object, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).replace("-", "_").lower()
            if normalized_key in SAFE_DATE_FIXTURE_KEYS:
                assert isinstance(item, str)
                assert re.fullmatch(r"[0-9]{8}", item)
                continue
            if normalized_key not in SAFE_SYNTHETIC_FIXTURE_KEYS:
                assert normalized_key not in FORBIDDEN_KEYS
                assert not any(part in normalized_key for part in FORBIDDEN_KEY_PARTS)
            scan_fixture(item, (*path, str(key)))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            scan_fixture(item, (*path, str(index)))
        return
    if path:
        assert_fixture_value_is_safe(path[-1], value)


def test_workflow_yaml_files_parse_with_github_on_key() -> None:
    workflows = {path.name: load_yaml(path) for path in WORKFLOWS.glob("*.yml")}

    assert set(workflows) == {"release.yml", "tests.yml", "validate.yml"}
    for workflow in workflows.values():
        assert isinstance(workflow_on(workflow), dict)


def test_tests_workflow_runs_required_ci_gates_without_continue_on_error() -> None:
    workflow = load_yaml(WORKFLOWS / "tests.yml")
    source = workflow_text("tests.yml")
    triggers = workflow_on(workflow)
    steps = run_steps(workflow, "tests")

    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["tests"]["runs-on"] == "ubuntu-24.04"
    assert_pinned_action(source, steps, "actions/checkout")
    assert_pinned_action(source, steps, "actions/setup-python")
    assert_pinned_action(source, steps, "actions/setup-node")
    assert cast("dict[str, Any]", steps[1]["with"]) == {
        "python-version": "3.14.4",
        "cache": "pip",
        "cache-dependency-path": "requirements_test.txt",
    }
    assert cast("dict[str, Any]", steps[3]["with"]) == {"node-version": "24.20.0", "cache": "npm"}
    assert run_values(steps) == [
        "python -m pip install -r requirements_test.txt",
        "npm ci",
        "npm run test:login-schema",
        "npm audit --audit-level=moderate",
        "python -m ruff format --check .",
        "python -m ruff check .",
        "python -m mypy",
        "python -m pytest tests/test_ci_metadata.py",
        (
            "python -m pytest --cov=custom_components.kepco_on --cov-report=term-missing "
            "--cov-fail-under=95"
        ),
    ]
    assert "continue-on-error" not in json.dumps(workflow)


def test_validate_workflow_runs_hacs_and_hassfest_required_checks() -> None:
    workflow = load_yaml(WORKFLOWS / "validate.yml")
    source = workflow_text("validate.yml")
    triggers = workflow_on(workflow)
    jobs = cast("dict[str, Any]", workflow["jobs"])

    assert set(triggers) == {"push", "pull_request", "workflow_dispatch", "schedule"}
    assert triggers["schedule"] == [{"cron": "17 3 * * 0"}]
    assert workflow["permissions"] == {"contents": "read"}
    assert set(jobs) == {"hacs", "hassfest"}
    for job_name in ("hacs", "hassfest"):
        assert jobs[job_name]["runs-on"] == "ubuntu-24.04"
        assert_pinned_action(source, run_steps(workflow, job_name), "actions/checkout")
    hacs_steps = run_steps(workflow, "hacs")
    hassfest_steps = run_steps(workflow, "hassfest")
    assert_pinned_action(source, hacs_steps, "hacs/action")
    assert cast("dict[str, Any]", hacs_steps[1]["with"]) == {"category": "integration"}
    assert_pinned_action(source, hassfest_steps, "home-assistant/actions/hassfest")
    assert "continue-on-error" not in json.dumps(workflow)


def test_release_workflow_waits_for_both_ci_workflows_and_publishes_hacs_archive() -> None:
    workflow = load_yaml(WORKFLOWS / "release.yml")
    source = workflow_text("release.yml")
    triggers = workflow_on(workflow)
    jobs = cast("dict[str, Any]", workflow["jobs"])
    release_job = cast("dict[str, Any]", jobs["release"])
    steps = run_steps(workflow, "release")

    assert triggers == {
        "workflow_run": {
            "workflows": ["Tests"],
            "types": ["completed"],
            "branches": ["main"],
        }
    }
    assert workflow["permissions"] == {"actions": "read", "contents": "write"}
    assert workflow["concurrency"] == {
        "group": "release-${{ github.event.workflow_run.head_sha }}",
        "cancel-in-progress": False,
    }
    assert set(jobs) == {"release"}
    assert release_job["runs-on"] == "ubuntu-24.04"
    assert release_job["if"] == "github.event.workflow_run.conclusion == 'success'"
    assert_pinned_action(source, steps, "actions/checkout")
    assert cast("dict[str, Any]", steps[0]["with"]) == {
        "ref": "${{ env.RELEASE_SHA }}",
        "fetch-depth": 0,
    }
    assert "name: Validate" in source or 'run.get("name") == "Validate"' in source
    assert "git archive" in source
    assert "custom_components/kepco_on/" in source
    assert "gh release create" in source
    assert "--notes-file RELEASE_NOTES.md" in source
    assert "pull_request_target" not in source
    assert "continue-on-error" not in source

    manifest = load_json(ROOT / "custom_components" / "kepco_on" / "manifest.json")
    release_notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    assert release_notes.startswith(f"## 한전ON v{manifest['version']}\n")
    assert any(line.startswith("- ") and line[2:].strip() for line in release_notes.splitlines())


def test_release_metadata_versions_and_runtime_dependencies_are_valid() -> None:
    manifest = load_json(ROOT / "custom_components" / "kepco_on" / "manifest.json")
    hacs = load_json(ROOT / "hacs.json")
    package = load_json(ROOT / "package.json")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    release_version = Version(cast("str", manifest["version"]))

    assert release_version == Version(cast("str", pyproject["project"]["version"]))
    assert release_version.is_prerelease is False
    assert release_version.is_devrelease is False
    assert manifest["requirements"] == []
    assert hacs["homeassistant"] == "2026.8.3"
    assert package["private"] is True
    assert "dependencies" not in package
    assert package["devDependencies"] == {"playwright": "1.62.1"}


def test_hacs_brand_icon_is_square_transparent_png() -> None:
    icon = ROOT / "custom_components" / "kepco_on" / "brand" / "icon.png"
    data = icon.read_bytes()

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert data[12:16] == b"IHDR"
    assert struct.unpack(">II", data[16:24]) == (256, 256)
    color_type = data[25]
    assert color_type in {3, 6}  # indexed or truecolor with alpha
    if color_type == 3:
        assert b"tRNS" in data


def test_test_requirements_include_yaml_parser_for_local_workflow_validation() -> None:
    requirements = [
        Requirement(line)
        for line in (ROOT / "requirements_test.txt").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]

    assert {requirement.name for requirement in requirements} >= {"PyYAML", "pytest-cov"}


def test_coverage_config_keeps_95_branch_gate_and_only_excludes_typing_surfaces() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    coverage = pyproject["tool"]["coverage"]
    report = coverage["report"]

    assert coverage["run"] == {"branch": True, "source": ["custom_components/kepco_on"]}
    assert report["fail_under"] == 95
    assert report["show_missing"] is True
    assert report["skip_covered"] is True
    assert report["exclude_also"] == [
        "^class .*\\(Protocol\\):$",
        "^if TYPE_CHECKING:$",
    ]
    assert "omit" not in coverage["run"]
    assert "exclude_lines" not in report


def test_repository_tracks_no_raw_capture_or_secret_artifacts() -> None:
    for relative in tracked_files():
        normalized = relative.lower()
        path = Path(relative)
        assert path.name.lower() not in SENSITIVE_FILE_NAMES
        assert not normalized.endswith(SENSITIVE_FILE_SUFFIXES)
        if path.parts[:2] == ("tests", "fixtures"):
            assert path.name in ALLOWED_FIXTURES
            continue
        if path.suffix in {".json", ".zip"}:
            assert not any(path.name.lower().startswith(prefix) for prefix in RAW_ARTIFACT_PREFIXES)


def test_committed_json_fixtures_are_sanitized_and_allowlisted() -> None:
    fixture_paths = [
        ROOT / relative
        for relative in tracked_files()
        if relative.lower().startswith("tests/fixtures/") and relative.lower().endswith(".json")
    ]

    assert {path.name for path in fixture_paths} == ALLOWED_FIXTURES
    for path in fixture_paths:
        scan_fixture(load_json(path))
