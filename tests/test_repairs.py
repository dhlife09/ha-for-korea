"""Repair issue lifecycle tests for KEPCO ON."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from custom_components.kepco_on.const import DOMAIN
from homeassistant.helpers.issue_registry import IssueSeverity

TOKEN_SECRET = "TOKEN_SECRET_CANARY"
CUSTOMER_SECRET = "CUSTOMER_SECRET_CANARY"


class FakeConfigEntry:
    """Config-entry stand-in with sensitive values."""

    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id
        self.title = "TITLE_SECRET_CANARY"
        self.data = {"token": TOKEN_SECRET, "customer_number": CUSTOMER_SECRET}


class FakeHass:
    """Hass stand-in for repair helper tests."""


@pytest.fixture
def issue_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Capture Home Assistant issue registry calls."""
    calls: dict[str, list[dict[str, Any]]] = {"create": [], "delete": []}

    def fake_create_issue(*args: Any, **kwargs: Any) -> None:
        calls["create"].append({"args": args, "kwargs": kwargs})

    def fake_delete_issue(*args: Any, **kwargs: Any) -> None:
        calls["delete"].append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(
        "custom_components.kepco_on.repairs.ir.async_create_issue",
        fake_create_issue,
    )
    monkeypatch.setattr(
        "custom_components.kepco_on.repairs.ir.async_delete_issue",
        fake_delete_issue,
    )
    return calls


@pytest.mark.parametrize(
    ("kind", "severity"),
    [
        ("login_schema_changed", IssueSeverity.ERROR),
        ("customer_schema_changed", IssueSeverity.ERROR),
        ("bill_schema_changed", IssueSeverity.WARNING),
        ("unsupported_account", IssueSeverity.ERROR),
        ("session_restore_failed", IssueSeverity.WARNING),
    ],
)
def test_async_create_issue_uses_stable_private_issue_metadata(
    issue_calls: dict[str, list[dict[str, Any]]],
    kind: str,
    severity: IssueSeverity,
) -> None:
    from custom_components.kepco_on.repairs import async_create_issue

    entry = FakeConfigEntry()

    async_create_issue(cast("Any", FakeHass()), cast("Any", entry), cast("Any", kind))
    async_create_issue(cast("Any", FakeHass()), cast("Any", entry), cast("Any", kind))

    first = issue_calls["create"][0]
    assert first["args"][:3] == (first["args"][0], DOMAIN, f"{entry.entry_id}_{kind}")
    assert first["kwargs"] == {
        "is_fixable": False,
        "is_persistent": True,
        "severity": severity,
        "translation_key": kind,
        "translation_placeholders": {"entry_id": entry.entry_id},
    }
    assert len(issue_calls["create"]) == 2
    assert TOKEN_SECRET not in json.dumps(issue_calls, default=str)
    assert CUSTOMER_SECRET not in json.dumps(issue_calls, default=str)
    assert "TITLE_SECRET_CANARY" not in json.dumps(issue_calls, default=str)


def test_async_clear_issue_deletes_stable_issue_id(
    issue_calls: dict[str, list[dict[str, Any]]],
) -> None:
    from custom_components.kepco_on.repairs import async_clear_issue

    entry = FakeConfigEntry("abc")

    async_clear_issue(cast("Any", FakeHass()), cast("Any", entry), "bill_schema_changed")

    assert issue_calls["delete"] == [
        {
            "args": (issue_calls["delete"][0]["args"][0], DOMAIN, "abc_bill_schema_changed"),
            "kwargs": {},
        }
    ]


def test_all_translation_files_have_repair_issue_parity() -> None:
    """Strings and translations contain every repair issue key."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    payloads = [
        json.loads((root / path).read_text(encoding="utf-8"))
        for path in (
            "custom_components/kepco_on/strings.json",
            "custom_components/kepco_on/translations/en.json",
            "custom_components/kepco_on/translations/ko.json",
        )
    ]
    expected = {
        "login_schema_changed",
        "customer_schema_changed",
        "bill_schema_changed",
        "unsupported_account",
        "session_restore_failed",
    }

    assert set(payloads[0]["issues"]) == set(payloads[1]["issues"]) == set(payloads[2]["issues"])
    assert set(payloads[0]["issues"]) >= expected
    for payload in payloads:
        for key in expected:
            assert payload["issues"][key]["title"]
            assert payload["issues"][key]["description"]
            assert "TOKEN_SECRET" not in payload["issues"][key]["description"]
