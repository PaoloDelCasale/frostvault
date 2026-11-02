#!/usr/bin/env python3
"""Regenerate the AWS list prices bundled with FrostVault (#413).

FrostVault estimates storage and restore costs from public AWS list prices
that ship with the application and are not editable at runtime. This script
reads the public AWS Price List bulk API (no credentials) and rewrites
``app/services/aws_list_prices.json``. Run it by hand when AWS changes S3
prices, then review the diff:

    python scripts/update_aws_list_prices.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "app" / "services" / "aws_list_prices.json"
BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws"
# Commercial regions only: no GovCloud, China, Local Zones or "aws-other".
REGION_PATTERN = re.compile(r"^(af|ap|ca|eu|il|me|mx|sa|us)-[a-z]+-\d$")

# Usage type suffix (after the region prefix) of the first-tier storage price.
STORAGE_USAGE = {
    "STANDARD": ("AmazonS3", "TimedStorage-ByteHrs"),
    "STANDARD_IA": ("AmazonS3", "TimedStorage-SIA-ByteHrs"),
    "ONEZONE_IA": ("AmazonS3", "TimedStorage-ZIA-ByteHrs"),
    "INTELLIGENT_TIERING": ("AmazonS3", "TimedStorage-INT-FA-ByteHrs"),
    "GLACIER_IR": ("AmazonS3", "TimedStorage-GIR-ByteHrs"),
    "GLACIER": ("AmazonS3", "TimedStorage-GlacierByteHrs"),
    "DEEP_ARCHIVE": ("AmazonS3GlacierDeepArchive", "TimedStorage-GDA-ByteHrs"),
}
# (offer, usage type suffix, operation) of each restore tier, per GB.
RESTORE_USAGE = {
    "GLACIER": {
        "Expedited": ("AmazonS3", "Expedited-Retrieval-Bytes", None),
        "Standard": ("AmazonS3", "Standard-Retrieval-Bytes", "RestoreObject"),
        "Bulk": ("AmazonS3", "Bulk-Retrieval-Bytes", "RestoreObject"),
    },
    "DEEP_ARCHIVE": {
        "Standard": ("AmazonS3GlacierDeepArchive", "Standard-Retrieval-Bytes", None),
        "Bulk": ("AmazonS3GlacierDeepArchive", "Bulk-Retrieval-Bytes", None),
    },
}
# Per-GB GET retrieval fee of the classes read without a RestoreObject.
INSTANT_USAGE = {
    "STANDARD_IA": "Retrieval-SIA",
    "ONEZONE_IA": "Retrieval-ZIA",
    "GLACIER_IR": "Retrieval-GIR",
}


def _fetch(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
        return json.load(response)


def _prices(offer: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return (attributes, price dimension) pairs of an offer file."""
    terms = offer.get("terms", {}).get("OnDemand", {})
    rows = []
    for sku, product in offer.get("products", {}).items():
        for term in terms.get(sku, {}).values():
            for dimension in term["priceDimensions"].values():
                rows.append((product["attributes"], dimension))
    return rows


def _lookup(
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
    usage: str,
    operation: str | None = None,
) -> float | None:
    for attributes, dimension in rows:
        # Usage types carry a region prefix (``EUS1-``, ``EU-``), except in
        # us-east-1; other products such as ``Files-`` share the suffix.
        usage_type = str(attributes.get("usagetype", ""))
        if not re.fullmatch(rf"(?:[A-Z]{{2,4}}\d?-)?{re.escape(usage)}", usage_type):
            continue
        if operation is not None and attributes.get("operation") != operation:
            continue
        # Intelligent-Tiering archive tiers reuse the restore usage types.
        if "INT" in str(attributes.get("volumeType", "")):
            continue
        if str(dimension.get("beginRange", "0")) != "0":
            continue
        return float(dimension["pricePerUnit"]["USD"])
    return None


def _region_prices(region: str) -> tuple[str, dict[str, Any] | None, str]:
    offers = {
        name: _prices(_fetch(f"{BASE}/{name}/current/{region}/index.json"))
        for name in ("AmazonS3", "AmazonS3GlacierDeepArchive")
    }
    storage = {
        storage_class: _lookup(offers[offer], usage)
        for storage_class, (offer, usage) in STORAGE_USAGE.items()
    }
    restore = {
        storage_class: {
            tier: _lookup(offers[offer], usage, operation)
            for tier, (offer, usage, operation) in tiers.items()
        }
        for storage_class, tiers in RESTORE_USAGE.items()
    }
    instant = {
        storage_class: _lookup(offers["AmazonS3"], usage)
        for storage_class, usage in INSTANT_USAGE.items()
    }
    values = [
        *storage.values(),
        *instant.values(),
        *(rate for tiers in restore.values() for rate in tiers.values()),
    ]
    if any(value is None for value in values):
        return region, None, "incomplete price list"
    return (
        region,
        {"storage": storage, "restore": restore, "instant_retrieval": instant},
        "",
    )


def main() -> None:
    index = _fetch(f"{BASE}/AmazonS3/current/region_index.json")
    regions = sorted(r for r in index["regions"] if REGION_PATTERN.match(r))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_region_prices, regions))
    prices = {region: data for region, data, _ in results if data is not None}
    for region, data, reason in results:
        if data is None:
            print(f"skipped {region}: {reason}", file=sys.stderr)
    publication = _fetch(f"{BASE}/AmazonS3/current/us-east-1/index.json")[
        "publicationDate"
    ]
    OUTPUT.write_text(
        json.dumps(
            {
                "source": f"{BASE}/AmazonS3 and AmazonS3GlacierDeepArchive",
                "currency": "USD",
                "publication_date": publication,
                "regions": prices,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(prices)} regions to {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
