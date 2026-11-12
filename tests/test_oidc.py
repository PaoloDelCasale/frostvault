from __future__ import annotations

from tests.postgres import PostgresConnection

import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app import oidc, oidc_network
from app.config import Settings

from tests.oidc_fake import FakeOidcProvider, at_hash_for
from tests.owner_fixtures import insert_owner
from tests.test_database import run_alembic


REDIRECT_URI = "https://app.example/auth/oidc/callback"
PUBLIC_OIDC_HOST = lambda _: ["93.184.216.34"]


class PinnedNetworkBackendTests(unittest.TestCase):
    def test_connects_to_the_validated_address_not_the_hostname(self) -> None:
        connected_hosts: list[str] = []

        class RecordingBackend:
            def connect_tcp(self, host: str, *args, **kwargs):
                connected_hosts.append(host)
                return object()

            def connect_unix_socket(self, *args, **kwargs):
                raise AssertionError("unexpected Unix socket")

            def sleep(self, seconds: float) -> None:
                pass

        backend = oidc_network._PinnedNetworkBackend(
            host_addresses=lambda _: ["93.184.216.34"],
            backend=RecordingBackend(),
        )

        backend.connect_tcp("identity.example", 443)

        self.assertEqual(connected_hosts, ["93.184.216.34"])

    def test_rejects_an_unsafe_address_before_connecting(self) -> None:
        class UnexpectedBackend:
            def connect_tcp(self, *args, **kwargs):
                raise AssertionError("unsafe address was connected")

        backend = oidc_network._PinnedNetworkBackend(
            host_addresses=lambda _: ["127.0.0.1"],
            backend=UnexpectedBackend(),
        )

        with self.assertRaisesRegex(
            oidc_network.OidcValidationError,
            "ssrf_blocked",
        ):
            backend.connect_tcp("identity.example", 443)


class OidcTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "oidc.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.provider = FakeOidcProvider()
        self.settings = replace(
            Settings(),
            oidc_enabled=True,
            oidc_issuer=self.provider.issuer,
            oidc_client_id=self.provider.client_id,
            oidc_client_secret="top-secret",
        )
        patcher = patch.object(oidc, "settings", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self) -> PostgresConnection:
        return PostgresConnection(str(self.database_path))

    def _rows(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM oidc_login").fetchall()


class BeginLoginTests(OidcTestBase):
    def test_begin_login_persists_state_and_returns_authorization_url(self) -> None:
        with self.connect() as connection:
            url = oidc.begin_login(
                connection,
                redirect_uri=REDIRECT_URI,
                return_to="/dashboard",
                http_client=self.provider.client(),
                host_addresses=PUBLIC_OIDC_HOST,
            )

        parsed = urlparse(url)
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            self.provider.discovery()["authorization_endpoint"],
        )
        query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["client_id"], self.provider.client_id)
        self.assertEqual(query["redirect_uri"], REDIRECT_URI)
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertTrue(query["code_challenge"])
        self.assertTrue(query["nonce"])

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["state"], query["state"])
        self.assertEqual(row["nonce"], query["nonce"])
        self.assertTrue(row["code_verifier"])
        self.assertEqual(row["return_to"], "/dashboard")
        self.assertGreater(
            row["expires_at"],
            datetime.now(timezone.utc),
        )

    def test_begin_login_without_prompt_omits_it(self) -> None:
        with self.connect() as connection:
            url = oidc.begin_login(
                connection,
                redirect_uri=REDIRECT_URI,
                http_client=self.provider.client(),
                host_addresses=PUBLIC_OIDC_HOST,
            )
        query = parse_qs(urlparse(url).query)
        self.assertNotIn("prompt", query)

    def test_begin_login_forwards_prompt_for_step_up(self) -> None:
        with self.connect() as connection:
            url = oidc.begin_login(
                connection,
                redirect_uri=REDIRECT_URI,
                prompt="login",
                http_client=self.provider.client(),
                host_addresses=PUBLIC_OIDC_HOST,
            )
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["prompt"], ["login"])


class CompleteLoginTests(OidcTestBase):
    def _begin(self, **kwargs: Any) -> str:
        with self.connect() as connection:
            oidc.begin_login(
                connection,
                redirect_uri=REDIRECT_URI,
                http_client=self.provider.client(),
                host_addresses=PUBLIC_OIDC_HOST,
                **kwargs,
            )
            return connection.execute(
                "SELECT state, nonce, code_verifier FROM oidc_login "
                "ORDER BY created_at DESC, state DESC LIMIT 1"
            ).fetchone()

    def _id_token(self, nonce: str, **kwargs: Any) -> str:
        return self.provider.make_id_token(nonce=nonce, **kwargs)

    def _complete(self, state: str, id_token: str, code: str = "auth-code") -> Any:
        with self.connect() as connection:
            return oidc.complete_login(
                connection,
                state=state,
                code=code,
                redirect_uri=REDIRECT_URI,
                http_client=self.provider.client(id_token=id_token),
                host_addresses=PUBLIC_OIDC_HOST,
            )

    def test_happy_path_returns_validated_claims_and_consumes_state(self) -> None:
        pending = self._begin(return_to="/home")
        id_token = self._id_token(pending["nonce"], subject="user-42")

        claims = self._complete(pending["state"], id_token)

        self.assertEqual(claims.issuer, self.provider.issuer)
        self.assertEqual(claims.subject, "user-42")
        self.assertEqual(claims.return_to, "/home")
        self.assertEqual(self._rows(), [])

    def test_state_is_single_use(self) -> None:
        pending = self._begin()
        id_token = self._id_token(pending["nonce"])
        self._complete(pending["state"], id_token)

        with self.assertRaises(oidc.OidcError) as error:
            self._complete(pending["state"], self._id_token(pending["nonce"]))
        self.assertEqual(error.exception.reason, "unknown_state")

    def test_unknown_state_is_rejected(self) -> None:
        self._begin()
        with self.assertRaises(oidc.OidcError) as error:
            self._complete("not-a-real-state", self._id_token("whatever"))
        self.assertEqual(error.exception.reason, "unknown_state")

    def test_expired_login_state_is_rejected(self) -> None:
        pending = self._begin()
        with self.connect() as connection:
            connection.execute(
                "UPDATE oidc_login SET expires_at=%s",
                (datetime.now(timezone.utc) - timedelta(minutes=1),),
            )
        with self.assertRaises(oidc.OidcError) as error:
            self._complete(pending["state"], self._id_token(pending["nonce"]))
        self.assertEqual(error.exception.reason, "expired")

    def test_nonce_mismatch_is_rejected(self) -> None:
        pending = self._begin()
        id_token = self._id_token("a-different-nonce")
        with self.assertRaises(oidc.OidcError) as error:
            self._complete(pending["state"], id_token)
        self.assertEqual(error.exception.reason, "nonce_mismatch")

    def test_wrong_audience_is_rejected(self) -> None:
        pending = self._begin()
        id_token = self._id_token(pending["nonce"], audience="another-client")
        with self.assertRaises(oidc.OidcError) as error:
            self._complete(pending["state"], id_token)
        self.assertEqual(error.exception.reason, "bad_audience")

    def test_wrong_issuer_is_rejected(self) -> None:
        pending = self._begin()
        id_token = self._id_token(pending["nonce"], issuer="https://evil.example")
        with self.assertRaises(oidc.OidcError) as error:
            self._complete(pending["state"], id_token)
        self.assertEqual(error.exception.reason, "bad_issuer")

    def test_expired_id_token_is_rejected(self) -> None:
        pending = self._begin()
        id_token = self._id_token(pending["nonce"], expires_in=-30)
        with self.assertRaises(oidc.OidcError) as error:
            self._complete(pending["state"], id_token)
        self.assertEqual(error.exception.reason, "token_expired")

    def test_at_hash_mismatch_is_rejected(self) -> None:
        pending = self._begin()
        id_token = self._id_token(pending["nonce"], at_hash="tampered")
        with self.assertRaises(oidc.OidcError) as error:
            self._complete(pending["state"], id_token)
        self.assertEqual(error.exception.reason, "at_hash_mismatch")

    def test_token_exchange_sends_the_pkce_verifier(self) -> None:
        pending = self._begin()
        self._complete(pending["state"], self._id_token(pending["nonce"]))
        exchange = self.provider.token_requests[-1]
        self.assertEqual(exchange["grant_type"], "authorization_code")
        self.assertEqual(exchange["code_verifier"], pending["code_verifier"])

    def test_sweep_removes_only_expired_login_state(self) -> None:
        expired = self._begin()
        fresh = self._begin()
        with self.connect() as connection:
            connection.execute(
                "UPDATE oidc_login SET expires_at=%s WHERE state=%s",
                (
                    datetime.now(timezone.utc) - timedelta(minutes=1),
                    expired["state"],
                ),
            )
        with self.connect() as connection:
            removed = oidc.sweep_expired_logins(connection)
        self.assertEqual(removed, 1)
        remaining = {row["state"] for row in self._rows()}
        self.assertEqual(remaining, {fresh["state"]})


def _claims(subject: str, issuer: str = "https://idp.example") -> oidc.OidcClaims:
    return oidc.OidcClaims(issuer=issuer, subject=subject, return_to=None, raw={})


class OwnerIdentityBindingTests(OidcTestBase):
    """The first OIDC login binds the Owner's Identity (ADR-0003, ADR-0010)."""

    def setUp(self) -> None:
        super().setUp()
        with self.connect() as connection:
            insert_owner(connection)

    def _owner(self) -> dict[str, Any]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT oidc_issuer, oidc_subject, oidc_bound_at FROM owner WHERE id=1"
            ).fetchone()

    def test_first_login_binds_the_identity(self) -> None:
        with self.connect() as connection:
            oidc.bind_owner_identity(connection, _claims("alice"))
        owner = self._owner()
        self.assertEqual(owner["oidc_issuer"], "https://idp.example")
        self.assertEqual(owner["oidc_subject"], "alice")
        self.assertIsNotNone(owner["oidc_bound_at"])

    def test_the_bound_identity_signs_in_again(self) -> None:
        with self.connect() as connection:
            oidc.bind_owner_identity(connection, _claims("alice"))
        bound_at = self._owner()["oidc_bound_at"]
        with self.connect() as connection:
            oidc.bind_owner_identity(connection, _claims("alice"))
        self.assertEqual(self._owner()["oidc_bound_at"], bound_at)

    def test_another_subject_or_issuer_is_rejected(self) -> None:
        with self.connect() as connection:
            oidc.bind_owner_identity(connection, _claims("alice"))
        for claims in (_claims("mallory"), _claims("alice", issuer="https://other.example")):
            with self.connect() as connection, self.assertRaises(oidc.IdentityRejected):
                oidc.bind_owner_identity(connection, claims)
        self.assertEqual(self._owner()["oidc_subject"], "alice")

    def test_no_identity_binds_without_an_owner(self) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM owner")
        with self.connect() as connection, self.assertRaises(oidc.IdentityRejected):
            oidc.bind_owner_identity(connection, _claims("alice"))

    def test_concurrent_first_logins_bind_exactly_one_subject(self) -> None:
        """Both logins race past the unbound read; the row lock serializes them."""
        subjects = ["alice", "mallory"]
        both_started = threading.Barrier(len(subjects), timeout=10)
        outcomes: dict[str, str] = {}

        def login(subject: str) -> None:
            with self.connect() as connection:
                both_started.wait()
                try:
                    oidc.bind_owner_identity(connection, _claims(subject))
                    # Hold the lock so the rival really waits on the row.
                    connection.execute("SELECT pg_sleep(0.2)")
                    outcomes[subject] = "bound"
                except oidc.IdentityRejected:
                    outcomes[subject] = "rejected"

        threads = [threading.Thread(target=login, args=(subject,)) for subject in subjects]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(sorted(outcomes.values()), ["bound", "rejected"])
        winner = next(subject for subject, outcome in outcomes.items() if outcome == "bound")
        self.assertEqual(self._owner()["oidc_subject"], winner)

    def test_the_database_keeps_the_binding_all_or_nothing(self) -> None:
        import psycopg

        with self.connect() as connection, self.assertRaises(psycopg.errors.CheckViolation):
            connection.execute("UPDATE owner SET oidc_subject='alice' WHERE id=1")


if __name__ == "__main__":
    unittest.main()
