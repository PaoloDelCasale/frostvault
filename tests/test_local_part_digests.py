"""#703: part digests recorded by the scan and reused by the multipart upload.

Seams under test:
1. ``hash_stable_regular_file`` records, in the same read, the multipart part
   digests of a file of 8 MiB or more, cut like the Upload Snapshot's parts.
2. The scan verification publishes them with the digest; every change of
   the digest clears them (catalog paths and the database trigger).
3. A fresh upload of 8 MiB or more whose observation carries them builds its
   Upload Snapshot from the catalog: no preparation read, no reader permit.
4. A part read by the send that contradicts them retires the fingerprint; the
   retry reconciles, hashes the whole file and uploads the new content, with
   no cycle.

``PartDigestReadTests`` and ``CatalogSnapshotExecutionTests`` need no
database; the other classes need the disposable PostgreSQL 16 of the CI.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services import upload_provenance as provenance
from app.services.local_files import hash_stable_regular_file
from app.services.upload_snapshot import (
    MULTIPART_THRESHOLD, PartDigests, prepare_upload_snapshot, sha256_b64, upload_part_size,
)
from tests import test_stale_upload_snapshot as stale
from tests import test_upload_provenance_worker as worker_fixtures
from tests.test_scan_fingerprints import _quiet_scan_patches
from tests.test_vault_adoption import _AdoptionFixture
from tests.upload_support import fake_worker

CHANGED = "Local file changed since fingerprinting"
MTIME_NS = 1_700_000_000_123_456_789
# Two parts of 8 MiB: the second differs between A and B, same size.
BIG_A = bytes(range(256)) * (MULTIPART_THRESHOLD // 256) + b"tail of content A"
BIG_B = bytes(range(256)) * (MULTIPART_THRESHOLD // 256) + b"tail of content B"


def _parts(payload: bytes) -> list[bytes]:
    size = upload_part_size(len(payload))
    return [hashlib.sha256(payload[start:start + size]).digest()
            for start in range(0, len(payload), size)]


class PartDigestReadTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "content"

    def test_one_read_gives_the_digest_and_the_upload_snapshot_parts(self) -> None:
        self.path.write_bytes(BIG_A)
        parts = PartDigests()
        digest, info = hash_stable_regular_file(self.path, parts=parts)
        snapshot, _ = prepare_upload_snapshot(self.path, lambda: None)
        self.assertEqual(digest, snapshot.plaintext_sha256)
        self.assertEqual(parts.record(info.st_size), snapshot.local_parts())
        self.assertEqual(parts.record(info.st_size), (MULTIPART_THRESHOLD, _parts(BIG_A)))
        self.assertEqual([sha256_b64(BIG_A[:MULTIPART_THRESHOLD]), sha256_b64(BIG_A[MULTIPART_THRESHOLD:])],
                         list(snapshot.part_checksums))

    def test_exact_multiple_of_the_part_size_has_no_empty_last_part(self) -> None:
        self.path.write_bytes(b"x" * (2 * MULTIPART_THRESHOLD))
        parts = PartDigests()
        _digest, info = hash_stable_regular_file(self.path, parts=parts)
        self.assertEqual(parts.record(info.st_size)[1], _parts(b"x" * (2 * MULTIPART_THRESHOLD)))

    def test_below_8_mib_or_for_another_size_nothing_is_recorded(self) -> None:
        self.path.write_bytes(b"x" * (MULTIPART_THRESHOLD - 1))
        parts = PartDigests()
        _digest, info = hash_stable_regular_file(self.path, parts=parts)
        self.assertIsNone(parts.record(info.st_size))
        self.path.write_bytes(BIG_A)
        hash_stable_regular_file(self.path, parts=parts)
        self.assertIsNone(parts.record(len(BIG_A) + MULTIPART_THRESHOLD))
        self.assertIsNone(PartDigests().record(len(BIG_A)), "nothing read, nothing recorded")


class _Stop(Exception):
    pass


class CatalogSnapshotExecutionTests(unittest.TestCase):
    """The execution seam with stubbed catalog units and transport (no DB)."""

    def setUp(self) -> None:
        from app.services import upload_execution as execution
        from app.services.upload_pipeline import _Pipeline

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = Path(temp.name) / "movie.mkv"
        self.source.write_bytes(BIG_A)
        os.utime(self.source, ns=(MTIME_NS, MTIME_NS))
        self.execution_module = execution
        self.pipeline = _Pipeline(4)
        self.reads, self.sent = [], []
        self.bound = False
        self.fingerprint = self.recorded(BIG_A)
        self.catalog = MagicMock()
        worker = fake_worker()
        worker.safe_local_path.return_value = self.source
        worker.prepare_upload_snapshot.side_effect = self.hash
        worker.catalog_unit.side_effect = (
            lambda job, mutation, shared=False: mutation(self.catalog))
        worker.upload_s3_file.side_effect = self.transport
        self.worker = worker
        self.execution = execution.UploadExecution(worker)
        attempt = SimpleNamespace(id="attempt", token="token")
        for name, value in {"provider_identity": "s3-compatible-test", "prepare": attempt,
                            "mark_send_started": attempt, "validated_vault": {"id": 2},
                            "ReceiptJournal": MagicMock()}.items():
            self.enterContext(patch.object(provenance, name, return_value=value))
        stages = execution.UploadExecution
        self.original_unchanged = stages._unchanged
        for name in ("_load", "_observe", "_unchanged", "_materialize", "_publish", "_complete"):
            self.enterContext(patch.object(stages, name, getattr(self, name.lstrip("_"))))

    def recorded(self, payload: bytes, **changes):
        row = {"generation": 7, "local_size": len(payload), "local_mtime_ns": MTIME_NS,
               "local_sha256": hashlib.sha256(payload).digest(),
               "local_part_size": upload_part_size(len(payload)),
               "local_part_sha256": _parts(payload), "fingerprint_is_current": False}
        row.update(changes)
        return row

    # -- stubs ---------------------------------------------------------------

    def hash(self, source, check_active, **kwargs):
        self.reads.append(self.pipeline.prepare._count)
        return prepare_upload_snapshot(source, check_active, **kwargs)

    def transport(self, transfer_job, *, source, key):
        """Like ``_validated_body``: each part read is checked before it is sent."""
        snapshot = transfer_job["_upload_snapshot"]
        self.sent.append((snapshot, self.pipeline.prepare._count, self.pipeline.network._count))
        data = source.read_bytes()
        for number, expected in enumerate(snapshot.part_checksums, 1):
            start = (number - 1) * snapshot.part_size
            if sha256_b64(data[start:start + snapshot.part_length(number)]) != expected:
                raise RuntimeError(CHANGED)
        return {"VersionId": "landed"}

    def load(self, run):
        run.observed = self.execution_module._regular_stat(run.source)
        run.extra.update(pin={"bucket": "bucket"}, key_never_written=True,
                         cleanup_pending=False, bound=self.bound)

    def observe(self, run):
        run.generation = 7
        run.extra["fingerprint"] = self.fingerprint
        return True

    def unchanged(self, run):
        return False

    def materialize(self, run, attempt, response):
        run.version = self.execution_module._Version(
            "version", "key", response["VersionId"], run.snapshot.size, "verified", None, "token")

    def publish(self, run):
        pass

    def complete(self, run):
        pass

    def run_job(self):
        job = {"id": 1, "vault_id": 2, "vault_uuid": "vault", "s3_key_layout": "vault_prefix", "path": "movie.mkv",
               "file_id": "file", "source_root": "/sources", "origin": "manual"}
        return self.execution._run_claimed(job, self.pipeline, lambda: None)

    # -- behavior ------------------------------------------------------------

    def test_recorded_parts_are_the_snapshot_without_a_preparation_read(self) -> None:
        self.assertEqual(self.run_job().status, "completed")
        self.assertEqual(self.reads, [], "no preparation read")
        (snapshot, readers, sending), = self.sent
        expected, info = prepare_upload_snapshot(self.source, lambda: None)
        self.assertEqual(snapshot, expected)
        self.assertEqual((snapshot.mtime_ns, snapshot.ownership.uid), (MTIME_NS, info.st_uid))
        self.assertEqual((readers, sending), (0, 1), "no reader permit, only the send")
        self.catalog.retire_local_fingerprint.assert_not_called()
        self.worker.set_job.assert_called_once_with(1, "uploading", message_key="job.plain_upload")

    def test_a_part_that_contradicts_them_retires_the_fingerprint(self) -> None:
        self.source.write_bytes(BIG_B)
        os.utime(self.source, ns=(MTIME_NS, MTIME_NS))
        with self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
            self.run_job()
        self.assertEqual(self.reads, [])
        self.catalog.retire_local_fingerprint.assert_called_once_with(
            "file", generation=7, plaintext_sha256=hashlib.sha256(BIG_A).digest(),
            size=len(BIG_A), mtime_ns=MTIME_NS)

    def test_failed_retirement_keeps_the_send_error(self) -> None:
        self.source.write_bytes(BIG_B)
        os.utime(self.source, ns=(MTIME_NS, MTIME_NS))
        self.catalog.retire_local_fingerprint.side_effect = _Stop("database down")
        with self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
            self.run_job()

    def test_other_send_errors_keep_the_fingerprint(self) -> None:
        self.worker.upload_s3_file.side_effect = RuntimeError("provider down")
        with self.assertRaisesRegex(RuntimeError, "provider down"):
            self.run_job()
        self.catalog.retire_local_fingerprint.assert_not_called()

    def test_a_bound_job_or_an_unusable_fingerprint_hashes_the_file(self) -> None:
        cases = {
            "bound to a snapshot": dict(bound=True),
            "no parts": dict(fingerprint=self.recorded(BIG_A, local_part_size=None,
                                                       local_part_sha256=None)),
            "no digest": dict(fingerprint=self.recorded(BIG_A, local_sha256=None)),
            "Current's digest": dict(fingerprint=self.recorded(BIG_A, fingerprint_is_current=True)),
            "another generation": dict(fingerprint=self.recorded(BIG_A, generation=6)),
            "another mtime": dict(fingerprint=self.recorded(BIG_A, local_mtime_ns=MTIME_NS + 1)),
            "another part size": dict(fingerprint=self.recorded(
                BIG_A, local_part_size=2 * MULTIPART_THRESHOLD, local_part_sha256=[b"x" * 32])),
            "wrong part count": dict(fingerprint=self.recorded(
                BIG_A, local_part_sha256=_parts(BIG_A)[:1])),
            "under 8 MiB": dict(size=MULTIPART_THRESHOLD - 1),
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                self.reads.clear()
                self.sent.clear()
                self.bound = case.get("bound", False)
                self.fingerprint = case.get("fingerprint", self.recorded(BIG_A))
                if "size" in case:
                    self.source.write_bytes(b"s" * case["size"])
                    self.fingerprint = self.recorded(b"s" * case["size"])
                    self.fingerprint.update(local_part_size=None, local_part_sha256=None)
                self.assertEqual(self.run_job().status, "completed")
                self.assertEqual(self.reads, [1], "one preparation read, under its permit")
                self.catalog.retire_local_fingerprint.assert_not_called()

    def test_a_whole_read_contradicting_recorded_parts_retires_them(self) -> None:
        # A retry bound to its snapshot rehashes; the stale fingerprint goes
        # even if the retirement after the failed send never committed.
        self.bound = True
        self.source.write_bytes(BIG_B)
        os.utime(self.source, ns=(MTIME_NS, MTIME_NS))
        self.assertEqual(self.run_job().status, "completed")
        self.assertEqual(self.reads, [1])
        self.catalog.retire_local_fingerprint.assert_called_once_with(
            "file", generation=7, plaintext_sha256=hashlib.sha256(BIG_A).digest(),
            size=len(BIG_A), mtime_ns=MTIME_NS)
        self.assertEqual(self.sent[0][0].plaintext_sha256, hashlib.sha256(BIG_B).hexdigest())

    def test_a_catalog_snapshot_never_matches_current_without_reading(self) -> None:
        run = SimpleNamespace(job={"id": 1}, snapshot=object(), observed=object(),
                              extra={"catalog_snapshot": True})
        with patch.object(self.execution, "_db", side_effect=AssertionError("no match unit")):
            self.assertFalse(self.original_unchanged(self.execution, run))


class _ScanFixture:
    """A full scan of an adopted Vault holding a multipart-sized file."""

    def _vault(self) -> dict:
        from app.services.vaults import VAULT_ROW_SQL
        from app.storage import runtime_status
        created = self._adopt()
        self.addCleanup(runtime_status.pop, int(created["id"]), None)
        with self.database.connect() as connection:
            return dict(connection.execute(
                f"{VAULT_ROW_SQL} WHERE v.id=%s", (created["id"],)).fetchone())

    def _scan(self, vault: dict):
        from contextlib import ExitStack
        from app.storage import scan_vault
        with ExitStack() as stack:
            for item in _quiet_scan_patches():
                stack.enter_context(item)
            return scan_vault(dict(vault))

    def _rows(self, vault_id: int) -> dict[str, dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, path, generation, local_size, local_mtime_ns, local_sha256, "
                "local_part_size, local_part_sha256 FROM files WHERE vault_id=%s",
                (vault_id,)).fetchall()
        return {row["path"]: row for row in rows}


class ScanPartDigestTests(_ScanFixture, _AdoptionFixture):
    def setUp(self) -> None:
        super().setUp()
        (self.albums / "movie.mkv").write_bytes(BIG_A)

    def test_scan_publishes_the_parts_of_large_files_only(self) -> None:
        vault = self._vault()
        self._scan(vault)
        rows = self._rows(int(vault["id"]))
        movie, photo = rows["movie.mkv"], rows["photo.jpg"]
        self.assertEqual(bytes(movie["local_sha256"]), hashlib.sha256(BIG_A).digest())
        self.assertEqual(movie["local_part_size"], MULTIPART_THRESHOLD)
        self.assertEqual([bytes(part) for part in movie["local_part_sha256"]], _parts(BIG_A))
        self.assertIsNotNone(photo["local_sha256"])
        self.assertIsNone(photo["local_part_size"])
        self.assertIsNone(photo["local_part_sha256"])

    def test_every_change_of_the_digest_clears_the_parts(self) -> None:
        from app.catalog import ArchiveCatalog
        from app.storage import now_iso
        vault = self._vault()
        self._scan(vault)
        vault_id = int(vault["id"])

        def reset() -> dict:
            row = self._rows(vault_id)["movie.mkv"]
            with self.database.connect() as connection:
                self.assertTrue(ArchiveCatalog(connection).publish_local_fingerprint(
                    str(row["id"]), generation=row["generation"],
                    plaintext_sha256=hashlib.sha256(BIG_A).digest(), size=row["local_size"],
                    mtime_ns=row["local_mtime_ns"], parts=(MULTIPART_THRESHOLD, _parts(BIG_A))))
            row = self._rows(vault_id)["movie.mkv"]
            self.assertIsNotNone(row["local_part_sha256"])
            return row

        def parts() -> tuple:
            row = self._rows(vault_id)["movie.mkv"]
            return row["local_part_size"], row["local_part_sha256"]

        row = reset()
        with self.database.connect() as connection:
            # The same digest without parts: the same bytes, the parts stay.
            ArchiveCatalog(connection).publish_local_fingerprint(
                str(row["id"]), generation=row["generation"],
                plaintext_sha256=hashlib.sha256(BIG_A).digest(), size=row["local_size"],
                mtime_ns=row["local_mtime_ns"])
        self.assertIsNotNone(parts()[1])
        with self.database.connect() as connection:
            # Another digest without parts: they described other bytes.
            ArchiveCatalog(connection).publish_local_fingerprint(
                str(row["id"]), generation=row["generation"],
                plaintext_sha256=hashlib.sha256(BIG_B).digest(), size=row["local_size"],
                mtime_ns=row["local_mtime_ns"])
        self.assertEqual(parts(), (None, None))

        row = reset()
        with self.database.connect() as connection:
            # A new size or mtime forgets the digest and its parts.
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=vault_id, path="movie.mkv", file_type="regular",
                size=row["local_size"], mtime_ns=row["local_mtime_ns"] + 1, observed_at=now_iso())
        self.assertEqual(parts(), (None, None))

        row = reset()
        with self.database.connect() as connection:
            # A content event with the same stat forgets them too.
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=vault_id, path="movie.mkv", file_type="regular",
                size=row["local_size"], mtime_ns=row["local_mtime_ns"], observed_at=now_iso(),
                invalidate_content=True)
        self.assertEqual(parts(), (None, None))

        row = reset()
        with self.database.connect() as connection:
            # Any other writer of the digest: the trigger drops stale parts.
            connection.execute("UPDATE files SET local_sha256=%s WHERE id=%s",
                               (hashlib.sha256(BIG_B).digest(), row["id"]))
        self.assertEqual(parts(), (None, None))

    def test_retirement_is_a_compare_and_swap_on_the_observation_and_digest(self) -> None:
        from app.catalog import ArchiveCatalog
        vault = self._vault()
        self._scan(vault)
        row = self._rows(int(vault["id"]))["movie.mkv"]
        stale = dict(generation=row["generation"], size=row["local_size"],
                     mtime_ns=row["local_mtime_ns"])
        with self.database.connect() as connection:
            catalog = ArchiveCatalog(connection)
            self.assertFalse(catalog.retire_local_fingerprint(
                str(row["id"]), plaintext_sha256=hashlib.sha256(BIG_B).digest(), **stale))
            self.assertFalse(catalog.retire_local_fingerprint(
                str(row["id"]), plaintext_sha256=hashlib.sha256(BIG_A).digest(),
                **{**stale, "generation": row["generation"] + 1}))
        self.assertIsNotNone(self._rows(int(vault["id"]))["movie.mkv"]["local_part_sha256"])
        with self.database.connect() as connection:
            self.assertTrue(ArchiveCatalog(connection).retire_local_fingerprint(
                str(row["id"]), plaintext_sha256=hashlib.sha256(BIG_A).digest(), **stale))
        retired = self._rows(int(vault["id"]))["movie.mkv"]
        self.assertEqual((retired["local_sha256"], retired["local_part_size"],
                          retired["local_part_sha256"], retired["generation"]),
                         (None, None, None, row["generation"]))

    def test_the_schema_refuses_parts_that_cannot_be_the_digest_parts(self) -> None:
        import psycopg
        vault = self._vault()
        self._scan(vault)
        rows = self._rows(int(vault["id"]))
        cases = {
            "no digest": ("UPDATE files SET local_sha256=NULL, local_part_size=%s, "
                          "local_part_sha256=%s WHERE id=%s",
                          (MULTIPART_THRESHOLD, [b"y" * 32, b"z" * 32], rows["movie.mkv"]["id"])),
            "under 8 MiB": ("UPDATE files SET local_part_size=%s, local_part_sha256=%s WHERE id=%s",
                            (MULTIPART_THRESHOLD, [b"x" * 32], rows["photo.jpg"]["id"])),
            "wrong count": ("UPDATE files SET local_part_sha256=%s WHERE id=%s",
                            (_parts(BIG_A)[:1], rows["movie.mkv"]["id"])),
            "size without digests": ("UPDATE files SET local_part_sha256=NULL WHERE id=%s",
                                     (rows["movie.mkv"]["id"],)),
        }
        for name, (statement, params) in cases.items():
            with self.subTest(case=name), self.database.connect() as connection:
                with self.assertRaises(psycopg.errors.CheckViolation):
                    connection.execute(statement, params)


class CatalogPartSnapshotWorkerTests(worker_fixtures.UploadProvenanceWorkerCase):
    """Real admission/claim/process_job with the persistent versioned provider."""

    run_job = stale.StaleUploadSnapshotTests.run_job
    intents = stale.StaleUploadSnapshotTests.intents
    bound = stale.StaleUploadSnapshotTests.bound

    def setUp(self) -> None:
        super().setUp()
        self.reads = 0
        real = prepare_upload_snapshot

        def counting(source, check_active, **kwargs):
            self.reads += 1
            return real(source, check_active, **kwargs)
        self.enterContext(patch("app.storage.prepare_upload_snapshot", side_effect=counting))
        self.provider.hook = self.validate_parts

    def validate_parts(self, stage, **kwargs):
        """Like ``_validated_body``: a part read for the send is checked first."""
        snapshot = kwargs.get("snapshot")
        if stage != "before_put" or snapshot is None:
            return
        data = self.file.read_bytes()
        for number, expected in enumerate(snapshot.part_checksums, 1):
            start = (number - 1) * snapshot.part_size
            if sha256_b64(data[start:start + snapshot.part_length(number)]) != expected:
                raise RuntimeError(CHANGED)

    def scan_fingerprint(self, *, with_parts: bool = True) -> None:
        """What the scan verification publishes for the current observation."""
        from app.catalog import ArchiveCatalog
        parts = PartDigests()
        digest, info = hash_stable_regular_file(self.file, parts=parts)
        with self.database.connect() as connection:
            generation = connection.execute(
                "SELECT generation FROM files WHERE id=%s", (self.file_id,)).fetchone()["generation"]
            self.assertTrue(ArchiveCatalog(connection).publish_local_fingerprint(
                self.file_id, generation=generation, plaintext_sha256=digest, size=info.st_size,
                mtime_ns=info.st_mtime_ns, parts=parts.record(info.st_size) if with_parts else None))

    def fingerprint(self) -> dict:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT local_sha256, local_part_size, local_part_sha256 FROM files WHERE id=%s",
                (self.file_id,)).fetchone()

    def assert_fingerprint(self, payload: bytes | None) -> None:
        row = self.fingerprint()
        if payload is None:
            self.assertEqual(tuple(row.values()), (None, None, None))
            return
        self.assertEqual(bytes(row["local_sha256"]), hashlib.sha256(payload).digest())
        self.assertEqual((row["local_part_size"], [bytes(p) for p in row["local_part_sha256"]]),
                         (upload_part_size(len(payload)), _parts(payload)))

    def test_recorded_parts_upload_without_a_preparation_read(self) -> None:
        self.observe_content(BIG_A, mtime_ns=MTIME_NS)
        self.scan_fingerprint()
        job = self.queue()
        self.run_job(job)
        version = self.assert_keeper(job)
        self.assertEqual(self.reads, 0)
        self.assertEqual(self.provider.puts, 1)
        self.assertEqual(self.provider.objects[version["object_key"], version["provider_version_id"]][0], BIG_A)
        self.assertEqual(bytes(version["plaintext_sha256"]), hashlib.sha256(BIG_A).digest())
        bound = self.bound(job)
        self.assertEqual([bytes(part) for part in bound["part_sha256"]], _parts(BIG_A))
        self.assertEqual((bound["mtime_ns"], bound["owner_uid"]), (MTIME_NS, self.file.stat().st_uid))
        self.assert_fingerprint(BIG_A)

    def test_without_recorded_parts_the_upload_hashes_and_records_them(self) -> None:
        self.observe_content(BIG_A, mtime_ns=MTIME_NS)
        self.scan_fingerprint(with_parts=False)
        job = self.queue()
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual(self.reads, 1)
        self.assert_fingerprint(BIG_A)

    def test_a_failed_send_keeps_the_fingerprint_of_the_whole_read(self) -> None:
        # #766: no scan fingerprint yet; the upload's read is the fingerprint,
        # published before the send, so a failed PUT keeps it.
        from app.services import source_devices

        self.observe_content(BIG_A, mtime_ns=MTIME_NS)

        def provider_down(stage, **kwargs):
            if stage == "before_put":
                raise RuntimeError("provider down")
        self.provider.hook = provider_down
        job = self.queue()
        self.run_job(job)
        row = self.row(job)
        self.assertIn(row["status"], {"retrying", "failed"}, row["message"])
        self.assertEqual((self.reads, self.provider.puts), (1, 0))
        self.assert_fingerprint(BIG_A)
        with self.database.connect() as connection:
            generation = connection.execute(
                "SELECT generation FROM files WHERE id=%s", (self.file_id,)).fetchone()["generation"]
        self.assertTrue(source_devices.upload_fingerprints().claim({
            "vault_file_id": str(self.file_id), "generation": generation,
            "size": len(BIG_A), "mtime_ns": MTIME_NS}))

    def test_contradicted_parts_retire_the_fingerprint_and_the_retry_rehashes(self) -> None:
        self.observe_content(BIG_A, mtime_ns=MTIME_NS)
        self.scan_fingerprint()
        # New content under the same size and mtime: the fingerprint is stale.
        self.file.write_bytes(BIG_B)
        os.utime(self.file, ns=(MTIME_NS, MTIME_NS))
        job = self.queue()

        self.run_job(job)
        row = self.row(job)
        self.assertEqual((row["status"], row["retry_count"]), ("retrying", 1), row["message"])
        self.assertEqual((self.reads, self.provider.puts), (0, 0))
        self.assert_fingerprint(None)
        (started,) = self.intents()
        self.assertTrue(started["send_started"])
        self.assertEqual(started["snapshot_sha256"], hashlib.sha256(BIG_A).hexdigest())

        # The retry reconciles the started send (proven absent), hashes the
        # whole file and closes snapshot A (ADR-0030/0036).
        self.run_job(job)
        row = self.row(job)
        self.assertEqual((row["status"], row["retry_count"]), ("retrying", 2), row["message"])
        self.assertEqual((self.reads, self.provider.puts), (1, 0))
        self.assertIsNone(self.bound(job))
        # That whole, stable read of B is its fingerprint (#766).
        self.assert_fingerprint(BIG_B)

        # The next one uploads B from it without reading again; snapshot A is
        # never offered again.
        self.run_job(job)
        version = self.assert_keeper(job)
        self.assertEqual((self.reads, self.provider.puts), (1, 1))
        self.assertEqual(self.provider.objects[version["object_key"], version["provider_version_id"]][0], BIG_B)
        self.assertEqual([i["snapshot_sha256"] for i in self.intents()],
                         [hashlib.sha256(BIG_A).hexdigest(), hashlib.sha256(BIG_B).hexdigest()])
        self.assert_fingerprint(BIG_B)


if __name__ == "__main__":
    unittest.main()
