"""The worker seam of the Upload/copy Modules and its production Adapter (#793).

``app.services.worker_seam`` declares what Upload, copy and cleanup ask of the
storage worker; ``storage.worker_adapter`` answers it. Pinned here: the Adapter
offers every declared name with the declared signature, resolves the storage
function when called (a patched ``app.storage.X`` runs), is one stateless
object, and the fake Adapter of the tests has the same names and no other.
"""
from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch, sentinel

from app import storage
from app.services import worker_seam
from app.services.shutdown import OperationCancelled, WorkerStopping
from tests.upload_support import fake_worker

_SEAMS = (worker_seam.JobClaim, worker_seam.SourceVolume, worker_seam.UploadCatalog,
          worker_seam.AutomaticUploadGate, worker_seam.UploadTransport)


def _methods(seam: type) -> dict[str, inspect.Signature]:
    return {name: inspect.signature(member) for name, member in vars(seam).items()
            if inspect.isfunction(member) and not name.startswith("_")}


class WorkerAdapterTests(unittest.TestCase):
    def test_the_adapter_offers_every_declared_name_with_its_signature(self) -> None:
        adapter = type(storage.worker_adapter)
        for seam in _SEAMS:
            for name, declared in _methods(seam).items():
                with self.subTest(seam=seam.__name__, name=name):
                    offered = inspect.signature(getattr(adapter, name))
                    self.assertEqual(
                        [(p.name, p.kind, p.default) for p in offered.parameters.values()],
                        [(p.name, p.kind, p.default) for p in declared.parameters.values()])
            for name in seam.__annotations__:
                with self.subTest(seam=seam.__name__, name=name):
                    self.assertTrue(hasattr(adapter, name))

    def test_the_adapter_offers_only_the_seam(self) -> None:
        declared = {name for seam in _SEAMS for name in (*_methods(seam), *seam.__annotations__)}
        offered = {name for name in vars(type(storage.worker_adapter)) if not name.startswith("_")}
        self.assertEqual(offered, declared)

    def test_stops_and_states_are_the_workers_own(self) -> None:
        adapter = storage.worker_adapter
        self.assertIs(adapter.OperationCancelled, OperationCancelled)
        self.assertIs(adapter.WorkerStopping, WorkerStopping)
        self.assertIs(adapter.JobLeaseLost, storage.JobLeaseLost)
        self.assertIs(adapter.UploadSourceRemoved, storage.UploadSourceRemoved)
        self.assertEqual(
            (adapter.UPLOAD_READY, adapter.UPLOAD_SOURCE_REMOVED, adapter.UPLOAD_OBSERVATION_WITHDRAWN),
            (storage.UPLOAD_READY, storage.UPLOAD_SOURCE_REMOVED, storage.UPLOAD_OBSERVATION_WITHDRAWN))

    def test_a_patched_storage_function_is_the_one_that_runs(self) -> None:
        adapter = storage.worker_adapter
        job = {"id": 7}
        with patch("app.storage.now_iso", return_value="patched"), \
                patch("app.storage._upload_catalog_unit", return_value=sentinel.unit) as unit, \
                patch("app.storage.s3_client", return_value=sentinel.client) as client, \
                patch("app.storage.prepare_upload_snapshot", return_value=sentinel.snapshot) as prepare:
            self.assertEqual(adapter.now_iso(), "patched")
            self.assertIs(adapter.catalog_unit(job, sentinel.mutation, shared=True), sentinel.unit)
            self.assertIs(adapter.s3_client(), sentinel.client)
            self.assertIs(adapter.prepare_upload_snapshot(sentinel.source, sentinel.check),
                          sentinel.snapshot)
        unit.assert_called_once_with(job, sentinel.mutation, shared=True)
        client.assert_called_once_with()
        # Without a payload the hashing pass is still called with two arguments.
        prepare.assert_called_once_with(sentinel.source, sentinel.check)

    def test_the_local_stop_check_is_shutdown_then_this_process_cancellation(self) -> None:
        adapter = storage.worker_adapter
        self.addCleanup(storage.shutdown_state.reset)
        with patch("app.storage.cancelled_jobs", {7}):
            self.assertTrue(adapter.stopped_in_process(7))
            self.assertFalse(adapter.stopped_in_process(8))
            storage.shutdown_state.request()
            with self.assertRaises(WorkerStopping) as stopped:
                adapter.stopped_in_process(8)
            # The same stop as the shutdown checkpoint it stands for.
            with self.assertRaises(WorkerStopping) as checkpoint:
                storage.shutdown_state.checkpoint()
            self.assertEqual(str(stopped.exception), str(checkpoint.exception))

    def test_the_adapter_is_one_stateless_object(self) -> None:
        self.assertEqual(type(storage.worker_adapter).__slots__, ())
        with self.assertRaises(AttributeError):
            storage.worker_adapter.anything = 1


class FakeWorkerTests(unittest.TestCase):
    def test_the_fake_has_the_seam_names_and_no_other(self) -> None:
        worker = fake_worker()
        self.assertIs(worker.JobLeaseLost, storage.JobLeaseLost)
        self.assertEqual(worker.UPLOAD_READY, storage.UPLOAD_READY)
        worker.catalog_unit.return_value = sentinel.unit
        self.assertIs(worker.catalog_unit({"id": 1}, None), sentinel.unit)
        for name in ("_upload_catalog_unit", "shutdown_state", "process_job"):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                getattr(worker, name)


if __name__ == "__main__":
    unittest.main()
