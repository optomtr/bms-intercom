"""Какой дорогой голос оператора идёт к панели: ISAPI или HCNetSDK.

Вынесено из device.py (правило 500 строк). Выбор канала:
- панель принимает звук по ISAPI (DS-KV6113) → `talk_via: isapi`, talkback.py;
- ISAPI звук не принимает (DS-K1T341AM: 404 notSupport), но помощник SDK
  (sdkaudio.py) на этой платформе живой → `talk_via: sdk`; разговор идёт через
  постоянный помощник (sdkhelper.py), поднятый заранее — вход к «Ответить»
  уже выполнен;
- ни того ни другого → `talk_supported: no`, `talk_via: none` и `talk_hint`
  с причиной — поп-ап прячет «Микрофон» и один раз говорит почему;
- ISAPI-вердикт ещё не выяснен (панель была офлайн) → `unknown`.

Браузер всегда шлёт G.711 µ-law — и ISAPI (кодек панели ставим сами), и SDK
(помощник говорит µ-law) принимают его без перекодирования.
"""
from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME

from . import sdkaudio, sdkhelper
from .const import CONF_HTTP_PORT, DEFAULT_HTTP_PORT
from .sdkaudio import SDKAudioError
from .sdkhelper import SDKHelper, SDKVoice
from .talkback import TwoWayAudioError, TwoWayAudioSession, TwoWayAudioUnsupported

_LOGGER = logging.getLogger(__name__)

VIA_ISAPI = "isapi"
VIA_SDK = "sdk"
VIA_NONE = "none"
VIA_UNKNOWN = "unknown"

#: talk_start, а постоянный помощник ещё входит (только что поднят или
#: перезапускается) — столько ждём его ready.
SDK_READY_WAIT = 6.0
#: Терминал занят своим вызовом (sdkaudio.BUSY_CODES): после reject/hangUp ему
#: нужно время выйти из режима вызова — повторяем S так часто и так долго.
SDK_BUSY_RETRY = 0.3
SDK_BUSY_WINDOW = 6.0
#: …и на повторах освобождаем терминал заново, но не чаще: answer из
#: «Ответить» идёт параллельно talk_start и мог дойти после нашего reject.
SDK_REFREE_EVERY = 1.5


class TalkRouteMixin:
    """Микрофон оператора → панель; состояние живёт на BMSIntercomDevice."""

    def _init_talk(self) -> None:
        self._talk: TwoWayAudioSession | SDKVoice | None = None
        # Постоянный помощник SDK этого терминала (создаётся, когда talk_via=sdk).
        self._sdk_helper: SDKHelper | None = None
        # Идущее открытие голоса: один запуск на все talk_start (см. ниже).
        self._talk_opening: asyncio.Task | None = None
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
        """ISAPI звук не принимает → проверить помощника SDK (кеш — дёшево).

        Голос пойдёт по SDK — сразу поднимаем постоянного помощника: вход на
        терминал (~5,5 с на HA Green) должен быть выполнен до первого звонка.
        """
        if self._isapi_talk is not False:
            return
        if not self._sdk_ok:
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
        self._sdk_helper_ensure()

    def _sdk_helper_ensure(self) -> SDKHelper | None:
        """Постоянный помощник этого терминала: создать и поднять в фоне."""
        if self.is_demo or self.talk_via != VIA_SDK:
            return None
        host = self.entry.data.get(CONF_HOST)
        if not host:
            return None
        if self._sdk_helper is None:
            self._sdk_helper = SDKHelper(
                self.name,
                host,
                sdkaudio.SDK_PORT,
                self.entry.data.get(CONF_USERNAME, ""),
                self.entry.data.get(CONF_PASSWORD, ""),
                create_task=lambda coro, name: self.entry.async_create_background_task(
                    self.hass, coro, name),
                on_fatal=self._on_sdk_fatal,
            )
        self._sdk_helper.start()
        return self._sdk_helper

    def _on_sdk_fatal(self, text: str, code: int | None) -> None:
        """Помощник больше не поднимается (неверный пароль): сказать сразу, не в «Ответить»."""
        _LOGGER.warning("[%s] Микрофон к терминалу: %s — помощник голоса больше не входит%s",
                        self.name, text, "; исправьте пароль в настройках интеграции"
                        if code in sdkhelper.FATAL_CODES else "")
        self._set_talk_error(text)

    def _talk_on_ringing(self) -> None:
        """Звонок: к «Ответить» помощник должен быть со входом — упавшего поднять сейчас."""
        helper = self._sdk_helper_ensure()
        if helper is not None:
            helper.kick()

    def _set_talk_error(self, text: str) -> None:
        if text != self.talk_error:
            self.talk_error = text
            self._notify()

    # --- сессия ------------------------------------------------------------
    async def async_talk_start(self) -> None:
        """Открыть голос оператора к панели той дорогой, что у неё есть.

        Карточка зовёт talk_start и по «Ответить», и по кнопке микрофона —
        второй вызов ждёт уже идущее открытие, а открытый голос по SDK берёт
        как есть. Иначе второй помощник вошёл бы на терминал рядом с первым.
        """
        if self.is_demo:
            return
        host = self.entry.data.get(CONF_HOST)
        if not host:
            return
        task = self._talk_opening
        if task is None or task.done():
            if isinstance(self._talk, SDKVoice) and self._talk.alive:
                _LOGGER.debug("[%s] Голос по SDK уже открыт — второй S не нужен", self.name)
                return
            # Между проверкой и запуском нет await — второй вызов не проскочит.
            task = self._talk_opening = asyncio.create_task(self._async_open_talk(host))
        else:
            _LOGGER.debug("[%s] Голос уже открывается — ждём тот же запуск", self.name)
        try:
            # shield: отмена одного ждущего (закрыли вкладку) не рвёт открытие
            # для другого; открытие отменяет только talk_stop.
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise

    async def _async_open_talk(self, host: str) -> None:
        # Закрываем прошлую сессию (вкладку закрыли без talk_stop) — иначе к
        # панели останется висеть two-way-сокет или процесс помощника.
        await self._async_close_talk()
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
        except asyncio.CancelledError:
            await sess.async_close()   # talk_stop во время открытия
            raise
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
        """Открыть голос на постоянном помощнике: освободить терминал, затем S.

        Вход уже выполнен помощником (sdkhelper) — здесь только StartVoiceCom.
        Живой DS-K1T341AM (0.3.6): пока терминал в своём режиме вызова (звонит
        на Main Station 0.0.0.0, и после нашего ISAPI answer тоже), StartVoiceCom
        отвечает 11. Поэтому СНАЧАЛА reject → hangUp (вызов в HA не трогаем),
        потом S; на 11/31 — тот же S тем же процессом каждые SDK_BUSY_RETRY с,
        до SDK_BUSY_WINDOW. 0.3.6–0.3.7 на каждый повтор запускали нового
        помощника (+5,5 с входа) — голос включался через ~12 с. Неверный пароль
        не повторяем: помощник его не перевходит (блокировка 153).
        """
        started = time.monotonic()
        helper = self._sdk_helper_ensure()
        if helper is None:
            return
        helper.kick()
        if not await helper.async_wait_ready(SDK_READY_WAIT):
            text = helper.fatal or "голос по SDK: помощник не вошёл на терминал за %g с%s" % (
                SDK_READY_WAIT, f" ({helper.last_error})" if helper.last_error else "")
            _LOGGER.warning("[%s] Микрофон к терминалу: %s", self.name, text)
            self._set_talk_error(text)
            return
        await self._async_free_terminal()
        freed_at = time.monotonic()
        deadline = freed_at + SDK_BUSY_WINDOW
        tries = 0
        while True:
            tries += 1
            try:
                codec = await helper.async_voice_start(sdkaudio.SDK_CHANNEL)
                break
            except SDKAudioError as err:
                busy = err.code in sdkaudio.BUSY_CODES
                if busy and time.monotonic() < deadline:
                    if tries == 1:
                        _LOGGER.info("[%s] Микрофон к терминалу: %s — повторяю каждые %g с "
                                     "до %g с", self.name, err, SDK_BUSY_RETRY, SDK_BUSY_WINDOW)
                    await asyncio.sleep(SDK_BUSY_RETRY)
                    if time.monotonic() - freed_at >= SDK_REFREE_EVERY:
                        await self._async_free_terminal()
                        freed_at = time.monotonic()
                    continue
                text = f"{err}; не освободился за {SDK_BUSY_WINDOW:g} с" if busy else str(err)
                _LOGGER.warning("[%s] Микрофон к терминалу: %s", self.name, text)
                self._set_talk_error(text)
                return
            except Exception as err:  # noqa: BLE001 - микрофон не должен ронять HA
                _LOGGER.warning("[%s] Микрофон к терминалу (SDK): %r", self.name, err)
                self._set_talk_error(f"голос по SDK: {err!r}")
                return
        if codec != "G.711ulaw":
            _LOGGER.warning("[%s] Помощник SDK ждёт %s, браузер шлёт µ-law", self.name, codec)
        _LOGGER.info("[%s] Голос к терминалу открыт за %d мс (попыток StartVoiceCom: %d)",
                     self.name, (time.monotonic() - started) * 1000, tries)
        self._set_talk_error("")
        self._begin_talk(SDKVoice(helper), "HCNetSDK")

    async def _async_free_terminal(self) -> None:
        """reject → hangUp на терминал, НЕ трогая вызов в HA.

        Не через _async_call_signal: тот перевёл бы вызов в idle и включил бы
        паузу «после Сбросить», а оператор как раз начинает говорить — поп-ап
        остаётся «Разговор», латч разговора держит его против idle от опроса.
        Обе команды, как в async_reject; ошибки не важны, важен повтор.
        Запасной тестовый вызов не трогаем: звонка на терминале нет, а
        reject/hangUp сбросили бы чужой разговор.
        """
        if self._client is None or self._test_call:
            return
        for cmd in ("reject", "hangUp"):
            try:
                await self._client.async_signal(cmd)
            except Exception as err:  # noqa: BLE001 - освобождение «на всякий случай»
                _LOGGER.debug("[%s] Освобождение терминала: «%s» не прошла: %s",
                              self.name, cmd, err)

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
        """Закрыть голос оператора (ISAPI-канал или процесс помощника).

        Идущее открытие тоже отменяем: повторы по SDK длятся до ~6 с, и
        «Сбросить» в это время иначе оставил бы голос открытым после вызова.
        Постоянный помощник SDK остаётся со входом — до следующего звонка.
        """
        task, self._talk_opening = self._talk_opening, None
        opening = task is not None and not task.done()
        if opening:
            task.cancel()
            await asyncio.wait({task})
        await self._async_close_talk()
        if opening and self._sdk_helper is not None:
            # Отменённый S мог успеть открыть голос у помощника — E безвреден и закроет его.
            await self._sdk_helper.async_voice_stop()

    async def async_talk_shutdown(self) -> None:
        """Выгрузка интеграции: голос закрыть, постоянного помощника остановить (Logout)."""
        await self.async_talk_stop()
        helper, self._sdk_helper = self._sdk_helper, None
        if helper is not None:
            await helper.async_close()

    async def _async_close_talk(self) -> None:
        sess, self._talk = self._talk, None
        if sess is not None:
            await sess.async_close()
