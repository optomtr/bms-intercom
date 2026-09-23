"""Hand-rolled HTTP digest: RFC vectors, challenge parsing, header shape.

The two response hashes below are the published RFC 7616 §3.9.1 test vectors,
so they pin our implementation to an external reference rather than to itself.

Run: python3 -m unittest discover -s tests -v   (no pytest, no Home Assistant)
"""
from __future__ import annotations

import hashlib
import unittest

from _loader import load

digest = load("digest")

RFC_CHALLENGE = (
    'Digest realm="http-auth@example.org", qop="auth, auth-int", '
    'algorithm=%s, nonce="7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v", '
    'opaque="FQhe/qaU925kfnzjCev0ciny7QMkPqMAFRtzCUYo5tdS"'
)
RFC_CNONCE = "f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ"
RFC_MD5 = "8ca523f5e9506fed4657c9700eebdbec"
RFC_SHA256 = "753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1"


def rfc_header(algorithm: str) -> str:
    return digest.build_authorization(
        "Mufasa", "Circle of Life", "GET", "/dir/index.html",
        digest.parse_challenge(RFC_CHALLENGE % algorithm),
        nc=1, cnonce=RFC_CNONCE,
    )


class TestRFCVectors(unittest.TestCase):
    def test_md5_response_matches_rfc7616(self):
        self.assertIn(f'response="{RFC_MD5}"', rfc_header("MD5"))

    def test_sha256_response_matches_rfc7616(self):
        self.assertIn(f'response="{RFC_SHA256}"', rfc_header("SHA-256"))

    def test_rfc2069_mode_when_no_qop_is_offered(self):
        """Old firmwares send no qop; then nc/cnonce/qop must be absent."""
        challenge = digest.parse_challenge(
            'Digest realm="DS-K1T341AM", nonce="0123456789abcdef"'
        )
        header = digest.build_authorization(
            "admin", "Sekret123!", "GET", "/ISAPI/Security/userCheck", challenge
        )
        ha1 = hashlib.md5(b"admin:DS-K1T341AM:Sekret123!").hexdigest()
        ha2 = hashlib.md5(b"GET:/ISAPI/Security/userCheck").hexdigest()
        expected = hashlib.md5(
            f"{ha1}:0123456789abcdef:{ha2}".encode()
        ).hexdigest()
        self.assertIn(f'response="{expected}"', header)
        for absent in ("qop=", "nc=", "cnonce="):
            self.assertNotIn(absent, header)

    def test_md5_sess(self):
        challenge = digest.parse_challenge(
            'Digest realm="r", nonce="n", qop="auth", algorithm=MD5-sess'
        )
        header = digest.build_authorization(
            "admin", "pw", "GET", "/x", challenge, nc=1, cnonce="abcdef0123456789"
        )
        base = hashlib.md5(b"admin:r:pw").hexdigest()
        ha1 = hashlib.md5(f"{base}:n:abcdef0123456789".encode()).hexdigest()
        ha2 = hashlib.md5(b"GET:/x").hexdigest()
        expected = hashlib.md5(
            f"{ha1}:n:00000001:abcdef0123456789:auth:{ha2}".encode()
        ).hexdigest()
        self.assertIn(f'response="{expected}"', header)


class TestHeaderShape(unittest.TestCase):
    """What the panel's literal parser sees."""

    def _challenge(self, extra: str = "") -> object:
        return digest.parse_challenge(
            'Digest qop="auth", realm="DS-K1T341AM", nonce="abc123"' + extra
        )

    def test_algorithm_is_echoed_only_when_offered(self):
        without = digest.build_authorization(
            "admin", "pw", "GET", "/x", self._challenge()
        )
        self.assertNotIn("algorithm", without)
        with_algo = digest.build_authorization(
            "admin", "pw", "GET", "/x", self._challenge(", algorithm=MD5")
        )
        self.assertIn("algorithm=MD5", with_algo)

    def test_uri_keeps_the_query_string(self):
        header = digest.build_authorization(
            "admin", "pw", "GET", "/ISAPI/VideoIntercom/callStatus?format=json",
            self._challenge(),
        )
        self.assertIn('uri="/ISAPI/VideoIntercom/callStatus?format=json"', header)

    def test_qop_and_nc_are_unquoted_and_nc_is_8_digits(self):
        header = digest.build_authorization(
            "admin", "pw", "GET", "/x", self._challenge(), nc=17
        )
        self.assertIn("qop=auth,", header + ",")
        self.assertIn("nc=00000011", header)
        self.assertNotIn('qop="auth"', header)

    def test_browser_field_order(self):
        header = digest.build_authorization(
            "admin", "pw", "GET", "/x", self._challenge(", algorithm=MD5"), nc=1
        )
        order = [part.split("=")[0].strip() for part in header[7:].split(", ")]
        self.assertEqual(
            order[:6],
            ["username", "realm", "nonce", "uri", "algorithm", "response"],
        )

    def test_password_is_never_in_the_header(self):
        header = digest.build_authorization(
            "admin", "Sekret123!", "GET", "/x", self._challenge()
        )
        self.assertNotIn("Sekret123", header)

    def test_opaque_is_echoed_back(self):
        header = digest.build_authorization(
            "admin", "pw", "GET", "/x", self._challenge(', opaque="OPQ"')
        )
        self.assertIn('opaque="OPQ"', header)


class TestChallengeParsing(unittest.TestCase):
    def test_unquoted_and_quoted_values(self):
        challenge = digest.parse_challenge(
            'Digest realm="DS-K1T341AM", qop=auth, nonce=abc123, algorithm=MD5, stale=TRUE'
        )
        self.assertEqual(challenge.realm, "DS-K1T341AM")
        self.assertEqual(challenge.qop, "auth")
        self.assertEqual(challenge.nonce, "abc123")
        self.assertEqual(challenge.algorithm, "MD5")
        self.assertEqual(challenge.stale, "TRUE")

    def test_basic_challenge_is_not_digest(self):
        self.assertIsNone(digest.parse_challenge('Basic realm="DS-K1T341AM"'))
        self.assertIsNone(digest.parse_challenge(None))

    def test_digest_is_picked_among_several_headers(self):
        chosen = digest.pick_digest_challenge(
            ['Basic realm="x"', 'Digest realm="y", nonce="n"']
        )
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.realm, "y")

    def test_auth_int_only_falls_back_to_rfc2069(self):
        self.assertEqual(digest.choose_qop("auth-int"), "")
        self.assertEqual(digest.choose_qop("auth-int, auth"), "auth")
        self.assertEqual(digest.choose_qop(""), "")

    def test_unknown_algorithm_falls_back_to_md5(self):
        challenge = digest.parse_challenge(
            'Digest realm="r", nonce="n", algorithm=WHIRLPOOL'
        )
        self.assertEqual(challenge.hash_name, "MD5")

    def test_describe_is_diagnostic_and_secret_free(self):
        challenge = digest.parse_challenge(RFC_CHALLENGE % "MD5")
        text = challenge.describe()
        self.assertIn("realm='http-auth@example.org'", text)
        self.assertIn("qop=auth, auth-int", text)
        self.assertIn("algorithm=MD5", text)
        self.assertIn("nonce=44 симв.", text)
        self.assertIn("opaque=44 симв.", text)


# The real challenge of the owner's DS-K1T341AM, firmware V3.2.30: no
# algorithm, and `opaque` present but EMPTY.
REAL_CHALLENGE = (
    'Digest qop="auth", realm="DS-11A8BC2D", '
    'nonce="NjNhNzdjNTVkNmM1ZTM0OGY3M2U2Zjg1ODM3NTNhNmY=", '
    'stale="false", opaque="", domain="::"'
)


class TestEmptyOpaque(unittest.TestCase):
    """An empty `opaque` is present, not absent — it must be echoed back."""

    def setUp(self):
        self.challenge = digest.parse_challenge(REAL_CHALLENGE)

    def test_empty_opaque_counts_as_present(self):
        self.assertTrue(self.challenge.has_opaque)
        self.assertEqual(self.challenge.opaque, "")

    def test_empty_opaque_is_echoed_verbatim(self):
        header = digest.build_authorization(
            "admin", "Sekret123!", "GET", "/ISAPI/Security/userCheck",
            self.challenge, nc=1, cnonce="1234567890abcdef",
        )
        self.assertIn('opaque=""', header)

    def test_absent_opaque_is_not_invented(self):
        challenge = digest.parse_challenge('Digest realm="r", nonce="n", qop="auth"')
        self.assertFalse(challenge.has_opaque)
        header = digest.build_authorization("admin", "pw", "GET", "/x", challenge)
        self.assertNotIn("opaque", header)

    def test_response_matches_an_independent_computation(self):
        header = digest.build_authorization(
            "admin", "Sekret123!", "GET", "/ISAPI/Security/userCheck",
            self.challenge, nc=1, cnonce="1234567890abcdef",
        )
        nonce = "NjNhNzdjNTVkNmM1ZTM0OGY3M2U2Zjg1ODM3NTNhNmY="
        ha1 = hashlib.md5(b"admin:DS-11A8BC2D:Sekret123!").hexdigest()
        ha2 = hashlib.md5(b"GET:/ISAPI/Security/userCheck").hexdigest()
        expected = hashlib.md5(
            f"{ha1}:{nonce}:00000001:1234567890abcdef:auth:{ha2}".encode()
        ).hexdigest()
        self.assertIn(f'response="{expected}"', header)
        # opaque and domain take no part in the hash, only in the header.
        self.assertIn('opaque=""', header)
        self.assertNotIn("domain", header)

    def test_no_algorithm_in_challenge_means_none_in_the_answer(self):
        header = digest.build_authorization("admin", "pw", "GET", "/x", self.challenge)
        self.assertNotIn("algorithm", header)

    def test_describe_says_the_opaque_is_empty(self):
        self.assertIn("opaque=пустой", self.challenge.describe())
        self.assertIn("algorithm=(не указан)", self.challenge.describe())
        self.assertIn("stale=false", self.challenge.describe())


class TestSafeAuthHeader(unittest.TestCase):
    def test_digest_header_is_shown_verbatim(self):
        header = rfc_header("MD5")
        self.assertEqual(digest.safe_auth_header(header), header)

    def test_basic_header_never_shows_its_payload(self):
        header = digest.basic_authorization("admin", "Sekret123!")
        safe = digest.safe_auth_header(header)
        self.assertEqual(safe, "Basic ***")
        self.assertNotIn(header.split(" ", 1)[1], safe)

    def test_empty(self):
        self.assertEqual(digest.safe_auth_header(None), "")


if __name__ == "__main__":
    unittest.main()
