"""Regression contracts for response models and generated OpenAPI artifacts."""

from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from starlette.routing import Route

from app.api_models import JsonObjectResponse, documented_schemas, passthrough, response_model
from app.main import app, lifespan
from tests import fixtures


ROOT = Path(__file__).resolve().parents[1]
# Binary download and SSE streams are non-JSON success bodies with explicit
# OpenAPI content types (octet-stream / text/event-stream), not JsonObjectResponse.
RAW_RESPONSE_EXCEPTIONS = {
    "/api/general/metadata-backups/download/{run_id}",
    "/api/recovery-kit/download",
    "/api/vaults/{vault_id}/catalog/events",
}
# Backward-compatible alias used by older call sites in this module.
RAW_JSON_EXCEPTIONS = RAW_RESPONSE_EXCEPTIONS


class OpenApiResponseContractTests(unittest.TestCase):
    def test_every_json_route_has_an_explicit_nonempty_success_schema(self) -> None:
        schema = app.openapi()
        checked = 0
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            if not (
                route.path.startswith("/api/") or route.path in {"/health", "/ready"}
            ):
                continue
            if route.path in RAW_JSON_EXCEPTIONS:
                continue
            checked += 1
            self.assertIsNotNone(route.response_model, route.path)
            operation = schema["paths"][route.path][next(iter(route.methods)).lower()]
            success = operation["responses"][str(route.status_code or 200)]
            response_schema = success["content"]["application/json"]["schema"]
            self.assertTrue(response_schema, route.path)
        # Guard against an empty traversal, not a minimum API size: the core
        # simplification deliberately removes routes (#410).
        self.assertGreater(checked, 0)

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
        self.assertIn({"type": "null"}, schemas["AuthMethod"]["anyOf"])
        self.assertNotEqual(
            schemas["ArchiveVersionItem"]["additionalProperties"], False
        )

    def test_raw_download_metrics_and_catalog_sse_are_explicit_openapi_exceptions(
        self,
    ) -> None:
        paths = app.openapi()["paths"]
        download = paths["/api/general/metadata-backups/download/{run_id}"]["get"]
        metrics = paths["/metrics"]["get"]
        catalog_events = paths["/api/vaults/{vault_id}/catalog/events"]["get"]
        self.assertEqual(
            download["responses"]["200"]["content"]["application/octet-stream"]["schema"],
            {"type": "string", "format": "binary"},
        )
        self.assertEqual(
            metrics["responses"]["200"]["content"]["text/plain"]["schema"],
            {"type": "string"},
        )
        self.assertEqual(
            catalog_events["responses"]["200"]["content"]["text/event-stream"][
                "schema"
            ],
            {"type": "string"},
        )
        self.assertNotIn(
            "application/json",
            catalog_events["responses"]["200"].get("content", {}),
        )

    def test_storage_estimate_response_documents_region_and_pricing_date(self) -> None:
        schema = app.openapi()
        operation = schema["paths"]["/api/general/cost-estimates/storage"]["post"]
        response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            response,
            {"$ref": "#/components/schemas/StorageEstimateResponse"},
        )
        estimate = schema["components"]["schemas"]["StorageEstimateResponse"]
        self.assertIn("region", estimate["required"])
        self.assertNotIn("price_book_id", estimate["properties"])
        self.assertIn("pricing_effective_at", estimate["required"])

    def test_files_and_me_have_no_offline_cache_protocol(self) -> None:
        schema = app.openapi()
        operation = schema["paths"]["/api/vaults/{vault_id}/files"]["get"]
        self.assertEqual(
            [parameter["name"] for parameter in operation["parameters"] if parameter["in"] != "query"],
            ["vault_id"],
        )
        self.assertNotIn("409", operation["responses"])
        self.assertNotIn("headers", operation["responses"]["200"])
        self.assertNotIn("offline_cache_generation", schema["components"]["schemas"]["MeResponse"]["properties"])

    def test_pause_resume_have_explicit_post_contract_and_separate_inventory_flag(self) -> None:
        schema = app.openapi()
        for action in ("pause", "resume"):
            path = schema["paths"][f"/api/vaults/{{vault_id}}/{action}"]
            self.assertEqual(set(path), {"post"})
            self.assertEqual(path["post"]["responses"]["200"]["content"]["application/json"]["schema"],
                             {"$ref": "#/components/schemas/VaultPauseResponse"})
        for name in ("GeneralVault", "VaultListItem"):
            self.assertEqual(schema["components"]["schemas"][name]["properties"]["paused"], {"type": "boolean"})

    def test_sign_in_answers_are_documented_from_their_models(self) -> None:
        document = app.openapi()
        schemas = document["components"]["schemas"]
        answers = {
            ("/api/login", "post"): "SessionMessageResponse",
            ("/api/logout", "post"): "SessionMessageResponse",
            ("/api/reauth", "post"): "ReauthResponse",
            ("/api/me", "get"): "MeResponse",
            ("/api/login/options", "get"): "LoginOptionsResponse",
            ("/api/i18n/catalog", "get"): "I18nCatalogResponse",
            ("/api/locale", "put"): "LocaleUpdateResponse",
        }
        for (path, method), name in answers.items():
            with self.subTest(path=path):
                answer = document["paths"][path][method]["responses"]["200"]
                self.assertEqual(answer["content"]["application/json"]["schema"],
                                 {"$ref": f"#/components/schemas/{name}"})
                self.assertIs(schemas[name]["additionalProperties"], False)
                self.assertNotIn(name, documented_schemas())
        # Both blocks are always in the answer, null when there is none.
        self.assertLessEqual({"vault", "stopped_vault"}, set(schemas["MeResponse"]["required"]))
        self.assertEqual(set(schemas["MeStoppedVault"]["required"]),
                         set(schemas["MeStoppedVault"]["properties"]))

    def test_admin_vault_schema_is_a_closed_non_secret_contract(self) -> None:
        vault = app.openapi()["components"]["schemas"]["GeneralVault"]
        self.assertFalse(vault["additionalProperties"])
        self.assertNotIn("rclone_remote", vault["properties"])
        self.assertTrue(
            {
                "crypt_password_ciphertext",
                "crypt_password2_ciphertext",
            }.isdisjoint(vault["properties"])
        )

    def test_no_retired_encryption_or_per_vault_recovery_contracts(self) -> None:
        schema = app.openapi()
        self.assertFalse(any(path.startswith("/api/vault/recovery") for path in schema["paths"]))
        self.assertFalse(any(name in {"RecoveryExport", "RecoveryCustody", "EncryptionMode"} for name in schema["components"]["schemas"]))
        self.assertIn("/api/recovery-kit/download", schema["paths"])
        self.assertNotIn("/api/vaults/{vault_id}/relocate", schema["paths"])
        for name in ("VaultSelfServiceCreate", "VaultCreateResponse", "GeneralVault"):
            properties = schema["components"]["schemas"][name]["properties"]
            self.assertFalse(any("encryption" in key or "custody" in key or "recovery_export" in key for key in properties))

    def test_committed_openapi_document_matches_the_application(self) -> None:
        text = (ROOT / "frontend" / "openapi.json").read_text(encoding="utf-8")
        committed = json.loads(text)
        self.assertEqual(committed, app.openapi())
        # scripts/export_openapi.py is the file's only writer: byte for byte.
        spec = importlib.util.spec_from_file_location(
            "export_openapi", ROOT / "scripts" / "export_openapi.py")
        exporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(exporter)
        self.assertEqual(exporter.document(), text)
        # The routers are registered in the order the routes always had (#796).
        self.assertEqual(list(committed["paths"]), list(app.openapi()["paths"]))

    def test_the_route_table_is_flat_and_the_spa_fallback_matches_last(self) -> None:
        # Every route sits on the application itself: an included router stays
        # nested and is resolved again at every request, and its lifespan wraps
        # the application's (#796).
        # (plain ``Route``: the documentation pages, when they are enabled).
        self.assertEqual({type(route) for route in app.routes} - {Route}, {APIRoute})
        routes = [route for route in app.routes if isinstance(route, APIRoute)]
        self.assertIs(app.router.lifespan_context, lifespan)
        # No router is registered twice and none is left out of the document.
        self.assertEqual(len({(route.path, *sorted(route.methods)) for route in routes}), len(routes))
        documented = {route.path_format for route in routes if route.include_in_schema}
        self.assertEqual(documented, set(app.openapi()["paths"]))
        self.assertEqual(
            [(route.name, route.path) for route in routes[-3:]],
            [
                ("spa_asset", "/assets/{asset_path:path}"),
                ("spa_fallback", "/{full_path:path}"),
                ("unknown_route", "/{full_path:path}"),
            ],
        )

    def test_notifications_list_documents_status_before_id_and_limit_query_params(
        self,
    ) -> None:
        operation = app.openapi()["paths"]["/api/notifications"]["get"]
        parameters = {
            parameter["name"]: parameter
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "query"
        }
        self.assertIn("limit", parameters)
        self.assertIn("status", parameters)
        self.assertIn("before_id", parameters)

        limit_schema = parameters["limit"]["schema"]
        self.assertEqual(limit_schema.get("minimum"), 1)
        self.assertEqual(limit_schema.get("maximum"), 200)
        self.assertEqual(limit_schema.get("default"), 50)

        status_schema = parameters["status"]["schema"]
        status_values = status_schema.get("enum") or status_schema.get("const")
        if isinstance(status_values, str):
            status_values = [status_values]
        self.assertEqual(set(status_values or []), {"all", "unread", "read"})
        self.assertEqual(status_schema.get("default"), "all")

        before_schema = parameters["before_id"]["schema"]
        # Optional positive cursor; OpenAPI may wrap nullability as anyOf.
        def _collect_schemas(node):
            if not isinstance(node, dict):
                return
            yield node
            for key in ("anyOf", "oneOf", "allOf"):
                for child in node.get(key) or []:
                    yield from _collect_schemas(child)

        minima = [
            schema.get("minimum")
            for schema in _collect_schemas(before_schema)
            if schema.get("minimum") is not None
        ]
        self.assertIn(1, minima)

        self.assertIn(
            "/api/notifications/read-all",
            app.openapi()["paths"],
        )

    def test_all_api_response_models_are_nonfiltering_contracts(self) -> None:
        for route in app.routes:
            if not isinstance(route, APIRoute) or route.path in RAW_RESPONSE_EXCEPTIONS:
                continue
            if route.path.startswith("/api/") or route.path in {"/health", "/ready"}:
                self.assertIsNotNone(route.response_model, route.path)
                self.assertTrue(
                    issubclass(route.response_model, JsonObjectResponse),
                    route.path,
                )

    def test_every_hand_written_schema_documents_a_route(self) -> None:
        # Until the last schema moves next to its route (ADR-0056), the
        # hand-written file may only hold what the document really uses.
        document = app.openapi()
        components = document["components"]["schemas"]

        def references(node: Any) -> set[str]:
            return set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(node)))

        reachable: set[str] = set()
        pending = list(references(document["paths"]))
        while pending:
            name = pending.pop()
            if name not in reachable:
                reachable.add(name)
                pending.extend(references(components[name]))
        hand_written = documented_schemas()
        # The frontend types the untyped answers of ``recover`` and
        # ``free-space`` with it; it goes when those routes declare their model.
        self.assertEqual(set(components) - reachable, {"FileOperationResponse"})
        # None is dead text under a Pydantic model of the same name.
        self.assertEqual(
            [name for name, schema in hand_written.items() if components[name] != schema], [])


class Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    name: str | None


class OpenProbe(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int


class PassthroughResponseModelTests(unittest.TestCase):
    """A declared model documents a route and is checked only by the tests (ADR-0056)."""

    BODIES = {
        "valid": {"id": 1, "name": None},
        "missing": {"id": 1},
        "wrong-type": {"id": "1", "name": None},
        "undeclared": {"id": 1, "name": "a", "extension": [{"future": "kept"}]},
    }

    def probe(self, declared: Any) -> FastAPI:
        probe = FastAPI()

        @probe.get("/probe/{case}", response_model=declared)
        def answer(case: str):
            return self.BODIES[case]

        return probe

    def test_the_model_never_validates_or_filters_the_answer(self) -> None:
        declared = passthrough(Probe)
        # The same validator and serializer as the open response of before.
        self.assertTrue(issubclass(declared, JsonObjectResponse))
        self.assertEqual(
            declared.__pydantic_core_schema__["schema"],
            JsonObjectResponse.__pydantic_core_schema__["schema"],
        )
        self.assertIs(declared, passthrough(Probe))
        before = TestClient(self.probe(response_model("Probe")))
        after = TestClient(self.probe(declared))
        for case, body in self.BODIES.items():
            with self.subTest(case):
                response = after.get(f"/probe/{case}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), body)
                self.assertEqual(response.content, before.get(f"/probe/{case}").content)
                self.assertEqual(response.headers, before.get(f"/probe/{case}").headers)

    def test_the_document_references_the_model_itself(self) -> None:
        document = self.probe(passthrough(Probe)).openapi()
        operation = document["paths"]["/probe/{case}"]["get"]
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/Probe"},
        )
        self.assertEqual(
            document["components"]["schemas"]["Probe"], Probe.model_json_schema())
        self.assertEqual(
            set(document["components"]["schemas"]),
            {"Probe", "HTTPValidationError", "ValidationError"},
        )

    def test_a_model_that_nests_another_twice_is_still_one_component(self) -> None:
        class Part(BaseModel):
            id: int

        class Whole(BaseModel):
            first: Part
            second: Part | None

        document = self.probe(passthrough(Whole)).openapi()
        operation = document["paths"]["/probe/{case}"]["get"]
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/Whole"},
        )
        self.assertEqual(
            set(document["components"]["schemas"]),
            {"Whole", "Part", "HTTPValidationError", "ValidationError"},
        )
        self.assertEqual(
            set(document["components"]["schemas"]["Whole"]["properties"]), {"first", "second"})

    def test_the_shared_test_client_checks_json_success_bodies(self) -> None:
        client = fixtures._contract_checking_client()(self.probe(passthrough(Probe)))
        self.assertEqual(client.get("/probe/valid").json(), self.BODIES["valid"])
        for case, problem in (
            ("missing", "Field required"),
            ("wrong-type", "valid integer"),
            ("undeclared", "Extra inputs are not permitted"),
        ):
            with self.subTest(case), self.assertRaises(AssertionError) as raised:
                client.get(f"/probe/{case}")
            self.assertIn("GET /probe/{case}", str(raised.exception))
            self.assertIn("Probe", str(raised.exception))
            self.assertIn(problem, str(raised.exception))
        # An error is not a success body of the model.
        self.assertEqual(client.get("/nowhere").status_code, 404)

    def test_an_open_model_is_checked_on_its_declared_fields_only(self) -> None:
        client = fixtures._contract_checking_client()(self.probe(passthrough(OpenProbe)))
        self.assertEqual(client.get("/probe/undeclared").json(), self.BODIES["undeclared"])
        with self.assertRaises(AssertionError):
            client.get("/probe/wrong-type")

    def test_the_report_lists_the_operations_no_test_called(self) -> None:
        probe = self.probe(passthrough(Probe))
        client = fixtures._contract_checking_client()(probe)
        fixtures._CONTRACTS_SEEN.discard("GET /probe/{case}")
        self.assertEqual(fixtures.response_contract_report(probe), ["unseen\tGET /probe/{case}"])
        client.get("/probe/valid")
        self.assertEqual(fixtures.response_contract_report(probe), ["seen\tGET /probe/{case}"])

    def test_the_application_client_is_the_checking_one(self) -> None:
        client = fixtures.http_client()
        self.assertIsInstance(client, fixtures._contract_checking_client())
        self.assertIs(client.app, app)
        declared = [line.partition("\t")[2] for line in fixtures.response_contract_report(app)]
        self.assertLessEqual(
            {"POST /api/vaults/{vault_id}/pause", "POST /api/vaults/{vault_id}/resume"},
            set(declared),
        )


if __name__ == "__main__":
    unittest.main()
