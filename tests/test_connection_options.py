"""Every database session starts with serial query plans (#718)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.connection_options import SERIAL_QUERY_OPTION, session_options


class SessionOptionsTests(unittest.TestCase):
    def test_serial_plans_are_added_to_an_empty_dsn(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(session_options(""), SERIAL_QUERY_OPTION)

    def test_the_dsn_options_are_kept(self):
        dsn = "postgresql+psycopg://u@h/db?options=-c%20statement_timeout%3D5s"
        with patch.dict("os.environ", {"PGOPTIONS": "-c work_mem=8MB"}, clear=True):
            self.assertEqual(session_options(dsn), f"-c statement_timeout=5s {SERIAL_QUERY_OPTION}")

    def test_pgoptions_is_kept_when_the_dsn_has_none(self):
        # An explicit libpq ``options`` would otherwise replace PGOPTIONS.
        with patch.dict("os.environ", {"PGOPTIONS": "-c work_mem=8MB"}, clear=True):
            self.assertEqual(session_options("host=h dbname=db"), f"-c work_mem=8MB {SERIAL_QUERY_OPTION}")


if __name__ == "__main__":
    unittest.main()
