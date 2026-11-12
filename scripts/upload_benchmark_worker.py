"""One isolated sample. Invoked by upload_benchmark.py, not a production tool."""
from __future__ import annotations

import hashlib
import json
import statistics
import sys
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if __package__:
    from .upload_benchmark import worker_env
    from .upload_benchmark_support import MemoryS3, Probe
else:
    from upload_benchmark import worker_env
    from upload_benchmark_support import MemoryS3, Probe


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def distribution(values):
    ordered = sorted(values)
    require(bool(ordered), "Empty latency sample")
    return {"min": ordered[0], "median": statistics.median(ordered),
            "p95": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
            "max": ordered[-1], "mean": statistics.mean(ordered),
            "stdev": statistics.stdev(ordered) if len(ordered) > 1 else 0}


def run_sample(config):
    worker_env()  # Revalidate loopback/configuration even if invoked without the supervisor.
    require(config["mode"] in {"s3_checksum", "read_back"}, "Unknown verification mode")
    require(1 <= config["concurrency"] <= 16, "Invalid concurrency")
    require(config["concurrency"] <= config["files"] <= 128, "Invalid measured file count")
    require(config["concurrency"] <= config["warmup"] <= 32, "Invalid warmup file count")
    repo = Path(config["repo"]).resolve()
    sys.path.insert(0, str(repo))
    from app import database, storage
    from app.catalog import ArchiveCatalog
    from app.services import directory_aggregates as aggregates
    from app.services.upload_snapshot import UploadSnapshot
    from app.services.vault_relocation import enroll_vault_root_identity
    from tests.postgres import PostgresConnection, database_dsn, run_alembic
    import app
    require(Path(app.__file__).resolve().is_relative_to(repo), "Wrong app imported")
    require(storage.settings.operation_concurrency == config["concurrency"], "Effective concurrency drift")
    require(storage.settings.bandwidth_limit_kibps in {None, 0}, "Bandwidth limit must be disabled")
    require(storage.settings.upload_verification == config["mode"], "Default verification mode drift")
    count, concurrency = config["files"], config["concurrency"]
    # The *same* harness runs against fixed v0.5.5 as well as the new
    # baseline. This is explicit historical fixture setup, never a production
    # schema adapter; keep the old sample's requester FK cost unchanged.
    legacy_owner_fixture = database.HEAD_SCHEMA_REVISION == "0072_upload_lineage_indexes"
    provider, probe = MemoryS3(), Probe()
    setup_started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="fv-upload-benchmark-") as temporary, ExitStack() as stack:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        identity = root / "catalog"
        migration_started = time.perf_counter()
        migration = run_alembic(identity)
        require(migration.returncode == 0, "Disposable fixture migration failed")
        migration_seconds = time.perf_counter() - migration_started
        dsn = database_dsn(identity)
        with PostgresConnection(identity) as connection:
            version = int(connection.execute("SHOW server_version_num").fetchone()["server_version_num"])
            durability = {key: connection.execute(f"SHOW {key}").fetchone()[key]
                          for key in ("fsync", "synchronous_commit", "full_page_writes")}
            require(version // 10000 == 16, "PG16 is required")
            require(all(value == "on" for value in durability.values()), "Durability must stay on")
            if legacy_owner_fixture:
                connection.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES(1,'owner','Owner','test-only')")
            else:
                connection.execute("INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'test-only')")
            connection.execute("""INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix)
                VALUES(2,'docs','Docs',%s,'benchmark-only','docs')""", (str(source),))
            enroll_vault_root_identity(connection, 2, str(source))
        stack.enter_context(patch.object(database, "settings", SimpleNamespace(
            database_dsn=dsn, operation_concurrency=concurrency)))
        # Readiness is an explicitly prevalidated test fixture, not timed bucket
        # provisioning/probing. Source identity, claims, proof and cleanup are real.
        stack.enter_context(patch.object(storage, "validate_cloud_vault", lambda *a, **k: None))
        aggregates.set_maintenance_scheduling_enabled(False)
        stack.callback(aggregates.set_maintenance_scheduling_enabled, True)
        stack.callback(database.close_pools)
        stack.enter_context(provider.installed())

        def seed(number, prefix):
            jobs, expected = [], {}
            with PostgresConnection(identity) as connection:
                catalog = ArchiveCatalog(connection)
                for index in range(number):
                    path = f"{prefix}/{index % 8:02d}/{index:04d}.bin"
                    target = source / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Deterministic, unique per path, exactly 10 KiB. Every sample
                    # uses newly created files and fresh Job/Version identities.
                    block = hashlib.sha256(path.encode()).digest()
                    payload = (block * 320)[:10240]
                    target.write_bytes(payload)
                    catalog.observe_local_copy(vault_id=2, path=path, file_type="regular",
                        size=len(payload), mtime_ns=target.stat().st_mtime_ns, observed_at=storage.now_iso())
                    file_id = connection.execute("""SELECT vault_file_id FROM file_paths
                        WHERE vault_id=2 AND path=%s AND valid_to IS NULL""", (path,)).fetchone()["vault_file_id"]
                    if legacy_owner_fixture:
                        query = """INSERT INTO jobs(vault_id,vault_file_id,path,action,status,
                            requested_by,requested_at,updated_at,total_bytes,upload_verification_requested)
                            VALUES(2,%s,%s,'upload','queued',1,%s,%s,%s,%s) RETURNING id"""
                    else:
                        query = """INSERT INTO jobs(vault_id,vault_file_id,path,action,status,
                            requested_at,updated_at,total_bytes,upload_verification_requested)
                            VALUES(2,%s,%s,'upload','queued',%s,%s,%s,%s) RETURNING id"""
                    job = connection.execute(query,
                        (file_id, path, storage.now_iso(), storage.now_iso(), len(payload), config["mode"])).fetchone()
                    jobs.append(job["id"])
                    expected[path] = hashlib.sha256(payload).hexdigest()
                aggregates.flush_directory_aggregates(connection, vault_id=2)
            return jobs, expected

        # Long-lived claim connections are harness infrastructure; opening them
        # is outside timing, but their real SQL/commits are counted under claim.*.
        claimers = [PostgresConnection(identity) for _ in range(concurrency)]
        for claimer in claimers:
            stack.callback(claimer.close)

        def burst(jobs):
            start = threading.Barrier(concurrency + 1)
            latency, handler_latency, claim_latency = [], [], []
            latency_lock = threading.Lock()

            def worker(slot):
                claimer = claimers[slot]
                start.wait(timeout=30)
                for job_id in jobs[slot::concurrency]:
                    began = time.perf_counter()
                    with probe.scope("claim"):
                        # SKIP LOCKED is allowed to defer a claim during contention.
                        # Bounded retries are included in latency and the trace.
                        deadline = began + 30
                        while True:
                            now = storage.now_iso()
                            claimed = ArchiveCatalog(claimer).claim_job(job_id=job_id,
                                claim_token=uuid.uuid4().hex, claimed_at=now,
                                claim_expires_at=storage._claim_expiry_at(now), now=now,
                                restore_due_before=now)
                            claimer.commit()
                            if claimed is not None:
                                break
                            require(time.perf_counter() < deadline, "Claim failed to acquire")
                            time.sleep(0.002)
                        job = claimer.execute("""SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix
                            FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s""", (job_id,)).fetchone()
                        claimer.commit()
                    require(bool(job["claim_token"]), "Job must be really claimed")
                    handler_started = time.perf_counter()
                    require(storage.process_job(job), "process_job did not execute")
                    ended = time.perf_counter()
                    with latency_lock:
                        latency.append(ended - began)
                        handler_latency.append(ended - handler_started)
                        claim_latency.append(handler_started - began)

            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [executor.submit(worker, slot) for slot in range(concurrency)]
                # Barrier action runs before releasing workers, so timing cannot
                # accidentally omit the first claim. End includes all handlers.
                began = time.perf_counter()
                start.wait(timeout=30)
                for future in futures:
                    future.result(timeout=300)
                elapsed = time.perf_counter() - began
            return {"elapsed_seconds": elapsed, "files_per_second": len(jobs) / elapsed,
                    "bytes_per_second": len(jobs) * 10240 / elapsed,
                    "claim_to_completed_seconds": distribution(latency),
                    "handler_seconds": distribution(handler_latency),
                    "claim_seconds": distribution(claim_latency)}

        def validate(jobs, expected):
            with PostgresConnection(identity) as connection:
                rows = connection.execute("""SELECT j.path,j.status,j.claim_token,j.archive_version_id,
                    j.upload_plaintext_sha256,j.upload_snapshot_json,j.upload_verification_requested,
                    av.object_key,av.provider_version_id,av.size,av.plaintext_sha256,av.integrity,av.availability,
                    av.verification_method,av.upload_verification_requested AS version_mode,
                    lc.matched_archive_version_id,ci.completed_at AS cleanup_completed,
                    ui.settled, (SELECT count(*) FROM upload_receipts ur WHERE ur.intent_id=ui.id) AS receipts
                    FROM jobs j JOIN archive_versions av ON av.id=j.archive_version_id
                    JOIN local_copies lc ON lc.vault_file_id=j.vault_file_id
                    JOIN snapshot_cleanup_inventories ci ON ci.job_id=j.id
                    JOIN upload_intents ui ON ui.original_job_id=j.id
                    WHERE j.id=ANY(%s)""", (jobs,)).fetchall()
                require(len(rows) == len(jobs), "Missing catalog/cleanup/intent outcome")
                for row in rows:
                    digest = expected[row["path"]]
                    require(row["status"] == "completed" and not row["claim_token"], "Job is not completed/released")
                    require(row["cleanup_completed"] and row["settled"], "Completion preceded cleanup/settlement")
                    require(row["receipts"] == 1, "Expected one immutable receipt per intent")
                    require(row["integrity"] == "verified" and row["availability"] == "available", "Wrong integrity/availability")
                    require(row["matched_archive_version_id"] == row["archive_version_id"], "Local Copy not matched")
                    require(row["size"] == 10240, "Wrong cloud length")
                    require(row["plaintext_sha256"] == row["upload_plaintext_sha256"] == digest, "Wrong independent digest")
                    snapshot = UploadSnapshot.from_json(row["upload_snapshot_json"])
                    require(snapshot.plaintext_sha256 == digest and snapshot.size == 10240, "Wrong frozen snapshot")
                    require(row["upload_verification_requested"] == row["version_mode"] == row["verification_method"] == config["mode"], "Verification mode drift/fallback")
                    identity_key = ("benchmark-only", row["object_key"], row["provider_version_id"])
                    require(identity_key in provider.objects, "Wrong exact VersionId")
                    require(hashlib.sha256(provider.objects[identity_key][0]).hexdigest() == digest, "Wrong exact provider content")
                current = {("benchmark-only", row["object_key"], row["provider_version_id"]) for row in rows}
                actual_puts = list(provider.puts)
                require(len(actual_puts) == len(jobs) and set(actual_puts) == current, "Extra PUT or wrong exact identity")
                version_count = connection.execute("""SELECT count(*) AS n FROM archive_versions
                    WHERE vault_file_id IN (SELECT vault_file_id FROM jobs WHERE id=ANY(%s))""", (jobs,)).fetchone()["n"]
                require(version_count == len(jobs), "Unexpected catalog Archive Version count")
                if config["mode"] == "read_back":
                    require(current.issubset(set(provider.gets)), "Missing independent exact GET")
                else:
                    require(not any(item in current for item in provider.gets), "Unexpected checksum fallback GET")
                after = {key: connection.execute(f"SHOW {key}").fetchone()[key] for key in durability}
                require(after == durability, "Durability changed")
            return {"completed_after_cleanup": len(rows), "exact_verified_versions": len(rows),
                    "extra_puts": 0, "bytes_per_file": 10240}

        seed_started = time.perf_counter()
        warm_jobs, warm_expected = seed(config["warmup"], "warmup")
        warm_seed_seconds = time.perf_counter() - seed_started
        warm_started = time.perf_counter()
        burst(warm_jobs)
        validate(warm_jobs, warm_expected)
        warm_seconds = time.perf_counter() - warm_started
        seed_started = time.perf_counter()
        jobs, expected = seed(count, "measured")
        measured_seed_seconds = time.perf_counter() - seed_started
        provider.reset_counts()
        setup_seconds = time.perf_counter() - setup_started
        # Sampling observes only this disposable database, never query text or
        # identifiers in output. Its read SQL is excluded from app/claim counts.
        stop = threading.Event()
        samples, sampler_errors = [], []
        sampler_ready = threading.Event()

        def sample_locks():
            try:
                with probe.scope("observer"), PostgresConnection(identity) as connection:
                    connection.autocommit = True
                    sampler_ready.set()
                    while not stop.is_set():
                        rows = connection.execute("""SELECT count(*) FILTER(WHERE wait_event_type='Lock') AS locks,
                            count(*) FILTER(WHERE state='active') AS active
                            FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()""").fetchone()
                        samples.append(dict(rows))
                        stop.wait(0.05)
            except BaseException as exc:
                sampler_errors.append(type(exc).__name__)
                sampler_ready.set()

        with probe.installed() if config["instrumented"] else nullcontext():
            sampler = threading.Thread(target=sample_locks, name="benchmark-observer")
            sampler.start()
            require(sampler_ready.wait(15), "Lock observer did not start")
            try:
                timing = burst(jobs)
            finally:
                stop.set()
                sampler.join(timeout=15)
            require(not sampler.is_alive() and not sampler_errors and samples, "Lock observer failed")
        validation_started = time.perf_counter()
        checks = validate(jobs, expected)
        validation_seconds = time.perf_counter() - validation_started
        metrics = probe.export()
        if config["instrumented"]:
            require(metrics["counts"].get("app.sql.calls", 0) > 0, "SQL probe did not observe application work")
            require(metrics["counts"].get("app.transaction.started", 0) > 0, "Transaction probe did not observe BEGIN")
        return {"schema_version": 1, "status": "ok", "sha": config["sha"],
                "label": config["label"], "repetition": config["repetition"],
                "instrumented": config["instrumented"], "mode": config["mode"],
                "files": count, "concurrency": concurrency, "warmup_files": config["warmup"],
                "warmup_seconds": warm_seconds, "setup_including_warmup_seconds": setup_seconds,
                "excluded_phase_seconds": {"migration": migration_seconds, "warmup_seed": warm_seed_seconds,
                                           "measured_seed": measured_seed_seconds, "validation": validation_seconds},
                "postgres_version_num": version, "durability": durability,
                "timing": timing, "checks": checks, "probe": metrics,
                "s3_operations": dict(sorted(provider.calls.items())),
                "locks": {"interval_seconds": 0.05, "samples": len(samples),
                          "waiting_backend_samples": sum(row["locks"] for row in samples),
                          "max_waiting_backends": max(row["locks"] for row in samples)},
                "scope": {"provider": "real SDK orchestration + in-memory API emulator; no HTTP/AWS",
                          "timed": "synchronized burst: real claim/load through process_job return after completion/cleanup",
                          "excluded": "migration, seed, warmup, initial harness claim connections, post-run validation, readiness probe, scheduler nomination, pause/cancel and aggregate maintenance; per-file SDK client construction remains timed",
                          "automatic_aggregate_scheduling": False, "bandwidth_limit_kibps": 0}}


def main():
    config = json.load(sys.stdin)
    output = Path(config["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = run_sample(config)
    except BaseException as exc:
        # Do not export exception text: a libpq/SDK error can contain credentials.
        result = {"schema_version": 1, "status": "error", "error_type": type(exc).__name__,
                  "sha": config["sha"], "label": config["label"], "repetition": config["repetition"],
                  "frames": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                             for frame in traceback.extract_tb(exc.__traceback__)[-12:]]}
        output.write_text(json.dumps(result, indent=2) + "\n")
        print(f"Benchmark worker failed ({type(exc).__name__}); no provider/DB exception text exported", file=sys.stderr)
        return 1
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
