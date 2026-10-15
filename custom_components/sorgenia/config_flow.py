"""Simple username, password, and OTP setup for Sorgenia."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiohttp
import voluptuous as vol
import yaml

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_AUTH_HEADER,
    CONF_AUTH_VALUE,
    CONF_BASIC_AUTH,
    CONF_BODY,
    CONF_CONSUMPTION_BODY,
    CONF_CONSUMPTION_METHOD,
    CONF_CONSUMPTION_URL,
    CONF_CURVE_BODY,
    CONF_CURVE_METHOD,
    CONF_CURVE_POINT_CODE,
    CONF_CURVE_URL,
    CONF_EXTRA_HEADERS,
    CONF_EXTRA_PR_CODES,
    CONF_GREENERS_METHOD,
    CONF_GREENERS_URL,
    CONF_METHOD,
    CONF_PASSWORD,
    CONF_REFRESH_URL,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE_CHANNEL,
    CONF_SUBSCRIPTION_KEY,
    CONF_TOKEN_EXPIRES_AT,
    CONF_URL,
    CONF_USERNAME,
    DEFAULT_BODY,
    DEFAULT_CONSUMPTION_BODY,
    DEFAULT_CONSUMPTION_METHOD,
    DEFAULT_CONSUMPTION_URL,
    DEFAULT_CURVE_BODY,
    DEFAULT_CURVE_METHOD,
    DEFAULT_CURVE_URL,
    DEFAULT_GREENERS_METHOD,
    DEFAULT_GREENERS_URL,
    DEFAULT_LOGIN_URL,
    DEFAULT_METHOD,
    DEFAULT_NAME,
    DEFAULT_REFRESH_URL,
    DEFAULT_SEND_OTP_URL,
    DEFAULT_SOURCE_CHANNEL,
    DEFAULT_URL,
    DEFAULT_VERIFY_OTP_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
LOCAL_CONFIG_FILE = "sorgenia.yaml"


def _safe_error_message(payload: dict, secrets: list[str]) -> str | None:
    """Extract a short API error message while masking submitted credentials."""
    message = next(
        (
            payload.get(key)
            for key in (
                "errorMessage",
                "error_message",
                "errorDescription",
                "error_description",
                "errorDesc",
                "error_desc",
                "message",
                "description",
                "error",
                "detail",
                "title",
            )
            if isinstance(payload.get(key), str) and payload.get(key).strip()
        ),
        None,
    )
    if message is None:
        return None
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    return " ".join(message.split())[:180]


def _credentials_schema(username: str = "") -> vol.Schema:
    """Show only the user's Sorgenia login credentials."""
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=username): str,
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


def _load_local_settings(path: str) -> dict:
    """Read API credentials and supply identifiers from /config/sorgenia.yaml."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(LOCAL_CONFIG_FILE)
    with path.open(encoding="utf-8") as config_file:
        settings = yaml.safe_load(config_file) or {}
    if not isinstance(settings, dict):
        raise ValueError("sorgenia.yaml must contain a YAML object")

    required = (
        "client_code",
        "electricity_pr_code",
        "gas_pr_code",
        "authorization_basic",
        "subscription_key",
    )
    missing = [key for key in required if not str(settings.get(key, "")).strip()]
    if missing:
        raise ValueError("Missing fields: " + ", ".join(missing))

    client_code = str(settings["client_code"]).strip()
    electricity_pr_code = str(settings["electricity_pr_code"]).strip()
    gas_pr_code = str(settings["gas_pr_code"]).strip()
    pr_codes = [electricity_pr_code, gas_pr_code]
    extras = settings.get("extra_headers", {}) or {}
    if not isinstance(extras, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in extras.items()
    ):
        raise ValueError("extra_headers must contain string keys and values")
    extras.setdefault("Origin", "https://areaclienti.sorgenia.it")

    body = {
        "clientCode": client_code,
        "prCode": pr_codes[0],
        "startDate": "{{month_24_months_ago}}",
        "endDate": "{{current_month}}",
    }
    consumption_body = {
        "codicePunto": gas_pr_code,
        "startDate": "{{date_24_months_ago_iso}}",
        "endDate": "{{today_iso}}",
        "username": "",
    }
    return {
        CONF_NAME: str(settings.get("name", DEFAULT_NAME)),
        CONF_URL: DEFAULT_URL,
        CONF_METHOD: DEFAULT_METHOD,
        CONF_BODY: json.dumps(body),
        CONF_EXTRA_PR_CODES: ",".join(pr_codes[1:]),
        CONF_CONSUMPTION_URL: DEFAULT_CONSUMPTION_URL,
        CONF_CONSUMPTION_METHOD: DEFAULT_CONSUMPTION_METHOD,
        CONF_CONSUMPTION_BODY: json.dumps(consumption_body),
        CONF_CURVE_URL: DEFAULT_CURVE_URL,
        CONF_CURVE_METHOD: DEFAULT_CURVE_METHOD,
        CONF_CURVE_BODY: DEFAULT_CURVE_BODY,
        CONF_CURVE_POINT_CODE: electricity_pr_code,
        CONF_GREENERS_URL: DEFAULT_GREENERS_URL,
        CONF_GREENERS_METHOD: DEFAULT_GREENERS_METHOD,
        CONF_REFRESH_URL: DEFAULT_REFRESH_URL,
        CONF_BASIC_AUTH: str(settings["authorization_basic"]).strip(),
        CONF_SUBSCRIPTION_KEY: str(settings["subscription_key"]).strip(),
        CONF_EXTRA_HEADERS: json.dumps(extras),
        CONF_SOURCE_CHANNEL: str(settings.get("source_channel", DEFAULT_SOURCE_CHANNEL)),
        CONF_AUTH_HEADER: "Authorization",
        CONF_AUTH_VALUE: "",
        CONF_SCAN_INTERVAL: int(settings.get("scan_interval", 360)),
    }


class SorgeniaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Sign in, verify OTP, and create the Sorgenia entry."""

    VERSION = 1

    async def _begin_login(self, username: str, password: str, entry=None):
        try:
            settings = await self.hass.async_add_executor_job(
                _load_local_settings, self.hass.config.path(LOCAL_CONFIG_FILE)
            )
        except FileNotFoundError:
            return self.async_show_form(
                step_id="user" if entry is None else "reconfigure",
                data_schema=_credentials_schema(username),
                errors={"base": "local_config_missing"},
            )
        except (OSError, ValueError, yaml.YAMLError) as err:
            _LOGGER.warning("Unable to read local Sorgenia settings: %s", err)
            return self.async_show_form(
                step_id="user" if entry is None else "reconfigure",
                data_schema=_credentials_schema(username),
                errors={"base": "local_config_invalid"},
            )

        basic = settings[CONF_BASIC_AUTH].strip()
        # Mirror the browser headers seen on the successful MySorgenia login.
        # Some API gateway deployments validate these even though they are
        # normally optional for JSON APIs.
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "it,en;q=0.9,en-US;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            **json.loads(settings[CONF_EXTRA_HEADERS]),
        }
        headers["Authorization"] = basic if basic.lower().startswith("basic ") else f"Basic {basic}"
        headers["ocp-apim-subscription-key"] = settings[CONF_SUBSCRIPTION_KEY]
        source = settings[CONF_SOURCE_CHANNEL]
        session = async_get_clientsession(self.hass)
        stage = "login"

        try:
            async with session.post(
                DEFAULT_LOGIN_URL,
                json={"username": username, "password": password, "sourceChannel": source},
                headers=headers,
            ) as response:
                if response.status >= 400:
                    api_code = None
                    api_message = None
                    error_payload = None
                    try:
                        error_payload = await response.json(content_type=None)
                        if isinstance(error_payload, dict):
                            api_code = (
                                error_payload.get("errorCode")
                                or error_payload.get("error_code")
                                or error_payload.get("code")
                            )
                            api_message = _safe_error_message(
                                error_payload,
                                [
                                    username,
                                    password,
                                    headers.get("Authorization", ""),
                                    headers.get("ocp-apim-subscription-key", ""),
                                ],
                            )
                    except (ValueError, aiohttp.ClientError):
                        pass
                    _LOGGER.warning(
                        "Sorgenia %s response HTTP %s (API error code: %s; message: %s; response fields: %s)",
                        stage,
                        response.status,
                        api_code or "not provided",
                        api_message or "not provided",
                        ", ".join(sorted(error_payload))
                        if isinstance(error_payload, dict)
                        else "not provided",
                    )
                response.raise_for_status()
                login_result = await response.json(content_type=None)
            _LOGGER.debug(
                "Sorgenia login response fields: %s",
                ", ".join(sorted(login_result)) if isinstance(login_result, dict) else type(login_result).__name__,
            )
            phone = login_result.get("validatedPhone")
            login_token = login_result.get("accessToken")
            if login_result.get("status") != "OK" or not phone:
                raise ValueError("Sorgenia did not return a validated phone")

            stage = "send_otp"
            async with session.post(
                DEFAULT_SEND_OTP_URL,
                json={
                    "username": username,
                    "phone": phone,
                    "phoneType": "VALIDATED",
                    "sourceChannel": source,
                },
                headers=headers,
            ) as response:
                if response.status >= 400:
                    api_code = None
                    api_message = None
                    error_payload = None
                    try:
                        error_payload = await response.json(content_type=None)
                        if isinstance(error_payload, dict):
                            api_code = (
                                error_payload.get("errorCode")
                                or error_payload.get("error_code")
                                or error_payload.get("code")
                            )
                            api_message = _safe_error_message(
                                error_payload,
                                [
                                    username,
                                    password,
                                    headers.get("Authorization", ""),
                                    headers.get("ocp-apim-subscription-key", ""),
                                ],
                            )
                    except (ValueError, aiohttp.ClientError):
                        pass
                    _LOGGER.warning(
                        "Sorgenia %s response HTTP %s (API error code: %s; message: %s; response fields: %s)",
                        stage,
                        response.status,
                        api_code or "not provided",
                        api_message or "not provided",
                        ", ".join(sorted(error_payload))
                        if isinstance(error_payload, dict)
                        else "not provided",
                    )
                response.raise_for_status()
                otp_result = await response.json(content_type=None)
            _LOGGER.debug(
                "Sorgenia sendOTP response fields: %s",
                ", ".join(sorted(otp_result)) if isinstance(otp_result, dict) else type(otp_result).__name__,
            )
            if otp_result.get("status") != "OK":
                raise ValueError("Sorgenia rejected the OTP request")
        except aiohttp.ClientResponseError as err:
            _LOGGER.warning("Sorgenia %s request returned HTTP %s", stage, err.status)
            if err.status == 400:
                error = "login_bad_request" if stage == "login" else "otp_request_bad_request"
            else:
                error = "api_auth_failed" if err.status in (401, 403) else "login_failed"
            return self.async_show_form(
                step_id="user" if entry is None else "reconfigure",
                data_schema=_credentials_schema(username),
                errors={"base": error},
            )
        except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError) as err:
            _LOGGER.warning("Sorgenia login/OTP request failed: %s", err)
            return self.async_show_form(
                step_id="user" if entry is None else "reconfigure",
                data_schema=_credentials_schema(username),
                errors={"base": "login_failed"},
            )

        settings[CONF_USERNAME] = username
        consumption_body = json.loads(settings[CONF_CONSUMPTION_BODY])
        consumption_body["username"] = username
        settings[CONF_CONSUMPTION_BODY] = json.dumps(consumption_body)
        self._pending_data = settings
        self._pending_username = username
        self._pending_phone = phone
        self._pending_source = source
        self._pending_headers = headers
        self._pending_login_token = login_token
        self._pending_entry = entry
        return await self.async_step_otp()

    async def async_step_user(self, user_input: dict | None = None):
        if user_input is not None:
            return await self._begin_login(
                user_input[CONF_USERNAME].strip(), user_input[CONF_PASSWORD]
            )
        return self.async_show_form(
            step_id="user", data_schema=_credentials_schema()
        )

    async def async_step_reauth(self, entry_data: dict):
        """Sorgenia rejected the token renewal: sign in again (credentials + OTP)."""
        return await self.async_step_reconfigure()

    async def async_step_reconfigure(self, user_input: dict | None = None):
        entry = (
            self._get_reauth_entry()
            if self.source == config_entries.SOURCE_REAUTH
            else self._get_reconfigure_entry()
        )
        if user_input is not None:
            return await self._begin_login(
                user_input[CONF_USERNAME].strip(), user_input[CONF_PASSWORD], entry
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_credentials_schema(entry.data.get(CONF_USERNAME, "")),
        )

    async def async_step_otp(self, user_input: dict | None = None):
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            # The browser's verifyOTP request carries no Authorization header,
            # so try without it first; fall back to the login Bearer token.
            base_headers = {
                key: value
                for key, value in self._pending_headers.items()
                if key.lower() != "authorization"
            }
            attempts = [("no_auth", base_headers)]
            login_token = getattr(self, "_pending_login_token", None)
            if login_token:
                attempts.append(
                    ("bearer", {**base_headers, "Authorization": f"Bearer {login_token}"})
                )
            payload = {
                "username": self._pending_username,
                "phone": self._pending_phone,
                "otp": user_input["otp"],
                "sourceChannel": self._pending_source,
                "phoneType": "VALIDATED",
                "dialingCode": "",
            }
            try:
                result = None
                for index, (label, attempt_headers) in enumerate(attempts):
                    async with session.post(
                        DEFAULT_VERIFY_OTP_URL, json=payload, headers=attempt_headers
                    ) as response:
                        if response.status == 401 and index < len(attempts) - 1:
                            _LOGGER.debug(
                                "Sorgenia verifyOTP attempt '%s' returned 401, trying next", label
                            )
                            continue
                        if response.status >= 400:
                            err_payload = None
                            try:
                                err_payload = await response.json(content_type=None)
                            except (ValueError, aiohttp.ClientError):
                                pass
                            if isinstance(err_payload, dict):
                                _LOGGER.debug(
                                    "Sorgenia verifyOTP '%s' HTTP %s: code=%s status=%s message=%s fields=%s",
                                    label,
                                    response.status,
                                    err_payload.get("errorCode") or err_payload.get("code"),
                                    err_payload.get("status"),
                                    _safe_error_message(err_payload, [self._pending_username]),
                                    ", ".join(sorted(err_payload)),
                                )
                            else:
                                _LOGGER.debug(
                                    "Sorgenia verifyOTP '%s' HTTP %s (no JSON body)",
                                    label,
                                    response.status,
                                )
                        response.raise_for_status()
                        result = await response.json(content_type=None)
                        _LOGGER.debug("Sorgenia verifyOTP accepted with attempt '%s'", label)
                        break
                if result is None:
                    raise ValueError("Sorgenia verifyOTP gave no result")
                if result.get("status") != "OK" or not result.get("accessToken"):
                    raise ValueError("Sorgenia rejected the OTP")
            except aiohttp.ClientResponseError as err:
                _LOGGER.warning("Sorgenia OTP verification returned HTTP %s", err.status)
                return self.async_show_form(
                    step_id="otp",
                    data_schema=vol.Schema({vol.Required("otp"): str}),
                    errors={"base": "otp_failed"},
                    description_placeholders={"phone": str(self._pending_phone)},
                )
            except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError) as err:
                _LOGGER.warning("Sorgenia OTP verification failed: %s", err)
                return self.async_show_form(
                    step_id="otp",
                    data_schema=vol.Schema({vol.Required("otp"): str}),
                    errors={"base": "otp_failed"},
                    description_placeholders={"phone": str(self._pending_phone)},
                )

            expiry = float(result.get("expiresIn", 0) or 0)
            self._pending_data[CONF_ACCESS_TOKEN] = result["accessToken"]
            if result.get("refreshToken"):
                self._pending_data[CONF_REFRESH_TOKEN] = result["refreshToken"]
            self._pending_data[CONF_TOKEN_EXPIRES_AT] = (
                expiry / 1000 if expiry > 1_000_000_000_000 else expiry
            )
            if self._pending_entry is not None:
                return self.async_update_reload_and_abort(
                    self._pending_entry, data_updates=self._pending_data
                )
            await self.async_set_unique_id(self._pending_data[CONF_NAME].lower())
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._pending_data[CONF_NAME], data=self._pending_data
            )

        return self.async_show_form(
            step_id="otp",
            data_schema=vol.Schema({vol.Required("otp"): str}),
            description_placeholders={"phone": str(self._pending_phone)},
        )
