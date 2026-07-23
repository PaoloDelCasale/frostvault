from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app import oidc
from app.config import Settings
from app.database import SQLiteConnection
from tests.oidc_fake import FakeOidcProvider, at_hash_for
from tests.test_database import run_alembic


REDIRECT_URI = "https://app.example/auth/oidc/callback"


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

    def connect(self) -> SQLiteConnection:
        return SQLiteConnection(str(self.database_path))

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
            datetime.fromisoformat(row["expires_at"]),
            datetime.now(timezone.utc),
        )

    def test_begin_login_without_prompt_omits_it(self) -> None:
        with self.connect() as connection:
            url = oidc.begin_login(
                connection,
                redirect_uri=REDIRECT_URI,
                http_client=self.provider.client(),
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
                **kwargs,
            )
            return connection.execute(
                "SELECT state, nonce, code_verifier FROM oidc_login "
                "ORDER BY rowid DESC LIMIT 1"
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
                ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
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
                    (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                    expired["state"],
                ),
            )
        with self.connect() as connection:
            removed = oidc.sweep_expired_logins(connection)
        self.assertEqual(removed, 1)
        remaining = {row["state"] for row in self._rows()}
        self.assertEqual(remaining, {fresh["state"]})


if __name__ == "__main__":
    unittest.main()
