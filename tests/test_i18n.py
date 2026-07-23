from __future__ import annotations

import unittest

from app import i18n


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
    def test_english_catalog_defines_every_critical_key(self) -> None:
        self.assertEqual(i18n.missing_critical_keys("en"), frozenset())

    def test_italian_catalog_defines_every_critical_key(self) -> None:
        self.assertEqual(i18n.missing_critical_keys("it"), frozenset())

    def test_critical_keys_are_not_orphaned_from_english_catalog(self) -> None:
        self.assertEqual(i18n.unused_critical_keys(), frozenset())

    def test_english_and_italian_catalogs_have_matching_keys(self) -> None:
        self.assertEqual(i18n.locale_key_parity(), {})

    def test_email_templates_render_from_stable_keys(self) -> None:
        subject = i18n.render_email(
            "email.subject.admin_action",
            locale="it",
            vault="Family",
        )
        body = i18n.render_email(
            "email.body.admin_action",
            locale="en",
            action="quota change",
            vault="Family",
        )
        self.assertEqual(subject, "Azione amministratore sul vault Family")
        self.assertEqual(
            body,
            "An administrator performed quota change on vault Family.",
        )


if __name__ == "__main__":
    unittest.main()
