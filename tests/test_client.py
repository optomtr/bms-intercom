"""ISAPIClient against a fake panel (httpx MockTransport).

Proves the behaviour the live site needs: digest first, one retry with basic,
remember what worked, walk the candidate list until something answers.

Run: python3 -m unittest discover -s tests -v   (skipped when httpx is absent)
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import unittest

try:
    import httpx
except ImportError:  # pragma: no cover - plain interpreter without httpx
    httpx = None

from _loader import load

if httpx is not None:
    isapi = load("isapi")


def run(coro):
    return asyncio.run(coro)


def make_client(handler, **kwargs):
    client = isapi.ISAPIClient("192.168.70.121", "admin", "Sekret123!", **kwargs)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


class FakeDigestPanel:
    """A panel that really checks the digest response (plain hashlib, no reuse
    of the module under test), the way DS-K1T341AM does for a browser."""

    def __init__(self, password="Sekret123!", *, qop="auth", algorithm="MD5",
                 realm="DS-K1T341AM", nonce="0a1b2c3d4e5f6789"):
        self.password = password
        self.qop = qop
        self.algorithm = algorithm
        self.realm = realm
        self.nonce = nonce
        self.seen: list[str] = []      # Authorization headers received
        self.challenges_sent = 0

    def challenge(self) -> str:
        parts = [f'realm="{self.realm}"', f'nonce="{self.nonce}"']
        if self.qop:
            parts.append(f'qop="{self.qop}"')
        if self.algorithm:
            parts.append(f"algorithm={self.algorithm}")
        return "Digest " + ", ".join(parts)

    def _unauthorized(self):
        self.challenges_sent += 1
        return httpx.Response(401, headers={"WWW-Authenticate": self.challenge()})

    def expected(self, params, method, raw_path):
        ha1 = hashlib.md5(
            f"admin:{self.realm}:{self.password}".encode()
        ).hexdigest()
        ha2 = hashlib.md5(f"{method}:{raw_path}".encode()).hexdigest()
        if self.qop:
            return hashlib.md5(
                f"{ha1}:{self.nonce}:{params['nc']}:{params['cnonce']}:"
                f"{params['qop']}:{ha2}".encode()
            ).hexdigest()
        return hashlib.md5(f"{ha1}:{self.nonce}:{ha2}".encode()).hexdigest()

    def __call__(self, request: "httpx.Request") -> "httpx.Response":
        auth = request.headers.get("authorization", "")
        self.seen.append(auth)
        if not auth.lower().startswith("digest "):
            return self._unauthorized()
        params = dict(
            (m.group(1).lower(), m.group(2) or m.group(3))
            for m in re.finditer(
                r'(\w+)=(?:"([^"]*)"|([^,\s]+))', auth[7:]
            )
        )
        raw_path = request.url.raw_path.decode()
        if params.get("uri") != raw_path:
            return self._unauthorized()   # uri must match what we received
        if params.get("response") != self.expected(params, request.method, raw_path):
            return self._unauthorized()
        return httpx.Response(
            200, headers={"Content-Type": "application/xml"},
            text="<userCheck><statusValue>200</statusValue></userCheck>",
        )


@unittest.skipIf(httpx is None, "httpx not installed")
class TestRealDigest(unittest.TestCase):
    """End-to-end against a panel that verifies the hash for real."""

    def test_challenge_is_answered_correctly(self):
        panel = FakeDigestPanel()
        client = make_client(panel)
        run(client.async_verify())
        self.assertEqual(client.auth_mode, "digest")
        self.assertEqual(panel.seen[0], "")            # first request is bare
        self.assertTrue(panel.seen[1].startswith("Digest "))
        self.assertEqual(panel.challenges_sent, 1)     # exactly one 401
        run(client.async_close())

    def test_every_request_takes_its_own_challenge_by_default(self):
        """DS-K1T341AM nonces are one-shot: never carry one over."""
        panel = FakeDigestPanel()
        client = make_client(panel)
        run(client.async_verify())
        panel.seen.clear()
        run(client.async_get_call_status())
        self.assertEqual(panel.seen[0], "")            # asks for a fresh nonce
        self.assertIn("nc=00000001", panel.seen[1])    # answers with nc=1
        run(client.async_close())

    def test_nonce_reuse_is_available_as_an_opt_in_fast_path(self):
        panel = FakeDigestPanel()
        client = make_client(panel, reuse_nonce=True)
        run(client.async_verify())
        panel.seen.clear()
        run(client.async_get_call_status())
        self.assertTrue(panel.seen[0].startswith("Digest "))
        self.assertIn("nc=00000002", panel.seen[0])    # no second 401 needed
        run(client.async_close())

    def test_cnonce_is_fresh_for_every_challenge(self):
        panel = FakeDigestPanel()
        client = make_client(panel)
        run(client.async_verify())
        run(client.async_get_call_status())
        cnonces = re.findall(r'cnonce="([^"]+)"', " ".join(panel.seen))
        self.assertEqual(len(cnonces), len(set(cnonces)))
        run(client.async_close())

    def test_query_string_is_part_of_the_signed_uri(self):
        panel = FakeDigestPanel()
        client = make_client(panel)
        run(client.async_get_call_status())  # /…/callStatus?format=json
        signed = [a for a in panel.seen if a.startswith("Digest ")][0]
        self.assertIn('uri="/ISAPI/VideoIntercom/callStatus?format=json"', signed)
        run(client.async_close())

    def test_panel_without_qop_or_algorithm_still_authenticates(self):
        panel = FakeDigestPanel(qop="", algorithm="")
        client = make_client(panel)
        run(client.async_verify())
        self.assertEqual(client.auth_mode, "digest")
        signed = [a for a in panel.seen if a.startswith("Digest ")][0]
        self.assertNotIn("qop=", signed)
        self.assertNotIn("algorithm", signed)
        run(client.async_close())

    def test_wrong_password_does_not_loop_forever(self):
        panel = FakeDigestPanel(password="another-one")
        client = make_client(panel)
        with self.assertRaises(isapi.ISAPIAuthError):
            run(client.async_verify())
        # one bare + one signed attempt per candidate endpoint, plus exactly
        # one basic rescue for the whole client — no retry storm
        self.assertLessEqual(len(panel.seen), 2 * len(load("endpoints").identity_calls()) + 1)
        run(client.async_close())

    def test_probe_report_shows_the_challenge_and_what_we_sent(self):
        panel = FakeDigestPanel(password="another-one")   # every request 401s
        client = make_client(panel)
        results, summary = run(client.async_probe())
        report = load("probe").format_probe_report(
            results, host="192.168.70.121", summary=summary
        )
        self.assertIn('Digest realm="DS-K1T341AM"', report)   # verbatim header
        self.assertIn("nonce=16 симв.", report)               # parsed challenge
        self.assertIn('username="admin"', report)             # what we sent
        self.assertIn("response=", report)
        self.assertNotIn("another-one", report)               # never the password
        self.assertIn("WWW-Authenticate (первый 401)", report)
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestDualAttemptDiagnostics(unittest.TestCase):
    """The basic fallback must never hide what digest sent and got back."""

    def _panel(self, accept_basic: bool):
        panel = FakeDigestPanel(password="совсем-другой")  # digest always 401s

        def handler(request):
            auth = request.headers.get("authorization", "")
            if accept_basic and auth.lower().startswith("basic "):
                panel.seen.append(auth)
                return httpx.Response(200, text="<userCheck/>")
            return panel(request)

        return panel, handler

    def test_both_attempts_are_reported_with_their_status(self):
        _panel, handler = self._panel(accept_basic=True)
        client = make_client(handler)
        results, summary = run(client.async_probe())
        report = load("probe").format_probe_report(results, summary=summary)
        # Both attempts must show up for the SAME endpoint — the basic
        # fallback must not replace the digest line of the one it rescued.
        rescued = [r for r in results if any(
            scheme == "basic" for scheme, _h, _s in r.attempts
        )]
        self.assertTrue(rescued, "разовая проверка basic не выполнялась")
        line = rescued[0].as_line()
        self.assertIn("digest → 401", line)
        self.assertIn("basic → 200", line)
        self.assertIn("digest → 401", report)          # per-endpoint attempt
        self.assertIn("basic → 200", report)           # its one-shot rescue
        # The digest header is still visible even though basic came last.
        self.assertIn('username="admin"', report)
        self.assertIn("Basic ***", report)
        # And the side-by-side line for the browser-proven endpoint.
        self.assertIn("проверка /ISAPI/Security/userCheck", report)
        self.assertNotIn("совсем-другой", report)
        run(client.async_close())

    def test_attempts_are_recorded_per_request(self):
        _panel, handler = self._panel(accept_basic=True)
        client = make_client(handler)
        run(client.async_verify())
        schemes = [scheme for scheme, _h, _s in client.last_attempts]
        self.assertEqual(schemes, ["без заголовка", "digest", "basic"])
        self.assertEqual([st for _s, _h, st in client.last_attempts], [401, 401, 200])
        run(client.async_close())

    def test_basic_rescue_happens_only_once(self):
        """A panel that rejects everything must not double every request."""
        _panel, handler = self._panel(accept_basic=False)
        client = make_client(handler)
        with self.assertRaises(isapi.ISAPIAuthError):
            run(client.async_verify())
        basics = sum(
            1 for scheme, _h, _s in client.last_attempts if scheme == "basic"
        )
        self.assertLessEqual(basics, 1)
        self.assertTrue(client._basic_rescue_tried)
        run(client.async_close())

    def test_auth_matrix_tries_both_on_usercheck(self):
        _panel, handler = self._panel(accept_basic=False)
        client = make_client(handler)
        matrix = run(client.async_auth_matrix())
        self.assertEqual(matrix["путь"], "/ISAPI/Security/userCheck")
        self.assertEqual(matrix["digest"], 401)
        self.assertEqual(matrix["basic"], 401)
        self.assertIn('username="admin"', matrix["digest_header"])
        self.assertIn("Digest realm=", matrix["challenge"])
        run(client.async_close())

    def test_auth_matrix_reports_digest_success(self):
        panel = FakeDigestPanel()          # correct password
        client = make_client(panel)
        matrix = run(client.async_auth_matrix())
        self.assertEqual(matrix["digest"], 200)
        run(client.async_close())

    def test_matrix_line_is_in_the_report(self):
        panel = FakeDigestPanel()
        client = make_client(panel)
        results, summary = run(client.async_probe())
        report = load("probe").format_probe_report(results, summary=summary)
        self.assertIn("проверка /ISAPI/Security/userCheck", report)
        self.assertIn("digest → 200", report)
        run(client.async_close())

    def test_empty_opaque_from_the_real_panel_is_echoed_over_the_wire(self):
        """End-to-end: a panel that only accepts an echoed empty opaque."""
        seen: list[str] = []

        def handler(request):
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("digest "):
                return httpx.Response(401, headers={
                    "WWW-Authenticate": 'Digest qop="auth", realm="DS-11A8BC2D", '
                    'nonce="abc", stale="false", opaque="", domain="::"'})
            seen.append(auth)
            if 'opaque=""' not in auth:
                return httpx.Response(401, headers={
                    "WWW-Authenticate": 'Digest qop="auth", realm="DS-11A8BC2D", '
                    'nonce="abc", stale="false", opaque="", domain="::"'})
            return httpx.Response(200, text="<userCheck/>")

        client = make_client(handler)
        run(client.async_verify())
        self.assertTrue(seen and 'opaque=""' in seen[0])
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestAuth(unittest.TestCase):
    def test_digest_is_used_when_the_panel_asks_for_digest(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("authorization", "")
            seen.append(auth.split(" ")[0] if auth else "none")
            if auth.lower().startswith("digest "):
                return httpx.Response(200, text="<DeviceInfo/>")
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Digest qop="auth", realm="DS-K1T341AM", '
                    'nonce="abc", opaque="xyz"'
                },
            )

        client = make_client(handler)
        run(client.async_verify())
        self.assertEqual(client.auth_mode, "digest")
        self.assertIn("Digest", seen)
        run(client.async_close())

    def test_basic_only_panel_falls_back_once_and_is_remembered(self):
        schemes: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("authorization", "")
            schemes.append(auth.split(" ")[0] if auth else "none")
            if auth.lower().startswith("basic "):
                return httpx.Response(200, text="<DeviceInfo/>")
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Basic realm="DS-K1T341AM"'}
            )

        client = make_client(handler)
        run(client.async_verify())
        self.assertEqual(client.auth_mode, "basic")
        # Remembered: the next call goes out as basic straight away.
        schemes.clear()
        run(client.async_get_call_status())
        self.assertNotIn("none", schemes)
        run(client.async_close())

    def test_wrong_credentials_raise_auth_error(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Digest realm="x", nonce="y"'}
            )

        client = make_client(handler)
        with self.assertRaises(isapi.ISAPIAuthError):
            run(client.async_verify())
        run(client.async_close())


class OneShotNoncePanel(FakeDigestPanel):
    """Hikvision V3.x behaviour: a nonce is accepted exactly once.

    A request built on an already-spent nonce comes back 401 with a brand-new
    nonce and no `stale=true` — indistinguishable from a wrong password unless
    the client simply takes the new challenge and answers it.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.spent: set[str] = set()
        self.issued = 0
        self.rejected_reuse = 0

    def _unauthorized(self):
        self.issued += 1
        self.nonce = f"nonce-{self.issued:04d}"
        return super()._unauthorized()

    def __call__(self, request):
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("digest "):
            used = re.search(r'nonce="([^"]*)"', auth)
            nonce = used.group(1) if used else ""
            if nonce in self.spent:
                self.rejected_reuse += 1
                self.seen.append(auth)
                return self._unauthorized()     # new nonce, no stale flag
            current = self.nonce
            self.nonce = nonce                  # verify against what was used
            try:
                response = super().__call__(request)
            finally:
                self.nonce = current
            if response.status_code == 200:
                self.spent.add(nonce)
            return response
        return super().__call__(request)


@unittest.skipIf(httpx is None, "httpx not installed")
class TestOneShotNonce(unittest.TestCase):
    """The real failure mode: digest → 200 once, then 401 on everything."""

    def test_catalogue_walk_succeeds_against_one_shot_nonces(self):
        panel = OneShotNoncePanel()
        client = make_client(panel)
        run(client.async_verify())
        run(client.async_get_call_status())
        run(client.async_open_door())
        self.assertEqual(client.auth_mode, "digest")
        self.assertEqual(panel.rejected_reuse, 0)   # we never reuse a nonce
        self.assertGreaterEqual(len(panel.spent), 3)
        run(client.async_close())

    def test_probe_reports_working_endpoints_not_a_wall_of_401(self):
        panel = OneShotNoncePanel()
        client = make_client(panel)
        results, summary = run(client.async_probe())
        self.assertTrue(all(r.ok for r in results if r.name == "userCheck"))
        self.assertNotIn("0 из", str(summary["успешно"]))
        self.assertIn("digest → 200", str(summary["проверка /ISAPI/Security/userCheck"]))
        run(client.async_close())

    def test_the_reuse_fast_path_recovers_instead_of_failing(self):
        """Even opted in, a rejected nonce must not break the request."""
        panel = OneShotNoncePanel()
        client = make_client(panel, reuse_nonce=True)
        run(client.async_verify())
        run(client.async_get_call_status())     # first reuse gets rejected
        self.assertGreaterEqual(panel.rejected_reuse, 1)
        self.assertEqual(client.auth_mode, "digest")
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestAlertStreamAuth(unittest.TestCase):
    """The event stream must answer its 401 too, not give up on it."""

    def _panel(self):
        panel = OneShotNoncePanel()
        events = (
            "--MIME_boundary\r\nContent-Type: application/xml\r\n\r\n"
            "<EventNotificationAlert><eventType>videoIntercom</eventType>"
            "<eventState>active</eventState><status>ring</status>"
            "</EventNotificationAlert>\r\n"
        )

        def handler(request):
            response = panel(request)
            if response.status_code == 200 and "alertStream" in str(request.url):
                return httpx.Response(
                    200,
                    headers={"Content-Type": "multipart/mixed; boundary=MIME_boundary"},
                    text=events,
                )
            return response

        return panel, handler

    def test_stream_authenticates_and_yields_events(self):
        _panel, handler = self._panel()
        client = make_client(handler)

        async def collect():
            got = []
            async for event in client.async_iter_alerts():
                got.append(event)
            return got

        events_got = run(collect())
        self.assertEqual(len(events_got), 1)
        self.assertEqual(events_got[0]["type"], "videointercom")
        run(client.async_close())

    def test_the_stream_never_sends_a_spent_nonce(self):
        panel, handler = self._panel()
        client = make_client(handler)
        run(client.async_verify())          # consumes a nonce

        async def collect():
            async for _event in client.async_iter_alerts():
                pass

        run(collect())
        self.assertEqual(panel.rejected_reuse, 0)
        run(client.async_close())

    def test_whole_probe_wastes_no_round_trip_on_a_spent_nonce(self):
        panel, handler = self._panel()
        client = make_client(handler)
        run(client.async_probe())
        self.assertEqual(panel.rejected_reuse, 0)
        run(client.async_close())

    def test_probe_of_the_stream_reports_200_not_401(self):
        _panel, handler = self._panel()
        client = make_client(handler)
        results, _summary = run(client.async_probe())
        stream = [r for r in results if r.name == "alertStream"][0]
        self.assertEqual(stream.status, 200)
        self.assertTrue(any(
            scheme == "digest" and status == 200
            for scheme, _h, status in stream.attempts
        ))
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestAuthModeReset(unittest.TestCase):
    def test_reset_auth_clears_a_remembered_basic(self):
        panel = FakeDigestPanel()
        client = make_client(panel)
        client.auth_mode = "basic"
        client._basic_rescue_tried = True
        client.reset_auth()
        self.assertEqual(client.auth_mode, "digest")
        self.assertIsNone(client._challenge)
        self.assertFalse(client._basic_rescue_tried)
        run(client.async_close())

    def test_a_digest_success_snaps_back_from_basic(self):
        panel = FakeDigestPanel()
        client = make_client(panel)
        client.auth_mode = "basic"      # stale memory of a broken session
        run(client.async_verify())
        self.assertEqual(client.auth_mode, "digest")
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestEndpointFallback(unittest.TestCase):
    def test_door_falls_through_to_the_json_shape(self):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
            paths.append(path)
            if "format=json" in path:
                return httpx.Response(200, text='{"statusCode":1}')
            return httpx.Response(404, text="<ResponseStatus/>")

        client = make_client(handler, door_no=1)
        run(client.async_open_door())
        self.assertEqual(paths[0], "/ISAPI/AccessControl/RemoteControl/door/1")
        self.assertTrue(paths[1].endswith("?format=json"))
        # Remembered: no second walk through the candidate list.
        paths.clear()
        run(client.async_open_door())
        self.assertEqual(len(paths), 1)
        run(client.async_close())

    def test_offline_panel_is_retryable_not_unsupported(self):
        tried = []

        def handler(request):
            tried.append(request.url.path)
            raise httpx.ConnectError("No route to host")

        client = make_client(handler)
        with self.assertRaises(isapi.ISAPIError) as ctx:
            run(client.async_get_call_status())
        self.assertNotIsInstance(ctx.exception, isapi.ISAPIUnsupported)
        self.assertEqual(len(tried), 1)          # no knocking three times
        run(client.async_close())

    def test_all_404_is_unsupported(self):
        client = make_client(lambda request: httpx.Response(404))
        with self.assertRaises(isapi.ISAPIUnsupported):
            run(client.async_get_call_status())
        run(client.async_close())

    def test_missing_answer_endpoint_is_tolerated(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(501, text="not implemented")

        client = make_client(handler)
        self.assertFalse(run(client.async_answer()))  # no raise: entity survives
        run(client.async_close())

    def test_rtsp_path_follows_the_channel_the_panel_confirms(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/ISAPI/Streaming/channels/101":
                return httpx.Response(404)
            return httpx.Response(200, text="<StreamingChannel/>")

        client = make_client(handler, rtsp_port=554)
        self.assertEqual(run(client.async_select_channel()), "/Streaming/Channels/102")
        self.assertIn("/Streaming/Channels/102", client.rtsp_url())
        self.assertNotIn("Sekret123", client.rtsp_url(redacted=True))
        run(client.async_close())

    def test_snapshot_uses_the_isapi_picture_endpoint(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/ISAPI/Streaming/channels/101/picture":
                return httpx.Response(
                    200, content=b"\xff\xd8\xff\xe0jpeg",
                    headers={"Content-Type": "image/jpeg"},
                )
            return httpx.Response(404)

        client = make_client(handler)
        self.assertEqual(run(client.async_snapshot()), b"\xff\xd8\xff\xe0jpeg")
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestProbe(unittest.TestCase):
    def _client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/ISAPI/System/deviceInfo":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/xml"},
                    text="<DeviceInfo><model>DS-K1T341AM</model>"
                    "<firmwareVersion>V3.2.30</firmwareVersion></DeviceInfo>",
                )
            if path == "/ISAPI/Streaming/channels/101/picture":
                return httpx.Response(
                    200, content=b"\xff\xd8" * 20,
                    headers={"Content-Type": "image/jpeg"},
                )
            return httpx.Response(404, text="<ResponseStatus><statusString>Invalid "
                                            "Operation</statusString></ResponseStatus>")

        return make_client(handler)

    def test_probe_reports_status_auth_and_model_without_secrets(self):
        client = self._client()
        results, summary = run(client.async_probe())
        report = load("probe").format_probe_report(
            results, host="192.168.70.121", summary=summary
        )
        self.assertIn("DS-K1T341AM", report)
        self.assertIn("auth=digest", report)
        self.assertIn("-> 404", report)
        self.assertNotIn("Sekret123", report)
        self.assertIn("не трогали", summary["реле двери"])  # door stays shut
        self.assertIn("двоичных данных", report)  # jpeg body is not dumped
        run(client.async_close())

    def test_probe_does_not_open_the_door_by_default(self):
        opened: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "RemoteControl/door" in request.url.path and request.method == "PUT":
                opened.append(request.url.path)
            return httpx.Response(200, text="<x/>")

        client = make_client(handler)
        run(client.async_probe())
        self.assertEqual(opened, [])
        run(client.async_probe(test_door=True))
        self.assertEqual(len(opened), 1)
        run(client.async_close())


if __name__ == "__main__":
    unittest.main()
