"""Disposable uvicorn fixture: real lifespan/HTTP/SSE/SDK, no DB/network S3.

Only startup dependencies, authorization/journal I/O and worker admission are
replaced. The upload uses the real pinned TransferManager with a stubbed PUT.
"""
from contextlib import ExitStack, asynccontextmanager
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import boto3
from botocore.config import Config
from botocore.stub import Stubber
from fastapi import FastAPI, Request

from app import main, storage
from app.services.catalog_event_hub import catalog_event_hub
from app.services.catalog_event_stream import StreamTickSnapshot
from app.services.s3_upload import upload_file
from app.services.shutdown import shutdown_state
from app.services.upload_snapshot import prepare_upload_snapshot


async def idle_producer():
    try:
        await asyncio.Event().wait()
    finally:
        print("fixture: producer stopped", flush=True)


@asynccontextmanager
async def lifespan(app):
    with ExitStack() as patches:
        for name in (
            "validate_settings", "prepare_sources_layout",
            "initialize_database", "reconcile_source_volume_identities",
            "validate_nested_mounts_after_identity", "cleanup_abandoned_restore_files",
            "reconcile_interrupted_jobs", "vault_relocation_service.reconcile_vault_root_identities",
            "protection_stop_service.reconcile_all",
        ):
            patches.enter_context(patch(f"app.main.{name}"))
        for name in ("background_loop", "auto_upload_loop", "cloud_preflight_loop",
                     "upload_remediation_loop", "history_retention_loop",
                     "filesystem_watch_loop"):
            patches.enter_context(patch(f"app.main.{name}", idle_producer))
        patches.enter_context(patch("app.main.settings", SimpleNamespace(filesystem_watch_enabled=True)))
        patches.enter_context(patch("app.services.catalog_event_stream.stream_tick_snapshot", return_value=StreamTickSnapshot(
            ok=True, session_vault_id=1,
            signal={"vault_id": 1, "revision": 0, "domains": [], "has_gap": False},
        )))
        # A long cadence proves shutdown wakes SSE rather than waiting for its
        # heartbeat. Keep the actual route and iterator in the request path.
        original_iter = main.iter_catalog_event_sse
        def long_poll(**kwargs):
            return original_iter(**kwargs, durable_poll_seconds=3600)
        patches.enter_context(patch("app.main.iter_catalog_event_sse", long_poll))
        with tempfile.TemporaryDirectory() as directory:
            active = queued = None
            receipts = []
            async with main.lifespan(app):
                mode = os.environ.get("FROSTVAULT_SHUTDOWN_FIXTURE_WORKER")
                if mode in {"stuck-operation", "stuck-default"}:
                    entered = threading.Event()
                    def stuck():
                        entered.set()
                        threading.Event().wait(60)
                    if mode == "stuck-operation":
                        pool = storage._operation_pool(1)
                        storage._operation_futures[1] = pool.submit(stuck)
                    else:
                        task = asyncio.create_task(asyncio.to_thread(stuck))
                        shutdown_state.register_producers([task])
                    assert await asyncio.to_thread(entered.wait, 3), "stuck worker never started"
                if mode == "upload":
                    source = Path(directory) / "snapshot"
                    source.write_bytes(b"disposable upload snapshot")
                    snapshot, _ = prepare_upload_snapshot(source, lambda: None)
                    client = boto3.client("s3", region_name="us-east-1",
                        aws_access_key_id="test", aws_secret_access_key="test",
                        config=Config(retries={"mode": "standard", "total_max_attempts": 1}))
                    stub = patches.enter_context(Stubber(client))
                    stub.add_response("put_object", {"VersionId": "late-receipt", "ChecksumSHA256": snapshot.checksum_sha256})
                    entered = threading.Event()
                    put = client.put_object
                    def delayed_put(**kwargs):
                        entered.set()
                        until = time.monotonic() + 10
                        while not shutdown_state.requested and time.monotonic() < until:
                            time.sleep(0.01)
                        # Complete AFTER cancellation while the real manager
                        # joins requests, retaining receipt evidence, not trust.
                        time.sleep(0.2)
                        return put(**kwargs)
                    patches.enter_context(patch.object(client, "put_object", delayed_put))
                    patches.enter_context(patch("app.storage._runtime_settings", return_value=SimpleNamespace()))
                    patches.enter_context(patch("app.storage._load_claimed_job_status", return_value={"status": "queued"}))
                    terminal = patches.enter_context(patch("app.storage.set_job", side_effect=AssertionError("shutdown must not mark Job cancelled/failed")))
                    def upload(job):
                        upload_file(client, source=source, bucket="bucket", key="key", snapshot=snapshot,
                            check_active=lambda: storage.ensure_job_active(job["id"], "stopped"),
                            on_progress=lambda _: None, on_acknowledged=receipts.append)
                    patches.enter_context(patch("app.storage.process_upload", upload))
                    pool = storage._operation_pool(1)
                    with storage._operation_lock:
                        active = pool.submit(storage.process_job, {"id": 1, "action": "upload"})
                        storage._operation_futures[1] = active
                        queued = pool.submit(lambda: (_ for _ in ()).throw(AssertionError("queued work started during drain")))
                        storage._operation_futures[2] = queued
                    assert await asyncio.to_thread(entered.wait, 3), "PUT never started"
                yield
            if active is not None:
                assert active.done() and active.result() is False, "worker did not drain"
                assert queued.cancelled(), "queued worker was not cancelled"
                assert receipts and receipts[0]["VersionId"] == "late-receipt", "late receipt lost"
                terminal.assert_not_called()
                stub.assert_no_pending_responses()
                print("fixture: upload drained; late receipt retained; queued work cancelled", flush=True)
    assert catalog_event_hub.subscriber_count() == 0, "SSE subscriber leaked"
    print("fixture: lifespan shutdown complete", flush=True)


app = FastAPI(lifespan=lifespan)


@app.get("/api/catalog/events")
async def events(request: Request):
    request.state.session = {"id": "disposable-session"}
    return await main.catalog_events_stream(request, after_revision=0, subscribe=True,
        last_event_id=None, vault={"id": 1}, user={"id": 1})
