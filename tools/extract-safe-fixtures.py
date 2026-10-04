#!/usr/bin/env python
"""Extract sanitized parser fixtures from the safe KEPCO ON wire capture."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

EXPECTED_CAPTURE_SHA256 = "cdd9f5f7443781e2986484cd030e5b95d9d89ae764a5ee2e759d144c2459620a"
EXPECTED_RECORD_COUNT = 611
ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures"
CAPTURE_LINE_KEY = "__capture_line__"

ALLOWED_SYNTHETIC_CUSTOMER_KEYS = {
    "APT_DONGNO",
    "APT_HONO",
    "APT_NAME",
    "CUST_NO",
    "SI_CUST_NO",
    "cntrMthdCd",
}
SENSITIVE_EXACT_KEYS = {
    "access_token",
    "accesstoken",
    "addr",
    "address",
    "auth_token",
    "authtoken",
    "email",
    "emailaddress",
    "mbrsnm",
    "mobile",
    "name",
    "phone",
    "refreshtoken",
    "secret",
    "set-cookie",
    "token",
    "user_email_addr",
    "user_mtel",
    "userid",
    "usermngseqno",
}
SENSITIVE_KEY_PARTS = (
    "addr",
    "address",
    "cookie",
    "email",
    "mobile",
    "password",
    "phone",
    "secret",
    "token",
)
SENSITIVE_NAME_SUFFIXES = ("name", "nm")

SYNTHETIC_CUSTOMERS = (
    {
        "APT_NAME": "TEST_APT_001",
        "APT_DONGNO": "1001",
        "APT_HONO": "0101",
        "CUST_NO": "TEST_CUST_001",
        "SI_CUST_NO": "TEST_HOUSE_001",
        "cntrMthdCd": "아파트(단일계약)",
    },
    {
        "APT_NAME": "TEST_APT_002",
        "APT_DONGNO": "1002",
        "APT_HONO": "0102",
        "CUST_NO": "TEST_CUST_002",
        "SI_CUST_NO": "TEST_HOUSE_002",
        "cntrMthdCd": "아파트(단일계약)",
    },
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    fixtures = _build_fixtures_from_capture(args.input)
    _audit_fixtures(fixtures)

    if args.check:
        missing_or_changed = [
            name
            for name, payload in fixtures.items()
            if not _output_path(name).exists()
            or _render_json(payload) != _output_path(name).read_text(encoding="utf-8")
        ]
        if missing_or_changed:
            raise SystemExit(
                f"fixtures are not deterministic/current: {', '.join(missing_or_changed)}"
            )
        print(f"OK: {len(fixtures)} sanitized fixtures are current")
        return 0

    for name, payload in fixtures.items():
        path = _output_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_json(payload), encoding="utf-8")
    print(f"OK: wrote {len(fixtures)} sanitized fixtures")
    return 0


def _build_fixtures_from_capture(path: Path) -> dict[str, dict[str, Any]]:
    digest = hashlib.sha256()
    count = 0
    selected: dict[str, dict[str, Any]] = {}

    with path.open("rb") as capture:
        for raw_line in capture:
            digest.update(raw_line)
            line = raw_line.strip()
            if not line:
                continue
            count += 1
            try:
                record = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as err:
                raise SystemExit(f"invalid JSONL record at line {count}") from err
            if not isinstance(record, dict):
                raise SystemExit(f"record at line {count} is not an object")
            record[CAPTURE_LINE_KEY] = count
            _select_fixture(record, selected)

    if digest.hexdigest() != EXPECTED_CAPTURE_SHA256:
        raise SystemExit("input capture SHA256 does not match the expected safe capture")
    if count != EXPECTED_RECORD_COUNT:
        raise SystemExit("input capture record count does not match the expected safe capture")
    return _finalize_fixtures(selected)


def _build_fixtures(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records, start=1):
        record.setdefault(CAPTURE_LINE_KEY, index)
        _select_fixture(record, selected)
    return _finalize_fixtures(selected)


def _select_fixture(record: dict[str, Any], selected: dict[str, dict[str, Any]]) -> None:
    body = _body(record)
    if body is None:
        return
    fixture_name = _fixture_name(record, body)
    if fixture_name is None:
        return
    if fixture_name in selected:
        raise SystemExit(f"duplicate selector matched {fixture_name}")
    selected[fixture_name] = body


def _finalize_fixtures(selected: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    required = {
        "session_check_success.json",
        "sso_check_success.json",
        "customer_list_single.json",
        "customer_list_multiple.json",
        "bill_latest.json",
        "bill_202607.json",
    }
    missing = sorted(required - selected.keys())
    if missing:
        raise SystemExit(f"expected response structures were not found: {', '.join(missing)}")

    return {
        "session_check_success.json": _sanitize_session(selected["session_check_success.json"]),
        "sso_check_success.json": _sanitize_sso(selected["sso_check_success.json"]),
        "customer_list_single.json": _sanitize_customers(
            selected["customer_list_single.json"], "dlt_appendList", 1
        ),
        "customer_list_multiple.json": _sanitize_customers(
            selected["customer_list_multiple.json"], "dlt_myPageAppendList", 2
        ),
        "bill_latest.json": _sanitize_bill(selected["bill_latest.json"]),
        "bill_202607.json": _sanitize_bill(selected["bill_202607.json"]),
    }


def _fixture_name(record: dict[str, Any], body: dict[str, Any]) -> str | None:
    line = record.get(CAPTURE_LINE_KEY)
    url = record.get("url")
    if line == 274 and url == "https://online.kepco.co.kr/sessionCheck":
        return "session_check_success.json"
    if line == 307 and url == "https://online.kepco.co.kr/ssoCheck":
        return "sso_check_success.json"
    if line == 328 and isinstance(body.get("dlt_appendList"), list):
        return "customer_list_single.json"
    if line == 295 and isinstance(body.get("dlt_myPageAppendList"), list):
        return "customer_list_multiple.json"
    if line == 380 and _is_bill(body, "20260701", "573"):
        return "bill_latest.json"
    if line == 604 and _is_bill(body, "20260601", "406"):
        return "bill_202607.json"
    return None


def _is_bill(body: dict[str, Any], period_start: str, usage_kwh: str) -> bool:
    result = _bill_result(body)
    return (
        result.get("DO_FROM_MMDD") == period_start
        and result.get("DO_KWH") == usage_kwh
        and _history_count(body) == 24
    )


def _body(record: dict[str, Any]) -> dict[str, Any] | None:
    body = record.get("body")
    if not isinstance(body, str):
        return None
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _bill_result(body: dict[str, Any]) -> dict[str, Any]:
    result = body.get("dma_result")
    return result if isinstance(result, dict) else {}


def _history_count(body: dict[str, Any]) -> int:
    history = body.get("dlt_chrtList")
    return len(history) if isinstance(history, list) else 0


def _sanitize_session(body: dict[str, Any]) -> dict[str, Any]:
    return {"result": body.get("result")}


def _sanitize_sso(body: dict[str, Any]) -> dict[str, Any]:
    return {"loginChk": body.get("loginChk")}


def _sanitize_customers(body: dict[str, Any], list_key: str, count: int) -> dict[str, Any]:
    sanitized = cast("dict[str, Any]", _strip_sensitive(deepcopy(body)))
    sanitized[list_key] = [dict(SYNTHETIC_CUSTOMERS[index]) for index in range(count)]
    return sanitized


def _sanitize_bill(body: dict[str, Any]) -> dict[str, Any]:
    sanitized = cast("dict[str, Any]", _strip_sensitive(deepcopy(body)))
    result = sanitized.get("dma_result")
    if isinstance(result, dict):
        result.pop("DO_CUSTNO", None)
    return sanitized


def _strip_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            if _is_sensitive_key(key):
                continue
            output[key] = _strip_sensitive(_normalize_placeholder(item))
        return output
    if isinstance(value, list):
        return [_strip_sensitive(item) for item in value]
    return _normalize_placeholder(value)


def _audit_fixtures(fixtures: dict[str, dict[str, Any]]) -> None:
    rendered = json.dumps(fixtures, ensure_ascii=False, sort_keys=True)
    forbidden_text = (
        "[REDACTED]",
        "USER_MTEL",
        "USER_EMAIL_ADDR",
        "ADDR",
        "mbrsNm",
        "userMngSeqno",
        "directive",
        "canary",
    )
    for text in forbidden_text:
        if text in rendered:
            raise SystemExit("sanitized fixtures contain forbidden sensitive metadata")

    def walk(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if _is_sensitive_key(key):
                    raise SystemExit("sanitized fixtures contain a sensitive key")
                if key in {"CUST_NO", "SI_CUST_NO"} and not str(item).startswith("TEST_"):
                    raise SystemExit("customer identifiers must be synthetic")
                walk(item, (*path, key))
        elif isinstance(value, list):
            for item in value:
                walk(item, path)

    walk(fixtures)


def _is_sensitive_key(key: str) -> bool:
    if key in ALLOWED_SYNTHETIC_CUSTOMER_KEYS:
        return False
    normalized = key.replace("-", "_").lower()
    compact = normalized.replace("_", "")
    return (
        normalized in SENSITIVE_EXACT_KEYS
        or compact in SENSITIVE_EXACT_KEYS
        or any(compact.endswith(suffix) for suffix in SENSITIVE_NAME_SUFFIXES)
        or any(part in normalized for part in SENSITIVE_KEY_PARTS)
    )


def _normalize_placeholder(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("[") and value.endswith("]"):
        return None
    return value


def _output_path(name: str) -> Path:
    path = FIXTURE_DIR / name
    resolved_fixture_dir = FIXTURE_DIR.resolve()
    resolved_path = path.resolve()
    if resolved_fixture_dir not in resolved_path.parents:
        raise SystemExit("refusing to write outside tests/fixtures")
    return path


def _render_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
