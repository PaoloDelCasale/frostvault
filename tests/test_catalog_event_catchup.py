"""What one catalog catch-up reads from the journal and what it answers (#804).

A reconnecting stream hands its ``Last-Event-ID`` as ``after_revision``. Each
case fixes the answer (signal, events read, backlog truncated) and how many
statements the catch-up runs: one when the high-water mark already decides,
two when a bounded page of events is read. Never more.
"""
from __future__ import annotations

from typing import Any

from app.services.catalog_event_hub import DEFAULT_INVALIDATE_DOMAINS
from app.services.catalog_event_stream import (
    MAX_CATCHUP_EVENTS,
    coalesced_catchup_from_connection,
    coalesced_catchup_signal,
)
from app.services.catalog_events import MAX_EVENT_PAGE_SIZE, CatalogEventStore
from tests.fixtures import FrostVaultTestCase, seed_vault


ALL_DOMAINS = list(DEFAULT_INVALIDATE_DOMAINS)


class CatalogEventCatchupTests(FrostVaultTestCase):
    def setUp(self) -> None:
        super().setUp()
        with self.database.connect() as connection:
            seed_vault(connection, id=1, name="Alpha", source_root=str(self.tmp_path / "alpha"))
            seed_vault(connection, id=2, name="Beta", source_root=str(self.tmp_path / "beta"))

    def publish(self, *invalidations: list[str] | None, vault_id: int = 1) -> None:
        with self.database.connect() as connection:
            store = CatalogEventStore(connection)
            for index, invalidate in enumerate(invalidations):
                payload: dict[str, Any] = {"reason": f"event-{index}"}
                if invalidate is not None:
                    payload["invalidate"] = invalidate
                store.append_event(vault_id=vault_id, domain="catalog", payload=payload)

    def catchup(
        self, after_revision: int, *, vault_id: int = 1, **options: Any,
    ) -> tuple[dict[str, Any], int, bool, list[str]]:
        """The answer of one catch-up and the statements it ran, in order."""
        statements: list[str] = []
        counter = [0]
        with self.database.connect() as connection:
            execute = connection.execute

            def recording_execute(query: Any, params: Any = None) -> Any:
                statements.append(" ".join(str(query).split()))
                return execute(query, params)

            connection.execute = recording_execute  # type: ignore[method-assign]
            signal, events_read, truncated = coalesced_catchup_from_connection(
                connection,
                vault_id=vault_id,
                after_revision=after_revision,
                statement_counter=counter,
                **options,
            )
        self.assertEqual(counter[0], len(statements))
        return signal, events_read, truncated, statements

    def assert_high_water_only(self, statements: list[str]) -> None:
        self.assertEqual(len(statements), 1, msg=statements)
        self.assertIn("FROM vault_catalog_revisions", statements[0])

    def assert_high_water_and_one_page(self, statements: list[str]) -> None:
        self.assertEqual(len(statements), 2, msg=statements)
        self.assertIn("FROM vault_catalog_revisions", statements[0])
        self.assertIn("FROM catalog_events", statements[1])

    def test_vault_that_never_published_answers_revision_zero(self) -> None:
        for after_revision in (0, 7):
            with self.subTest(after_revision=after_revision):
                signal, events_read, truncated, statements = self.catchup(after_revision)
                self.assertEqual(
                    signal, {"vault_id": 1, "revision": 0, "domains": [], "has_gap": False})
                self.assertEqual((events_read, truncated), (0, False))
                self.assert_high_water_only(statements)

    def test_cursor_at_or_past_the_high_water_mark_reads_no_event(self) -> None:
        self.publish(["files"], ["stats"])
        for after_revision in (2, 3, 500):
            with self.subTest(after_revision=after_revision):
                signal, events_read, truncated, statements = self.catchup(after_revision)
                self.assertEqual(
                    signal, {"vault_id": 1, "revision": 2, "domains": [], "has_gap": False})
                self.assertEqual((events_read, truncated), (0, False))
                self.assert_high_water_only(statements)

    def test_missing_cursor_replays_every_event_as_one_signal(self) -> None:
        self.publish(["files"], ["stats"], ["files", "jobs"])
        signal, events_read, truncated, statements = self.catchup(0)
        self.assertEqual(signal, {
            "vault_id": 1, "revision": 3,
            "domains": ["files", "stats", "jobs"], "has_gap": False,
        })
        self.assertEqual((events_read, truncated), (3, False))
        self.assert_high_water_and_one_page(statements)

    def test_cursor_inside_the_journal_replays_only_newer_events_in_order(self) -> None:
        self.publish(["files"], ["stats"], ["jobs"], ["stats", "files"])
        signal, events_read, truncated, statements = self.catchup(1)
        self.assertEqual(signal, {
            "vault_id": 1, "revision": 4,
            "domains": ["stats", "jobs", "files"], "has_gap": False,
        })
        self.assertEqual((events_read, truncated), (3, False))
        self.assert_high_water_and_one_page(statements)

    def test_event_without_domains_invalidates_everything(self) -> None:
        self.publish(None)
        signal, events_read, truncated, _statements = self.catchup(0)
        self.assertEqual(signal, {
            "vault_id": 1, "revision": 1, "domains": ALL_DOMAINS, "has_gap": False,
        })
        self.assertEqual((events_read, truncated), (1, False))

    def test_other_vaults_events_are_not_read(self) -> None:
        self.publish(["files"], vault_id=1)
        self.publish(["stats"], ["jobs"], vault_id=2)
        signal, events_read, truncated, _statements = self.catchup(0, vault_id=1)
        self.assertEqual(
            signal, {"vault_id": 1, "revision": 1, "domains": ["files"], "has_gap": False})
        self.assertEqual((events_read, truncated), (1, False))
        signal, events_read, truncated, _statements = self.catchup(0, vault_id=2)
        self.assertEqual(signal, {
            "vault_id": 2, "revision": 2, "domains": ["stats", "jobs"], "has_gap": False,
        })
        self.assertEqual((events_read, truncated), (2, False))

    def test_cursor_older_than_retention_is_a_gap_without_reading_events(self) -> None:
        self.publish(["files"], ["files"], ["files"], ["stats"], ["stats"])
        with self.database.connect() as connection:
            CatalogEventStore(connection).prune_events(vault_id=1, retain_from_revision=4)
        for after_revision in (0, 1, 2):
            with self.subTest(after_revision=after_revision):
                signal, events_read, truncated, statements = self.catchup(after_revision)
                self.assertEqual(signal, {
                    "vault_id": 1, "revision": 5, "domains": ALL_DOMAINS, "has_gap": True,
                })
                self.assertEqual((events_read, truncated), (0, True))
                self.assert_high_water_only(statements)

    def test_cursor_just_before_retention_replays_the_retained_events(self) -> None:
        self.publish(["files"], ["files"], ["files"], ["stats"], ["jobs"])
        with self.database.connect() as connection:
            CatalogEventStore(connection).prune_events(vault_id=1, retain_from_revision=4)
        signal, events_read, truncated, statements = self.catchup(3)
        self.assertEqual(signal, {
            "vault_id": 1, "revision": 5, "domains": ["stats", "jobs"], "has_gap": False,
        })
        self.assertEqual((events_read, truncated), (2, False))
        self.assert_high_water_and_one_page(statements)

    def test_everything_pruned_and_cursor_at_high_water_is_up_to_date(self) -> None:
        self.publish(["files"], ["stats"])
        with self.database.connect() as connection:
            CatalogEventStore(connection).prune_events(vault_id=1, retain_from_revision=3)
        signal, events_read, truncated, statements = self.catchup(2)
        self.assertEqual(
            signal, {"vault_id": 1, "revision": 2, "domains": [], "has_gap": False})
        self.assertEqual((events_read, truncated), (0, False))
        self.assert_high_water_only(statements)
        signal, events_read, truncated, statements = self.catchup(1)
        self.assertEqual(
            signal, {"vault_id": 1, "revision": 2, "domains": ALL_DOMAINS, "has_gap": True})
        self.assertEqual((events_read, truncated), (0, True))
        self.assert_high_water_only(statements)

    def test_backlog_at_the_bound_is_replayed_and_one_more_is_a_gap(self) -> None:
        self.publish(*[["files"] if index % 2 else ["stats"] for index in range(5)])
        signal, events_read, truncated, statements = self.catchup(0, max_events=5)
        self.assertEqual(signal, {
            "vault_id": 1, "revision": 5, "domains": ["stats", "files"], "has_gap": False,
        })
        self.assertEqual((events_read, truncated), (5, False))
        self.assert_high_water_and_one_page(statements)

        signal, events_read, truncated, statements = self.catchup(0, max_events=4)
        self.assertEqual(signal, {
            "vault_id": 1, "revision": 5, "domains": ALL_DOMAINS, "has_gap": True,
        })
        self.assertEqual((events_read, truncated), (4, True))
        self.assert_high_water_and_one_page(statements)

    def test_page_bound_is_clamped_between_one_and_the_store_maximum(self) -> None:
        self.publish(*[["files"]] * (MAX_EVENT_PAGE_SIZE + 1))
        _signal, events_read, truncated, _statements = self.catchup(0, max_events=0)
        self.assertEqual((events_read, truncated), (1, True))
        _signal, events_read, truncated, _statements = self.catchup(0, max_events=-3)
        self.assertEqual((events_read, truncated), (1, True))
        _signal, events_read, truncated, _statements = self.catchup(0, max_events=10_000)
        self.assertEqual((events_read, truncated), (MAX_EVENT_PAGE_SIZE, True))
        signal, events_read, truncated, _statements = self.catchup(1, max_events=10_000)
        self.assertEqual((events_read, truncated), (MAX_EVENT_PAGE_SIZE, False))
        self.assertEqual(signal["revision"], MAX_EVENT_PAGE_SIZE + 1)
        _signal, events_read, truncated, _statements = self.catchup(0)
        self.assertEqual((events_read, truncated), (MAX_CATCHUP_EVENTS, True))

    def test_retained_events_missing_from_the_journal_are_a_gap(self) -> None:
        self.publish(["files"], ["stats"], ["jobs"])
        with self.database.connect() as connection:
            connection.execute("DELETE FROM catalog_events WHERE vault_id=1 AND revision=2")
        with self.subTest("the first event after the cursor is missing"):
            signal, events_read, truncated, statements = self.catchup(1)
            self.assertEqual(signal, {
                "vault_id": 1, "revision": 3, "domains": ALL_DOMAINS, "has_gap": True,
            })
            self.assertEqual((events_read, truncated), (1, True))
            self.assert_high_water_and_one_page(statements)
        with self.subTest("a later event is missing: the page read is replayed"):
            signal, events_read, truncated, _statements = self.catchup(0)
            self.assertEqual(signal, {
                "vault_id": 1, "revision": 3, "domains": ["files", "jobs"], "has_gap": False,
            })
            self.assertEqual((events_read, truncated), (2, False))
        with self.database.connect() as connection:
            connection.execute("DELETE FROM catalog_events WHERE vault_id=1")
        with self.subTest("no event at all after the cursor"):
            signal, events_read, truncated, statements = self.catchup(1)
            self.assertEqual(signal, {
                "vault_id": 1, "revision": 3, "domains": ALL_DOMAINS, "has_gap": True,
            })
            self.assertEqual((events_read, truncated), (0, True))
            self.assert_high_water_and_one_page(statements)

    def test_one_shot_signal_clamps_a_negative_cursor_to_zero(self) -> None:
        self.publish(["files"], ["stats"])
        expected = {
            "vault_id": 1, "revision": 2, "domains": ["files", "stats"], "has_gap": False,
        }
        self.assertEqual(coalesced_catchup_signal(vault_id=1, after_revision=-4), expected)
        self.assertEqual(coalesced_catchup_signal(vault_id=1, after_revision=0), expected)
