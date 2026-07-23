"""Security-sensitive UI messages must not use unsafe HTML interpolation.

Seams under test:
- ``notify`` in app.js assigns through ``textContent``
- ``escapeHtml`` escapes angle brackets before any HTML embedding
- ``loadStats`` / ``loadFiles`` escape catalog-derived DOM text before ``innerHTML``
- ``present_job_message`` / ``translate`` treat catalogs as plain text, not HTML
"""

from __future__ import annotations

import unittest
from pathlib import Path

from app import i18n
from app.main import _tojson_filter


ROOT = Path(__file__).resolve().parents[1]


class HtmlSafetyTests(unittest.TestCase):
    def test_notify_assigns_message_via_text_content(self) -> None:
        source = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        self.assertIn(
            'node.querySelector(".notice-message").textContent = message;',
            source,
        )
        self.assertNotIn(
            'node.querySelector(".notice-message").innerHTML = message',
            source,
        )

    def test_escape_html_is_defined_for_dynamic_table_cells(self) -> None:
        source = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("function escapeHtml(value)", source)
        self.assertIn("escapeHtml(details)", source)

    def test_summary_cards_escape_catalog_labels_before_inner_html(self) -> None:
        source = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("escapeHtml(label)", source)
        self.assertNotRegex(
            source,
            r"#summary\"\)\.innerHTML = cards\.map\(\(\[label,[^\]]*\]\) =>\s*"
            r"`<div class=\"card\"><span>\$\{label\}</span>",
        )

    def test_directory_state_escapes_state_label_before_inner_html(self) -> None:
        source = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("escapeHtml(stateLabel(file.state))", source)

    def test_quota_error_messages_are_escaped_before_inner_html(self) -> None:
        source = (ROOT / "app/static/vault_access.js").read_text(encoding="utf-8")
        self.assertIn("escapeHtml(message)", source)

    def test_translate_does_not_interpret_html_in_parameters(self) -> None:
        rendered = i18n.translate(
            "job.recovered_to",
            locale="en",
            target='<img src=x onerror=alert(1)>',
        )
        self.assertEqual(
            rendered,
            "Recovered to <img src=x onerror=alert(1)>",
        )
        self.assertNotIn("&lt;", rendered)

    def test_present_job_message_keeps_params_as_plain_text(self) -> None:
        row = {
            "message_key": "job.retrying_transient",
            "message_params": '{"error": "<b>SlowDown</b>"}',
            "message": "unused",
        }
        self.assertEqual(
            i18n.present_job_message(row, "en"),
            "Retrying after transient error: <b>SlowDown</b>",
        )

    def test_tojson_filter_cannot_terminate_a_script_element(self) -> None:
        rendered = str(_tojson_filter({"value": "</script>&\u2028\u2029"}))

        self.assertNotIn("</script>", rendered)
        self.assertNotIn("&", rendered)
        self.assertNotIn("\u2028", rendered)
        self.assertNotIn("\u2029", rendered)
        self.assertIn("\\u003c/script\\u003e", rendered)
        self.assertIn("\\u2028", rendered)
        self.assertIn("\\u2029", rendered)


if __name__ == "__main__":
    unittest.main()
