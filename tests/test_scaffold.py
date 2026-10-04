"""Scaffold contract tests for the KEPCO ON integration."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.kepco_on import const
from custom_components.kepco_on.exceptions import (
    KepcoOnAuthError,
    KepcoOnConnectionError,
    KepcoOnError,
    KepcoOnMfaRequired,
    KepcoOnNoCustomersError,
    KepcoOnPartialUpdateError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnSessionExpired,
    KepcoOnUnsupportedAccount,
)
from homeassistant.const import Platform
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def _test_requirements() -> dict[str, Requirement]:
    lines = (ROOT / "requirements_test.txt").read_text(encoding="utf-8").splitlines()
    requirements = [Requirement(line) for line in lines if line and not line.startswith("#")]
    return {requirement.name: requirement for requirement in requirements}


def test_manifest_matches_integration_contract() -> None:
    manifest = json.loads((ROOT / "custom_components/kepco_on/manifest.json").read_text())

    assert list(manifest) == [
        "domain",
        "name",
        "codeowners",
        "config_flow",
        "documentation",
        "integration_type",
        "iot_class",
        "issue_tracker",
        "requirements",
        "version",
    ]

    assert manifest == {
        "domain": "kepco_on",
        "name": "HA for Korea",
        "codeowners": ["@dhlife09"],
        "config_flow": True,
        "documentation": "https://github.com/dhlife09/ha-for-korea",
        "integration_type": "hub",
        "iot_class": "cloud_polling",
        "issue_tracker": "https://github.com/dhlife09/ha-for-korea/issues",
        "requirements": [],
        "version": "1.3.0",
    }


def test_hacs_metadata_uses_supported_minimum_keys() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))

    assert hacs == {
        "name": "HA for Korea",
        "content_in_root": False,
        "homeassistant": "2026.8.3",
    }


def test_test_requirements_keep_windows_default_pytest_collectable() -> None:
    requirements = _test_requirements()

    assert str(requirements["homeassistant"].specifier) == "==2026.8.3"
    assert requirements["homeassistant"].marker is None

    ha_plugin = requirements["pytest-homeassistant-custom-component"]
    assert str(ha_plugin.specifier) == "==0.13.357"
    assert ha_plugin.marker is not None
    assert not ha_plugin.marker.evaluate({"platform_system": "Windows"})
    assert ha_plugin.marker.evaluate({"platform_system": "Linux"})

    pytest_asyncio = requirements["pytest-asyncio"]
    assert str(pytest_asyncio.specifier) == "==1.4.0"
    assert pytest_asyncio.marker is None


def test_gitignore_blocks_capture_and_secret_artifacts() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert gitignore == [
        "*.jsonl",
        "*.har",
        "*.trace.zip",
        ".kepco-on-capture-profile/",
        ".kepco-on-login-profile/",
        "login-schema*.json",
        "session*.json",
        "cookies*.json",
        ".storage/",
        "secrets.yaml",
        ".env",
        ".env.*",
        "__pycache__/",
        "*.py[cod]",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        ".coverage",
        "htmlcov/",
        ".venv/",
        "node_modules/",
        ".worktrees/",
    ]


def test_constants_are_fixed_and_capture_safe() -> None:
    assert const.DOMAIN == "kepco_on"
    assert const.NAME == "HA for Korea"
    assert const.VERSION == "1.3.0"
    assert const.CONFIG_ENTRY_VERSION == 3
    assert const.BASE_URL == "https://online.kepco.co.kr"
    assert const.PAGE_URL == "https://online.kepco.co.kr/MYM001D00"
    assert const.ENDPOINT_LOGIN_INDI == "/cyb/me/login/indi/api"
    assert const.ENDPOINT_FIRST_LOGIN_CHECK == "/me/login/firstLogin/check"
    assert const.ENDPOINT_SESSION_CHECK == "/sessionCheck"
    assert const.ENDPOINT_APT_BILL_DETAIL == "/my/charge/pay/aptBillDetail"
    assert const.POLLING_INTERVAL_HOURS == (1, 3, 6, 12, 24)
    assert const.DEFAULT_POLLING_INTERVAL_HOURS == 12
    assert const.DEFAULT_CO2_FACTOR_KG_PER_KWH == 0.459
    assert const.PLATFORMS == (Platform.SENSOR,)
    assert frozenset({"JSESSIONID", "kepcoSSO"}) == const.CANDIDATE_COOKIE_NAMES
    assert frozenset({"JSESSIONID", "kepcoSSO"}) == const.PERSISTED_COOKIE_ALLOWLIST


def test_option_and_config_keys_are_stable_and_legacy_toggles_remain_migration_only() -> None:
    assert const.CONF_USERNAME == "username"
    assert const.CONF_SAVE_PASSWORD == "save_password"
    assert const.CONF_SELECTED_CUSTOMERS == "selected_customers"
    assert const.OPT_POLLING_INTERVAL_HOURS == "polling_interval_hours"
    assert const.OPT_ENABLE_DETAILED_SENSORS == "enable_detailed_sensors"
    assert const.OPT_ENABLE_CO2_ESTIMATE == "enable_co2_estimate"
    assert const.OPT_CO2_FACTOR_KG_PER_KWH == "co2_factor_kg_per_kwh"
    assert const.OPT_HISTORY_MONTHS == "history_months"


def test_exception_hierarchy_is_documented() -> None:
    assert issubclass(KepcoOnAuthError, KepcoOnError)
    assert issubclass(KepcoOnSessionExpired, KepcoOnAuthError)
    assert issubclass(KepcoOnMfaRequired, KepcoOnAuthError)
    assert issubclass(KepcoOnUnsupportedAccount, KepcoOnError)
    assert issubclass(KepcoOnNoCustomersError, KepcoOnError)
    assert issubclass(KepcoOnConnectionError, KepcoOnError)
    assert issubclass(KepcoOnRateLimitError, KepcoOnConnectionError)
    assert issubclass(KepcoOnProtocolError, KepcoOnError)
    assert issubclass(KepcoOnPartialUpdateError, KepcoOnError)
