"""Какой дорогой голос оператора идёт к панели: ISAPI или HCNetSDK.

Вынесено из device.py (правило 500 строк). Выбор канала:
- панель принимает звук по ISAPI (DS-KV6113) → `talk_via: isapi`, talkback.py;
- ISAPI звук не принимает (DS-K1T341AM: 404 notSupport), но помощник SDK
  (sdkaudio.py) на этой платформе живой → `talk_via: sdk`;
- ни того ни другого → `talk_supported: no`, `talk_via: none` и `talk_hint`
  с причиной — поп-ап прячет «Микрофон» и один раз говорит почему;
- ISAPI-вердикт ещё не выяснен (панель была офлайн) → `unknown`.

Браузер всегда шлёт G.711 µ-law — и ISAPI (кодек панели ставим сами), и SDK
(помощник говорит µ-law) принимают его без перекодирования.
"""
from __future__ import annotations

import logging

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME

from . import sdkaudio
from .const import CONF_HTTP_PORT, DEFAULT_HTTP_PORT
from .sdkaudio import SDKAudioError, SDKTalkSession
from .talkback import TwoWayAudioError, TwoWayAudioSession, TwoWayAudioUnsupported

_LOGGER = logging.getLogger(__name__)

VIA_ISAPI = "isapi"
VIA_SDK = "sdk"
VIA_NONE = "none"
VIA_UNKNOWN = "unknown"


class TalkRouteMixin:
    """Микрофон оператора → панель; состояние живёт на BMSIntercomDevice."""

    def _init_talk(self) -> None:
        self._talk: TwoWayAudioSession | SDKTalkSession | None = None
        self._talk_bytes = 0
        self._talk_logged_at = 0
        # Вердикт помощника SDK: None = не проверяли, иначе (живой?, пояснение).
        self._sdk_ok: bool | None = None
        self._sdk_why = ""
        self.talk_error = ""

    # --- вердикт -----------------------------------------------------------
    @property
    def _isapi_talk(self) -> bool | None:
        if self.is_demo:
            return True
        return None if self._client is None else self._client.talk_supported

    @property
    def talk_via(self) -> str:
        isapi = self._isapi_talk
        if isapi:
            return VIA_ISAPI
        if isapi is None:
            return VIA_UNKNOWN
        return VIA_SDK if self._sdk_ok else VIA_NONE

    @property
    def talk_supported(self) -> bool | None:
        """Дойдёт ли голос оператора хоть какой-то дорогой. None = не выяснено."""
        via = self.talk_via
        return None if via == VIA_UNKNOWN else via != VIA_NONE

    @property
    def talk_attributes(self) -> dict[str, str]:
        via = self.talk_via
        supported = self.talk_supported
        attrs = {
            "talk_supported": "unknown" if supported is None else ("yes" if supported else "no"),
            "talk_via": via,
        }
        if via == VIA_NONE:
            attrs["talk_hint"] = self._sdk_why or sdkaudio.NO_PLATFORM
        if self.talk_error:
            attrs["talk_error"] = self.talk_error
        return attrs

    async def _async_route_talk(self) -> None:
        """ISAPI звук не принимает → проверить помощника SDK (кеш — дёшево)."""
        if self._isapi_talk is not False or self._sdk_ok:
            return
        try:
            ok, why = await sdkaudio.async_check()
        except Exception as err:  # noqa: BLE001 - проверка не должна ронять HA
            ok, why = False, f"проверка помощника голоса сорвалась: {err}"
        if (ok, why) != (self._sdk_ok, self._sdk_why):
            self._sdk_ok, self._sdk_why = ok, why
            _LOGGER.info(
                "[%s] Голос оператора: %s", self.name,
                "по HCNetSDK (%s)" % why if ok else "недоступен — %s" % why,
            )
            self._notify()

    def _set_talk_error(self, text: str) -> None:
        if text != self.talk_error:
            self.talk_error = text
            self._notify()

    # --- сессия ------------------------------------------------------------
    async def async_talk_start(self) -> None:
        """Открыть голос оператора к панели той дорогой, что у неё есть."""
        if self.is_demo:
            return
        host = self.entry.data.get(CONF_HOST)
        if not host:
            return
        # Закрываем прошлую сессию (вкладку закрыли без talk_stop) — иначе к
        # панели останется висеть two-way-сокет или процесс помощника.
        await self.async_talk_stop()
        # Вердикт по SDK мог устареть (сбой selftest повторяется) — каждый раз.
        await self._async_route_talk()
        if self.talk_via == VIA_SDK:
            await self._async_talk_start_sdk(host)
            return
        if self._isapi_talk is False:
            # Не стучимся в open/audioData модели, которая звук не принимает.
            _LOGGER.debug("[%s] Микрофон не открыт: %s", self.name, self._sdk_why)
            return
        sess = TwoWayAudioSession(
            host,
            self._opt(CONF_HTTP_PORT, DEFAULT_HTTP_PORT),
            self.entry.data.get(CONF_USERNAME, ""),
            self.entry.data.get(CONF_PASSWORD, ""),
        )
        try:
            await sess.async_open()
        except TwoWayAudioUnsupported as err:
            # Профиль при старте мог не прочитаться (панель была офлайн) —
            # запоминаем вердикт здесь и сразу пробуем SDK.
            _LOGGER.warning("[%s] Микрофон к панели: %s", self.name, err)
            await sess.async_close()
            if self._client is not None:
                self._client.talk_supported = False
                self._notify()
                await self._async_route_talk()
                if self.talk_via == VIA_SDK:
                    await self._async_talk_start_sdk(host)
            return
        except Exception as err:  # noqa: BLE001 - микрофон не должен ронять HA
            _LOGGER.warning("[%s] Не удалось открыть микрофон к панели: %s", self.name, err)
            await sess.async_close()
            return
        self._begin_talk(sess, "ISAPI")

    async def _async_talk_start_sdk(self, host: str) -> None:
        sess = SDKTalkSession()
        try:
            codec = await sess.async_open(
                host,
                sdkaudio.SDK_PORT,
                self.entry.data.get(CONF_USERNAME, ""),
                self.entry.data.get(CONF_PASSWORD, ""),
            )
        except SDKAudioError as err:
            _LOGGER.warning("[%s] Микрофон к терминалу: %s", self.name, err)
            self._set_talk_error(str(err))
            return
        except Exception as err:  # noqa: BLE001 - микрофон не должен ронять HA
            _LOGGER.warning("[%s] Микрофон к терминалу (SDK): %r", self.name, err)
            await sess.async_close()
            self._set_talk_error(f"голос по SDK: {err!r}")
            return
        if codec != "G.711ulaw":
            _LOGGER.warning("[%s] Помощник SDK ждёт %s, браузер шлёт µ-law", self.name, codec)
        self._set_talk_error("")
        self._begin_talk(sess, "HCNetSDK")

    def _begin_talk(self, sess, via: str) -> None:
        self._talk = sess
        self._talk_bytes = 0
        self._talk_logged_at = 0
        _LOGGER.debug("[%s] Микрофон к панели открыт: %s, кодек %s", self.name, via, sess.codec)

    async def async_talk_send(self, data: bytes) -> None:
        """Передать кусок G.711 от браузера на панель."""
        if self._talk is None:
            return
        try:
            await self._talk.async_send(data)
        except (TwoWayAudioError, SDKAudioError) as err:
            _LOGGER.warning("[%s] Микрофон: поток к панели оборвался (%s)", self.name, err)
            await self.async_talk_stop()
            return
        # Раз в ~2 с (16000 байт при 8 кГц) отметить в debug, что звук уходит.
        self._talk_bytes += len(data)
        if self._talk_bytes - self._talk_logged_at >= 16000:
            self._talk_logged_at = self._talk_bytes
            _LOGGER.debug("[%s] Микрофон → панель: отправлено %d Б", self.name, self._talk_bytes)

    async def async_talk_stop(self) -> None:
        """Закрыть голос оператора (ISAPI-канал или процесс помощника)."""
        sess, self._talk = self._talk, None
        if sess is not None:
            await sess.async_close()
