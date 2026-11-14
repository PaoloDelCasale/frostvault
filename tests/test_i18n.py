from __future__ import annotations

import unittest

from app import i18n


class PluralAndFormattingTests(unittest.TestCase):
    """#654: counted strings and locale-aware sizes and amounts."""

    def test_count_selects_one_and_other_in_both_languages(self) -> None:
        self.assertEqual(i18n.translate("nav.problem_count", locale="it", count=1), "1 problema")
        self.assertEqual(i18n.translate("nav.problem_count", locale="it", count=3), "3 problemi")
        self.assertEqual(i18n.translate("nav.problem_count", locale="it", count=0), "0 problemi")
        self.assertEqual(i18n.translate("nav.problem_count", locale="en", count=1), "1 problem")
        self.assertEqual(i18n.translate("nav.problem_count", locale="en", count=2), "2 problems")

    def test_plural_key_without_count_and_unknown_keys(self) -> None:
        self.assertTrue(i18n.has_message("nav.problem_count"))
        self.assertFalse(i18n.has_message("nav.problem_count_two"))
        self.assertEqual(i18n.translate("missing.counted", count=1), "missing.counted")
        self.assertIsNone(i18n.plural_category(True))
        self.assertIsNone(i18n.plural_category("1"))
        self.assertEqual(i18n.plural_category(1.5), "other")

    def test_format_bytes_uses_binary_units_and_locale_separator(self) -> None:
        self.assertEqual(i18n.format_bytes(0, "en"), "0 B")
        self.assertEqual(i18n.format_bytes(1536, "en"), "1.5 KiB")
        self.assertEqual(i18n.format_bytes(1536, "it"), "1,5 KiB")
        self.assertEqual(i18n.format_bytes(2 * 1024**3, "it"), "2,0 GiB")
        self.assertEqual(i18n.format_bytes(1024**4, "en"), "1.0 TiB")
        self.assertEqual(i18n.format_bytes("junk", "en"), "—")

    def test_format_money_matches_the_spa(self) -> None:
        self.assertEqual(i18n.format_money("1234.5", "en"), "€1,234.50")
        self.assertEqual(i18n.format_money("1234.5", "it"), "1.234,50 €")
        self.assertEqual(i18n.format_money("0.00002", "it"), "< 0,01 €")
        self.assertEqual(i18n.format_money("3.42", "en", "USD"), "3.42 USD")
        self.assertEqual(i18n.format_money("nan", "en"), "—")
        self.assertEqual(i18n.format_money("x", "en"), "—")


class TranslateTests(unittest.TestCase):
    def test_translate_returns_english_default_for_known_key(self) -> None:
        self.assertEqual(i18n.translate("ui.sign_out"), "Sign out")

    def test_translate_returns_italian_for_known_key(self) -> None:
        self.assertEqual(i18n.translate("ui.sign_out", locale="it"), "Esci")

    def test_translate_formats_named_parameters(self) -> None:
        self.assertEqual(
            i18n.translate("job.recovered_to", locale="en", target="/tmp/file"),
            "Recovered to /tmp/file",
        )
        self.assertEqual(
            i18n.translate("job.recovered_to", locale="it", target="/tmp/file"),
            "Recuperato in /tmp/file",
        )

    def test_unknown_locale_falls_back_to_english(self) -> None:
        self.assertEqual(i18n.translate("ui.sign_out", locale="fr"), "Sign out")

    def test_missing_key_returns_key_itself(self) -> None:
        self.assertEqual(i18n.translate("missing.not_defined"), "missing.not_defined")


class LocaleNormalizationTests(unittest.TestCase):
    def test_normalize_accepts_language_tags(self) -> None:
        self.assertEqual(i18n.normalize_locale("en"), "en")
        self.assertEqual(i18n.normalize_locale("en-US"), "en")
        self.assertEqual(i18n.normalize_locale("it-IT"), "it")
        self.assertEqual(i18n.normalize_locale("IT"), "it")

    def test_normalize_rejects_unknown_to_default(self) -> None:
        self.assertEqual(i18n.normalize_locale("de"), "en")
        self.assertEqual(i18n.normalize_locale(""), "en")
        self.assertEqual(i18n.normalize_locale(None), "en")


class CatalogPathSafetyTests(unittest.TestCase):
    """Seam: i18n.resolved_catalog_path — on-disk catalogs are allowlisted constants."""

    def test_path_like_locale_resolves_to_english_catalog_file(self) -> None:
        path = i18n.resolved_catalog_path("../../etc/passwd")
        self.assertEqual(path, i18n.LOCALES_DIR / "en.json")
        self.assertEqual(path.name, "en.json")

    def test_italian_locale_resolves_to_italian_catalog_file(self) -> None:
        path = i18n.resolved_catalog_path("it-IT")
        self.assertEqual(path, i18n.LOCALES_DIR / "it.json")
        self.assertTrue(path.is_file())

    def test_resolved_path_never_embeds_raw_locale_segments(self) -> None:
        raw = "en/../../tmp/evil"
        path = i18n.resolved_catalog_path(raw)
        self.assertNotIn("..", path.parts)
        self.assertEqual(path.parent, i18n.LOCALES_DIR)


class AvailableLocalesTests(unittest.TestCase):
    def test_available_locales_are_english_and_italian(self) -> None:
        self.assertEqual(i18n.available_locales(), ("en", "it"))


class CatalogIntegrityTests(unittest.TestCase):
    def test_upload_activity_chrome_keys_are_critical(self) -> None:
        expected = {
            "activity.heading", "activity.by_vault", "activity.idle",
            "activity.stability_wait", "activity.queued", "activity.uploading",
            "activity.verifying", "activity.sent", "activity.stable_in",
            "activity.waiting_observation", "activity.admission",
            "activity.current_work", "activity.unavailable",
        }
        self.assertEqual(expected - i18n.CRITICAL_KEYS, set())

    def test_english_catalog_defines_every_critical_key(self) -> None:
        self.assertEqual(i18n.missing_critical_keys("en"), frozenset())

    def test_italian_catalog_defines_every_critical_key(self) -> None:
        self.assertEqual(i18n.missing_critical_keys("it"), frozenset())

    def test_critical_keys_are_not_orphaned_from_english_catalog(self) -> None:
        self.assertEqual(i18n.unused_critical_keys(), frozenset())

    def test_english_and_italian_catalogs_have_matching_keys(self) -> None:
        self.assertEqual(i18n.locale_key_parity(), {})


if __name__ == "__main__":
    unittest.main()


class ReconciliationReasonCatalogTests(unittest.TestCase):
    """The SPA renders ``reconciliation.reason.<reason>`` for every backend reason."""

    def emitted_reasons(self) -> set[str]:
        import re
        from pathlib import Path
        from app.services import upload_provenance
        root = Path(__file__).resolve().parents[1] / "app"
        source = "".join(path.read_text() for path in root.rglob("*.py"))
        literals = set(re.findall(r'UploadBlocked\(\s*"([a-z_]+)"', source))
        literals |= set(re.findall(r'_reject\([^)]*"([a-z_]+)"\)', source))
        literals |= {"retry_budget_exhausted", "operation_failed", "transient"}
        return literals | set(upload_provenance.REMEDIES)

    def test_every_reason_the_backend_can_emit_has_an_english_and_italian_text(self) -> None:
        reasons = self.emitted_reasons()
        self.assertIn("already_resolved", reasons)
        self.assertIn("target_superseded", reasons)
        import json
        from pathlib import Path
        for locale in ("en", "it"):
            catalog = json.loads((Path(__file__).resolve().parents[1] / "app" / "locales" / f"{locale}.json").read_text())
            missing = sorted(r for r in reasons if f"reconciliation.reason.{r}" not in catalog)
            self.assertEqual(missing, [], f"{locale}: raw keys would be shown for {missing}")
