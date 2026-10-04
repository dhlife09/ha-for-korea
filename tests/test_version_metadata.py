"""Version metadata consistency tests."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from custom_components.kepco_on.const import VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_version_metadata_is_consistent() -> None:
    manifest = json.loads(
        (ROOT / "custom_components/kepco_on/manifest.json").read_text(encoding="utf-8")
    )
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert VERSION == manifest["version"] == pyproject["project"]["version"]
    assert f"- 버전: `v{VERSION}`." in readme
