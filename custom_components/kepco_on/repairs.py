"""Repair issue helpers for KEPCO ON."""

from __future__ import annotations

from typing import Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.issue_registry import IssueSeverity

from .const import DOMAIN

type KepcoOnRepairIssueKind = Literal[
    "login_schema_changed",
    "customer_schema_changed",
    "bill_schema_changed",
    "unsupported_account",
    "session_restore_failed",
]

ISSUE_SEVERITIES: dict[KepcoOnRepairIssueKind, IssueSeverity] = {
    "login_schema_changed": IssueSeverity.ERROR,
    "customer_schema_changed": IssueSeverity.ERROR,
    "bill_schema_changed": IssueSeverity.WARNING,
    "unsupported_account": IssueSeverity.ERROR,
    "session_restore_failed": IssueSeverity.WARNING,
}


def issue_id(entry_id: str, kind: KepcoOnRepairIssueKind) -> str:
    """Return the stable issue id for one config entry and issue kind."""
    return f"{entry_id}_{kind}"


@callback
def async_create_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    kind: KepcoOnRepairIssueKind,
) -> None:
    """Create or update a persistent safe repair issue."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry.entry_id, kind),
        is_fixable=False,
        is_persistent=True,
        severity=ISSUE_SEVERITIES[kind],
        translation_key=kind,
        translation_placeholders={"entry_id": entry.entry_id},
    )


@callback
def async_clear_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    kind: KepcoOnRepairIssueKind,
) -> None:
    """Clear one persistent repair issue."""
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id, kind))


__all__ = ["KepcoOnRepairIssueKind", "async_clear_issue", "async_create_issue", "issue_id"]
