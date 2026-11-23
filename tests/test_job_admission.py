"""Job admission as a service: its refusals and its single pricing pass (#796).

``tests/test_job_admission_http.py`` fixes what the routes answer. Here the
service is called directly: the refusals no route can reach, the domain error
of each kind, and the price list read once for a whole request.
"""
from __future__ import annotations

from unittest.mock import patch

from app.services import job_admission
from app.services.cost_estimates import list_prices
from tests.fixtures import FrostVaultTestCase
from tests.test_recovery_verification import _add_cloud_only_version, _prepare_cloud_only_version

GIB = 1024**3


class JobAdmissionServiceTests(FrostVaultTestCase):
    owner = None
    settings_overrides = dict(
        aws_region="eu-south-1", restore_tier="Standard", restore_days=3,
        restore_high_impact_gib=100, restore_high_impact_eur=10.0,
    )

    def setUp(self) -> None:
        super().setUp()
        _source, self.glacier = _prepare_cloud_only_version(
            self.database, self.tmp_path, relative_path="photos/c.jpg", payload=b"x",
            storage_class="GLACIER",
        )
        self.deep = _add_cloud_only_version(
            self.database, relative_path="photos/a.jpg", payload=b"x",
            storage_class="DEEP_ARCHIVE", provider_version_id="s3-a",
            object_key="docs/photos/a.jpg",
        )
        with self.database.connect() as connection:
            connection.execute("UPDATE archive_versions SET size=%s", (GIB,))

    def assert_refused(self, kind: type[Exception], reason: str, *args, **kwargs) -> None:
        with self.assertRaises(kind) as raised:
            job_admission.queue_jobs(*args, **kwargs)
        self.assertIs(type(raised.exception), kind)
        self.assertEqual(str(raised.exception), reason)

    def jobs(self) -> int:
        with self.database.connect() as connection:
            return connection.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]

    def test_requests_that_contradict_themselves_are_invalid(self) -> None:
        selection = [("photos", True)]
        self.assert_refused(
            job_admission.InvalidAdmission, "Invalid recovery selection",
            "", "upload", 2, recovery_selection=selection)
        self.assert_refused(
            job_admission.InvalidAdmission, "Invalid recovery selection",
            "", "recover", 2, recovery_selection=selection, archive_version_id=self.deep)
        self.assert_refused(
            job_admission.InvalidAdmission, "Invalid operation", "photos/a.jpg", "rename", 2)
        self.assert_refused(
            job_admission.InvalidAdmission,
            "archive_version_id is only valid for single-file recover",
            "photos/a.jpg", "upload", 2, archive_version_id=self.deep)
        self.assertEqual(self.jobs(), 0)

    def test_an_unknown_or_stopped_vault_admits_nothing(self) -> None:
        self.assert_refused(
            job_admission.AdmissionTargetNotFound, "Vault not found", "photos/a.jpg", "recover", 99)
        with self.database.connect() as connection:
            connection.execute("UPDATE vaults SET lifecycle='stopping' WHERE id=2")
        self.assert_refused(
            job_admission.AdmissionRefused, "Vault is no longer protected",
            "photos/a.jpg", "recover", 2)
        self.assertEqual(self.jobs(), 0)

    def test_every_refusal_is_an_admission_error(self) -> None:
        for kind in (job_admission.InvalidAdmission, job_admission.AdmissionTargetNotFound,
                     job_admission.AdmissionRefused, job_admission.LocalStorageUnavailable):
            self.assertTrue(issubclass(kind, job_admission.AdmissionError))
        localized = job_admission.AdmissionRefused("api.local_release_blocked_path", path="photos")
        self.assertEqual(localized.message_params, {"path": "photos"})
        self.assertIn("photos", str(localized))

    def test_a_request_reads_the_price_list_once(self) -> None:
        requests = {
            "estimate of one file": lambda: job_admission.estimate_recovery(
                2, path="photos/c.jpg"),
            "estimate of a folder": lambda: job_admission.estimate_recovery(
                2, path="photos", is_directory=True),
            "estimate of a selection": lambda: job_admission.estimate_recovery(
                2, selection=[("photos/a.jpg", False), ("photos/c.jpg", False)]),
            "recovery of one file": lambda: job_admission.queue_jobs(
                "photos/c.jpg", "recover", 2),
            "recovery of a folder": lambda: job_admission.queue_jobs(
                "photos", "recover", 2, True),
        }
        for name, request in requests.items():
            with self.subTest(name), patch.object(
                    job_admission, "list_prices", wraps=list_prices) as prices:
                request()
                self.assertEqual(prices.call_count, 1)

    def test_the_estimate_and_the_queued_request_carry_the_same_amount(self) -> None:
        for path, directory in (("photos/c.jpg", False), ("photos", True)):
            with self.subTest(path=path):
                estimate = job_admission.estimate_recovery(2, path=path, is_directory=directory)
                queued = job_admission.queue_jobs(path, "recover", 2, directory)
                self.assertEqual(
                    queued["estimated_cost_eur"], estimate["estimate"]["estimated_cost_eur"])
                self.assertEqual(
                    queued["estimated_hours"], estimate["estimate"]["estimated_hours"])
                self.assertEqual(queued["restore_tier"], estimate["estimate"]["tier"])
                self.assertEqual(queued["restore_days"], estimate["estimate"]["days"])

    def test_request_impact_separates_archived_rows_from_new_restores(self) -> None:
        rate = list_prices("eu-south-1").restore_rates
        impact = job_admission.recovery_request_impact([
            {"bytes": 2 * GIB, "storage_class": "DEEP_ARCHIVE", "restore_state": "restoring"},
            {"bytes": 3 * GIB, "storage_class": "GLACIER", "restore_state": None},
            {"bytes": 5 * GIB, "storage_class": "STANDARD"},
        ], tier="Standard")
        self.assertEqual(impact, {
            "requires_restore": True,
            "size_bytes": 3 * GIB,
            "estimated_cost_eur": 3 * rate["GLACIER"]["Standard"],
            "estimated_hours": 5.0,
            "high_impact": False,
            "archived": True,
            "archived_hours": 12.0,
        })
        nothing = job_admission.recovery_request_impact(
            [{"bytes": 5 * GIB, "storage_class": "STANDARD"}])
        self.assertEqual(
            (nothing["requires_restore"], nothing["archived"], nothing["archived_hours"],
             nothing["high_impact"]), (False, False, 0, False))
