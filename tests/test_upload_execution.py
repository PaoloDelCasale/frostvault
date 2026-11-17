"""Upload protocol's DB-only internal seams; real Job/provider cases live in
 test_upload_provenance_worker (mandatory disposable PostgreSQL16 in CI).
"""
import json
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import psycopg

from app.services import upload_provenance as provenance
from app.services.upload_pipeline import ACCOUNTED_CHUNK


class ReceiptJournalTests(unittest.TestCase):
    def test_callback_then_return_appends_the_same_committed_fact_once(self):
        journal = provenance.ReceiptJournal(MagicMock(), "intent", lambda: "now")
        with patch.object(provenance, "append_receipt") as append:
            journal({"VersionId": "exact", "ETag": "etag", "ResponseMetadata": {"ignored": True}})
            journal({"ETag": "etag", "VersionId": "exact"})
            append.assert_called_once()
            self.assertEqual(append.call_args.args[2], {"ETag": "etag", "VersionId": "exact"})

    def test_ambiguous_commit_is_retried_on_late_ack_without_claim_or_put(self):
        journal = provenance.ReceiptJournal(MagicMock(), "intent", lambda: "now")
        with patch.object(provenance, "append_receipt", side_effect=[psycopg.OperationalError("commit uncertain"), None]) as append:
            with self.assertRaises(psycopg.OperationalError):
                journal({"VersionId": "exact"})
            journal({"VersionId": "exact"})
            journal({"VersionId": "exact"})
            self.assertEqual(append.call_count, 2)

    def test_a_different_fact_is_never_deduplicated_by_version_id_alone(self):
        journal = provenance.ReceiptJournal(MagicMock(), "intent", lambda: "now")
        with patch.object(provenance, "append_receipt") as append:
            journal({"VersionId": "exact", "ChecksumSHA256": "a"})
            journal({"VersionId": "exact", "ChecksumSHA256": "b"})
            self.assertEqual(append.call_count, 2)


class JournalAdmissionTests(unittest.TestCase):
    def setUp(self):
        from app import storage
        from app.services.upload_execution import UploadExecution
        from app.services.upload_pipeline import _Pipeline
        from app.services.upload_snapshot import prepare_upload_snapshot
        from tests.upload_provenance_provider import VersionedProvider

        provider = VersionedProvider()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "file"
            source.write_bytes(b"frozen content")
            snapshot, _ = prepare_upload_snapshot(source, lambda: None)
        provider.add("old/file", b"frozen content", {provenance.TOKEN_KEY: "intent-token"})
        intent = {"id": "intent", "send_started": True, "cancelled": True,
            "target_json": json.dumps({"provider": provenance.provider_identity(provider),
                "bucket": "bucket", "key": "old/file"}),
            "inventory_json": json.dumps({"versions": [], "markers": []}),
            "snapshot_json": snapshot.as_json(), "token": "intent-token"}
        worker = MagicMock()
        worker.OperationCancelled, worker.JobLeaseLost = storage.OperationCancelled, storage.JobLeaseLost
        connection = worker.db.return_value.__enter__.return_value
        def query(sql, *args):
            result = MagicMock()
            result.fetchone.return_value = None if "upload_receipts" in sql else intent
            result.fetchall.return_value = []
            return result
        connection.execute.side_effect = query
        worker.s3_client.return_value = provider
        coordinator = _Pipeline(4)
        coordinator.journal_failed("intent", "lost-fact")
        self.worker, self.provider, self.coordinator = worker, provider, coordinator
        self.execution = UploadExecution(worker)

    def test_reachable_journal_does_not_wait_for_reconciliation_of_an_active_intent(self):
        query = self.worker.db.return_value.__enter__.return_value.execute.side_effect
        def active_intent(sql, *args):
            result = query(sql, *args)
            if "FROM upload_intents" in sql:
                result.fetchone.return_value = {**result.fetchone.return_value, "cancelled": False}
            return result
        self.worker.db.return_value.__enter__.return_value.execute.side_effect = active_intent
        self.execution._refresh_journal_gate(self.coordinator)
        self.coordinator.require_send_admission()
        self.assertEqual(self.provider.puts, 0)
        self.worker._resume_materialized_upload.assert_not_called()

    def test_cancelled_intent_cannot_force_provider_reads_on_other_sends(self):
        self.worker.s3_client.side_effect = AssertionError("Admission must be DB-only, even for cancelled intents")
        with patch.object(provenance, "append_receipt") as append:
            self.execution._refresh_journal_gate(self.coordinator)
            self.coordinator.require_send_admission()
            append.assert_not_called()
        self.worker._publish_upload_receipt.assert_not_called()
        self.worker._resume_materialized_upload.assert_not_called()
        self.assertEqual((self.provider.puts, self.provider.deletes, self.provider.aborted), (0, 0, []))

    def test_failed_probe_or_commit_keeps_send_admission_closed(self):
        from app.services.upload_pipeline import UploadJournalHeld
        for stage in ("probe", "commit"):
            with self.subTest(stage=stage):
                context = self.worker.db.return_value
                context.__exit__.side_effect = psycopg.OperationalError("unknown commit") if stage == "commit" else None
                with patch.object(provenance, "probe_receipt_journal",
                                  side_effect=psycopg.OperationalError("write unavailable") if stage == "probe" else None):
                    with self.assertRaises(UploadJournalHeld):
                        self.execution._refresh_journal_gate(self.coordinator)
                with self.assertRaises(UploadJournalHeld):
                    self.coordinator.require_send_admission()
        self.worker.s3_client.assert_not_called()

    def test_same_fact_failing_again_during_probe_keeps_the_new_hold(self):
        with patch.object(provenance, "probe_receipt_journal", side_effect=lambda connection:
                          self.coordinator.journal_failed("intent", "lost-fact")):
            self.execution._refresh_journal_gate(self.coordinator)
        with self.assertRaises(psycopg.OperationalError):
            self.coordinator.require_send_admission()

    def test_another_failed_late_fact_during_probe_is_not_cleared(self):
        with patch.object(provenance, "probe_receipt_journal", side_effect=lambda connection:
                          self.coordinator.journal_failed("intent", "different-late-fact")):
            self.execution._refresh_journal_gate(self.coordinator)
        self.assertEqual(self.coordinator.uncertain_journals(), (("intent", "different-late-fact"),))
        with self.assertRaises(psycopg.OperationalError):
            self.coordinator.require_send_admission()


class _Stop(BaseException):
    """Neither Exception nor cancellation: only ``finally`` sees it."""


class SinglePutPayloadTests(unittest.TestCase):
    """#702 at the execution seam: below 8 MiB one read feeds hash and PUT.

    Catalog units are stubbed (the real-PG suite covers them); the hashing
    pass, the stage capacities and the small-PUT transport are the real ones.
    """

    def setUp(self):
        from app import storage
        from app.services import upload_execution as execution
        from app.services.s3_upload import upload_file
        from app.services.upload_pipeline import _Pipeline
        from app.services.upload_snapshot import prepare_upload_snapshot

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = Path(temp.name) / "report.txt"
        self.source.write_bytes(b"snapshot A")
        self.storage, self.execution_module = storage, execution
        self.pipeline = _Pipeline(4)
        self.events, self.puts, self.match = [], [], False
        self.put_error: BaseException | None = None
        self.prepare = prepare_upload_snapshot
        worker = MagicMock()
        worker.OperationCancelled, worker.JobLeaseLost = storage.OperationCancelled, storage.JobLeaseLost
        worker.safe_local_path.return_value = self.source
        worker.prepare_upload_snapshot.side_effect = self.hash
        worker._upload_catalog_unit.side_effect = (
            lambda job, mutation, shared=False: mutation(SimpleNamespace(connection=MagicMock())))

        def put(**kwargs):
            self.events.append(("put", self.held()))
            if self.put_error is not None:
                raise self.put_error
            self.puts.append(kwargs["Body"].read())
            return {"VersionId": "landed"}
        client = SimpleNamespace(put_object=put)
        worker.upload_s3_file.side_effect = lambda transfer_job, *, source, key: upload_file(
            client, source=source, bucket="bucket", key="key",
            snapshot=transfer_job["_upload_snapshot"], payload=transfer_job.get("_upload_payload"),
            metadata=transfer_job["_upload_metadata"],
            on_acknowledged=transfer_job["_on_upload_acknowledged"],
            check_active=transfer_job["_upload_check_active"], on_progress=lambda _: None)
        self.worker = worker
        self.execution = execution.UploadExecution(worker)
        attempt = SimpleNamespace(id="attempt", token="token")
        for name, value in {"provider_identity": "s3-compatible-test", "prepare": attempt,
                            "mark_send_started": attempt, "validated_vault": {"id": 2},
                            "ReceiptJournal": MagicMock()}.items():
            self.enterContext(patch.object(provenance, name, return_value=value))
        stages = execution.UploadExecution
        for name in ("_load", "_observe", "_unchanged", "_materialize", "_publish", "_complete"):
            self.enterContext(patch.object(stages, name, getattr(self, name.lstrip("_"))))
        # Every open of the Local Copy, by the hashing pass or the transport.
        self.opened = []
        real_os_open, real_io_open = os.open, io.open
        def os_open(path, *args, **kwargs):
            if Path(path) == self.source:
                self.opened.append("os.open")
            return real_os_open(path, *args, **kwargs)
        def io_open(path, *args, **kwargs):
            if isinstance(path, (str, Path)) and Path(path) == self.source:
                self.opened.append("io.open")
            return real_io_open(path, *args, **kwargs)
        self.enterContext(patch("os.open", side_effect=os_open))
        self.enterContext(patch("io.open", side_effect=io_open))
        self.enterContext(patch("app.services.s3_upload.validated_snapshot_buffer",
                                side_effect=AssertionError("second read of the source")))

    # -- stubbed stages ------------------------------------------------------

    def held(self):
        """(buffer permits, buffer bytes, reader permits, send-count permits)."""
        p = self.pipeline
        return (p._buffers._count, p._buffers._bytes, p.prepare._count, p.network._count)

    def hash(self, source, check_active, **kwargs):
        self.events.append(("read", self.held(), "payload" in kwargs))
        return self.prepare(source, check_active, **kwargs)

    def load(self, run):
        run.observed = self.execution_module._regular_stat(run.source)
        run.extra.update(pin={"bucket": "bucket"}, key_never_written=True, cleanup_pending=False)

    def observe(self, run):
        run.generation = 1
        return True

    def unchanged(self, run):
        self.events.append(("unchanged", self.held()))
        if isinstance(self.match, BaseException):
            raise self.match
        return self.match

    def materialize(self, run, attempt, response):
        self.events.append(("materialize", self.held()))
        run.version = self.execution_module._Version(
            "version", "key", response["VersionId"], run.snapshot.size, "verified", None, "token")

    def publish(self, run):
        pass

    def complete(self, run):
        pass

    def run_job(self, origin="manual"):
        job = {"id": 1, "vault_id": 2, "vault_uuid": "vault", "s3_key_layout": "vault_prefix", "path": "report.txt",
               "source_root": "/sources", "origin": origin}
        return self.execution._run_claimed(job, self.pipeline, lambda: None)

    def assert_released(self):
        self.assertEqual(self.held(), (0, 0, 0, 0))
        for gate in (self.pipeline._buffers, self.pipeline.prepare, self.pipeline.network,
                     self.pipeline._large_buffers):
            self.assertEqual(len(gate._waiting), 0)

    # -- behavior ------------------------------------------------------------

    def test_small_file_is_opened_and_read_once_and_its_hashed_bytes_are_sent(self):
        weight = len(b"snapshot A") + 2 * ACCOUNTED_CHUNK
        outcome = self.run_job()
        self.assertEqual((outcome.status, outcome.archive_version_id), ("completed", "version"))
        self.assertEqual(self.opened, ["os.open"], "one open of the Local Copy per upload")
        self.assertEqual(self.puts, [b"snapshot A"])
        self.assertEqual(self.events, [
            # The buffer is reserved before the read, the reader permit during it.
            ("read", (1, weight, 1, 0), True),
            ("unchanged", (1, weight, 0, 0)),
            # The send count only around the PUT.
            ("put", (1, weight, 0, 1)),
            # PUT and immediate journal done: nothing is held for materialization.
            ("materialize", (0, 0, 0, 0)),
        ])
        self.assert_released()

    def test_a_file_changing_during_the_read_is_never_uploaded(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                self.source.write_bytes(b"snapshot A" * ACCOUNTED_CHUNK)
                self.events.clear()
                def changing(source, check_active, **kwargs):
                    calls = 0
                    def active():
                        nonlocal calls
                        check_active()
                        calls += 1
                        if calls == 2:  # After the open, before the first chunk.
                            if replace:
                                other = source.with_name("replacement")
                                other.write_bytes(b"snapshot B" * ACCOUNTED_CHUNK)
                                other.replace(source)
                            else:
                                source.write_bytes(b"snapshot B, longer" * ACCOUNTED_CHUNK)
                    return self.hash(source, active, **kwargs)
                self.worker.prepare_upload_snapshot.side_effect = changing
                with self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
                    self.run_job()
                self.assertEqual(self.puts, [])
                provenance.mark_send_started.assert_not_called()
                self.assertEqual([event[0] for event in self.events], ["read"])
                self.assert_released()

    def test_payload_reservation_is_released_on_every_exit(self):
        weight = len(b"snapshot A") + 2 * ACCOUNTED_CHUNK
        cases = {
            "match_with_current": dict(match=True),
            "automatic_gate": dict(origin="automatic"),
            "cancellation": dict(match=self.storage.OperationCancelled("stopped")),
            "base_exception": dict(match=_Stop()),
            "send_error": dict(put_error=RuntimeError("provider down")),
            "send_cancelled": dict(put_error=self.storage.OperationCancelled("stopped")),
        }
        gate_seen = []
        def gate(job, *, after_hash=False):
            if after_hash and job["origin"] == "automatic":
                gate_seen.append(self.held())
                return self.execution_module.UploadOutcome(int(job["id"]), "queued")
            return None
        for name, case in cases.items():
            with self.subTest(exit=name), patch.object(
                    self.execution_module.UploadExecution, "_automatic_gate", side_effect=gate):
                self.events.clear()
                self.match, self.put_error = case.get("match", False), case.get("put_error")
                if name == "match_with_current":
                    self.assertEqual(self.run_job().status, "completed")
                elif name == "automatic_gate":
                    self.assertEqual(self.run_job(origin="automatic").status, "queued")
                    self.assertEqual(gate_seen, [(1, weight, 0, 0)])
                else:
                    expected = type(case.get("match") or case.get("put_error"))
                    with self.assertRaises(expected):
                        self.run_job()
                if "put_error" in case:
                    self.assertEqual(self.events[-1], ("put", (1, weight, 0, 1)))
                elif name != "automatic_gate":
                    self.assertEqual(self.events[-1], ("unchanged", (1, weight, 0, 0)))
                self.assertEqual(self.puts, [])
                self.assert_released()

    def test_files_from_8_mib_keep_hashing_then_send_buffers(self):
        from app.services.upload_snapshot import MULTIPART_THRESHOLD
        self.source.write_bytes(b"a" * MULTIPART_THRESHOLD)
        sent = []
        def transport(transfer_job, *, source, key):
            sent.append(("_upload_payload" in transfer_job, self.held()))
            return {"VersionId": "landed"}
        self.worker.upload_s3_file.side_effect = transport
        self.assertEqual(self.run_job().status, "completed")
        snapshot_weight = 2 * (8 * ACCOUNTED_CHUNK + ACCOUNTED_CHUNK)
        self.assertEqual(self.events, [
            ("read", (0, 0, 1, 0), False),
            ("unchanged", (0, 0, 0, 0)),
            ("materialize", (0, 0, 0, 0)),
        ])
        self.assertEqual(sent, [(False, (1, snapshot_weight, 0, 1))])
        self.assert_released()


class TargetCapabilityTests(unittest.TestCase):
    def test_target_is_reused_only_inside_the_same_connection_job_and_scope(self):
        job = {"id": 1, "vault_id": 2, "vault_file_id": "file", "path": "file"}
        pinned = {"vault_id": 2, "vault_file_id": "file", "path": "file", "key": "key", "provider": "provider"}
        connection = MagicMock()
        with patch.object(provenance, "_target", return_value=(pinned, {})) as target:
            with provenance._validated_target(connection, job, key="key", provider="provider", shared=True):
                provenance.require_target(connection, job, pinned, shared=True)
                provenance.require_target(connection, job, pinned, shared=True)
                self.assertEqual(target.call_count, 1)
                provenance.require_target(MagicMock(), job, pinned, shared=True)
                self.assertEqual(target.call_count, 2)
                provenance.require_target(connection, {**job, "id": 3}, pinned, shared=True)
                self.assertEqual(target.call_count, 3)
                with self.assertRaises(provenance.UploadBlocked):
                    provenance.require_target(connection, job, {**pinned, "path": "changed"}, shared=True)
            provenance.require_target(connection, job, pinned, shared=True)
            self.assertEqual(target.call_count, 4, "No capability survives the DB unit")

    def test_share_capability_rejects_an_unaudited_exclusive_nested_check(self):
        job = {"id": 1}
        pinned = {"key": "key", "provider": "provider"}
        connection = MagicMock()
        with patch.object(provenance, "_target", return_value=(pinned, {})):
            with provenance._validated_target(connection, job, key="key", provider="provider", shared=True):
                with self.assertRaisesRegex(RuntimeError, "upgrade"):
                    provenance.require_target(connection, job, pinned)

    def test_context_is_revoked_on_rollback(self):
        connection, job = MagicMock(), {"id": 1}
        pinned = {"key": "key", "provider": "provider"}
        with patch.object(provenance, "_target", return_value=(pinned, {})) as target:
            with self.assertRaises(RuntimeError):
                with provenance._validated_target(connection, job, **pinned):
                    raise RuntimeError("rollback")
            provenance.require_target(connection, job, pinned)
            self.assertEqual(target.call_count, 2)
