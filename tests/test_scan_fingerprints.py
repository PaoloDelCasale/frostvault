"""Verification phase of a full scan on a large adoption (#691).

Seams under test:
1. ``SCAN_HASH_CONCURRENCY`` reads several Local Copies at once, bounded.
2. Digests are published in compare-and-swap batches as they complete, so
   progress is durable before the whole Vault is verified.
3. The running scan reports files/bytes verified in the runtime status.
4. A superseded scan stops reading and publishes nothing more.
5. A ``source`` error from a previous scan keeps its time while a new scan runs.
"""
from __future__ import annotations

import threading
from unittest.mock import patch

from app.config import Settings
from tests.fixtures import override_settings
from tests.test_vault_adoption import _AdoptionFixture
from app.services.vaults import VAULT_ROW_SQL


def _quiet_scan_patches():
    return (
        patch("app.storage.scan_cloud", return_value=0),
        patch("app.storage.validate_cloud_vault"),
        patch("app.storage.sync_lifecycle_rules_for_bucket", return_value=0),
        patch("app.storage.queue_auto_uploads", return_value=0),
        patch("app.storage.queue_auto_local_cleanups", return_value=0),
    )


class ScanFingerprintTests(_AdoptionFixture):
    def setUp(self) -> None:
        super().setUp()
        for index in range(12):
            (self.albums / f"img{index:02d}.heic").write_bytes(b"x" * (100 + index))

    def test_default_concurrency_is_four(self) -> None:
        self.assertEqual(Settings().scan_hash_concurrency, 4)

    def _vault(self) -> dict:
        from app.storage import runtime_status
        created = self._adopt()
        # The runtime status is process-wide; Vault ids repeat across tests.
        self.addCleanup(runtime_status.pop, int(created["id"]), None)
        with self.database.connect() as connection:
            return dict(connection.execute(
                f"{VAULT_ROW_SQL} WHERE v.id=%s", (created["id"],),
            ).fetchone())

    def _concurrency(self, value: int):
        return override_settings(scan_hash_concurrency=value)

    def _digests(self, vault_id: int) -> dict[str, str | None]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT path, local_sha256 FROM files WHERE vault_id=%s", (vault_id,),
            ).fetchall()
        return {row["path"]: row["local_sha256"] for row in rows}

    def _scan(self, vault: dict, *extra):
        from contextlib import ExitStack
        from app.storage import scan_vault
        with ExitStack() as stack:
            for item in (*_quiet_scan_patches(), *extra):
                stack.enter_context(item)
            return scan_vault(dict(vault))

    def test_hashing_reads_several_files_at_once_and_never_more_than_configured(self) -> None:
        import app.storage as storage
        vault = self._vault()
        real_hash = storage.hash_stable_regular_file
        lock = threading.Lock()
        active = {"now": 0, "peak": 0}
        overlapped = threading.Event()

        def counting(path, **kwargs):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
                if active["now"] >= 2:
                    overlapped.set()
            try:
                # Wait (bounded) for a second reader, so overlap does not
                # depend on how fast the tiny files are read.
                overlapped.wait(2)
                return real_hash(path, **kwargs)
            finally:
                with lock:
                    active["now"] -= 1

        result = self._scan(
            vault, self._concurrency(3),
            patch("app.storage.hash_stable_regular_file", side_effect=counting),
        )
        self.assertEqual(result["local"], 13)
        self.assertEqual(result["rename_hashed"], 13)
        self.assertGreaterEqual(active["peak"], 2)
        self.assertLessEqual(active["peak"], 3)
        self.assertTrue(all(self._digests(int(vault["id"])).values()))

    def test_one_reader_keeps_the_sequential_behaviour(self) -> None:
        import app.storage as storage
        vault = self._vault()
        threads = set()
        real_hash = storage.hash_stable_regular_file

        def record(path, **kwargs):
            threads.add(threading.get_ident())
            return real_hash(path, **kwargs)

        self._scan(vault, self._concurrency(1),
                   patch("app.storage.hash_stable_regular_file", side_effect=record))
        self.assertEqual(threads, {threading.get_ident()})
        self.assertTrue(all(self._digests(int(vault["id"])).values()))

    def test_batches_are_published_while_the_rest_is_still_being_verified(self) -> None:
        import app.storage as storage
        vault = self._vault()
        real_hash = storage.hash_stable_regular_file
        published_before_last: list[int] = []

        def observe(path, **kwargs):
            if path.name == "photo.jpg":  # the last path in catalog order
                published_before_last.append(
                    sum(1 for value in self._digests(int(vault["id"])).values() if value)
                )
            return real_hash(path, **kwargs)

        self._scan(
            vault, self._concurrency(1),
            patch("app.storage.LOCAL_SCAN_WRITE_BATCH_SIZE", 4),
            patch("app.storage.hash_stable_regular_file", side_effect=observe),
        )
        self.assertEqual(published_before_last, [12])
        self.assertTrue(all(self._digests(int(vault["id"])).values()))

    def test_runtime_status_reports_files_and_bytes_verified(self) -> None:
        import app.storage as storage
        from app.services.runtime_status import snapshot_runtime_status_for_stats
        vault = self._vault()
        vault_id = int(vault["id"])
        real_hash = storage.hash_stable_regular_file
        seen: list[dict] = []

        def observe(path, **kwargs):
            seen.append(snapshot_runtime_status_for_stats(vault_id))
            return real_hash(path, **kwargs)

        self._scan(vault, self._concurrency(1),
                   patch("app.storage.hash_stable_regular_file", side_effect=observe))
        total_bytes = sum(100 + index for index in range(12)) + len(b"jpeg-bytes")
        self.assertEqual({entry["scan_phase"] for entry in seen}, {"verification"})
        self.assertEqual({entry["verify_files_total"] for entry in seen}, {13})
        self.assertEqual({entry["verify_bytes_total"] for entry in seen}, {total_bytes})
        self.assertEqual([entry["verify_files_done"] for entry in seen], list(range(13)))
        self.assertTrue(all(entry["scanning"] for entry in seen))
        after = snapshot_runtime_status_for_stats(vault_id)
        self.assertFalse(after["scanning"])
        self.assertIsNone(after.get("scan_phase"))
        self.assertIsNone(after.get("verify_files_total"))

    def test_superseded_scan_stops_reading_and_publishes_nothing_more(self) -> None:
        import app.storage as storage
        vault = self._vault()
        vault_id = int(vault["id"])
        real_hash = storage.hash_stable_regular_file
        calls = []

        def supersede(path, **kwargs):
            calls.append(path)
            with storage.status_lock:
                storage.runtime_status[vault_id]["scan_id"] = "newer-scan"
            return real_hash(path, **kwargs)

        result = self._scan(
            vault, self._concurrency(2),
            patch("app.storage.LOCAL_SCAN_WRITE_BATCH_SIZE", 2),
            patch("app.storage.hash_stable_regular_file", side_effect=supersede),
        )
        self.assertEqual(result.get("root_identity_mismatch"), 1)
        # The window holds at most 2 × concurrency reads; the rest never start.
        self.assertLessEqual(len(calls), 6)
        self.assertFalse(any(self._digests(vault_id).values()))

    def test_unreadable_file_during_parallel_hashing_names_its_path(self) -> None:
        import app.storage as storage
        vault = self._vault()
        real_hash = storage.hash_stable_regular_file

        def deny(path, **kwargs):
            if path.name == "img03.heic":
                raise PermissionError("denied")
            return real_hash(path, **kwargs)

        result = self._scan(vault, self._concurrency(4),
                            patch("app.storage.hash_stable_regular_file", side_effect=deny))
        self.assertEqual(result["local_incomplete"], 1)
        digests = self._digests(int(vault["id"]))
        # Not observed by this scan (#696): its new row is undone.
        self.assertNotIn("img03.heic", digests)
        self.assertEqual(sum(1 for value in digests.values() if value), 12)
        with self.database.connect() as connection:
            problems = connection.execute(
                "SELECT event, message_params FROM notifications WHERE problem_status='open'"
            ).fetchall()
        self.assertTrue(any(
            row["event"] == "file_unreadable" and "img03.heic" in str(row["message_params"])
            for row in problems
        ))


class PreviousSourceErrorTests(_AdoptionFixture):
    """An older ``source`` error is the last scan's outcome, with its time."""

    def test_failure_time_survives_a_new_scan_start_and_a_restart(self) -> None:
        import app.storage as storage
        from app.services.component_health import load_vault_health, runtime_fields_from_health
        from app.services.runtime_status import snapshot_runtime_status_for_stats
        created = self._adopt()
        vault_id = int(created["id"])
        self.addCleanup(storage.runtime_status.pop, vault_id, None)
        storage._record_scan_component_health(
            vault_id, "source", ok=False, error="Source scan blocked: replaced",
        )
        failed_at = storage.runtime_status[vault_id]["last_error_source_at"]
        self.assertTrue(failed_at)
        with storage.status_lock:
            storage.runtime_status[vault_id].update(scanning=True, last_error=None)
        runtime = snapshot_runtime_status_for_stats(vault_id)
        self.assertTrue(runtime["scanning"])
        self.assertEqual(runtime["last_error_source"], "Source scan blocked: replaced")
        self.assertEqual(runtime["last_error_source_at"], failed_at)
        with self.database.connect() as connection:
            overlay = runtime_fields_from_health(load_vault_health(connection, vault_id))
        self.assertEqual(overlay["last_error_source"], "Source scan blocked: replaced")
        self.assertIsNotNone(overlay["last_error_source_at"])

        storage._record_scan_component_health(vault_id, "source", ok=True, verified=True)
        self.assertIsNone(storage.runtime_status[vault_id]["last_error_source_at"])
        with self.database.connect() as connection:
            overlay = runtime_fields_from_health(load_vault_health(connection, vault_id))
        self.assertIsNone(overlay["last_error_source_at"])
