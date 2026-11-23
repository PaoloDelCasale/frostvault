"""Status and body of the errors the HTTP API answers with, route by route.

Each case is a real request against a seeded catalog: Vault 1 ``Docs`` is
active (its folder is not on disk), Vault 2 ``Old`` a Tombstone, Vault 3
``New`` still provisioning. The cases whose reason comes from a table of the
service (Vault creation, Source Volume folders, Reconnect, the disk of an
Excluded Folder, Job admission, Upload reconciliation) raise the service's own
error at its seam.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import psycopg

from app.i18n import translate
from tests.fixtures import FrostVaultTestCase, seed_job, seed_vault

X = None  # no request body
NO_GROUP = "00000000-0000-0000-0000-000000000000"


# The ``code`` of each status: the kind of refusal, which the status follows.
CODES = {400: "bad_request", 401: "unauthenticated", 403: "forbidden", 404: "not_found",
         409: "conflict", 422: "invalid", 429: "rate_limited", 500: "failed",
         502: "upstream_failed", 503: "unavailable"}


def problem(key: str, locale: str = "en", **params: Any) -> dict[str, Any]:
    """The catalog message of an error, in the locale of the request."""
    return {"detail": translate(key, locale=locale, **params),
            "message_key": key, "message_params": params}


# The one refusal whose code is not the plain one of its status.
REAUTH_REQUIRED = {**problem("api.reauth_required"), "code": "reauth_required"}


class ApiErrorCase(FrostVaultTestCase):
    owner = {"password": "correct horse battery"}

    def setUp(self) -> None:
        super().setUp()
        with self.database.connect() as connection:
            seed_vault(connection, id=1)
            seed_vault(connection, id=2, name="Old", source_root="/sources/managed/old",
                       lifecycle="tombstone", root_released_at=datetime.now(timezone.utc))
            seed_vault(connection, id=3, name="New", source_root="/sources/managed/new",
                       lifecycle="provisioning")
            self.job_id = seed_job(connection, vault_id=1, path="a.txt")
        self.client = self.http_client()
        self.sign_in(send_csrf_header=True,
                     reauthenticated_until=datetime.now(timezone.utc).isoformat())

    def stale_reauthentication(self) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE sessions SET reauth_at='2000-01-01T00:00:00+00:00'")

    def check(self, method: str, url: str, body: Any, status: int, payload: Any,
              **options: Any) -> Any:
        """One request; its status and JSON body are the contract.

        The body is the single error shape: ``payload`` plus the ``code`` of the status.
        """
        response = self.client.request(method, url, json=body, **options)
        self.same(response, status, payload, f"{method} {url} {body or ''}".strip())
        return response

    def same(self, response: Any, status: int, payload: Any, label: str = "") -> None:
        with self.subTest(label):
            expected = {"code": CODES[status], **payload}
            self.assertEqual((response.status_code, response.json()), (status, expected))


class SessionErrors(ApiErrorCase):
    def test_without_a_session(self) -> None:
        anonymous = self.http_client()
        self.same(anonymous.get("/api/me"), 401, problem("api.authentication_required"))
        anonymous.cookies.set(self.settings.session_cookie_name, "not-a-session")
        self.same(anonymous.get("/api/me"), 401, problem("api.invalid_session"))

    def test_reauthentication_required(self) -> None:
        self.stale_reauthentication()
        for method, url, body in (
            ("POST", "/api/vaults/3/creation/cancel", X),
            ("GET", "/api/recovery-kit/download", X),
            ("POST", "/api/vaults/2/vault-deletion", {"confirmation": "Old", "reason": "r"}),
            ("POST", "/api/vaults/2/vault-deletion/retry", X),
            ("POST", "/api/vaults/1/stop-protection",
             {"cloud_disposition": "delete", "confirmation": "Docs", "reason": "r"}),
            ("PUT", "/api/vaults/1/duplicate-cleanup", {"enabled": True}),
            ("PUT", "/api/vaults/1/mirror-cleanup", {"enabled": True}),
            ("POST", "/api/vaults/1/cloud-purge", {"path": "a.txt", "confirmation": "Docs", "reason": "r"}),
        ):
            self.check(method, url, body, 403, REAUTH_REQUIRED)

    def test_vault_of_the_request(self) -> None:
        missing = problem("api.protected_vault_not_found")
        self.check("GET", "/api/vaults/99/jobs", X, 404, missing)
        self.check("GET", "/api/vaults/2/jobs", X, 404, missing)
        self.check("POST", "/api/vaults/select", {"vault_id": 99}, 404, missing)
        self.check("POST", "/api/vaults/select", {"vault_id": 2}, 404, missing)
        with self.database.connect() as connection:
            connection.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=1")
        self.check("GET", "/api/vaults/1/jobs", X, 404, missing)
        me = self.client.get("/api/me")
        self.assertEqual((me.status_code, me.json()["vault"]), (200, None))

    def test_sign_in_and_locale(self) -> None:
        self.check("PUT", "/api/locale", {"locale": "fr"}, 400, problem("api.unsupported_locale"))
        self.check("GET", "/auth/oidc/login", X, 404, problem("api.sign_in.oidc_disabled"))
        self.check("GET", "/auth/oidc/reauth", X, 404, problem("api.sign_in.oidc_disabled"))
        self.check("GET", "/auth/oidc/callback?state=s&code=c", X,
                   400, problem("api.sign_in.oidc_failed", reason="disabled"))
        self.check("POST", "/api/reauth", {"password": "wrong"},
                   401, problem("api.sign_in.incorrect_password"))
        response = self.http_client().post("/api/login", json={"password": "wrong"})
        self.same(response, 401, problem("api.sign_in.incorrect_emergency_password"))
        outside = self.http_client(client=("203.0.113.9", 50000))
        response = outside.post("/api/login", json={"password": "wrong"})
        self.same(response, 403, problem("api.sign_in.emergency_network"))
        self.client = outside
        self.sign_in(send_csrf_header=True)
        self.check("POST", "/api/reauth", {"password": "wrong"},
                   403, problem("api.sign_in.reauth_not_allowed"))

    def test_too_many_attempts(self) -> None:
        anonymous = self.http_client()
        for send in (lambda: anonymous.post("/api/login", json={"password": "wrong"}),
                     lambda: self.client.post("/api/reauth", json={"password": "wrong"})):
            response = send()
            for _ in range(12):
                if response.status_code == 429:
                    break
                response = send()
            self.same(response, 429, problem("api.sign_in.too_many_attempts"))
            self.assertGreater(int(response.headers["Retry-After"]), 0)


class VaultManagementErrors(ApiErrorCase):
    def test_vault_not_found(self) -> None:
        stop = {"cloud_disposition": "keep", "confirmation": "Docs", "reason": "r"}
        deletion = {"confirmation": "Old", "reason": "r"}
        reconnect = {"volume_alias": "disk", "relative_path": "docs", "reason": "moved"}
        vault_not_found = problem("api.vault_not_found")
        for method, url, body, status, payload in (
            ("POST", "/api/vaults/99/pause", X, 404, vault_not_found),
            ("POST", "/api/vaults/99/resume", X, 404, vault_not_found),
            ("GET", "/api/vaults/99/bucket", X, 404, vault_not_found),
            ("GET", "/api/vaults/99/retention-lock", X, 404, vault_not_found),
            ("POST", "/api/vaults/99/bucket/retry", X, 404, vault_not_found),
            ("POST", "/api/vaults/99/creation/cancel", X,
             404, problem("api.vault_creation_cancel.not_found")),
            ("GET", "/api/vaults/99/stop-protection", X, 404, vault_not_found),
            ("POST", "/api/vaults/99/stop-protection", stop, 404, vault_not_found),
            ("POST", "/api/vaults/99/stop-protection/cancel-deletion", X,
             404, vault_not_found),
            ("GET", "/api/vaults/99/vault-deletion", X,
             404, problem("api.vault_deletion.not_found")),
            ("GET", "/api/vaults/99/vault-deletion/preview", X,
             404, problem("api.vault_deletion.not_found")),
            ("POST", "/api/vaults/99/vault-deletion", deletion,
             404, problem("api.vault_deletion.not_found")),
            ("POST", "/api/vaults/99/vault-deletion/cancel", X, 404, vault_not_found),
            ("POST", "/api/vaults/99/vault-deletion/retry", X, 404, vault_not_found),
            ("GET", "/api/vaults/99/duplicate-cleanup", X,
             404, problem("api.duplicate_cleanup.not_found")),
            ("PUT", "/api/vaults/99/duplicate-cleanup", {"enabled": False},
             404, vault_not_found),
            ("GET", "/api/vaults/99/mirror-cleanup", X,
             404, problem("api.mirror_cleanup.not_found")),
            ("PUT", "/api/vaults/99/mirror-cleanup", {"enabled": False},
             404, vault_not_found),
            ("GET", "/api/vaults/99/minimum-storage", X,
             404, problem("api.minimum_storage.not_found")),
            ("GET", "/api/vaults/99/cloud-usage", X, 404, problem("api.cloud_usage.not_found")),
            ("GET", "/api/vaults/99/billed-cost", X, 404, problem("api.billed_cost.not_found")),
            ("POST", "/api/vaults/99/reconnect", reconnect,
             404, problem("api.reconnect.not_found")),
            ("POST", "/api/vaults/99/upload/retry-failed", X, 404, vault_not_found),
            ("POST", "/api/general/cost-estimates/storage", {"vault_id": 99},
             404, vault_not_found),
        ):
            self.check(method, url, body, status, payload)

    def test_state_of_the_vault(self) -> None:
        stop = {"cloud_disposition": "keep", "confirmation": "Docs", "reason": "r"}
        folders = problem("api.mirror_cleanup.folders")
        for method, url, body, status, payload in (
            ("POST", "/api/vaults/2/pause", X,
             409, problem("api.vault_pause.not_operational")),
            ("POST", "/api/vaults/1/creation/cancel", X,
             409, problem("api.vault_creation_cancel.not_provisioning")),
            ("POST", "/api/vaults/1/stop-protection", {**stop, "confirmation": "wrong"},
             422, problem("api.confirmation_required")),
            ("POST", "/api/vaults/3/stop-protection", {**stop, "confirmation": "New"},
             409, problem("api.stop_protection.bucket_creating")),
            ("POST", "/api/vaults/1/stop-protection/cancel-deletion", X,
             409, problem("api.stop_protection.no_pending_deletion")),
            ("GET", "/api/vaults/1/vault-deletion/preview", X,
             409, problem("api.vault_deletion.not_tombstone")),
            ("GET", "/api/vaults/2/vault-deletion/preview", X,
             502, problem("api.vault_deletion.listing_failed")),
            ("POST", "/api/vaults/1/vault-deletion", {"confirmation": "Docs", "reason": "r"},
             409, problem("api.vault_deletion.not_tombstone")),
            ("POST", "/api/vaults/2/vault-deletion/cancel", X,
             409, problem("api.vault_deletion.not_requested")),
            ("POST", "/api/vaults/2/vault-deletion/retry", X,
             409, problem("api.vault_deletion.not_failed")),
            ("PUT", "/api/vaults/2/duplicate-cleanup", {"enabled": False},
             409, problem("api.duplicate_cleanup.stopped")),
            ("PUT", "/api/vaults/2/mirror-cleanup", {"enabled": False},
             409, problem("api.mirror_cleanup.stopped")),
            ("PUT", "/api/vaults/1/mirror-cleanup", {"folders": ["../outside"]}, 422, folders),
            ("PUT", "/api/vaults/1/mirror-cleanup", {"folders": ["a*"]}, 422, folders),
        ):
            self.check(method, url, body, status, payload)

    def test_vault_deletion_request(self) -> None:
        from app.services import vault_deletion
        preview = {"vault_id": 2, "bucket": "bucket", "prefix": "p/", "objects": 0,
                   "versions": 0, "bytes": 0}
        url = "/api/vaults/2/vault-deletion"
        with patch.object(vault_deletion, "preview", return_value=preview), \
                patch("app.routes.vaults.s3_client"):
            self.check("POST", url, {"confirmation": "wrong", "reason": "r"},
                       422, problem("api.vault_deletion.confirmation"))
            self.check("POST", url, {"confirmation": "Old", "reason": " "},
                       409, problem("api.vault_deletion.reason"))
            self.check("POST", url, {"confirmation": "Old", "reason": "r", "delete_bucket": True},
                       409, problem("api.vault_deletion.refused"))
            with self.database.connect() as connection:
                seed_job(connection, vault_id=2, status="queued")
            self.check("POST", url, {"confirmation": "Old", "reason": "r"},
                       409, problem("api.vault_deletion.active_jobs"))

    def test_vault_creation(self) -> None:
        from app.services import vaults
        for error, status, payload in (
            (vaults.InvalidVaultName("Vault name is required"),
             422, problem("api.vault_name_invalid")),
            (vaults.VaultProvisioningUnavailable("no region"),
             503, problem("api.vault_provisioning_unavailable")),
            (vaults.VaultAdoptionError("occupied"), 422, problem("api.vault_adoption.occupied")),
            (vaults.VaultAdoptionError("made_up"), 422, problem("api.vault_adoption.failed")),
            (vaults.VaultCreationError("Could not create the Vault"),
             409, problem("ui.vault_create.failed")),
        ):
            with patch("app.routes.vaults.create_vault", side_effect=error):
                self.check("POST", "/api/vaults", {"name": "Photos"}, status, payload)

    def test_source_volume_folders(self) -> None:
        from app.services import source_directories
        self.check("GET", "/api/source-volumes/nope/browse", X,
                   404, problem("api.source_directory.volume_not_found"))
        for reason, status in (
            ("volume_not_found", 404),
            ("volume_unavailable", 409),
            ("invalid_volume", 422),
            ("invalid_path", 422),
            ("path_missing", 404),
            ("occupied", 409),
            ("made_up", 400),
        ):
            key = "failed" if reason == "made_up" else reason
            error = source_directories.SourceDirectoryError(reason, "internal text")
            with patch.object(source_directories, "browse_source_directories", side_effect=error):
                self.check("GET", f"/api/source-volumes/disk/browse?path={reason}", X,
                           status, problem(f"api.source_directory.{key}"))

    def test_reconnect(self) -> None:
        from app.services import vault_relocation
        from app.services.runtime_status import scan_lock_for_vault
        body = {"volume_alias": "disk", "relative_path": "docs", "reason": "moved"}
        for reason, status in (
            ("not_found", 404),
            ("reason_required", 422),
            ("invalid_path", 422),
            ("not_directory", 422),
            ("volume_unavailable", 409),
            ("managed_not_own", 409),
            ("managed_missing", 409),
            ("different_volume", 409),
            ("symlink", 409),
            ("inaccessible", 409),
            ("overlap", 409),
            ("active_jobs", 409),
            ("identity_ambiguous", 409),
            ("identity_mismatch", 409),
            ("protection_stopped", 409),
            ("made_up", 400),
        ):
            key = "failed" if reason == "made_up" else reason
            error = vault_relocation.VaultRelocationError(reason, "internal text")
            with patch.object(vault_relocation, "reconnect_vault_root", side_effect=error):
                self.check("POST", f"/api/vaults/1/reconnect?case={reason}", body,
                           status, problem(f"api.reconnect.{key}"))
        params = {"path": "/sources/disk/docs", "identity": "99:100", "groups": "100",
                  "owner": "0:0", "mode": "0755"}
        for reason in ("volume_not_writable", "destination_not_writable"):
            error = vault_relocation.VaultRelocationError(reason, "internal text", params=params)
            with patch.object(vault_relocation, "reconnect_vault_root", side_effect=error):
                self.check("POST", f"/api/vaults/1/reconnect?case={reason}", body,
                           409, problem(f"api.reconnect_{reason}", **params))
        lock = scan_lock_for_vault(1)
        self.assertTrue(lock.acquire(blocking=False, operation="scan"))
        try:
            self.check("POST", "/api/vaults/1/reconnect", body,
                       409, problem("api.reconnect.active_jobs"))
            self.check("POST", "/api/vaults/1/stop-protection",
                       {"cloud_disposition": "keep", "confirmation": "Docs", "reason": "r"},
                       409, problem("api.stop_protection.scan_running"))
        finally:
            lock.release()


class BrowseErrors(ApiErrorCase):
    def test_files_jobs_and_notifications(self) -> None:
        self.check("GET", "/api/vaults/1/files/detail?path=missing.txt", X,
                   404, problem("api.file_not_found"))
        self.check("GET", "/api/vaults/1/files/detail?path=../outside", X,
                   422, problem("api.invalid_path"))
        self.check("GET", "/api/vaults/1/files/jobs?path=missing.txt", X,
                   404, problem("api.file_not_found"))
        self.check("GET", "/api/vaults/1/jobs/99999", X, 404, problem("api.job_not_found"))
        self.check("POST", "/api/notifications/read", {"notification_id": 99},
                   404, problem("api.notification_not_found"))
        self.check("GET", "/api/vaults/1/catalog/events?subscribe=false", X,
                   422, problem("api.last_event_id_invalid"),
                   headers={"Last-Event-ID": "not-a-number"})

    def test_recovery_kit_and_metadata_backups(self) -> None:
        from app.services import metadata_backups
        self.check("GET", "/api/recovery-kit/download", X,
                   409, problem("api.recovery_kit.master_key"))
        self.check("POST", "/api/recovery-kit/custody", {"fingerprint": "0" * 24},
                   409, problem("api.recovery_kit.fingerprint_changed"))
        self.check("GET", "/api/general/metadata-backups/download/99", X,
                   404, problem("api.metadata_backup.run_not_found"))
        error = metadata_backups.BackupError("api.metadata_backup.pg_dump_failed")
        with patch.object(metadata_backups, "run_metadata_backup", side_effect=error):
            self.check("POST", "/api/general/metadata-backups/run", {"reason": "before a change"},
                       500, problem("api.metadata_backup.pg_dump_failed"))


class StoragePlanErrors(ApiErrorCase):
    def test_storage_plan(self) -> None:
        url = "/api/vaults/1/lifecycle"
        self.check("PUT", url, {"guided_profile": "made_up"},
                   400, problem("api.storage_plan.unknown", name="made_up"))
        self.check("PUT", url, {"profile": {"transitions": [
            {"days": 0, "storage_class": "MADE_UP"}]}},
            400, problem("api.storage_plan.invalid",
                         errors="Unsupported storage class: MADE_UP"))
        self.check("PUT", url, {"profile": {"transitions": [
            {"days": 10, "storage_class": "DEEP_ARCHIVE"},
            {"days": 20, "storage_class": "GLACIER_IR"}]}},
            400, problem("api.storage_plan.invalid",
                         errors="Unsupported transition from DEEP_ARCHIVE to GLACIER_IR"))

    def test_vault_move(self) -> None:
        move = {"target_class": "DEEP_ARCHIVE"}
        self.check("POST", "/api/vaults/1/move", move,
                   409, problem("api.vault_move.acknowledge_costs"))
        self.check("POST", "/api/vaults/1/move", {**move, "acknowledge_costs": True},
                   409, problem("api.vault_move.choose_colder"))
        with self.database.connect() as connection:
            connection.execute("UPDATE vaults SET lifecycle='reconnect_required' WHERE id=1")
        self.check("GET", "/api/vaults/1/move", X,
                   409, problem("api.vault_move.not_ready"))


class VaultAddressCase(ApiErrorCase):
    """Operations that take their Vault from the path (ADR-0056): the shared checks."""

    def session_vault(self) -> Any:
        with self.database.connect() as connection:
            return connection.execute("SELECT vault_id FROM sessions").fetchone()["vault_id"]

    def only_an_operational_vault_is_accepted(self, operations: Any) -> None:
        absent = problem("api.protected_vault_not_found")
        for method, suffix, body in operations:
            # A Tombstone, a Vault still being created, an unknown id.
            for vault_id in (2, 3, 99):
                self.check(method, f"/api/vaults/{vault_id}/{suffix}", body, 404, absent)
            with self.subTest(method=method, suffix=suffix, vault_id=0):
                response = self.client.request(method, f"/api/vaults/0/{suffix}", json=body)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertIsNone(self.session_vault())

    def each_address_is_matched_by_its_own_route(self, operations: Any) -> None:
        from starlette.routing import Match
        routes = self.client.app.router.routes
        for method, suffix, _body in operations:
            path = f"/api/vaults/1/{suffix.partition('?')[0]}"
            scope = {"type": "http", "method": method, "path": path}
            first = next(route for route in routes if route.matches(scope)[0] is Match.FULL)
            self.assertEqual(first.path, f"/api/vaults/{{vault_id}}/{suffix.partition('?')[0]}")


class VaultSettingsAddress(VaultAddressCase):
    """The 13 Vault settings operations take their Vault from the path (ADR-0056)."""

    OPERATIONS = (
        ("GET", "operation-policy", X),
        ("PUT", "operation-policy", {}),
        ("POST", "operation-policy/globs",
         {"field": "exclude_globs", "action": "add", "glob": "*.tmp"}),
        ("POST", "operation-policy/preview-globs", {}),
        ("GET", "excluded-folders", X),
        ("GET", "exclusions/item?path=a", X),
        ("GET", "exclusions/inclusion-preview?path=a", X),
        ("POST", "exclusions/exclude", {"path": "a", "is_directory": True}),
        ("POST", "exclusions/include", {"path": "a", "is_directory": True}),
        ("GET", "move", X),
        ("POST", "move", {"target_class": "DEEP_ARCHIVE"}),
        ("GET", "lifecycle", X),
        ("PUT", "lifecycle", {"guided_profile": "standard_only"}),
    )

    @staticmethod
    def files_on_disk(connection: Any) -> None:
        """The seeded Vault Files have a Local Copy, so the file list shows them."""
        connection.execute(
            "UPDATE files SET local_presence='present', local_file_type='regular', local_size=1,"
            " local_mtime_ns=1, local_seen_at=now(), local_observed_at=now()")

    def test_only_an_operational_vault_in_the_path_is_accepted(self) -> None:
        self.only_an_operational_vault_is_accepted(self.OPERATIONS)

    def test_the_session_vault_is_neither_an_address_nor_written(self) -> None:
        with self.database.connect() as connection:
            seed_vault(connection, id=4, name="Second", source_root="/sources/managed/second")
        for pinned in (None, 4):
            with self.database.connect() as connection:
                connection.execute("UPDATE sessions SET vault_id=%s", (pinned,))
            saved = self.client.put("/api/vaults/1/operation-policy",
                                    json={"stability_seconds": 900 if pinned else 600})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(self.client.get("/api/vaults/1/operation-policy").json()
                             ["stability_seconds"], 900 if pinned else 600)
            self.assertEqual(self.client.get("/api/vaults/4/operation-policy").json()
                             ["stability_seconds"], 300)
            self.assertEqual(self.session_vault(), pinned)

    def test_the_old_addresses_are_gone_without_an_alias(self) -> None:
        paths = self.client.app.openapi()["paths"]
        self.assertFalse([path for path in paths if path.startswith("/api/vault/")])
        for method, suffix, body in self.OPERATIONS:
            with self.subTest(method=method, suffix=suffix):
                separator = "&" if "?" in suffix else "?"
                response = self.client.request(
                    method, f"/api/vault/{suffix}{separator}vault_id=1", json=body)
                self.assertIn(response.status_code, (404, 405), response.text)

    def test_each_new_address_is_matched_by_its_own_route(self) -> None:
        self.each_address_is_matched_by_its_own_route(self.OPERATIONS)


class BrowseAddress(VaultAddressCase):
    """The file list, a file's detail and the statistics take their Vault from the path (ADR-0056)."""

    OPERATIONS = (
        ("GET", "files", X),
        ("GET", "files/detail?path=a.txt", X),
        ("GET", "stats", X),
    )

    @staticmethod
    def files_on_disk(connection: Any) -> None:
        """The seeded Vault Files have a Local Copy, so the file list shows them."""
        connection.execute(
            "UPDATE files SET local_presence='present', local_file_type='regular', local_size=1,"
            " local_mtime_ns=1, local_seen_at=now(), local_observed_at=now()")

    def test_only_an_operational_vault_in_the_path_is_accepted(self) -> None:
        self.only_an_operational_vault_is_accepted(self.OPERATIONS)

    def test_the_session_vault_is_neither_an_address_nor_written(self) -> None:
        with self.database.connect() as connection:
            seed_vault(connection, id=4, name="Second", source_root="/sources/managed/second")
            seed_job(connection, vault_id=4, path="b.txt")
            self.files_on_disk(connection)
        for pinned in (None, 4):
            with self.database.connect() as connection:
                connection.execute("UPDATE sessions SET vault_id=%s", (pinned,))
            for vault_id, path, other in ((1, "a.txt", "b.txt"), (4, "b.txt", "a.txt")):
                with self.subTest(pinned=pinned, vault_id=vault_id):
                    listing = self.client.get(f"/api/vaults/{vault_id}/files")
                    self.assertEqual(listing.status_code, 200, listing.text)
                    self.assertEqual([item["path"] for item in listing.json()["items"]], [path])
                    detail = self.client.get(f"/api/vaults/{vault_id}/files/detail?path={path}")
                    self.assertEqual((detail.status_code, detail.json()["path"]), (200, path))
                    self.check("GET", f"/api/vaults/{vault_id}/files/detail?path={other}", X,
                               404, problem("api.file_not_found"))
                    stats = self.client.get(f"/api/vaults/{vault_id}/stats")
                    self.assertEqual(stats.status_code, 200, stats.text)
                    self.assertEqual(sum(stats.json()["states"].values()), 1)
            self.assertEqual(self.session_vault(), pinned)

    def test_the_session_vault_is_still_written_by_me_alone(self) -> None:
        # What the interface opens on never came from these routes: ``/api/me``
        # names the first operational Vault and writes it into the Session.
        with self.database.connect() as connection:
            self.files_on_disk(connection)
        for _method, suffix, _body in self.OPERATIONS:
            self.assertEqual(self.client.get(f"/api/vaults/1/{suffix}").status_code, 200)
        self.assertIsNone(self.session_vault())
        self.assertEqual(self.client.get("/api/me").json()["vault"]["id"], 1)
        self.assertEqual(self.session_vault(), 1)

    def test_without_an_operational_vault_the_answer_is_the_path_one(self) -> None:
        # The Session fallback is gone with its 403 ``api.no_vault``.
        with self.database.connect() as connection:
            connection.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=1")
        for method, suffix, body in self.OPERATIONS:
            self.check(method, f"/api/vaults/1/{suffix}", body,
                       404, problem("api.protected_vault_not_found"))
        self.assertIsNone(self.session_vault())

    def test_the_old_addresses_are_gone_without_an_alias(self) -> None:
        paths = self.client.app.openapi()["paths"]
        for old in ("/api/files", "/api/files/detail", "/api/stats"):
            self.assertNotIn(old, paths)
        for _method, suffix, _body in self.OPERATIONS:
            for query in ("", "vault_id=1"):
                with self.subTest(suffix=suffix, query=query):
                    separator = "&" if "?" in suffix else "?"
                    response = self.client.get(f"/api/{suffix}{separator if query else ''}{query}")
                    self.assertEqual((response.status_code, response.json()),
                                     (404, {"detail": "Not Found"}))
        self.assertIsNone(self.session_vault())

    def test_the_vault_is_not_a_query_parameter_any_more(self) -> None:
        paths = self.client.app.openapi()["paths"]
        for _method, suffix, _body in self.OPERATIONS:
            parameters = paths[f"/api/vaults/{{vault_id}}/{suffix.partition('?')[0]}"]["get"]["parameters"]
            self.assertEqual([(item["name"], item["in"]) for item in parameters
                              if item["name"] == "vault_id"], [("vault_id", "path")])
        # A leftover ``?vault_id=`` is ignored: the path is the whole address.
        with self.database.connect() as connection:
            self.files_on_disk(connection)
        listing = self.client.get("/api/vaults/1/files?vault_id=99")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual([item["path"] for item in listing.json()["items"]], ["a.txt"])

    def test_each_new_address_is_matched_by_its_own_route(self) -> None:
        self.each_address_is_matched_by_its_own_route(self.OPERATIONS)


class JobsAddress(VaultAddressCase):
    """Jobs, scan, catalog events, Uploads, recovery and cloud deletion take their Vault from the path (ADR-0056)."""

    PURGE = {"path": "a.txt", "confirmation": "Docs", "reason": "r"}
    OPERATIONS = (
        ("GET", "jobs", X),
        ("GET", "jobs/1", X),
        ("POST", "jobs/cancel", {"group_id": NO_GROUP, "action": "upload"}),
        ("GET", f"job-groups/{NO_GROUP}/jobs", X),
        ("GET", "files/jobs?path=a.txt", X),
        ("GET", "files/versions?path=a.txt", X),
        ("POST", "files/cloud-only-conflict/replace", {"path": "a.txt"}),
        ("POST", "scan", X),
        ("GET", "catalog/revision", X),
        ("GET", "catalog/events?subscribe=false", X),
        ("POST", "upload", {"path": "a.txt"}),
        ("GET", f"upload/admissions/{NO_GROUP}", X),
        ("POST", "upload/reconcile", {"path": "a.txt"}),
        ("POST", "upload/cancel-pending", {"path": ""}),
        ("POST", "recover", {"path": "a.txt"}),
        ("POST", "recover/estimate", {"path": "a.txt"}),
        ("POST", "recover/approve", {"group_id": NO_GROUP}),
        ("POST", "free-space", {"path": "a.txt"}),
        ("POST", "cloud-deletion/preview", {"path": "a.txt"}),
        ("POST", "cloud-purge", PURGE),
    )
    # The route of an address whose last segments are parameters.
    TEMPLATES = {
        "jobs/1": "jobs/{job_id}",
        f"job-groups/{NO_GROUP}/jobs": "job-groups/{group_id}/jobs",
        f"upload/admissions/{NO_GROUP}": "upload/admissions/{group_id}",
    }
    RETRY = "upload/retry-failed"

    def template(self, suffix: str) -> str:
        path = suffix.partition("?")[0]
        return f"/api/vaults/{{vault_id}}/{self.TEMPLATES.get(path, path)}"

    def events(self, vault_id: int) -> list[tuple[str, dict[str, Any]]]:
        """The frames of the finite event stream of one Vault."""
        response = self.client.get(f"/api/vaults/{vault_id}/catalog/events?subscribe=false")
        self.assertEqual(response.status_code, 200, response.text)
        frames = []
        for block in response.text.strip().split("\n\n"):
            fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
            if "event" in fields:
                frames.append((fields["event"], json.loads(fields["data"])))
        return frames

    def test_the_table_is_every_operation_of_the_router(self) -> None:
        from app.routes import jobs
        routed = {(method, route.path) for route in jobs.router.routes
                  for method in route.methods if "{vault_id}" in route.path}
        listed = {(method, self.template(suffix)) for method, suffix, _body in self.OPERATIONS}
        self.assertEqual(routed, listed | {("POST", f"/api/vaults/{{vault_id}}/{self.RETRY}")})
        # What stays without a Vault is installation-wide.
        self.assertEqual(
            {(method, route.path) for route in jobs.router.routes
             for method in route.methods if "{vault_id}" not in route.path},
            {("POST", "/api/upload/retry-failed"), ("GET", "/api/cloud-deletion")})

    def test_only_an_operational_vault_in_the_path_is_accepted(self) -> None:
        self.only_an_operational_vault_is_accepted(self.OPERATIONS)

    def test_the_session_vault_is_neither_an_address_nor_written(self) -> None:
        with self.database.connect() as connection:
            seed_vault(connection, id=4, name="Second", source_root="/sources/managed/second")
            other_job = seed_job(connection, vault_id=4, path="b.txt")
        for pinned in (None, 4):
            with self.database.connect() as connection:
                connection.execute("UPDATE sessions SET vault_id=%s", (pinned,))
            for vault_id, job_id, path, foreign in ((1, self.job_id, "a.txt", other_job),
                                                    (4, other_job, "b.txt", self.job_id)):
                with self.subTest(pinned=pinned, vault_id=vault_id):
                    base = f"/api/vaults/{vault_id}"
                    jobs = self.client.get(f"{base}/jobs")
                    self.assertEqual(jobs.status_code, 200, jobs.text)
                    self.assertEqual([item["path"] for item in jobs.json()["items"]], [path])
                    self.assertEqual(self.client.get(f"{base}/jobs/{job_id}").json()["path"], path)
                    self.check("GET", f"{base}/jobs/{foreign}", X, 404, problem("api.job_not_found"))
                    revision = self.client.get(f"{base}/catalog/revision")
                    self.assertEqual((revision.status_code, revision.json()["vault_id"]), (200, vault_id))
                    # A Session on another Vault does not close the stream of this one.
                    self.assertEqual(self.events(vault_id), [("hello", {"vault_id": vault_id, "revision": 0})])
            self.assertEqual(self.session_vault(), pinned)

    def test_the_job_history_of_an_existing_file_fails_as_before_the_move(self) -> None:
        # Not fixed here: the query names ``file_id``, a column ``job_rows``
        # does not have, so the history of a file that exists is a 500.
        with self.assertRaises(psycopg.errors.UndefinedColumn):
            self.client.get("/api/vaults/1/files/jobs?path=a.txt")

    def test_the_session_vault_is_still_written_by_me_alone(self) -> None:
        for suffix in ("jobs", "catalog/revision", "catalog/events?subscribe=false"):
            self.assertEqual(self.client.get(f"/api/vaults/1/{suffix}").status_code, 200)
        self.assertEqual(self.client.post("/api/vaults/1/scan").status_code, 202)
        self.assertIsNone(self.session_vault())
        self.assertEqual(self.client.get("/api/me").json()["vault"]["id"], 1)
        self.assertEqual(self.session_vault(), 1)

    def test_without_an_operational_vault_the_answer_is_the_path_one(self) -> None:
        # The Session fallback is gone with its 403 ``api.no_vault``.
        with self.database.connect() as connection:
            connection.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=1")
        for method, suffix, body in self.OPERATIONS:
            self.check(method, f"/api/vaults/1/{suffix}", body,
                       404, problem("api.protected_vault_not_found"))
        self.assertIsNone(self.session_vault())

    def test_the_old_addresses_are_gone_without_an_alias(self) -> None:
        paths = self.client.app.openapi()["paths"]
        for method, suffix, body in self.OPERATIONS:
            old = self.template(suffix).replace("/vaults/{vault_id}", "")
            self.assertNotIn(old, paths)
            for query in ("", "vault_id=1"):
                with self.subTest(method=method, suffix=suffix, query=query):
                    separator = ("&" if "?" in suffix else "?") if query else ""
                    response = self.client.request(method, f"/api/{suffix}{separator}{query}", json=body)
                    self.assertEqual((response.status_code, response.json()),
                                     (404, {"detail": "Not Found"}))
        self.assertIsNone(self.session_vault())

    def test_the_vault_is_not_a_query_parameter_any_more(self) -> None:
        paths = self.client.app.openapi()["paths"]
        operations = [(method, self.template(suffix)) for method, suffix, _body in self.OPERATIONS]
        for method, path in (*operations, ("POST", f"/api/vaults/{{vault_id}}/{self.RETRY}")):
            parameters = paths[path][method.lower()]["parameters"]
            self.assertEqual([(item["name"], item["in"]) for item in parameters
                              if item["name"] == "vault_id"], [("vault_id", "path")], path)
        self.assertNotIn("parameters", paths["/api/upload/retry-failed"]["post"])
        # A leftover ``?vault_id=`` is ignored: the path is the whole address.
        jobs = self.client.get("/api/vaults/1/jobs?vault_id=99")
        self.assertEqual(jobs.status_code, 200, jobs.text)
        self.assertEqual([item["path"] for item in jobs.json()["items"]], ["a.txt"])

    def test_each_new_address_is_matched_by_its_own_route(self) -> None:
        from starlette.routing import Match
        routes = self.client.app.router.routes
        for method, suffix, _body in (*self.OPERATIONS, ("POST", self.RETRY, X)):
            scope = {"type": "http", "method": method, "path": f"/api/vaults/1/{suffix.partition('?')[0]}"}
            first = next(route for route in routes if route.matches(scope)[0] is Match.FULL)
            self.assertEqual(first.path, self.template(suffix), (method, suffix))
        # As before the move: ``cancel`` is a Job id only for a GET, and not a valid one.
        self.assertEqual(self.client.get("/api/vaults/1/jobs/cancel").status_code, 422)

    def test_one_vault_retry_accepts_any_existing_vault(self) -> None:
        # Unlike the other operations: a Tombstone and a Vault being created are accepted.
        for vault_id in (1, 2, 3):
            response = self.client.post(f"/api/vaults/{vault_id}/{self.RETRY}")
            self.assertEqual((response.status_code, response.json()), (202, {"item_count": 0}), vault_id)
        self.check("POST", f"/api/vaults/99/{self.RETRY}", X, 404, problem("api.vault_not_found"))
        self.assertEqual(self.client.post(f"/api/vaults/0/{self.RETRY}").status_code, 422)
        self.assertIsNone(self.session_vault())

    def test_the_installation_retry_keeps_its_address_and_loses_the_vault_parameter(self) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE sessions SET vault_id=1")
        from app import storage
        for query in ("", "?vault_id=99"):
            with patch.object(storage, "queue_vault_auto_uploads", return_value=1) as queue:
                response = self.client.post(f"/api/upload/retry-failed{query}")
            self.assertEqual((response.status_code, response.json()), (202, {"item_count": 3}), query)
            self.assertEqual([call.args[0]["id"] for call in queue.call_args_list], [1, 2, 3])


class PolicyErrors(ApiErrorCase):
    def test_operation_policy(self) -> None:
        glob = {"field": "exclude_globs", "action": "remove", "glob": "*.tmp"}
        gone = problem("api.policy.rule_gone")
        self.check("POST", "/api/vaults/1/operation-policy/globs", glob, 409, gone)
        self.check("POST", "/api/vaults/1/operation-policy/globs",
                   {**glob, "action": "add", "glob": "  "}, 422, problem("api.policy.glob_empty"))
        self.check("PUT", "/api/vaults/1/operation-policy", {"exclude_globs": ["/absolute"]},
                   422, problem("api.policy.glob_absolute"))
        self.check("POST", "/api/vaults/1/operation-policy/preview-globs",
                   {"paths": ["a.txt"], "include_globs": ["/absolute"], "exclude_globs": []},
                   422, problem("api.policy.glob_absolute"))
        exclude, include = "/api/vaults/1/exclusions/exclude", "/api/vaults/1/exclusions/include"
        self.check("POST", exclude, {"path": "a*", "is_directory": True},
                   422, problem("api.policy.glob_characters"))
        self.check("POST", exclude, {"path": "a.txt", "is_directory": False},
                   422, problem("api.policy.root_file_rule"))
        self.check("POST", include, {"path": "a*", "is_directory": True},
                   409, problem("api.policy.no_own_rule"))
        self.check("POST", include, {"path": "dir", "is_directory": True}, 409, gone)

    def test_vault_gone_while_saving_the_policy(self) -> None:
        from app.routes import policy
        from app.services import operation_policies
        for reason, status, payload in (
            ("vault_quiesced", 409, problem("api.vault_no_longer_protected")),
            ("vault_not_found", 404, problem("api.vault_not_found")),
        ):
            error = operation_policies.VaultUnavailable(reason)
            with patch.object(policy, "lock_vault_policy", side_effect=error):
                self.check("PUT", f"/api/vaults/1/operation-policy?case={reason}", {},
                           status, payload)

    def test_disk_of_an_excluded_folder(self) -> None:
        from app.services import vault_exclusions
        for reason, status, payload in (
            ("unavailable", 409, problem("api.disk_read.unavailable")),
            ("missing", 404, problem("api.disk_read.missing")),
            ("unreadable", 409, problem("api.disk_read.unreadable")),
            ("made_up", 409, problem("api.disk_read.unreadable")),
        ):
            with patch.object(vault_exclusions, "directory_listing",
                              side_effect=vault_exclusions.DiskReadError(reason)):
                self.check("GET", f"/api/vaults/1/excluded-folders?directory={reason}", X,
                           status, payload)


class JobErrors(ApiErrorCase):
    def test_admission(self) -> None:
        nothing = problem("api.admission.none_eligible")
        self.check("POST", "/api/vaults/1/upload", {"path": "a.txt"}, 409, nothing)
        self.check("POST", "/api/vaults/1/upload", {"path": "../outside"}, 422, problem("api.invalid_path"))
        self.check("POST", "/api/vaults/1/recover", {"path": "a.txt"}, 409, nothing)
        self.check("POST", "/api/vaults/1/recover/estimate", {"path": "a.txt"},
                   404, problem("api.admission.no_recoverable_version"))
        self.check("POST", "/api/vaults/1/free-space", {"path": "a.txt"}, 409, nothing)
        self.check("POST", "/api/vaults/1/files/cloud-only-conflict/replace", {"path": "a.txt"},
                   409, problem("api.admission.no_cloud_only_conflict"))
        self.check("GET", f"/api/vaults/1/upload/admissions/{NO_GROUP}", X,
                   404, problem("api.upload_admission.not_found"))
        self.check("POST", "/api/vaults/1/upload/reconcile", {"path": "a.txt", "is_directory": True},
                   422, problem("api.reconcile.single_file"))
        with patch("app.routes.jobs.vault_local_access") as access:
            access.return_value.local_operations_allowed = False
            self.check("POST", "/api/vaults/1/upload/reconcile", {"path": "a.txt"},
                       503, problem("api.reconcile.volume_unavailable"))
        self.check("POST", "/api/vaults/1/recover/approve", {"group_id": NO_GROUP},
                   409, problem("api.recover_approval.none_waiting"))

    def test_admission_refusals(self) -> None:
        from app.services import job_admission
        for error, status, payload in (
            (job_admission.InvalidAdmission("api.admission.restore_tier"),
             422, problem("api.admission.restore_tier")),
            (job_admission.AdmissionTargetNotFound("api.admission.no_recoverable_version"),
             404, problem("api.admission.no_recoverable_version")),
            (job_admission.AdmissionRefused("api.admission.none_eligible"),
             409, problem("api.admission.none_eligible")),
            (job_admission.LocalStorageUnavailable("api.admission.local_storage_unavailable"),
             503, problem("api.admission.local_storage_unavailable")),
            (job_admission.AdmissionRefused("api.local_release_unconfirmed"),
             409, problem("api.local_release_unconfirmed")),
            (job_admission.AdmissionRefused("api.local_release_blocked_path", path="a/b"),
             409, problem("api.local_release_blocked_path", path="a/b")),
        ):
            with patch.object(job_admission, "queue_jobs", side_effect=error):
                self.check("POST", f"/api/vaults/1/free-space?case={type(error).__name__}",
                           {"path": "a.txt"}, status, payload)

    def test_upload_reconciliation(self) -> None:
        from app.services import upload_provenance, upload_remediation
        for reason, key in (("already_resolved", "already_resolved"),
                            ("vault_suspended", "vault_suspended"),
                            ("identity_changed", "identity_changed"),
                            ("made_up", "proof_unavailable")):
            error = upload_provenance.UploadBlocked(reason)
            with patch.object(upload_remediation, "schedule_resume", side_effect=error), \
                    patch("app.routes.jobs.vault_local_access") as access:
                access.return_value.local_operations_allowed = True
                self.check("POST", f"/api/vaults/1/upload/reconcile?case={reason}", {"path": "a.txt"},
                           409, problem(f"reconciliation.reason.{key}"))

    def test_folder_upload(self) -> None:
        folder = {"path": "folder", "is_directory": True, "request_id": NO_GROUP}
        self.assertEqual(self.client.post("/api/vaults/1/upload", json=folder).status_code, 202)
        self.check("POST", "/api/vaults/1/upload", {**folder, "path": "other"},
                   409, problem("api.upload_admission.request_reused"))

    def test_cancel(self) -> None:
        cancel = {"group_id": NO_GROUP, "action": "upload"}
        self.check("POST", "/api/vaults/1/jobs/cancel", {**cancel, "action": "made-up"},
                   422, problem("api.invalid_operation"))
        self.check("POST", "/api/vaults/1/jobs/cancel", cancel,
                   409, problem("api.operation_not_running"))
        self.check("POST", "/api/vaults/1/jobs/cancel", {**cancel, "action": "cloud-purge"},
                   409, problem("api.cloud_deletion.not_cancellable"))

    def test_cloud_deletion(self) -> None:
        only_cloud = problem("api.cloud_deletion.cloud_only_required")
        self.check("POST", "/api/vaults/1/cloud-deletion/preview", {"path": "a.txt"}, 409, only_cloud)
        self.check("POST", "/api/vaults/1/cloud-deletion/preview", {"path": "../outside"},
                   409, problem("api.cloud_deletion.invalid_paths"))
        purge = {"path": "a.txt", "confirmation": "Docs", "reason": "r"}
        self.check("POST", "/api/vaults/1/cloud-purge", purge, 409, only_cloud)
        self.check("POST", "/api/vaults/1/cloud-purge", {**purge, "confirmation": "wrong"},
                   422, problem("api.confirmation_required"))
        self.check("POST", "/api/vaults/1/cloud-purge", {
            "directories": ["folder"], "confirmation": "Docs", "reason": "r",
            "fingerprint": "stale"}, 409, problem("api.cloud_purge_selection_changed"))


class ItalianErrors(ApiErrorCase):
    def test_an_error_follows_the_locale_of_the_request(self) -> None:
        self.client.cookies.set("frostvault_locale", "it")
        self.check("GET", "/api/vaults/99/vault-deletion", X,
                   404, problem("api.vault_deletion.not_found", locale="it"))
        self.check("GET", "/api/vaults/1/files/detail?path=../outside", X,
                   422, problem("api.invalid_path", locale="it"))
        self.check("GET", "/api/vaults/99/bucket", X,
                   404, problem("api.vault_not_found", locale="it"))
