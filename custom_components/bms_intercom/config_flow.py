"""Config flow for BMS Intercom."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback

from .const import (
    CONF_CALL_TIMEOUT,
    CONF_CHANNEL,
    CONF_DOOR_NO,
    CONF_HTTP_PORT,
    CONF_HTTPS_URL,
    CONF_MODE,
    CONF_PROXY_PORT,
    CONF_RTSP_PORT,
    CONF_USE_ALERT_STREAM,
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_CHANNEL,
    DEFAULT_DOOR_NO,
    DEFAULT_HTTP_PORT,
    DEFAULT_NAME,
    DEFAULT_PROXY_PORT,
    DEFAULT_RTSP_PORT,
    DEFAULT_USE_ALERT_STREAM,
    DOMAIN,
    MODE_DEMO,
    MODE_REAL,
)
from .isapi import ISAPIAuthError, ISAPIClient, ISAPIError


class BMSIntercomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Guide the user through creating an intercom (demo or real)."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "BMSIntercomOptionsFlow":
        return BMSIntercomOptionsFlow()

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """First screen: pick demo or real panel."""
        return self.async_show_menu(step_id="user", menu_options=["demo", "real"])

    async def async_step_demo(self, user_input=None) -> ConfigFlowResult:
        """Demo mode: virtual panel, no hardware needed."""
        if user_input is not None:
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data={CONF_MODE: MODE_DEMO, **user_input},
            )
        schema = vol.Schema({vol.Required(CONF_NAME, default=DEFAULT_NAME): str})
        return self.async_show_form(step_id="demo", data_schema=schema)

    async def async_step_real(self, user_input=None) -> ConfigFlowResult:
        """Real mode: connect to a DS-KV panel over the network."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = ISAPIClient(
                user_input[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                http_port=user_input[CONF_HTTP_PORT],
                rtsp_port=user_input[CONF_RTSP_PORT],
                door_no=user_input[CONF_DOOR_NO],
                channel=user_input[CONF_CHANNEL],
            )
            try:
                await client.async_verify()
            except ISAPIAuthError:
                errors["base"] = "invalid_auth"
            except ISAPIError:
                errors["base"] = "cannot_connect"
            finally:
                await client.async_close()
            if not errors:
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={CONF_MODE: MODE_REAL, **user_input},
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_USERNAME, default="admin"): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_HTTP_PORT, default=DEFAULT_HTTP_PORT): int,
                vol.Optional(CONF_RTSP_PORT, default=DEFAULT_RTSP_PORT): int,
                vol.Optional(CONF_DOOR_NO, default=DEFAULT_DOOR_NO): int,
                vol.Optional(CONF_CHANNEL, default=DEFAULT_CHANNEL): int,
                vol.Optional(
                    CONF_USE_ALERT_STREAM, default=DEFAULT_USE_ALERT_STREAM
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="real", data_schema=schema, errors=errors
        )


class BMSIntercomOptionsFlow(OptionsFlow):
    """Options: set the HTTPS address used for the microphone (secure context)."""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = (user_input.get(CONF_HTTPS_URL) or "").strip().rstrip("/")
            if url and not url.startswith("https://"):
                errors["base"] = "https_required"
            else:
                data = {
                    CONF_PROXY_PORT: user_input.get(
                        CONF_PROXY_PORT, DEFAULT_PROXY_PORT
                    ),
                    CONF_HTTPS_URL: url,
                    CONF_CALL_TIMEOUT: user_input.get(
                        CONF_CALL_TIMEOUT, DEFAULT_CALL_TIMEOUT
                    ),
                }
                if CONF_CHANNEL in user_input:
                    data[CONF_CHANNEL] = user_input[CONF_CHANNEL]
                if CONF_USE_ALERT_STREAM in user_input:
                    data[CONF_USE_ALERT_STREAM] = user_input[CONF_USE_ALERT_STREAM]
                return self.async_create_entry(title="", data=data)

        opts = self.config_entry.options
        data = self.config_entry.data
        fields: dict = {
            vol.Optional(
                CONF_PROXY_PORT,
                default=opts.get(CONF_PROXY_PORT, DEFAULT_PROXY_PORT),
            ): int,
            vol.Optional(CONF_HTTPS_URL, default=opts.get(CONF_HTTPS_URL, "")): str,
            vol.Optional(
                CONF_CALL_TIMEOUT,
                default=opts.get(CONF_CALL_TIMEOUT, DEFAULT_CALL_TIMEOUT),
            ): int,
        }
        # Panel-only options: hidden for a demo intercom.
        if CONF_HOST in data:
            fields[
                vol.Optional(
                    CONF_CHANNEL,
                    default=opts.get(
                        CONF_CHANNEL, data.get(CONF_CHANNEL, DEFAULT_CHANNEL)
                    ),
                )
            ] = int
            fields[
                vol.Optional(
                    CONF_USE_ALERT_STREAM,
                    default=opts.get(
                        CONF_USE_ALERT_STREAM,
                        data.get(CONF_USE_ALERT_STREAM, DEFAULT_USE_ALERT_STREAM),
                    ),
                )
            ] = bool
        schema = vol.Schema(fields)
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
