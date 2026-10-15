"""Unofficial Sorgenia bills integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import re

import aiohttp
import calendar
import yaml
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_AUTH_HEADER,
    CONF_AUTH_VALUE,
    CONF_BODY,
    CONF_EXTRA_PR_CODES,
    CONF_CONSUMPTION_BODY,
    CONF_CONSUMPTION_METHOD,
    CONF_CONSUMPTION_URL,
    CONF_CURVE_BODY,
    CONF_CURVE_POINT_CODE,
    CONF_CURVE_METHOD,
    CONF_CURVE_URL,
    CONF_GAS_UTILITY_CODE,
    CONF_GREENERS_METHOD,
    CONF_GREENERS_URL,
    CONF_REFRESH_URL,
    CONF_BASIC_AUTH,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    CONF_USERNAME,
    CONF_SOURCE_CHANNEL,
    CONF_EXTRA_HEADERS,
    CONF_METHOD,
    CONF_SCAN_INTERVAL,
    CONF_SUBSCRIPTION_KEY,
    CONF_URL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_CONSUMPTION_BODY,
    DEFAULT_CONSUMPTION_METHOD,
    DEFAULT_CONSUMPTION_URL,
    DEFAULT_CURVE_METHOD,
    DEFAULT_CURVE_BODY,
    DEFAULT_CURVE_URL,
    DEFAULT_BILL_COSTS_URL,
    DEFAULT_GAS_METER_URL,
    DEFAULT_GREENERS_METHOD,
    DEFAULT_GREENERS_URL,
    DEFAULT_REFRESH_URL,
    DEFAULT_SOURCE_CHANNEL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _read_integration_version() -> str:
    try:
        manifest = json.loads(Path(__file__).with_name("manifest.json").read_text(encoding="utf-8"))
        return str(manifest.get("version", "unknown"))
    except (OSError, ValueError):
        return "unknown"


INTEGRATION_VERSION = _read_integration_version()
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]


def _to_float(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _pick_latest_bill(bills: list[dict]) -> dict:
    """Most recently issued bill (dd/mm/YYYY or ISO dates)."""
    def key(bill):
        value = str(bill.get("issueDate", ""))[:10]
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        return datetime.min
    return max(bills, key=key) if bills else {}


def _read_optional_settings(path: str) -> dict:
    """Read optional keys from /config/sorgenia.yaml (missing file is fine)."""
    try:
        with open(path, encoding="utf-8") as config_file:
            settings = yaml.safe_load(config_file) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return settings if isinstance(settings, dict) else {}


def _safe_api_text(value, limit: int = 160) -> str:
    """Short API error text for logs: e-mail addresses and token-like strings are masked."""
    text = str(value) if value is not None else "not provided"
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", text)
    text = re.sub(r"[A-Za-z0-9_\-.=+/]{24,}", "<hidden>", text)
    return text[:limit]


class SorgeniaCoordinator(DataUpdateCoordinator[dict]):
    """Fetch bill history from the configured Sorgenia API request."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.session = async_get_clientsession(hass)
        self._access_token: str | None = entry.data.get(CONF_ACCESS_TOKEN)
        self._access_token_expires_at = float(entry.data.get(CONF_TOKEN_EXPIRES_AT, 0) or 0)
        self._access_token_type = "Bearer"
        self._refresh_token: str | None = entry.data.get(CONF_REFRESH_TOKEN)
        self._cost_cache: dict = {}
        self._token_lock = asyncio.Lock()
        self._unsub_token_timer = None
        self.token_last_refresh: datetime | None = None
        self.token_refresh_method: str | None = None
        self.token_error: str | None = None
        self.integration_version = INTEGRATION_VERSION
        interval = timedelta(minutes=entry.data.get(CONF_SCAN_INTERVAL, 360))
        super().__init__(
            hass,
            _LOGGER,
            name=f"Sorgenia {entry.data.get(CONF_NAME, 'bills')}",
            update_interval=interval if interval >= timedelta(minutes=15) else DEFAULT_SCAN_INTERVAL,
        )

    @property
    def token_expires_at(self) -> float:
        return self._access_token_expires_at

    @property
    def has_refresh_token(self) -> bool:
        return bool(self._refresh_token)

    def _token_request_headers(self, basic_auth: str) -> dict:
        data = self.entry.data
        # Match the headers used by the successful MySorgenia browser requests.
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "it,en;q=0.9,en-US;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        subscription_key = data.get(CONF_SUBSCRIPTION_KEY, "")
        if subscription_key:
            headers["ocp-apim-subscription-key"] = subscription_key
        try:
            extra_headers = json.loads(data.get(CONF_EXTRA_HEADERS, "{}") or "{}")
        except ValueError:
            extra_headers = {}
        if isinstance(extra_headers, dict):
            headers.update(
                {
                    key: value
                    for key, value in extra_headers.items()
                    if isinstance(key, str) and isinstance(value, str) and key.lower() != "authorization"
                }
            )
        headers["Authorization"] = (
            basic_auth if basic_auth.lower().startswith("basic ") else f"Basic {basic_auth}"
        )
        return headers

    async def async_refresh_access_token(self, stale_token: str | None = None) -> None:
        """Renew the access token, persist it and schedule the next renewal.

        Tries the stored refresh token first, then the legacy username-only request.
        Raises ConfigEntryAuthFailed when Sorgenia rejects every attempt and UpdateFailed
        for temporary problems (network, HTTP 5xx).
        """
        async with self._token_lock:
            if stale_token is not None and self._access_token != stale_token:
                return  # somebody else already renewed it
            data = self.entry.data
            username = str(data.get(CONF_USERNAME, "")).strip()
            basic_auth = str(data.get(CONF_BASIC_AUTH, "")).strip()
            if not (username and basic_auth):
                raise UpdateFailed("Automatic token refresh is not configured")
            _LOGGER.debug("Requesting a Sorgenia API access-token refresh")
            headers = self._token_request_headers(basic_auth)
            url = data.get(CONF_REFRESH_URL, DEFAULT_REFRESH_URL)
            base_body = {
                "username": username,
                "sourceChannel": data.get(CONF_SOURCE_CHANNEL, DEFAULT_SOURCE_CHANNEL),
            }
            refresh_token = self._refresh_token
            if not refresh_token:
                self.token_error = "no refresh token stored; sign in again"
                self.async_cancel_token_timer()
                raise ConfigEntryAuthFailed("No Sorgenia refresh token stored; sign in again")
            # Sorgenia reads the refresh token from a `refreshToken` HTTP header; the body only
            # carries username and sourceChannel (found by trying the possible placements).
            attempts = [("header_refreshToken", base_body, {"refreshToken": refresh_token}, None)]
            rejected = []
            server_errors = 0
            for label, body, extra_headers, params in attempts:
                try:
                    async with self.session.post(
                        url, json=body, headers={**headers, **extra_headers}, params=params
                    ) as response:
                        status = response.status
                        try:
                            payload = await response.json(content_type=None)
                        except (aiohttp.ClientError, ValueError):
                            payload = None
                except (aiohttp.ClientError, TimeoutError) as err:
                    self.token_error = f"{label}: {type(err).__name__}"
                    raise UpdateFailed(
                        f"Unable to refresh Sorgenia access token: {type(err).__name__}"
                    ) from err
                if status >= 500:
                    server_errors += 1
                    rejected.append(f"{label}: HTTP {status}")
                    continue
                if (
                    status < 400
                    and isinstance(payload, dict)
                    and payload.get("accessToken")
                    and payload.get("status") in (None, "OK")
                ):
                    self._apply_token_payload(payload, label)
                    return
                fields = ", ".join(sorted(payload)) if isinstance(payload, dict) else "no JSON body"
                detail = ""
                if isinstance(payload, dict):
                    code = payload.get("errorCode") or payload.get("code") or "not provided"
                    description = (
                        payload.get("errorDescription")
                        or payload.get("message")
                        or payload.get("error_description")
                    )
                    detail = f"; code: {_safe_api_text(code, 40)}; description: {_safe_api_text(description)}"
                rejected.append(f"{label}: HTTP {status} (fields: {fields}{detail})")
            if server_errors == len(attempts):
                self.token_error = "; ".join(rejected)
                raise UpdateFailed("Unable to refresh Sorgenia access token: server error")
            self.token_error = "; ".join(rejected)
            _LOGGER.warning("Sorgenia rejected the token refresh: %s", self.token_error)
            self.async_cancel_token_timer()
            raise ConfigEntryAuthFailed("Sorgenia rejected the token refresh; sign in again")

    def _apply_token_payload(self, payload: dict, method: str) -> None:
        now_timestamp = dt_util.utcnow().timestamp()
        self._access_token = str(payload["accessToken"])
        self._access_token_type = str(payload.get("tokenType") or "Bearer").strip()
        if payload.get("refreshToken"):
            self._refresh_token = str(payload["refreshToken"])
        try:
            expiry = float(payload.get("expiresIn"))
            if expiry > 1_000_000_000_000:
                self._access_token_expires_at = expiry / 1000
            elif expiry > 1_000_000_000:
                self._access_token_expires_at = expiry
            else:
                self._access_token_expires_at = now_timestamp + expiry
        except (TypeError, ValueError):
            self._access_token_expires_at = now_timestamp + 3600
        self.token_last_refresh = dt_util.utcnow()
        self.token_refresh_method = method
        self.token_error = None
        updated_data = {
            **self.entry.data,
            CONF_ACCESS_TOKEN: self._access_token,
            CONF_TOKEN_EXPIRES_AT: self._access_token_expires_at,
        }
        if self._refresh_token:
            updated_data[CONF_REFRESH_TOKEN] = self._refresh_token
        if updated_data != dict(self.entry.data):
            # Token-only changes are ignored by the update listener (no reload).
            self.hass.config_entries.async_update_entry(self.entry, data=updated_data)
        _LOGGER.debug("Sorgenia API access token refreshed and saved (%s)", method)
        self.async_schedule_token_refresh()
        self.async_update_listeners()

    def async_cancel_token_timer(self) -> None:
        if self._unsub_token_timer is not None:
            self._unsub_token_timer()
            self._unsub_token_timer = None

    def async_schedule_token_refresh(self, delay: float | None = None) -> None:
        """Renew the token shortly before it expires, independent of the polling interval."""
        self.async_cancel_token_timer()
        data = self.entry.data
        if not (str(data.get(CONF_USERNAME, "")).strip() and str(data.get(CONF_BASIC_AUTH, "")).strip()):
            return
        now_timestamp = dt_util.utcnow().timestamp()
        if delay is not None:
            due = now_timestamp + delay
        else:
            due = max(self._access_token_expires_at - 600, now_timestamp + 300)
        self._unsub_token_timer = async_track_point_in_utc_time(
            self.hass, self._async_token_timer_fired, dt_util.utc_from_timestamp(due)
        )

    async def _async_token_timer_fired(self, _now) -> None:
        self._unsub_token_timer = None
        try:
            await self.async_refresh_access_token()
        except ConfigEntryAuthFailed:
            self.entry.async_start_reauth(self.hass)
        except UpdateFailed as err:
            _LOGGER.warning("Sorgenia scheduled token refresh failed, retrying in 10 minutes: %s", err)
            self.async_schedule_token_refresh(delay=600)

    async def _async_update_data(self) -> dict:
        data = self.entry.data
        # Match the headers used by the successful MySorgenia browser requests.
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "it,en;q=0.9,en-US;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        auth_header = data.get(CONF_AUTH_HEADER, "").strip()
        auth_value = data.get(CONF_AUTH_VALUE, "")
        username = str(data.get(CONF_USERNAME, "")).strip()
        basic_auth = str(data.get(CONF_BASIC_AUTH, "")).strip()
        automatic_refresh = bool(username and basic_auth)
        if auth_header and auth_value and not automatic_refresh:
            headers[auth_header] = auth_value
        subscription_key = data.get(CONF_SUBSCRIPTION_KEY, "")
        if subscription_key:
            headers["ocp-apim-subscription-key"] = subscription_key
        try:
            extra_headers = json.loads(data.get(CONF_EXTRA_HEADERS, "{}") or "{}")
            if not isinstance(extra_headers, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in extra_headers.items()
            ):
                raise ValueError("headers must be a JSON object of strings")
            if automatic_refresh:
                extra_headers = {
                    key: value for key, value in extra_headers.items()
                    if key.lower() != "authorization"
                }
            headers.update(extra_headers)
        except ValueError as err:
            raise UpdateFailed("Configured extra headers are not a JSON object of strings") from err

        method = data[CONF_METHOD].upper()
        body = json.loads(data[CONF_BODY] or "{}")
        now = dt_util.now()
        month = now.year * 12 + now.month - 1
        month_ago = month - 24
        start_year = now.year - 2
        start_day = min(now.day, calendar.monthrange(start_year, now.month)[1])
        replacements = {
            "{{current_month}}": f"{now.month:02d}/{now.year}",
            "{{month_24_months_ago}}": f"{month_ago % 12 + 1:02d}/{month_ago // 12}",
            "{{today_iso}}": now.date().isoformat(),
            "{{date_24_months_ago_iso}}": now.date().replace(year=start_year, day=start_day).isoformat(),
            "{{today_it}}": now.strftime("%d/%m/%Y"),
            "{{date_24_months_ago_it}}": now.date().replace(year=start_year, day=start_day).strftime("%d/%m/%Y"),
        }

        def replace_dates(value):
            if isinstance(value, dict):
                return {key: replace_dates(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace_dates(item) for item in value]
            if isinstance(value, str):
                for placeholder, replacement in replacements.items():
                    value = value.replace(placeholder, replacement)
            return value

        def bearer_value() -> str:
            return (
                self._access_token
                if self._access_token.lower().startswith("bearer ")
                else f"{self._access_token_type} {self._access_token}"
            )

        async def request_json(
            url: str,
            request_method: str,
            request_body: dict,
            kind: str,
            request_headers: dict | None = None,
            allow_auth_retry: bool = True,
        ) -> dict:
            target_headers = request_headers if request_headers is not None else headers
            for attempt in range(2):
                request_kwargs = {"headers": target_headers}
                if request_method == "GET":
                    request_kwargs["params"] = request_body
                else:
                    request_kwargs["json"] = request_body
                retry_with_new_token = False
                try:
                    async with self.session.request(request_method, url, **request_kwargs) as response:
                        if (
                            attempt == 0
                            and allow_auth_retry
                            and automatic_refresh
                            and kind != "access token"
                            and response.status in (401, 403)
                        ):
                            _LOGGER.debug(
                                "Sorgenia %s request returned HTTP %s; refreshing token and retrying once",
                                kind,
                                response.status,
                            )
                            retry_with_new_token = True
                        else:
                            if response.status >= 400:
                                error_code = "not provided"
                                response_fields = "not provided"
                                try:
                                    error_payload = await response.json(content_type=None)
                                    if isinstance(error_payload, dict):
                                        error_code = str(
                                            error_payload.get("errorCode")
                                            or error_payload.get("error_code")
                                            or error_payload.get("code")
                                            or "not provided"
                                        )
                                        response_fields = ", ".join(sorted(error_payload))
                                except (ValueError, aiohttp.ClientError):
                                    pass
                                _LOGGER.warning(
                                    "Sorgenia %s request returned HTTP %s (API error code: %s; response fields: %s)",
                                    kind,
                                    response.status,
                                    error_code,
                                    response_fields,
                                )
                            response.raise_for_status()
                            result = await response.json(content_type=None)
                except aiohttp.ClientResponseError as err:
                    # No URL in the message: GET requests carry the client code in the query string.
                    raise UpdateFailed(
                        f"Unable to retrieve Sorgenia {kind}: HTTP {err.status} {err.message}"
                    ) from err
                except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                    raise UpdateFailed(
                        f"Unable to retrieve Sorgenia {kind}: {type(err).__name__}"
                    ) from err
                if retry_with_new_token:
                    auth_name = auth_header or "Authorization"
                    if target_headers.get(auth_name) == bearer_value():
                        # The rejected token is still the current one: renew it.
                        await async_refresh_access_token(self._access_token)
                    # else: another renewal already happened, just use the new token.
                    target_headers[auth_name] = bearer_value()
                    continue
                break
            if not isinstance(result, dict):
                raise UpdateFailed(f"Sorgenia {kind} response was not a JSON object")
            if result.get("status") not in (None, "OK"):
                raise UpdateFailed(f"Sorgenia {kind} API returned status {result.get('status')!r}")
            return result

        async def async_refresh_access_token(stale_token: str | None = None) -> None:
            """Renew the token through the coordinator and use it for later requests."""
            await self.async_refresh_access_token(stale_token)
            headers[auth_header or "Authorization"] = bearer_value()

        stale_token = self._access_token
        if automatic_refresh:
            now_timestamp = dt_util.utcnow().timestamp()
            if not self._access_token or self._access_token_expires_at <= now_timestamp + 300:
                await async_refresh_access_token(stale_token)
            else:
                headers[auth_header or "Authorization"] = bearer_value()

        body = replace_dates(json.loads(data[CONF_BODY] or "{}"))
        primary_pr_code = str(body.get("prCode", "")).strip()
        extra_pr_codes = [
            code.strip()
            for code in data.get(CONF_EXTRA_PR_CODES, "").replace("\n", ",").split(",")
            if code.strip()
        ]
        pr_codes = list(dict.fromkeys(([primary_pr_code] if primary_pr_code else []) + extra_pr_codes))
        if not pr_codes:
            raise UpdateFailed("At least one prCode is required to retrieve bills")
        bills = []
        bill_filters = []
        for supply_index, pr_code in enumerate(pr_codes, start=1):
            point_body = {**body, "prCode": pr_code}
            point_payload = await request_json(
                data[CONF_URL], method, point_body, f"bills for supply {supply_index}"
            )
            point_bills = point_payload.get("billsList")
            if not isinstance(point_bills, list):
                raise UpdateFailed("Sorgenia response did not contain a billsList array")
            bills.extend(point_bills)
            bill_filters.extend(point_payload.get("filtersToDisplay", []))
        # Multiple supplies can return overlapping invoices; retain one copy per invoice ID.
        bills = list({bill.get("invoiceId", f"{bill.get('prCode')}:{idx}"): bill for idx, bill in enumerate(bills)}.values())

        consumption_body = data.get(CONF_CONSUMPTION_BODY, DEFAULT_CONSUMPTION_BODY)
        consumption_body = replace_dates(json.loads(consumption_body or "{}"))
        consumption_method = data.get(CONF_CONSUMPTION_METHOD, DEFAULT_CONSUMPTION_METHOD).upper()
        consumption_payload = await request_json(
            data.get(CONF_CONSUMPTION_URL, DEFAULT_CONSUMPTION_URL),
            consumption_method,
            consumption_body,
            "consumption data",
        )
        consumption = consumption_payload.get("consumi", {}).get("list")
        if not isinstance(consumption, list):
            raise UpdateFailed("Sorgenia response did not contain consumi.list")

        # Billed consumption of the electricity supply: same endpoint, electricity PR code.
        # Optional: a failure here must not disable the other sensors.
        electricity_consumption: list = []
        electricity_consumption_error = None
        electricity_point = str(data.get(CONF_CURVE_POINT_CODE, "")).strip()
        gas_point = str(consumption_body.get("codicePunto", "")).strip()
        if electricity_point and electricity_point != gas_point:
            try:
                electricity_payload = await request_json(
                    data.get(CONF_CONSUMPTION_URL, DEFAULT_CONSUMPTION_URL),
                    consumption_method,
                    {**consumption_body, "codicePunto": electricity_point},
                    "electricity consumption data",
                )
                electricity_list = electricity_payload.get("consumi", {}).get("list")
                if not isinstance(electricity_list, list):
                    raise UpdateFailed("Sorgenia response did not contain consumi.list")
                electricity_consumption = electricity_list
            except UpdateFailed as err:
                electricity_consumption_error = str(err)
        else:
            electricity_consumption_error = "electricity PR code missing or equal to the gas PR code"

        curve_body_text = data.get(CONF_CURVE_BODY, "").strip() or DEFAULT_CURVE_BODY
        try:
            curve_body = replace_dates(json.loads(curve_body_text or "{}"))
        except (json.JSONDecodeError, ValueError) as err:
            raise UpdateFailed("Curve request body is not valid JSON") from err
        curve_body["codiceCliente"] = str(body.get("clientCode", "")).strip()
        curve_body["codicePunto"] = str(
            data.get(CONF_CURVE_POINT_CODE)
            or consumption_body.get("codicePunto", "")
        ).strip()
        if not curve_body["codiceCliente"] or not curve_body["codicePunto"]:
            raise UpdateFailed("Curve request requires clientCode and codicePunto in the bills and consumption settings")
        curve_payload = await request_json(
            data.get(CONF_CURVE_URL, DEFAULT_CURVE_URL),
            data.get(CONF_CURVE_METHOD, DEFAULT_CURVE_METHOD).upper(),
            curve_body,
            "consumption curves",
        )
        curves = curve_payload.get("curve")
        if not isinstance(curves, list):
            raise UpdateFailed("Sorgenia response did not contain a curve array")

        # Gas consumption curve (optional): monthly gas volumes, more recent than the billed list.
        gas_point = str(consumption_body.get("codicePunto", "")).strip()
        gas_curves: list = []
        gas_curve_error = None
        if gas_point:
            try:
                gas_curve_payload = await request_json(
                    data.get(CONF_CURVE_URL, DEFAULT_CURVE_URL),
                    data.get(CONF_CURVE_METHOD, DEFAULT_CURVE_METHOD).upper(),
                    {**curve_body, "codicePunto": gas_point},
                    "gas consumption curves",
                )
                gas_curve_list = gas_curve_payload.get("curve")
                if not isinstance(gas_curve_list, list):
                    raise UpdateFailed("Sorgenia response did not contain a curve array")
                gas_curves = gas_curve_list
                gas_curve_api_total = gas_curve_payload.get("totaleCurveConsumo")
            except UpdateFailed as err:
                gas_curve_error = str(err)
                gas_curve_api_total = None
        else:
            gas_curve_error = "gas PR code missing"
            gas_curve_api_total = None

        # Gas meter reading (optional): needs the gas utility code (codiceUtenza).
        optional_settings = await self.hass.async_add_executor_job(
            _read_optional_settings, self.hass.config.path("sorgenia.yaml")
        )
        gas_utility_code = str(
            data.get(CONF_GAS_UTILITY_CODE) or optional_settings.get(CONF_GAS_UTILITY_CODE) or ""
        ).strip()
        gas_meter: dict = {}
        gas_meter_info: dict = {}
        gas_meter_error = None
        if gas_utility_code and gas_point and curve_body["codiceCliente"]:
            try:
                gas_meter_payload = await request_json(
                    DEFAULT_GAS_METER_URL,
                    "POST",
                    {
                        "codicePR": gas_point,
                        "codiceCliente": curve_body["codiceCliente"],
                        "codiceUtenza": gas_utility_code,
                        "canale": "WEB",
                    },
                    "gas meter reading",
                )
                gas_meter = gas_meter_payload.get("ultimalettura") or {}
                gas_meter_info = gas_meter_payload.get("anagraficaAcquisita") or {}
            except UpdateFailed as err:
                gas_meter_error = str(err)
        else:
            gas_meter_error = "gas_utility_code missing in /config/sorgenia.yaml"

        # Cost breakdown of the latest bill of each supply (optional). Cached per invoice.
        bill_costs: dict = {}
        for commodity, codes, fallback_point in (
            ("electricity", ("ele", "electricity"), electricity_point),
            ("gas", ("gas",), gas_point),
        ):
            selected = [b for b in bills if str(b.get("commodity", "")).lower() in codes]
            bill = _pick_latest_bill(selected)
            invoice_id = str(bill.get("invoiceId") or "").strip()
            if not invoice_id:
                continue
            cached = self._cost_cache.get(invoice_id)
            if cached is None:
                point = str(bill.get("prCode") or fallback_point or "").strip()
                try:
                    cost_payload = await request_json(
                        DEFAULT_BILL_COSTS_URL,
                        "POST",
                        {"codicePunto": point, "idFattura": invoice_id, "username": username},
                        f"{commodity} bill costs",
                    )
                    items = {}
                    for entry in cost_payload.get("array") or []:
                        if isinstance(entry, dict) and entry.get("item"):
                            number = _to_float(entry.get("amount"))
                            if number is not None:
                                items[str(entry["item"])] = number
                    cached = {
                        "invoice_id": invoice_id,
                        "product": cost_payload.get("prodotto"),
                        "items": items,
                    }
                    self._cost_cache[invoice_id] = cached
                except UpdateFailed as err:
                    cached = {"invoice_id": invoice_id, "items": {}, "error": str(err)}
            bill_costs[commodity] = cached

        greeners = {}
        greeners_error = None
        client_code = body.get("clientCode")
        if client_code:
            try:
                greeners_payload = await request_json(
                    data.get(CONF_GREENERS_URL, DEFAULT_GREENERS_URL),
                    data.get(CONF_GREENERS_METHOD, DEFAULT_GREENERS_METHOD).upper(),
                    {"codiceCliente": client_code},
                    "Greeners status",
                )
                greeners_items = greeners_payload.get("wekit_data")
                if not isinstance(greeners_items, list):
                    raise UpdateFailed("Sorgenia response did not contain wekit_data")
                greeners = greeners_items[0] if greeners_items else {}
            except UpdateFailed as err:
                # Greeners is an optional endpoint; its failure must not disable bills/consumption.
                greeners_error = str(err)
        else:
            greeners_error = "clientCode is missing from the bills request body"

        return {
            "bills": bills,
            "filters": bill_filters,
            "consumption": consumption,
            "consumption_electricity": electricity_consumption,
            "consumption_electricity_error": electricity_consumption_error,
            "curves": curves,
            "gas_curves": gas_curves,
            "gas_curve_error": gas_curve_error,
            "gas_curve_api_total": gas_curve_api_total,
            "gas_meter": gas_meter,
            "gas_meter_info": gas_meter_info,
            "gas_meter_error": gas_meter_error,
            "bill_costs": bill_costs,
            "curve_total": curve_payload.get("totaleCurveConsumo"),
            "greeners": greeners,
            "greeners_error": greeners_error,
        }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = SorgeniaCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.info("Sorgenia Bills %s loaded", INTEGRATION_VERSION)
    coordinator.async_schedule_token_refresh()
    entry.async_on_unload(coordinator.async_cancel_token_timer)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
