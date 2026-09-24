"""BMSIntercomDevice end to end: setup, polling, stream fallback.

Three bugs reached the panel from this layer, none of them visible to the
client-level tests: a NameError in async_setup (0.2.4), a NameError on the
last line of every poll (0.2.4–0.2.5: the status was read, never applied),
and a stream fallback that never kicked in. These tests run the real device
code against a fake panel, with Home Assistant's scheduling replaced by plain
asyncio.

Needs Home Assistant importable (the prepared venv); skipped otherwise.
"""
from __future__ import annotations

import asyncio
import unittest

try:
    import homeassistant  # noqa: F401
    import httpx
    HAVE_HA = True
except ImportError:  # pragma: no cover
    HAVE_HA = False

from _loader import load

if HAVE_HA:
    const = load("const")
    callsource = load("callsource")
    device_mod = load("device")
    isapi = load("isapi")
    binary_sensor = load("binary_sensor")
    sdkaudio = load("sdkaudio")
    from test_ds_k1t341am import Panel


class FakeEntry:
    def __init__(self, data, options=None):
        self.entry_id = "entry1"
        self.data = dict(data)
        self.options = dict(options or {})
        self.tasks: list[asyncio.Task] = []

    def async_create_background_task(self, hass, coro, name):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task


class FakeConfigEntries:
    def __init__(self):
        self.updates = 0

    def async_update_entry(self, entry, *, data=None, options=None):
        self.updates += 1
        if data is not None:
            entry.data = dict(data)
        if options is not None:
            entry.options = dict(options)


class FakeHass:
    def __init__(self):
        self.data: dict = {}
        self.config_entries = FakeConfigEntries()


class Scheduler:
    """Stands in for async_track_time_interval / async_call_later."""

    def __init__(self):
        self.intervals: list = []
        self.cancelled = 0

    def track_interval(self, hass, action, interval):
        self.intervals.append(action)

        def unsub():
            self.cancelled += 1
            if action in self.intervals:
                self.intervals.remove(action)

        return unsub

    def call_later(self, hass, delay, action):
        return lambda: None

    async def tick(self, times=1):
        from datetime import datetime

        for _ in range(times):
            for action in list(self.intervals):
                await action(datetime.now())


REAL = {
    "name": "Домофон",
    "mode": "real",
    "host": "192.168.70.121",
    "username": "admin",
    "password": "Sekret123!",
    "http_port": 80,
    "rtsp_port": 554,
    "door_no": 1,
    "channel": 101,
    "use_alert_stream": True,
}


@unittest.skipUnless(HAVE_HA, "Home Assistant not installed")
class DeviceTestCase(unittest.TestCase):
    def setUp(self):
        self.sched = Scheduler()
        self._saved = {
            "track": callsource.async_track_time_interval,
            "later": callsource.async_call_later,
            "dispatch": device_mod.async_dispatcher_send,
            "client": device_mod.ISAPIClient,
            "backoff_start": callsource.ALERT_BACKOFF_START,
            "backoff_max": callsource.ALERT_BACKOFF_MAX,
            "sdk_helper": sdkaudio._prepare_helper,
        }
        # Помощник SDK — внешний процесс: по умолчанию его «нет», чтобы тесты
        # не зависели от того, что лежит в sdk/ и на какой машине они идут.
        sdkaudio._prepare_helper = lambda: sdkaudio.NO_PLATFORM
        sdkaudio.reset_cache()
        callsource.async_track_time_interval = self.sched.track_interval
        callsource.async_call_later = self.sched.call_later
        self.notifications = 0

        def dispatch(hass, signal, *args):
            self.notifications += 1

        device_mod.async_dispatcher_send = dispatch
        callsource.ALERT_BACKOFF_START = 0.01
        callsource.ALERT_BACKOFF_MAX = 0.02

    def tearDown(self):
        callsource.async_track_time_interval = self._saved["track"]
        callsource.async_call_later = self._saved["later"]
        device_mod.async_dispatcher_send = self._saved["dispatch"]
        device_mod.ISAPIClient = self._saved["client"]
        callsource.ALERT_BACKOFF_START = self._saved["backoff_start"]
        callsource.ALERT_BACKOFF_MAX = self._saved["backoff_max"]
        sdkaudio._prepare_helper = self._saved["sdk_helper"]
        sdkaudio.reset_cache()

    def use_panel(self, handler):
        real_client = self._saved["client"]

        def factory(*args, **kwargs):
            client = real_client(*args, **kwargs)
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            return client

        device_mod.ISAPIClient = factory

    def make_device(self, data=None, options=None):
        hass = FakeHass()
        entry = FakeEntry(data or REAL, options)
        return device_mod.BMSIntercomDevice(hass, entry), hass, entry

    async def settle(self, entry, predicate, timeout=3.0):
        """Wait until `predicate()` holds or the background tasks finish."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return predicate()

    async def shutdown(self, device, entry):
        await device.async_shutdown()
        for task in entry.tasks:
            task.cancel()
        await asyncio.gather(*entry.tasks, return_exceptions=True)


class TestSetup(DeviceTestCase):
    def test_setup_runs_for_a_real_panel(self):
        """0.2.4: NameError here — the integration never came up on site."""
        self.use_panel(Panel())

        async def main():
            device, _hass, entry = self.make_device()
            await device.async_setup()
            self.assertIsNotNone(device.client)
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_learned_profile_is_exposed(self):
        self.use_panel(Panel())

        async def main():
            device, _hass, entry = self.make_device()
            await device.async_setup()
            await self.settle(entry, lambda: device.signal_supported is not None
                              and device.snapshot_supported is not None)
            self.assertIs(device.signal_supported, False)
            self.assertIs(device.snapshot_supported, False)
            await self.shutdown(device, entry)

        asyncio.run(main())


class TestPollingAppliesTheStatus(DeviceTestCase):
    """0.2.4–0.2.5: the poll read "ring" and then died on a NameError."""

    def _poll_device(self, panel):
        self.use_panel(panel)
        return self.make_device(options={"use_alert_stream": False})

    def test_ring_reaches_the_call_state(self):
        panel = Panel("idle")

        async def main():
            device, _hass, entry = self._poll_device(panel)
            await device.async_setup()
            self.assertEqual(len(self.sched.intervals), 1)
            await self.sched.tick()
            self.assertEqual(device.call_state, "idle")
            panel.status = "ring"
            await self.sched.tick()
            self.assertEqual(device.call_state, "ringing")
            panel.status = "onCall"
            await self.sched.tick()
            self.assertEqual(device.call_state, "answered")
            panel.status = "hangUp"
            await self.sched.tick()
            self.assertEqual(device.call_state, "idle")
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_unknown_word_keeps_the_state(self):
        panel = Panel("ring")

        async def main():
            device, _hass, entry = self._poll_device(panel)
            await device.async_setup()
            await self.sched.tick()
            self.assertEqual(device.call_state, "ringing")
            panel.status = "somethingNew"
            await self.sched.tick()
            self.assertEqual(device.call_state, "ringing")
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_poll_errors_back_off(self):
        calls = {"n": 0}

        def broken(request):
            calls["n"] += 1
            raise httpx.ConnectError("panel unreachable")

        async def main():
            self.use_panel(broken)
            device, _hass, entry = self.make_device(options={"use_alert_stream": False})
            await device.async_setup()
            await self.settle(entry, lambda: all(t.done() for t in entry.tasks))
            before = calls["n"]
            await self.sched.tick(5)
            # One failing poll, then quiet for CALL_POLL_ERROR_BACKOFF seconds.
            self.assertLessEqual(calls["n"] - before, 1)
            self.assertIs(device.panel_available, False)
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_an_outage_never_switches_the_doorbell_off(self):
        """0.2.4–0.2.5: an offline panel looked "unsupported" and the poller
        stopped for good — a power blip at the gate killed the doorbell."""
        panel = Panel("idle")
        state = {"offline": True}

        def handler(request):
            if state["offline"]:
                raise httpx.ConnectError("No route to host")
            return panel(request)

        saved = callsource.CALL_POLL_ERROR_BACKOFF
        callsource.CALL_POLL_ERROR_BACKOFF = 0.05
        self.addCleanup(setattr, callsource, "CALL_POLL_ERROR_BACKOFF", saved)
        self.use_panel(handler)

        async def main():
            device, _hass, entry = self.make_device(options={"use_alert_stream": False})
            await device.async_setup()
            await self.sched.tick()
            self.assertIs(device.panel_available, False)
            self.assertEqual(len(self.sched.intervals), 1, "опрос остановлен насовсем")
            # The panel comes back and a visitor presses the button.
            state["offline"] = False
            panel.status = "ring"
            await asyncio.sleep(0.06)            # error backoff expires
            await self.sched.tick()
            self.assertIs(device.panel_available, True)
            self.assertEqual(device.call_state, "ringing")
            await self.shutdown(device, entry)

        asyncio.run(main())


class NeverEndingBody(httpx.AsyncByteStream):
    """A 401 body the panel never finishes (the suspected field behaviour)."""

    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        while True:
            await asyncio.sleep(3600)
            yield b""

    async def aclose(self):
        self.closed = True


class TestStreamFallback(DeviceTestCase):
    def test_404_falls_back_and_is_remembered_without_the_checkbox(self):
        self.use_panel(Panel())

        async def main():
            device, hass, entry = self.make_device()
            await device.async_setup()
            ok = await self.settle(entry, lambda: not device._use_alert_stream)
            self.assertTrue(ok, "откат на опрос не произошёл сам")
            self.assertEqual(device.call_source, "опрос callStatus")
            self.assertIs(entry.data.get(const.CONF_ALERT_STREAM_SUPPORTED), False)
            self.assertEqual(len(self.sched.intervals), 1)      # exactly one poller
            # Writing the verdict down must not reload the integration.
            self.assertIn(entry.entry_id, hass.data[callsource._SKIP_RELOAD])
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_405_counts_as_unsupported_too(self):
        class Panel405(Panel):
            def __call__(self, request):
                if "alertStream" in request.url.path:
                    return httpx.Response(405, text="methodNotAllowed")
                return super().__call__(request)

        self.use_panel(Panel405())

        async def main():
            device, _hass, entry = self.make_device()
            await device.async_setup()
            self.assertTrue(await self.settle(entry, lambda: not device._use_alert_stream))
            self.assertIs(entry.data.get(const.CONF_ALERT_STREAM_SUPPORTED), False)
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_hanging_401_body_does_not_block_the_404(self):
        """The 401 body is never read, so the 404 on attempt 1 is reached."""
        bodies: list[NeverEndingBody] = []
        panel = Panel()

        def handler(request):
            if "alertStream" in request.url.path:
                if not request.headers.get("authorization"):
                    body = NeverEndingBody()
                    bodies.append(body)
                    return httpx.Response(
                        401,
                        headers={"WWW-Authenticate":
                                 'Digest qop="auth", realm="DS-11A8BC2D", nonce="n1", opaque=""'},
                        stream=body,
                    )
                return httpx.Response(404, text="notSupport")
            return panel(request)

        self.use_panel(handler)

        async def main():
            device, _hass, entry = self.make_device()
            await device.async_setup()
            ok = await self.settle(entry, lambda: not device._use_alert_stream, timeout=3)
            self.assertTrue(ok, "висящее тело 401 заблокировало откат")
            self.assertTrue(bodies and all(b.closed for b in bodies))
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_unexplained_stream_failures_start_polling_at_once(self):
        panel = Panel("ring")

        def handler(request):
            if "alertStream" in request.url.path:
                raise httpx.ReadTimeout("")        # the empty-message error
            return panel(request)

        self.use_panel(handler)

        async def main():
            device, _hass, entry = self.make_device()
            await device.async_setup()
            # Polling starts after the FIRST failure — the doorbell is covered.
            self.assertTrue(await self.settle(entry, lambda: len(self.sched.intervals) == 1))
            await self.sched.tick()
            self.assertEqual(device.call_state, "ringing")
            # …and after STREAM_GIVE_UP failures the stream is dropped for the
            # session, but NOT remembered (the cause is not a clear 404).
            self.assertTrue(await self.settle(entry, lambda: not device._use_alert_stream))
            self.assertNotIn(const.CONF_ALERT_STREAM_SUPPORTED, entry.data)
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_remembered_verdict_skips_the_stream_after_restart(self):
        self.use_panel(Panel())

        async def main():
            data = {**REAL, const.CONF_ALERT_STREAM_SUPPORTED: False}
            device, _hass, entry = self.make_device(data=data)
            await device.async_setup()
            self.assertFalse(device._use_alert_stream)
            self.assertIsNone(device._alert_task)
            self.assertEqual(len(self.sched.intervals), 1)
            await self.shutdown(device, entry)

        asyncio.run(main())


def acs_part(sub: int, extra: str = "") -> str:
    """Одна часть alertStream терминала: AccessControllerEvent в JSON."""
    return (
        "--MIME_boundary\r\nContent-Type: application/json; charset=\"UTF-8\"\r\n\r\n"
        '{"ipAddress":"192.168.70.121","eventType":"AccessControllerEvent",'
        '"eventState":"active","AccessControllerEvent":{"majorEventType":5,'
        f'"subEventType":{sub},{extra}"name":"Иванов Иван","employeeNoString":"1007",'
        '"pictureURL":"http://192.168.70.121/LOCALS/pic/1.jpg"}}\r\n'
    )


class TestPanelEventsAttribute(DeviceTestCase):
    """0.3.1: нажатие «Вызов» на DS-K1T341AM шло accesscontrollerevent-ом,
    а мы его не узнавали и не показывали, что именно пришло."""

    def test_stream_events_reach_the_call_sensor(self):
        panel = Panel()
        sent = {"n": 0}

        def handler(request):
            if "alertStream" in request.url.path:
                sent["n"] += 1
                # Первое подключение — догон истории (старый «Вызов» 5/0x25
                # с currentEvent=false) и «лицо», второе — настоящий вызов.
                old = acs_part(0x25, '"currentEvent":false,')
                body = old + acs_part(75) if sent["n"] == 1 else acs_part(0x25)
                return httpx.Response(
                    200,
                    headers={"Content-Type": "multipart/mixed; boundary=MIME_boundary"},
                    text=body,
                )
            return panel(request)

        self.use_panel(handler)

        async def main():
            device, _hass, entry = self.make_device()
            await device.async_setup()
            ok = await self.settle(entry, lambda: device.call_state == "ringing")
            self.assertTrue(ok, "кнопка вызова 5/0x25 не дала звонок")
            attrs = binary_sensor.CallBinarySensor(device).extra_state_attributes
            events = attrs["panel_events"]
            self.assertTrue(events)
            # В атрибуте только нераспознанное, без персональных полей; старый
            # «Вызов» — там же с пометкой history, звонком он не стал.
            self.assertEqual([(e["fields"]["subeventtype"], e.get("history"))
                              for e in events], [("37", True), ("75", None)])
            for e in events:
                for key in ("name", "employeenostring", "pictureurl"):
                    self.assertNotIn(key, e["fields"])
            self.assertNotIn("Иванов", str(attrs))
            await self.shutdown(device, entry)

        asyncio.run(main())


class TestErrorText(unittest.TestCase):
    def test_empty_httpx_errors_still_have_a_reason(self):
        transport = load("transport")
        self.assertEqual(transport.describe_error(httpx.ReadTimeout("")), "ReadTimeout")
        self.assertEqual(
            transport.describe_error(httpx.ConnectError("refused")),
            "ConnectError: refused",
        )


if __name__ == "__main__":
    unittest.main()
