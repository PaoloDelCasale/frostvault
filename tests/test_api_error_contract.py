"""Status and body of the errors the HTTP API answers with, route by route.

Each case is a real request against a seeded catalog: Vault 1 ``Docs`` is
active (its folder is not on disk), Vault 2 ``Old`` a Tombstone, Vault 3
``New`` still provisioning. The cases whose reason comes from a table of the
service (Vault creation, Source Volume folders, Reconnect, the disk of an
Excluded Folder, Job admission, Upload reconciliation) raise the service's own
error at its seam.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from app.i18n import translate
from tests.fixtures import FrostVaultTestCase, seed_job, seed_vault

X = None  # no request body
NO_GROUP = "00000000-0000-0000-0000-000000000000"
REAUTH_REQUIRED = {"error": "reauth_required"}


def plain(text: str) -> dict[str, Any]:
    """An error that carries English text only."""
    return {"detail": text}


def localized(key: str, locale: str = "en", **params: Any) -> dict[str, Any]:
    """An error whose text is a catalog message, in the locale of the request."""
    text = translate(key, locale=locale, **params)
    return {"detail": text, "message": text, "message_key": key, "message_params": params}


def creation(key: str) -> dict[str, Any]:
    text = translate(key, locale="en")
    return {"detail": text, "message": text, "message_key": key}


def invalid_path(locale: str = "en") -> dict[str, Any]:
    text = translate("api.invalid_path", locale=locale)
    return {"detail": text, "message_key": "api.invalid_path", "message": text}


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
        """One request; its status and JSON body are the contract."""
        response = self.client.request(method, url, json=body, **options)
        self.same(response, status, payload, f"{method} {url} {body or ''}".strip())
        return response

    def same(self, response: Any, status: int, payload: Any, label: str = "") -> None:
        with self.subTest(label):
            self.assertEqual((response.status_code, response.json()), (status, payload))


class SessionErrors(ApiErrorCase):
    def test_without_a_session(self) -> None:
        anonymous = self.http_client()
        self.same(anonymous.get("/api/me"), 401, plain("Authentication required"))
        anonymous.cookies.set(self.settings.session_cookie_name, "not-a-session")
        self.same(anonymous.get("/api/me"), 401, plain("Invalid session"))

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
            ("POST", "/api/cloud-purge", {"path": "a.txt", "confirmation": "Docs", "reason": "r"}),
        ):
            self.check(method, url, body, 403, REAUTH_REQUIRED)

    def test_vault_of_the_request(self) -> None:
        self.check("GET", "/api/jobs?vault_id=99", X, 404, plain("Protected Vault not found"))
        self.check("GET", "/api/jobs?vault_id=2", X, 404, plain("Protected Vault not found"))
        self.check("POST", "/api/vaults/select", {"vault_id": 99}, 403, plain("Vault access denied"))
        self.check("POST", "/api/vaults/select", {"vault_id": 2}, 403, plain("Vault access denied"))
        with self.database.connect() as connection:
            connection.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=1")
        self.check("GET", "/api/jobs", X, 403, plain("No vault exists yet"))
        me = self.client.get("/api/me")
        self.assertEqual((me.status_code, me.json()["vault"]), (200, None))

    def test_sign_in_and_locale(self) -> None:
        self.check("PUT", "/api/locale", {"locale": "fr"}, 400, plain("Unsupported locale"))
        self.check("GET", "/auth/oidc/login", X, 404, plain("OIDC login is not enabled"))
        self.check("GET", "/auth/oidc/reauth", X, 404, plain("OIDC login is not enabled"))
        self.check("GET", "/auth/oidc/callback?state=s&code=c", X,
                   400, plain("OIDC login failed: disabled"))
        self.check("POST", "/api/reauth", {"password": "wrong"}, 401, plain("Incorrect password"))
        response = self.http_client().post("/api/login", json={"password": "wrong"})
        self.same(response, 401, plain("Incorrect emergency password"))
        outside = self.http_client(client=("203.0.113.9", 50000))
        response = outside.post("/api/login", json={"password": "wrong"})
        self.same(response, 403, plain("The emergency password is not accepted from this network"))
        self.client = outside
        self.sign_in(send_csrf_header=True)
        self.check("POST", "/api/reauth", {"password": "wrong"},
                   403, plain("Password reauthentication is not allowed here"))

    def test_too_many_attempts(self) -> None:
        anonymous = self.http_client()
        for send in (lambda: anonymous.post("/api/login", json={"password": "wrong"}),
                     lambda: self.client.post("/api/reauth", json={"password": "wrong"})):
            response = send()
            for _ in range(12):
                if response.status_code == 429:
                    break
                response = send()
            self.same(response, 429, plain("Too many attempts; try again later"))
            self.assertGreater(int(response.headers["Retry-After"]), 0)


class VaultManagementErrors(ApiErrorCase):
    def test_vault_not_found(self) -> None:
        stop = {"cloud_disposition": "keep", "confirmation": "Docs", "reason": "r"}
        deletion = {"confirmation": "Old", "reason": "r"}
        reconnect = {"volume_alias": "disk", "relative_path": "docs", "reason": "moved"}
        for method, url, body, status, payload in (
            ("POST", "/api/vaults/99/pause", X, 404, plain("Vault not found")),
            ("POST", "/api/vaults/99/resume", X, 404, plain("Vault not found")),
            ("GET", "/api/vaults/99/bucket", X, 404, plain("Vault not found")),
            ("GET", "/api/vaults/99/retention-lock", X, 404, plain("Vault not found")),
            ("POST", "/api/vaults/99/bucket/retry", X, 404, plain("Vault not found")),
            ("POST", "/api/vaults/99/creation/cancel", X,
             404, localized("api.vault_creation_cancel.not_found")),
            ("GET", "/api/vaults/99/stop-protection", X, 404, plain("Vault not found")),
            ("POST", "/api/vaults/99/stop-protection", stop, 409, plain("Vault not found")),
            ("POST", "/api/vaults/99/stop-protection/cancel-deletion", X,
             409, plain("Vault not found")),
            ("GET", "/api/vaults/99/vault-deletion", X,
             404, localized("api.vault_deletion.not_found")),
            ("GET", "/api/vaults/99/vault-deletion/preview", X,
             404, localized("api.vault_deletion.not_found")),
            ("POST", "/api/vaults/99/vault-deletion", deletion,
             404, localized("api.vault_deletion.not_found")),
            ("POST", "/api/vaults/99/vault-deletion/cancel", X,
             409, localized("api.vault_deletion.refused")),
            ("POST", "/api/vaults/99/vault-deletion/retry", X,
             409, localized("api.vault_deletion.refused")),
            ("GET", "/api/vaults/99/duplicate-cleanup", X,
             404, localized("api.duplicate_cleanup.not_found")),
            ("PUT", "/api/vaults/99/duplicate-cleanup", {"enabled": False},
             422, localized("api.duplicate_cleanup.invalid")),
            ("GET", "/api/vaults/99/mirror-cleanup", X,
             404, localized("api.mirror_cleanup.not_found")),
            ("PUT", "/api/vaults/99/mirror-cleanup", {"enabled": False},
             422, localized("api.mirror_cleanup.invalid")),
            ("GET", "/api/vaults/99/minimum-storage", X,
             404, localized("api.minimum_storage.not_found")),
            ("GET", "/api/vaults/99/cloud-usage", X, 404, localized("api.cloud_usage.not_found")),
            ("GET", "/api/vaults/99/billed-cost", X, 404, localized("api.billed_cost.not_found")),
            ("POST", "/api/vaults/99/reconnect", reconnect, 404, plain("Vault not found")),
            ("POST", "/api/upload/retry-failed?vault_id=99", X, 404, plain("Vault not found")),
            ("POST", "/api/general/cost-estimates/storage", {"vault_id": 99},
             404, plain("Vault not found")),
        ):
            self.check(method, url, body, status, payload)

    def test_state_of_the_vault(self) -> None:
        stop = {"cloud_disposition": "keep", "confirmation": "Docs", "reason": "r"}
        folders = localized("api.mirror_cleanup.folders")
        for method, url, body, status, payload in (
            ("POST", "/api/vaults/2/pause", X,
             409, plain("Only an operational Vault can be paused or resumed")),
            ("POST", "/api/vaults/1/creation/cancel", X,
             409, localized("api.vault_creation_cancel.not_provisioning")),
            ("POST", "/api/vaults/1/stop-protection", {**stop, "confirmation": "wrong"},
             422, plain("Type the exact Vault name to confirm")),
            ("POST", "/api/vaults/3/stop-protection", {**stop, "confirmation": "New"},
             409, plain("The Vault's bucket is still being created")),
            ("POST", "/api/vaults/1/stop-protection/cancel-deletion", X,
             409, plain("No pending cloud deletion")),
            ("GET", "/api/vaults/1/vault-deletion/preview", X,
             409, localized("api.vault_deletion.not_tombstone")),
            ("GET", "/api/vaults/2/vault-deletion/preview", X,
             502, localized("api.vault_deletion.listing_failed")),
            ("POST", "/api/vaults/1/vault-deletion", {"confirmation": "Docs", "reason": "r"},
             409, localized("api.vault_deletion.not_tombstone")),
            ("POST", "/api/vaults/2/vault-deletion/cancel", X,
             409, localized("api.vault_deletion.not_requested")),
            ("POST", "/api/vaults/2/vault-deletion/retry", X,
             409, localized("api.vault_deletion.not_failed")),
            ("PUT", "/api/vaults/2/duplicate-cleanup", {"enabled": False},
             409, localized("api.duplicate_cleanup.stopped")),
            ("PUT", "/api/vaults/2/mirror-cleanup", {"enabled": False},
             409, localized("api.mirror_cleanup.stopped")),
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
                       422, localized("api.vault_deletion.confirmation"))
            self.check("POST", url, {"confirmation": "Old", "reason": " "},
                       409, localized("api.vault_deletion.reason"))
            self.check("POST", url, {"confirmation": "Old", "reason": "r", "delete_bucket": True},
                       409, localized("api.vault_deletion.refused"))
            with self.database.connect() as connection:
                seed_job(connection, vault_id=2, status="queued")
            self.check("POST", url, {"confirmation": "Old", "reason": "r"},
                       409, localized("api.vault_deletion.active_jobs"))

    def test_vault_creation(self) -> None:
        from app.services import vaults
        for error, status, payload in (
            (vaults.InvalidVaultName("Vault name is required"),
             422, creation("api.vault_name_invalid")),
            (vaults.VaultProvisioningUnavailable("no region"),
             503, creation("api.vault_provisioning_unavailable")),
            (vaults.VaultAdoptionError("occupied"), 422, creation("api.vault_adoption.occupied")),
            (vaults.VaultAdoptionError("made_up"), 422, creation("api.vault_adoption.failed")),
            (vaults.VaultCreationError("Could not create the Vault"),
             409, creation("ui.vault_create.failed")),
        ):
            with patch("app.routes.vaults.create_vault", side_effect=error):
                self.check("POST", "/api/vaults", {"name": "Photos"}, status, payload)

    def test_source_volume_folders(self) -> None:
        from app.services import source_directories
        self.check("GET", "/api/source-volumes/nope/browse", X,
                   404, plain("Source Volume not found"))
        for reason, status, text in (
            ("volume_not_found", 404, "Source Volume not found"),
            ("volume_unavailable", 409, "Source Volume is not available"),
            ("invalid_volume", 422, "Source Volume is not usable for Vault roots"),
            ("invalid_path", 422, "Source directory path is invalid"),
            ("path_missing", 404, "Source directory does not exist"),
            ("occupied", 409, "Occupied Vault roots cannot be browsed"),
            ("made_up", 400, "Request could not be completed"),
        ):
            error = source_directories.SourceDirectoryError(reason, "internal text")
            with patch.object(source_directories, "browse_source_directories", side_effect=error):
                self.check("GET", f"/api/source-volumes/disk/browse?path={reason}", X,
                           status, plain(text))

    def test_reconnect(self) -> None:
        from app.services import vault_relocation
        from app.services.runtime_status import scan_lock_for_vault
        body = {"volume_alias": "disk", "relative_path": "docs", "reason": "moved"}
        for reason, status, text in (
            ("not_found", 404, "Vault not found"),
            ("reason_required", 422, "A reason between 3 and 500 characters is required"),
            ("invalid_path", 422, "Reconnect destination is invalid"),
            ("not_directory", 422, "Reconnect destination is not a directory"),
            ("volume_unavailable", 409, "Destination Source Volume is unavailable"),
            ("managed_not_own", 409, "Only this Vault's own managed folder can be reconnected"),
            ("managed_missing", 409, "Managed folder missing"),
            ("different_volume", 409, "Destination crosses a nested mount"),
            ("symlink", 409, "Reconnect paths cannot contain symbolic links"),
            ("inaccessible", 409, "Reconnect destination is inaccessible"),
            ("overlap", 409, "Reconnect destination overlaps another Vault"),
            ("active_jobs", 409, "Vault has active Jobs or a scan"),
            ("identity_ambiguous", 409, "Source Volume identity is unavailable"),
            ("identity_mismatch", 409, "Destination changed during reconnect"),
            ("protection_stopped", 409, "Folder protection has stopped; reconnect is unavailable"),
            ("made_up", 400, "Vault reconnect could not be completed"),
        ):
            error = vault_relocation.VaultRelocationError(reason, "internal text")
            with patch.object(vault_relocation, "reconnect_vault_root", side_effect=error):
                self.check("POST", f"/api/vaults/1/reconnect?case={reason}", body,
                           status, plain(text))
        params = {"path": "/sources/disk/docs", "identity": "99:100", "groups": "100",
                  "owner": "0:0", "mode": "0755"}
        for reason in ("volume_not_writable", "destination_not_writable"):
            error = vault_relocation.VaultRelocationError(reason, "internal text", params=params)
            with patch.object(vault_relocation, "reconnect_vault_root", side_effect=error):
                self.check("POST", f"/api/vaults/1/reconnect?case={reason}", body,
                           409, localized(f"api.reconnect_{reason}", **params))
        lock = scan_lock_for_vault(1)
        self.assertTrue(lock.acquire(blocking=False, operation="scan"))
        try:
            self.check("POST", "/api/vaults/1/reconnect", body,
                       409, plain("Vault has active Jobs or a scan"))
            self.check("POST", "/api/vaults/1/stop-protection",
                       {"cloud_disposition": "keep", "confirmation": "Docs", "reason": "r"},
                       409, plain("Wait for the Vault scan to finish"))
        finally:
            lock.release()


class BrowseErrors(ApiErrorCase):
    def test_files_jobs_and_notifications(self) -> None:
        self.check("GET", "/api/files/detail?path=missing.txt", X,
                   404, plain("Vault File not found"))
        self.check("GET", "/api/files/detail?path=../outside", X, 422, invalid_path())
        self.check("GET", "/api/files/jobs?path=missing.txt", X,
                   404, plain("Vault File not found"))
        self.check("GET", "/api/jobs/99999", X, 404, plain("Job not found in this Vault"))
        self.check("POST", "/api/notifications/read", {"notification_id": 99},
                   404, plain("Notification not found"))
        self.check("GET", "/api/catalog/events?subscribe=false", X,
                   422, plain("Last-Event-ID must be an integer revision"),
                   headers={"Last-Event-ID": "not-a-number"})

    def test_recovery_kit_and_metadata_backups(self) -> None:
        from app.services import metadata_backups
        self.check("GET", "/api/recovery-kit/download", X,
                   409, plain("Recovery Kit requires a valid ARCHIVE_MASTER_KEY"))
        self.check("POST", "/api/recovery-kit/custody", {"fingerprint": "0" * 24},
                   409, plain("Recovery Kit fingerprint changed; download the current kit"))
        self.check("GET", "/api/general/metadata-backups/download/99", X,
                   404, plain("Backup run not found"))
        with patch.object(metadata_backups, "run_metadata_backup",
                          side_effect=metadata_backups.BackupError("pg_dump failed")):
            self.check("POST", "/api/general/metadata-backups/run", {"reason": "before a change"},
                       500, plain("pg_dump failed"))


class StoragePlanErrors(ApiErrorCase):
    def test_storage_plan(self) -> None:
        url = "/api/vault/lifecycle"
        self.check("PUT", url, {"guided_profile": "made_up"},
                   400, plain("Unknown storage plan: made_up"))
        self.check("PUT", url, {"profile": {"transitions": [
            {"days": 0, "storage_class": "MADE_UP"}]}},
            400, plain("Unsupported storage class: MADE_UP"))
        self.check("PUT", url, {"profile": {"transitions": [
            {"days": 10, "storage_class": "DEEP_ARCHIVE"},
            {"days": 20, "storage_class": "GLACIER_IR"}]}},
            400, plain("Unsupported transition from DEEP_ARCHIVE to GLACIER_IR"))

    def test_vault_move(self) -> None:
        move = {"vault_id": 1, "target_class": "DEEP_ARCHIVE"}
        self.check("POST", "/api/vault/move", {**move, "vault_id": 2},
                   409, plain("Selected Vault changed; refresh the move preview"))
        self.check("POST", "/api/vault/move", move,
                   409, plain("Acknowledge minimum-duration and early-deletion charges"))
        self.check("POST", "/api/vault/move", {**move, "acknowledge_costs": True},
                   409, plain("Choose a colder class on this Vault's saved Storage Plan"))
        with self.database.connect() as connection:
            connection.execute("UPDATE vaults SET lifecycle='reconnect_required' WHERE id=1")
        self.check("GET", "/api/vault/move", X,
                   409, plain("Vault must be protected and reconnect must be complete"))


class PolicyErrors(ApiErrorCase):
    def test_operation_policy(self) -> None:
        glob = {"field": "exclude_globs", "action": "remove", "glob": "*.tmp"}
        gone = plain("The rule is no longer in the Vault policy")
        self.check("POST", "/api/vault/operation-policy/globs", glob, 409, gone)
        self.check("POST", "/api/vault/operation-policy/globs",
                   {**glob, "action": "add", "glob": "  "}, 422, plain("glob must not be empty"))
        self.check("PUT", "/api/vault/operation-policy", {"exclude_globs": ["/absolute"]},
                   422, plain("globs must be relative paths"))
        self.check("POST", "/api/vault/operation-policy/preview-globs",
                   {"paths": ["a.txt"], "include_globs": ["/absolute"], "exclude_globs": []},
                   422, plain("globs must be relative paths"))
        exclude, include = "/api/vault/exclusions/exclude", "/api/vault/exclusions/include"
        self.check("POST", exclude, {"path": "a*", "is_directory": True}, 422, plain(
            "The name contains glob characters; edit the exclusion rules instead"))
        self.check("POST", exclude, {"path": "a.txt", "is_directory": False}, 422, plain(
            "A file at the Vault root cannot have its own rule; edit the exclusion rules instead"))
        self.check("POST", include, {"path": "a*", "is_directory": True}, 409, plain(
            "The item has no rule of its own; edit the exclusion rules instead"))
        self.check("POST", include, {"path": "dir", "is_directory": True}, 409, gone)

    def test_vault_gone_while_saving_the_policy(self) -> None:
        from app.routes import policy
        for reason, status, text in (
            ("vault_quiesced", 409, "Vault is no longer protected"),
            ("vault_not_found", 404, "Vault not found"),
        ):
            with patch.object(policy, "lock_vault_policy", side_effect=LookupError(reason)):
                self.check("PUT", f"/api/vault/operation-policy?case={reason}", {},
                           status, plain(text))

    def test_disk_of_an_excluded_folder(self) -> None:
        from app.services import vault_exclusions
        for reason, status, text in (
            ("unavailable", 409, "The Vault folder is not available on disk"),
            ("missing", 404, "Folder not found on disk"),
            ("unreadable", 409, "Folder cannot be read"),
            ("made_up", 409, "Folder cannot be read"),
        ):
            with patch.object(vault_exclusions, "directory_listing",
                              side_effect=vault_exclusions.DiskReadError(reason)):
                self.check("GET", f"/api/vault/excluded-folders?directory={reason}", X,
                           status, plain(text))


class JobErrors(ApiErrorCase):
    def test_admission(self) -> None:
        nothing = plain("No files are eligible for this operation")
        self.check("POST", "/api/upload", {"path": "a.txt"}, 409, nothing)
        self.check("POST", "/api/upload", {"path": "../outside"}, 422, invalid_path())
        self.check("POST", "/api/recover", {"path": "a.txt"}, 409, nothing)
        self.check("POST", "/api/recover/estimate", {"path": "a.txt"},
                   404, plain("No recoverable Archive Version found"))
        self.check("POST", "/api/free-space", {"path": "a.txt"}, 409, nothing)
        self.check("POST", "/api/files/cloud-only-conflict/replace", {"path": "a.txt"},
                   409, plain("No conflict with a cloud-only file at this path"))
        self.check("GET", f"/api/upload/admissions/{NO_GROUP}", X,
                   404, plain("Upload admission not found"))
        self.check("POST", "/api/upload/reconcile", {"path": "a.txt", "is_directory": True},
                   422, plain("Reconciliation requires one stable Vault File"))
        with patch("app.routes.jobs.vault_local_access") as access:
            access.return_value.local_operations_allowed = False
            self.check("POST", "/api/upload/reconcile", {"path": "a.txt"},
                       503, plain("Expected Source Volume is unavailable"))
        self.check("POST", "/api/recover/approve", {"group_id": NO_GROUP}, 409, plain(
            "No high-impact restore is waiting for approval (it may have expired)"))

    def test_admission_refusals(self) -> None:
        from app.services import job_admission
        for error, status, payload in (
            (job_admission.InvalidAdmission("Choose Standard or Bulk"),
             422, plain("Choose Standard or Bulk")),
            (job_admission.AdmissionTargetNotFound("No recoverable Archive Version found"),
             404, plain("No recoverable Archive Version found")),
            (job_admission.AdmissionRefused("No files are eligible for this operation"),
             409, plain("No files are eligible for this operation")),
            (job_admission.LocalStorageUnavailable("Source Volume is unavailable"),
             503, plain("Source Volume is unavailable")),
            (job_admission.LocalizedAdmissionRefused("api.local_release_unconfirmed"),
             409, localized("api.local_release_unconfirmed")),
            (job_admission.LocalizedAdmissionRefused("api.local_release_blocked_path", path="a/b"),
             409, localized("api.local_release_blocked_path", path="a/b")),
        ):
            with patch.object(job_admission, "queue_jobs", side_effect=error):
                self.check("POST", f"/api/free-space?case={type(error).__name__}",
                           {"path": "a.txt"}, status, payload)

    def test_upload_reconciliation(self) -> None:
        from app.services import upload_provenance, upload_remediation
        for reason in ("already_resolved", "vault_suspended", "identity_changed", "made_up"):
            error = upload_provenance.UploadBlocked(reason)
            with patch.object(upload_remediation, "schedule_resume", side_effect=error), \
                    patch("app.routes.jobs.vault_local_access") as access:
                access.return_value.local_operations_allowed = True
                self.check("POST", f"/api/upload/reconcile?case={reason}", {"path": "a.txt"},
                           409, plain(str(error)))

    def test_folder_upload(self) -> None:
        folder = {"path": "folder", "is_directory": True, "request_id": NO_GROUP}
        self.assertEqual(self.client.post("/api/upload", json=folder).status_code, 202)
        self.check("POST", "/api/upload", {**folder, "path": "other"},
                   409, plain("Upload request identifier already used for a different scope"))

    def test_cancel(self) -> None:
        cancel = {"group_id": NO_GROUP, "action": "upload"}
        self.check("POST", "/api/jobs/cancel", {**cancel, "action": "made-up"},
                   422, plain("Invalid operation"))
        self.check("POST", "/api/jobs/cancel", cancel,
                   409, plain("The operation is no longer running"))
        self.check("POST", "/api/jobs/cancel", {**cancel, "action": "cloud-purge"},
                   409, plain("Deletion has started or is no longer cancellable"))

    def test_cloud_deletion(self) -> None:
        only_cloud = plain("Only Cloud-only files can be deleted from the cloud")
        self.check("POST", "/api/cloud-deletion/preview", {"path": "a.txt"}, 409, only_cloud)
        self.check("POST", "/api/cloud-deletion/preview", {"path": "../outside"},
                   409, plain("Select valid relative file paths"))
        purge = {"path": "a.txt", "confirmation": "Docs", "reason": "r"}
        self.check("POST", "/api/cloud-purge", purge, 409, only_cloud)
        self.check("POST", "/api/cloud-purge", {**purge, "confirmation": "wrong"},
                   422, plain("Type the exact Vault name to confirm"))
        self.check("POST", "/api/cloud-purge", {
            "directories": ["folder"], "confirmation": "Docs", "reason": "r",
            "fingerprint": "stale"}, 409, localized("api.cloud_purge_selection_changed"))


class ItalianErrors(ApiErrorCase):
    def test_an_error_follows_the_locale_of_the_request(self) -> None:
        self.client.cookies.set("frostvault_locale", "it")
        self.check("GET", "/api/vaults/99/vault-deletion", X,
                   404, localized("api.vault_deletion.not_found", locale="it"))
        self.check("GET", "/api/files/detail?path=../outside", X, 422, invalid_path("it"))
        self.check("GET", "/api/vaults/99/bucket", X, 404, plain("Vault not found"))
