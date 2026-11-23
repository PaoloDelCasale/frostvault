"""The inbox is given the Problem families at one explicit place (#804).

No family may be left without its resolution, and none is registered as a side
effect of importing the module that declares it.
"""
import importlib
import pkgutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import app.services
from app.services import notifications as inbox
from app.services import problem_families
from tests.fixtures import FrostVaultTestCase, seed_vault


def declared_families() -> dict[str, inbox.ProblemFamily]:
    """Every ``ProblemFamily`` a service module declares, by where it is."""
    found = {}
    for info in pkgutil.iter_modules(app.services.__path__):
        module = importlib.import_module(f"app.services.{info.name}")
        for name, value in vars(module).items():
            if isinstance(value, inbox.ProblemFamily):
                found[f"{info.name}.{name}"] = value
    return found


class ProblemFamilyListTests(unittest.TestCase):
    def test_every_declared_family_is_given_to_the_inbox(self):
        declared = declared_families()
        self.assertEqual(len(declared), 9)
        missing = sorted(where for where, family in declared.items()
                         if family not in problem_families.FAMILIES)
        self.assertEqual(missing, [])
        self.assertEqual(len(problem_families.FAMILIES), len(declared))

    def test_install_gives_the_inbox_every_family_and_its_events(self):
        problem_families.install()
        installed = inbox._problem_resolutions()
        self.assertEqual(tuple(installed), problem_families.FAMILIES)
        self.assertEqual(
            sorted(event for family in installed for event in family.events),
            ["archive_version_missing", "catalog_audit_failed", "cloud_only_conflict",
             "duplicate_cleanup_capped", "file_unreadable", "job_failed", "job_failed_group",
             "mirror_cleanup_capped", "upload_reconciliation", "vault_bucket_failed"])

    def test_an_event_declared_by_two_families_is_refused(self):
        family = inbox.ProblemFamily(frozenset({"job_failed"}), "n.event='job_failed'")
        domain = inbox.ProblemDomain(families=(family, family), closed_upload_failure_sql="FALSE",
                                     forget_unit_dirty_marks=lambda connection: None)
        with patch.object(inbox, "_problem_domain", None):
            with self.assertRaises(ValueError):
                inbox.install_problem_domain(domain)

    def test_a_family_without_events_or_resolution_is_refused(self):
        for family in (inbox.ProblemFamily(frozenset(), "TRUE"),
                       inbox.ProblemFamily(frozenset({"job_failed"}), "  ")):
            domain = inbox.ProblemDomain(families=(family,), closed_upload_failure_sql="FALSE",
                                         forget_unit_dirty_marks=lambda connection: None)
            with self.subTest(family=family), patch.object(inbox, "_problem_domain", None):
                with self.assertRaises(ValueError):
                    inbox.install_problem_domain(domain)

    def test_importing_the_declaring_modules_installs_nothing(self):
        """Whatever is imported first, only ``install()`` gives the families."""
        script = (
            "import importlib, pkgutil, app.services\n"
            "from app.services import notifications as inbox\n"
            "for info in pkgutil.iter_modules(app.services.__path__):\n"
            "    importlib.import_module('app.services.' + info.name)\n"
            "assert inbox._problem_domain is None, 'installed by an import'\n"
            "from app.services import problem_families\n"
            "problem_families.install()\n"
            "assert tuple(inbox._problem_resolutions()) == problem_families.FAMILIES\n"
            "import app.main\n"
            "print('ok')\n"
        )
        done = subprocess.run([sys.executable, "-W", "ignore", "-c", script],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"), done.stderr[-2000:])

    def test_the_application_installs_the_families_when_imported(self):
        script = (
            "from app.services import notifications as inbox\n"
            "assert inbox._problem_domain is None\n"
            "import app.main\n"
            "from app.services import problem_families\n"
            "assert tuple(inbox._problem_resolutions()) == problem_families.FAMILIES\n"
            "print('ok')\n"
        )
        done = subprocess.run([sys.executable, "-W", "ignore", "-c", script],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"), done.stderr[-2000:])


class InboxWithoutFamiliesTests(FrostVaultTestCase):
    def test_an_inbox_given_no_families_refuses_to_reconcile(self):
        with self.database.connect() as c:
            vault_id = seed_vault(c)["id"]
            inbox.set_problem(c, key="internal_error:1", event="internal_error",
                              vault_id=vault_id, active=True, email=False)
            with patch.object(inbox, "_problem_domain", None):
                with self.assertRaises(RuntimeError):
                    inbox.reconcile_problem_states(c)
                with self.assertRaises(RuntimeError):
                    inbox.enqueue_job_terminal_notification_best_effort(c, job_id=1)
            inbox.reconcile_problem_states(c)
            row = c.execute("SELECT problem_status FROM notifications").fetchone()
            self.assertEqual(row["problem_status"], "open")
