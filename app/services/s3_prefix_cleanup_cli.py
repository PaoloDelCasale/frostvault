"""CLI to rerun S3 test-prefix cleanup and surface leftovers (issue #13)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .s3_prefix_cleanup import cleanup_prefix_versions
from ..storage import s3_client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete all object versions under CI/test prefixes and report leftovers."
    )
    parser.add_argument("--bucket", required=True)
    parser.add_argument(
        "--prefix",
        action="append",
        dest="prefixes",
        required=True,
        help="Prefix to clean (repeatable). Empty prefixes are refused.",
    )
    parser.add_argument("--report-path", default="")
    args = parser.parse_args(argv)

    client = s3_client()
    reports = []
    failed = False
    for prefix in args.prefixes:
        report = cleanup_prefix_versions(client, bucket=args.bucket, prefix=prefix)
        payload = {
            "bucket": report.bucket,
            "prefix": report.prefix,
            "deleted_versions": report.deleted_versions,
            "leftover_keys": list(report.leftover_keys),
            "ok": report.ok,
            "message": report.message,
        }
        reports.append(payload)
        status = "ok" if report.ok else "FAILED"
        print(
            f"[{status}] s3://{report.bucket}/{report.prefix}/ "
            f"deleted={report.deleted_versions} leftovers={len(report.leftover_keys)}"
        )
        if report.message:
            print(f"  {report.message}")
        if not report.ok:
            failed = True

    if args.report_path:
        path = Path(args.report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
