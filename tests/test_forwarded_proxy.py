"""DB-free integration of real Uvicorn forwarding with FrostVault's routes.

Only persistence and provider network I/O are replaced. Request URL generation,
OIDC authorization construction, app client-IP resolution, network gating, and
Host middleware run unchanged. PostgreSQL-backed companions live in
test_oidc_http and test_breakglass_http and run in PR CI.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from starlette.requests import Request

from app import breakglass, main
from app.config import settings
from tests.oidc_fake import FakeOidcProvider
from tests.proxy_stack import uvicorn_proxy_app


PROXY = "172.30.0.2"
TRUST = f"{PROXY}/32"
PUBLIC_HOST = "frostvault.example.com"
OUTSIDE = "203.0.113.7"
LAN = "192.168.1.20"


class ForwardedProxyIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = FakeOidcProvider()
        self.configuration = replace(
            settings,
            cookie_secure=True,
            allowed_hosts=PUBLIC_HOST,
            trusted_proxies=TRUST,
            break_glass_allowed_cidrs="192.168.1.0/24",
            oidc_enabled=True,
            oidc_issuer=self.provider.issuer,
            oidc_client_id=self.provider.client_id,
            oidc_client_secret="test-only-not-a-real-secret",
        )
        for target in ("app.main.settings", "app.oidc.settings", "app.breakglass.settings"):
            patcher = patch(target, self.configuration)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.connection = Mock()
        database = patch.object(main, "db", lambda: nullcontext(self.connection))
        database.start()
        self.addCleanup(database.stop)
        provider_client = self.provider.client()
        self.addCleanup(provider_client.close)
        for target, value in (
            ("_oidc_client", lambda: provider_client),
            ("_oidc_host_addresses", lambda _: ["93.184.216.34"]),
        ):
            patcher = patch.object(main, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        audit = patch.object(main, "audit_log")
        self.audit = audit.start()
        self.addCleanup(audit.stop)
        self.observed = []

    def _client(self, peer=PROXY, uvicorn_trust=TRUST) -> TestClient:
        async def observe(scope, receive, send):
            if scope["type"] == "http":
                request = Request(scope)
                self.observed.append({
                    "scheme": scope["scheme"],
                    "client": scope["client"][0],
                    "resolved": main._client_ip(request),
                    "forwarded_for": request.headers.get("x-forwarded-for"),
                })
            await main.app(scope, receive, send)

        client = TestClient(
            uvicorn_proxy_app(observe, uvicorn_trust),
            base_url=f"http://{PUBLIC_HOST}",
            client=(peer, 40000),
            follow_redirects=False,
        )
        self.addCleanup(client.close)
        return client

    def _oidc_redirect(self, client, headers, scheme):
        response = client.get("/auth/oidc/login", headers=headers)
        self.assertEqual(response.status_code, 303, response.text)
        url = urlsplit(response.headers["location"])
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}",
                         self.provider.discovery()["authorization_endpoint"])
        query = parse_qs(url.query)
        self.assertEqual(query["redirect_uri"],
                         [f"{scheme}://{PUBLIC_HOST}/auth/oidc/callback"])
        # Real begin_login generated and persisted state/PKCE, not a fake URL.
        sql, parameters = self.connection.execute.call_args.args
        self.assertIn("INSERT INTO oidc_login", sql)
        self.assertEqual(query["state"], [parameters[0]])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        return response

    def _deny_password(self, client, forwarded_for, resolved):
        response = client.post(
            "/api/login",
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": forwarded_for},
            json={"username": "owner", "password": "test-only-password"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["detail"],
                         "The emergency password is not accepted from this network")
        self.assertNotIn("set-cookie", response.headers)
        self.audit.assert_called_with(
            "break_glass_denied", connection=self.connection, actor="emergency", ip=resolved
        )
        # Rejection happened before any credential or session SQL.
        self.connection.execute.assert_not_called()

    def test_trusted_proxy_sets_https_and_rewrites_client_before_app(self):
        headers = {"X-Forwarded-Proto": "https", "X-Forwarded-For": OUTSIDE}
        self._oidc_redirect(self._client(), headers, "https")
        self.assertEqual(self.observed[-1], {
            "scheme": "https", "client": OUTSIDE, "resolved": OUTSIDE,
            "forwarded_for": OUTSIDE,
        })

    def test_untrusted_peer_cannot_force_scheme_or_claim_loopback_or_lan(self):
        for peer in (OUTSIDE, "172.30.0.3"):
            for claimed in ("127.0.0.1", "::1", LAN, f"127.0.0.1, {PROXY}"):
                with self.subTest(peer=peer, claimed=claimed):
                    client = self._client(peer=peer)
                    self._oidc_redirect(client, {
                        "X-Forwarded-Proto": "https", "X-Forwarded-For": claimed,
                    }, "http")
                    self.assertEqual(self.observed[-1]["client"], peer)
                    self.connection.reset_mock()
                    self._deny_password(client, claimed, peer)

    def test_trusted_proxy_rejects_spoofed_leftmost_password_clients(self):
        for claimed in ("127.0.0.1", "::1", LAN):
            with self.subTest(claimed=claimed):
                self._deny_password(self._client(), f"{claimed}, {OUTSIDE}", OUTSIDE)
                self.assertEqual(self.observed[-1]["client"], OUTSIDE)

    def test_multiple_trusted_hops_strip_right_to_left_at_both_layers(self):
        chain = f"127.0.0.1, {OUTSIDE}, 172.30.0.4"
        trust = f"{TRUST},172.30.0.4/32"
        with patch.object(main, "settings", replace(self.configuration, trusted_proxies=trust)):
            client = self._client(uvicorn_trust=trust)
            self._oidc_redirect(client, {
                "X-Forwarded-Proto": "https", "X-Forwarded-For": chain,
            }, "https")
            self.assertEqual(self.observed[-1]["resolved"], OUTSIDE)
            self.connection.reset_mock()
            self._deny_password(client, chain, OUTSIDE)

    def test_ipv6_proxy_and_client_use_the_same_trust_boundary(self):
        proxy, outside = "2001:db8:30::2", "2001:db8:99::7"
        trust = f"{proxy}/128"
        with patch.object(main, "settings", replace(self.configuration, trusted_proxies=trust)):
            client = self._client(peer=proxy, uvicorn_trust=trust)
            self._oidc_redirect(client, {
                "X-Forwarded-Proto": "https", "X-Forwarded-For": outside,
            }, "https")
            self.connection.reset_mock()
            self._deny_password(client, f"::1, {outside}", outside)

    def test_missing_or_unusable_forwarded_client_does_not_admit_proxy(self):
        client = self._client()
        for forwarded_for in (None, "", "unknown"):
            with self.subTest(forwarded_for=forwarded_for):
                headers = {"X-Forwarded-Proto": "https"}
                if forwarded_for is not None:
                    headers["X-Forwarded-For"] = forwarded_for
                response = client.post(
                    "/api/login", headers=headers,
                    json={"username": "owner", "password": "test-only-password"},
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertNotIn("set-cookie", response.headers)
                self.connection.execute.assert_not_called()

    def test_app_allowlist_alone_does_not_enable_https(self):
        client = self._client(uvicorn_trust="")
        self._oidc_redirect(client, {
            "X-Forwarded-Proto": "https", "X-Forwarded-For": OUTSIDE,
        }, "http")
        self.assertEqual(self.observed[-1]["client"], PROXY)
        self.assertEqual(self.observed[-1]["resolved"], OUTSIDE)
        self.connection.reset_mock()
        self._deny_password(client, f"127.0.0.1, {OUTSIDE}", OUTSIDE)

    def test_broader_uvicorn_list_is_unsafe_not_vetoed_by_app(self):
        # Deliberately BAD config: this counterexample prevents documentation
        # from claiming app TRUSTED_PROXIES can reject Uvicorn's prior rewrite.
        client = self._client(peer=OUTSIDE, uvicorn_trust=f"{TRUST},{OUTSIDE}/32")
        self._oidc_redirect(client, {
            "X-Forwarded-Proto": "https", "X-Forwarded-For": "127.0.0.1",
        }, "https")
        self.assertEqual(self.observed[-1]["resolved"], "127.0.0.1")
        self.assertTrue(breakglass.is_break_glass_allowed(self.observed[-1]["resolved"]))

    def test_broader_app_list_can_unsafely_reinterpret_uvicorn_client(self):
        # Deliberately BAD config in the opposite direction: do not compensate
        # for mismatches by widening either list.
        broader = replace(self.configuration, trusted_proxies=f"{TRUST},{OUTSIDE}/32")
        with patch.object(main, "settings", broader):
            self._oidc_redirect(self._client(), {
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": f"127.0.0.1, {OUTSIDE}",
            }, "https")
        self.assertEqual(self.observed[-1]["client"], OUTSIDE)
        self.assertEqual(self.observed[-1]["resolved"], "127.0.0.1")
        self.assertTrue(breakglass.is_break_glass_allowed(self.observed[-1]["resolved"]))

    def test_forwarded_host_does_not_override_actual_host_validation(self):
        response = self._client().get("/auth/oidc/login", headers={
            "Host": "attacker.example", "X-Forwarded-Host": PUBLIC_HOST,
            "X-Forwarded-Proto": "https", "X-Forwarded-For": OUTSIDE,
        })
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"], "Host not allowed")
        self.connection.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
