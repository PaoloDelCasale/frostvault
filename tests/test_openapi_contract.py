"""Regression contracts for response models and generated OpenAPI artifacts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api_models import JsonObjectResponse, response_model
from app.main import app


ROOT = Path(__file__).resolve().parents[1]
RAW_JSON_EXCEPTIONS = {"/api/admin/metadata-backups/download/{run_id}"}


class OpenApiResponseContractTests(unittest.TestCase):
    def test_every_json_route_has_an_explicit_nonempty_success_schema(self) -> None:
        schema = app.openapi()
        checked = 0
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            if not (route.path.startswith("/api/") or route.path in {"/health", "/ready"}):
                continue
            if route.path in RAW_JSON_EXCEPTIONS:
                continue
            checked += 1
            self.assertIsNotNone(route.response_model, route.path)
            operation = schema["paths"][route.path][next(iter(route.methods)).lower()]
            success = operation["responses"][str(route.status_code or 200)]
            response_schema = success["content"]["application/json"]["schema"]
            self.assertTrue(response_schema, route.path)
        self.assertGreater(checked, 100)

    def test_response_serialization_preserves_all_existing_fields(self) -> None:
        probe = FastAPI()
        payload = {
            "required": "value",
            "nullable": None,
            "union": 7,
            "nested": [{"known": True, "extension": {"future": "kept"}}],
        }

        @probe.get("/probe", response_model=response_model("CompatibilityProbe"))
        def compatibility_probe():
            return payload

        response = TestClient(probe).get("/probe")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)

    def test_nested_nullable_and_union_shapes_are_documented(self) -> None:
        schemas = app.openapi()["components"]["schemas"]
        self.assertEqual(
            schemas["ArchiveListItem"]["anyOf"],
            [
                {"$ref": "#/components/schemas/VaultFileListItem"},
                {"$ref": "#/components/schemas/DirectoryListItem"},
            ],
        )
        self.assertIn("null", schemas["AuthMethod"]["type"])
        self.assertNotEqual(
            schemas["ArchiveVersionSummary"]["additionalProperties"], False
        )

    def test_raw_download_and_metrics_are_explicit_openapi_exceptions(self) -> None:
        paths = app.openapi()["paths"]
        download = paths["/api/admin/metadata-backups/download/{run_id}"]["get"]
        metrics = paths["/metrics"]["get"]
        self.assertEqual(
            download["responses"]["200"]["content"]["application/octet-stream"]["schema"],
            {"type": "string", "format": "binary"},
        )
        self.assertEqual(
            metrics["responses"]["200"]["content"]["text/plain"]["schema"],
            {"type": "string"},
        )

    def test_storage_estimate_response_documents_atomic_price_book_identity(self) -> None:
        schema = app.openapi()
        operation = schema["paths"]["/api/admin/cost-estimates/storage"]["post"]
        response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            response,
            {"$ref": "#/components/schemas/StorageEstimateResponse"},
        )
        estimate = schema["components"]["schemas"]["StorageEstimateResponse"]
        self.assertIn("price_book_id", estimate["required"])
        self.assertIn("price_book_name", estimate["required"])
        self.assertIn("pricing_effective_at", estimate["required"])

    def test_committed_openapi_document_matches_the_application(self) -> None:
        committed = json.loads((ROOT / "frontend" / "openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(committed, app.openapi())

    def test_all_api_response_models_are_nonfiltering_contracts(self) -> None:
        for route in app.routes:
            if not isinstance(route, APIRoute) or route.path in RAW_JSON_EXCEPTIONS:
                continue
            if route.path.startswith("/api/") or route.path in {"/health", "/ready"}:
                self.assertTrue(
                    issubclass(route.response_model, JsonObjectResponse),
                    route.path,
                )


if __name__ == "__main__":
    unittest.main()
