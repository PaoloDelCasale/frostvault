"""Disposable CI fault process, exits immediately after provider acceptance.

Only invoked by test_upload_provenance_worker. Fixture DSN arrives on stdin,
never process arguments; provider bytes/Metadata persist independently on disk.
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from psycopg.conninfo import conninfo_to_dict
from app import storage
from app.catalog import ArchiveCatalog
from app.database import db
from tests.upload_provenance_provider import VersionedProvider
from tests.upload_support import claimed_job_row


def main():
    config = json.load(sys.stdin)
    if not os.environ.get("TEST_POSTGRES_URL") or not conninfo_to_dict(config["dsn"]).get("dbname", "").startswith("fvtest_"):
        raise RuntimeError("Disposable CI PostgreSQL configuration is required")
    provider = VersionedProvider()
    def crash(stage, **kwargs):
        if stage == "accepted":
            provider.save(config["state_path"])
            os._exit(31)
    provider.hook = crash
    with patch("app.database.settings", SimpleNamespace(database_dsn=config["dsn"])), patch("app.storage.validate_cloud_vault"), patch("app.storage.s3_client", return_value=provider), patch("app.storage.upload_file", side_effect=provider.upload):
        with db() as connection:
            claimed = ArchiveCatalog(connection).claim_job(job_id=config["job_id"], claim_token=str(uuid.uuid4()), claimed_at=storage.now_iso(), claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(), restore_due_before=storage.now_iso())
            job = claimed_job_row(connection, config["job_id"], claimed)
        storage.process_job(job)
    raise AssertionError("Fault hook did not terminate the worker")


if __name__ == "__main__":
    main()
