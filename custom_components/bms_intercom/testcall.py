"""«Тестовый звонок» для реальной панели: проверить звонок, не выходя к калитке.

Отдельный модуль, как callsource.py: device.py и так у предела в 500 строк.

1. Сначала просим саму панель позвонить (callSignal cmdType=request, формы —
   endpoints.call_request_calls). Настоящий звонок засчитан, только если
   панель ответила OK И наш источник вызова (поток событий или опрос
   callStatus) за TEST_CALL_WAIT_SECONDS увидел ringing: «OK» без звонка —
   не звонок, владелец должен узнать об этом, а не гадать.
2. Не вышло — запасной «тестовый вызов с живым видео»: вызов живёт только в
   HA (call_source = «тест»), поп-ап показывает настоящее видео с панели,
   микрофон и «Открыть» работают как обычно (открыть дверь — явное действие
   человека), а «Ответить»/«Сбросить» НЕ шлют callSignal: звонка на панели нет,
   команда ушла бы в пустоту или сбросила бы чужой разговор.
   Как опрос со словом idle не гасит тест раньше времени — см. защиту в
   callsource._apply_panel_state.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .callsource import STATE_ANSWERED, STATE_RINGING
from .isapi import ISAPIError

_LOGGER = logging.getLogger(__name__)

#: Сколько ждать ringing от своего источника после «OK» панели.
TEST_CALL_WAIT_SECONDS = 8.0
#: Значение атрибута call_source, пока идёт запасной тестовый вызов.
TEST_CALL_SOURCE = "тест"


class CallTestMixin:
    """Половина BMSIntercomDevice про кнопку «Тестовый звонок»."""

    #: Идёт запасной (только в HA) тестовый вызов.
    _test_call = False
    #: До этого момента (monotonic) слово idle от панели тест не гасит.
    _test_call_until = 0.0
    #: Проверка уже идёт — повторное нажатие не запускает вторую.
    _test_call_busy = False
    #: Что вышло в последний раз — для атрибута test_call_result.
    test_call_result = ""

    async def async_test_call(self) -> None:
        """Нажата «Тестовый звонок»: настоящий звонок, иначе запасной режим."""
        if self.is_demo:
            await self.async_simulate_call()
            return
        if self._test_call_busy:
            _LOGGER.info("[%s] Тестовый звонок уже проверяется", self.name)
            return
        if self.call_active:
            # Идёт настоящий вызов — тест только помешал бы ему.
            self._report_test_call("не начат: уже идёт вызов")
            return
        self._test_call_busy = True
        try:
            reason = await self._async_try_real_call()
        finally:
            self._test_call_busy = False
        if reason is None:
            return
        if self.call_active:
            self._report_test_call("не начат: пока проверяли, начался вызов")
            return
        self._report_test_call(
            f"запасной режим — вызов только в HA, видео с панели живое. "
            f"Причина: {reason}"
        )
        self._test_call = True
        now = time.monotonic()
        self._test_call_until = now + self.call_timeout
        self._ringing_at = now
        # Тот же путь, что у демо и у настоящего звонка: таймер вызова взводится
        # здесь же, так что тест гаснет сам и не висит вечно.
        self._apply_call_state(STATE_RINGING)

    async def _async_try_real_call(self) -> str | None:
        """Попросить панель позвонить. None = настоящий звонок пошёл, иначе причина."""
        client = self._client
        if client is None:
            return "нет связи с панелью"
        if client.call_signal_supported is None:
            # Профиль ещё не прочитан (нажали сразу после запуска).
            await client.async_load_capabilities()
        if client.call_signal_supported is False:
            # По capabilities команд вызова нет (профиль DS-K1T341AM V3.2.30):
            # стучаться незачем — сразу запасной режим.
            return "у модели нет команд вызова (callSignal)"
        try:
            accepted, detail = await client.async_request_call()
        except ISAPIError as err:
            return f"панель не ответила: {err}"
        if not accepted:
            return detail
        self._report_test_call(f"ждём звонок от панели: {detail}")
        if await self._async_wait_ringing():
            self._report_test_call(f"настоящий звонок с панели ({detail})")
            return None
        return f"{detail}, но звонок не появился за {TEST_CALL_WAIT_SECONDS:g} с"

    async def _async_wait_ringing(self) -> bool:
        """Ждать, пока поток событий или опрос callStatus покажут вызов."""
        deadline = time.monotonic() + TEST_CALL_WAIT_SECONDS
        while True:
            if self.call_state in (STATE_RINGING, STATE_ANSWERED):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)

    def _report_test_call(self, text: str) -> None:
        """Итог — в журнал INFO и в атрибут: владелец видит его без логов."""
        self.test_call_result = text
        _LOGGER.info("[%s] Тестовый звонок: %s", self.name, text)
        self._notify()
