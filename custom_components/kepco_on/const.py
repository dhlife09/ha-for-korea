"""Constants for the KEPCO ON integration."""

from homeassistant.const import CONF_PASSWORD, Platform

DOMAIN = "kepco_on"
NAME = "HA for Korea"
VERSION = "1.3.0"
CONFIG_ENTRY_VERSION = 3
# Scope the user-reported request and Wh conversion to the exact tested contract.
COMBINED_APARTMENT_PLANNER_CONTRACT = "아파트(종합계약)"

BASE_URL = "https://online.kepco.co.kr"
PAGE_URL = "https://online.kepco.co.kr/MYM001D00"

ENDPOINT_LOGIN_INDI = "/cyb/me/login/indi/api"
ENDPOINT_FIRST_LOGIN_CHECK = "/me/login/firstLogin/check"
ENDPOINT_SESSION_CHECK = "/sessionCheck"
ENDPOINT_SSO_CHECK = "/ssoCheck"
ENDPOINT_IS_CORP = "/isCorp"
ENDPOINT_MYPAGE_CUST_NO_LIST = "/my/indi/info/myPageCustNoList"
ENDPOINT_CUST_NO_LIST = "/my/indi/info/custNoList"
ENDPOINT_APT_BILL_DETAIL = "/my/charge/pay/aptBillDetail"
ENDPOINT_MAIN_CHART = "/my/memo/mainChart"
ENDPOINT_POWER_PLANNER = "/my/memo/powerPlanner"

POLLING_INTERVAL_HOURS = (1, 3, 6, 12, 24)
DEFAULT_POLLING_INTERVAL_HOURS = 12
DEFAULT_CO2_FACTOR_KG_PER_KWH = 0.459

PLATFORMS = (Platform.SENSOR,)

CONF_USERNAME = "username"
CONF_SAVE_PASSWORD = "save_password"
CONF_SELECTED_CUSTOMERS = "selected_customers"
CONF_ACCOUNT_UID_HASH = "account_uid_hash"
CONF_CUSTOMERS = "customers"
CONF_DISPLAY_NAME = "display_name"
CONF_SESSION_HANDOFF = "session_handoff"
# Task 6 must consume CONF_SESSION_HANDOFF into the private session Store and
# scrub it from entry data; until then HA redaction must treat it as sensitive.
SENSITIVE_CONFIG_DATA_KEYS = frozenset({CONF_PASSWORD, CONF_SESSION_HANDOFF})

DATA_STABLE_KEY = "stable_key"
DATA_APARTMENT_NAME = "apartment_name"
DATA_DONG = "dong"
DATA_HO = "ho"
DATA_CONTRACT_METHOD = "contract_method"
DATA_IS_SUPPORTED = "is_supported"
DATA_CUSTOMER_NUMBER = "customer_number"
DATA_HOUSE_CONTRACT_NUMBER = "house_contract_number"

OPT_POLLING_INTERVAL_HOURS = "polling_interval_hours"
OPT_ENABLE_DETAILED_SENSORS = "enable_detailed_sensors"
OPT_ENABLE_CO2_ESTIMATE = "enable_co2_estimate"
OPT_CO2_FACTOR_KG_PER_KWH = "co2_factor_kg_per_kwh"
OPT_HISTORY_MONTHS = "history_months"

# Only the two KEPCO ON authentication-session cookies are persisted. The
# session store still restricts them to KEPCO domains, valid paths and expiry.
CANDIDATE_COOKIE_NAMES: frozenset[str] = frozenset({"JSESSIONID", "kepcoSSO"})
PERSISTED_COOKIE_ALLOWLIST: frozenset[str] = CANDIDATE_COOKIE_NAMES

__all__ = [
    "BASE_URL",
    "CANDIDATE_COOKIE_NAMES",
    "COMBINED_APARTMENT_PLANNER_CONTRACT",
    "CONFIG_ENTRY_VERSION",
    "CONF_ACCOUNT_UID_HASH",
    "CONF_CUSTOMERS",
    "CONF_DISPLAY_NAME",
    "CONF_PASSWORD",
    "CONF_SAVE_PASSWORD",
    "CONF_SELECTED_CUSTOMERS",
    "CONF_SESSION_HANDOFF",
    "CONF_USERNAME",
    "DATA_APARTMENT_NAME",
    "DATA_CONTRACT_METHOD",
    "DATA_CUSTOMER_NUMBER",
    "DATA_DONG",
    "DATA_HO",
    "DATA_HOUSE_CONTRACT_NUMBER",
    "DATA_IS_SUPPORTED",
    "DATA_STABLE_KEY",
    "DEFAULT_CO2_FACTOR_KG_PER_KWH",
    "DEFAULT_POLLING_INTERVAL_HOURS",
    "DOMAIN",
    "ENDPOINT_APT_BILL_DETAIL",
    "ENDPOINT_CUST_NO_LIST",
    "ENDPOINT_FIRST_LOGIN_CHECK",
    "ENDPOINT_IS_CORP",
    "ENDPOINT_LOGIN_INDI",
    "ENDPOINT_MAIN_CHART",
    "ENDPOINT_MYPAGE_CUST_NO_LIST",
    "ENDPOINT_POWER_PLANNER",
    "ENDPOINT_SESSION_CHECK",
    "ENDPOINT_SSO_CHECK",
    "NAME",
    "OPT_CO2_FACTOR_KG_PER_KWH",
    "OPT_ENABLE_CO2_ESTIMATE",
    "OPT_ENABLE_DETAILED_SENSORS",
    "OPT_HISTORY_MONTHS",
    "OPT_POLLING_INTERVAL_HOURS",
    "PAGE_URL",
    "PERSISTED_COOKIE_ALLOWLIST",
    "PLATFORMS",
    "POLLING_INTERVAL_HOURS",
    "SENSITIVE_CONFIG_DATA_KEYS",
    "VERSION",
]
