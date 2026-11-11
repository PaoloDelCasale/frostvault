"""Test-only SDK/provider and PostgreSQL probes for the upload benchmark.

No app imports: the same file is loaded alongside either checkout. Parameters,
keys, VersionIds and connection strings never enter the exported counters.
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch


def sql_shape(query, connection=None):
    if hasattr(query, "as_string"):
        query = query.as_string(connection)
    if isinstance(query, bytes):
        query = query.decode("utf-8", "replace")
    text = re.sub(r"/\*.*?\*/|--[^\n]*", " ", str(query), flags=re.S)
    text = re.sub(r"'(?:''|[^'])*'", "'?'", text)
    text = re.sub(r"\b\d+\b", "?", text)
    text = re.sub(r"_pg3_\w+", "savepoint", text)
    return " ".join(text.lower().split())


def sql_category(shape):
    operation = shape.split(" ", 1)[0] if shape else "empty"
    table = re.search(r"\b(?:from|into|update) ([a-z_]+)\b", shape)
    lock = re.search(r"\bfor (no key update|key share|share|update)\b", shape)
    return operation, table.group(1) if table else "none", lock.group(1) if lock else "none"


class Probe:
    """Count actual driver calls, including psycopg's implicit BEGIN/COMMIT.

    Cursor.execute is the single SQL boundary (Connection.execute delegates to
    it). _exec_command observes driver-internal transaction commands, not cursor
    SQL a second time. No DB result, mutation or durability setting is replaced.
    Counts are attempts; successful/failed calls are separate. Server-side trigger
    statements and physical fsyncs are NOT counted as separate client statements.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.local = threading.local()
        self.counts = Counter()
        self.nanoseconds = Counter()
        self.shapes = {}

    @contextmanager
    def scope(self, name):
        previous = getattr(self.local, "scope", "app")
        self.local.scope = name
        try:
            yield
        finally:
            self.local.scope = previous

    def record(self, query, elapsed, failed, connection=None, internal=False, outcome=None):
        scope = getattr(self.local, "scope", "app")
        if scope == "observer":
            return
        shape = sql_shape(query, connection)
        op, table, lock = sql_category(shape)
        labels = ["driver.commands" if internal else "sql.calls", f"sql.operation.{op}"]
        if not internal:
            labels += [f"sql.table.{table}"]
        if lock != "none":
            labels += [f"sql.lock.{lock.replace(' ', '_')}"]
        if op in {"begin", "commit", "rollback", "savepoint", "release"}:
            labels += [f"transaction.{op}"]  # command attempts, including redundant BEGIN/savepoints
        if outcome:
            labels += [f"transaction.{outcome}"]
        # Find mutations even inside a CTE; never depend on caller/function
        # names or on the refactor retaining the baseline's statement layout.
        if re.search(r"\binsert into (?:public\.)?upload_receipts\b", shape):
            labels += ["receipt.insert_attempts"]
        jobs_update = re.search(r"\bupdate (?:public\.)?jobs set (.*)", shape)
        if jobs_update:
            assignments = jobs_update.group(1).split(" where ", 1)[0]
            if "claim_expires_at" in assignments:
                labels += ["renewal.update_attempts"]
            if "transferred_bytes" in assignments:
                labels += ["progress.update_attempts"]
        with self.lock:
            for label in labels:
                self.counts[f"{scope}.{label}"] += 1
            self.counts[f"{scope}.errors"] += int(failed)
            self.nanoseconds[f"{scope}.driver_time"] += elapsed
            if op == "commit":
                self.nanoseconds[f"{scope}.commit_time"] += elapsed
            # Bounded, parameter-free, normalized SQL trace. Count every shape;
            # fail instead of silently truncating an unexpectedly huge workload.
            fingerprint = hashlib.sha256(shape.encode()).hexdigest()[:16]
            key = (scope, fingerprint)
            if key not in self.shapes:
                if len(self.shapes) >= 512:
                    raise AssertionError("SQL trace exceeded 512 shapes")
                self.shapes[key] = {"scope": scope, "fingerprint": fingerprint,
                                    "sql": shape[:1200], "calls": 0, "errors": 0}
            self.shapes[key]["calls"] += 1
            self.shapes[key]["errors"] += int(failed)

    def event(self, name, elapsed, failed):
        scope = getattr(self.local, "scope", "app")
        if scope == "observer":
            return
        with self.lock:
            self.counts[f"{scope}.{name}.attempts"] += 1
            self.counts[f"{scope}.{name}.successes"] += int(not failed)
            self.nanoseconds[f"{scope}.{name}_time"] += elapsed

    @contextmanager
    def installed(self):
        import psycopg
        execute = psycopg.Cursor.execute
        executemany = psycopg.Cursor.executemany
        command = psycopg.Connection._exec_command
        class_connect = psycopg.Connection.connect.__func__
        module_connect = psycopg.connect
        probe = self
        from psycopg.pq import TransactionStatus

        def outcome(connection, before, failed, *, allow_start=True):
            if failed:
                return None
            after = connection.info.transaction_status
            if allow_start and before == TransactionStatus.IDLE and after == TransactionStatus.INTRANS:
                return "started"
            if before == TransactionStatus.INTRANS and after == TransactionStatus.IDLE:
                return "finished"
            return None

        def traced_execute(cursor, query, *args, **kwargs):
            before = cursor.connection.info.transaction_status
            start, failed = time.perf_counter_ns(), True
            try:
                result = execute(cursor, query, *args, **kwargs)
                failed = False
                return result
            finally:
                # For non-autocommit cursors the implicit BEGIN was already
                # observed at _exec_command; a redundant explicit BEGIN is not
                # another transaction. Keep command attempts separate.
                state = outcome(cursor.connection, before, failed, allow_start=cursor.connection.autocommit)
                probe.record(query, time.perf_counter_ns() - start, failed, cursor.connection, outcome=state)

        def traced_many(cursor, query, *args, **kwargs):
            start, failed = time.perf_counter_ns(), True
            try:
                result = executemany(cursor, query, *args, **kwargs)
                failed = False
                return result
            finally:
                probe.record(query, time.perf_counter_ns() - start, failed, cursor.connection)

        def traced_command(connection, query, *args, **kwargs):
            before = connection.info.transaction_status
            start, failed = time.perf_counter_ns(), True
            try:
                result = yield from command(connection, query, *args, **kwargs)
                failed = False
                return result
            finally:
                state = outcome(connection, before, failed)
                probe.record(query, time.perf_counter_ns() - start, failed, connection, True, state)

        def connect(call, *args, **kwargs):
            start, failed = time.perf_counter_ns(), True
            try:
                result = call(*args, **kwargs)
                failed = False
                return result
            finally:
                probe.event("connections", time.perf_counter_ns() - start, failed)

        def traced_class_connect(cls, *args, **kwargs):
            return connect(class_connect, cls, *args, **kwargs)

        def traced_module_connect(*args, **kwargs):
            # psycopg.connect is a bound alias of the original class method,
            # so these two entry points are counted once, not recursively.
            return connect(module_connect, *args, **kwargs)

        with patch.object(psycopg.Cursor, "execute", traced_execute), \
                patch.object(psycopg.Cursor, "executemany", traced_many), \
                patch.object(psycopg.Connection, "_exec_command", traced_command), \
                patch.object(psycopg.Connection, "connect", classmethod(traced_class_connect)), \
                patch.object(psycopg, "connect", traced_module_connect):
            yield

    def export(self):
        with self.lock:
            return {"counts": dict(sorted(self.counts.items())),
                    "seconds": {key: value / 1e9 for key, value in sorted(self.nanoseconds.items())},
                    "sql_trace": sorted(self.shapes.values(), key=lambda row: (row["scope"], row["fingerprint"]))}


class MemoryS3:
    """Strict thread-safe S3 API emulator, NOT a transport replacement.

    Intercepts BaseClient._make_api_call: real client construction, upload
    facade, SDK managers/threads, bounded buffer validation, callbacks and Job
    pipeline still execute. No serialization/HTTP/retry service/AWS latency.
    Unsupported operations fail closed; HEAD/GET require exact identities.
    """
    def __init__(self, *, page_size=8):
        self.lock = threading.RLock()
        self.objects = {}
        self.calls = Counter()
        self.puts = []
        self.gets = []
        self.page_size = page_size
        self.hook = lambda operation, params: None

    def reset_counts(self):
        with self.lock:
            self.calls.clear()
            self.puts.clear()
            self.gets.clear()

    def _head(self, bucket, key, version):
        if not version or version == "null":
            raise AssertionError("HEAD/GET latest is forbidden")
        from botocore.exceptions import ClientError
        try:
            payload, metadata = self.objects[bucket, key, version]
        except KeyError:
            raise ClientError({"Error": {"Code": "NoSuchVersion"},
                               "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject") from None
        return {"VersionId": version, "ContentLength": len(payload), "Metadata": dict(metadata),
                "ChecksumType": "FULL_OBJECT", "ETag": '"not-integrity-proof"',
                "StorageClass": "STANDARD",
                "ChecksumSHA256": base64.b64encode(hashlib.sha256(payload).digest()).decode()}

    def call(self, operation, params):
        with self.lock:
            self.calls[operation] += 1
        self.hook(operation, params)
        bucket = params["Bucket"]
        if operation == "PutObject":
            body = params["Body"]
            if hasattr(body, "signal_transferring"):
                body.signal_transferring()  # HTTP request-created hook, absent in this emulator.
            payload = body.read() if hasattr(body, "read") else bytes(body)
            if len(payload) > 1024 * 1024:
                raise AssertionError("Benchmark fake accepts bounded single PUTs only")
            checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode()
            if params.get("ChecksumSHA256") != checksum:
                raise AssertionError("PUT lacks independently validated SHA256")
            with self.lock:
                version = f"bench-version-{len(self.objects) + 1}"
                key = params["Key"]
                self.objects[bucket, key, version] = (payload, dict(params.get("Metadata", {})))
                self.puts.append((bucket, key, version))
                return self._head(bucket, key, version)
        with self.lock:
            if operation in {"HeadObject", "GetObject"}:
                result = self._head(bucket, params["Key"], params.get("VersionId"))
                if operation == "GetObject":
                    identity = (bucket, params["Key"], params["VersionId"])
                    self.gets.append(identity)
                    result["Body"] = io.BytesIO(self.objects[identity][0])
                return result
            if operation == "ListObjectVersions":
                rows = [(b, k, v) for b, k, v in sorted(self.objects)
                        if b == bucket and k.startswith(params["Prefix"])]
                marker = params.get("KeyMarker"), params.get("VersionIdMarker")
                if marker[0] is not None:
                    rows = [row for row in rows if row[1:] > marker]
                page = rows[:self.page_size]
                result = {"Versions": [{"Key": k, "VersionId": v, "IsLatest": True,
                                        "Size": len(self.objects[b, k, v][0]), "StorageClass": "STANDARD",
                                        "LastModified": datetime(2026, 11, 1, tzinfo=timezone.utc)}
                                       for b, k, v in page], "DeleteMarkers": [],
                          "IsTruncated": len(rows) > len(page)}
                if result["IsTruncated"]:
                    result.update(NextKeyMarker=page[-1][1], NextVersionIdMarker=page[-1][2])
                return result
            if operation == "ListMultipartUploads":
                return {"Uploads": [], "IsTruncated": False}
            if operation == "GetObjectTagging":
                return {"TagSet": []}
            # Fresh files cannot legitimately delete, copy, restore or use MPU.
            raise AssertionError(f"Unexpected benchmark S3 operation: {operation}")

    @contextmanager
    def installed(self):
        from botocore.client import BaseClient
        provider = self

        def api(client, operation, params):
            if client.meta.service_model.service_name != "s3":
                raise AssertionError("Benchmark forbids non-S3 SDK calls")
            return provider.call(operation, params)

        with patch.object(BaseClient, "_make_api_call", api):
            yield self
