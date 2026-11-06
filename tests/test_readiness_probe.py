"""Issue #463: authorized local /ready probe without Host exceptions.

No database is needed: HTTP tests use the real middleware and readiness route,
but substitute each dependency check at its existing seam. A loopback HTTP server
exercises the standard-library transport without starting application lifespan.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import unittest
from dataclasses import replace
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import yaml
from fastapi.testclient import TestClient

from app import main, readiness_probe
from app.config import settings

REPO_ROOT = Path(__file__).resolve().parents[1]


class ProbeHostTests(unittest.TestCase):
    def test_empty_allowlist_uses_loopback(self) -> None:
        for value in ("", "  ", " , , \t"):
            with self.subTest(value=value):
                self.assertEqual(readiness_probe._probe_host(value), "127.0.0.1")

    def test_first_safe_exact_entry_is_selected(self) -> None:
        for value, expected in (
            ("vault.example", "vault.example"),
            (" , Vault.Example , second.example, ", "Vault.Example"),
            ("https://bad.example, good.example, other.example", "good.example"),
            ("*.example, localhost", "localhost"),
            ("[::1], 127.0.0.1", "127.0.0.1"),
            ("vault.example:8080, vault.example.", "vault.example."),
            ("xn--bcher-kva.example", "xn--bcher-kva.example"),
        ):
            with self.subTest(value=value):
                self.assertEqual(readiness_probe._probe_host(value), expected)

    def test_unsafe_or_unsupported_entries_fail_closed(self) -> None:
        for value in (
            "vault.example\r\nX-Injected: yes",
            "vault.example\nX-Injected: yes",
            "vault.example\x00",
            "$(touch /tmp/not-executed)",
            "vault.example; echo injected",
            "`id`", "vault.example/path", "user:password@example.com",
            "https://vault.example", "vault.example:8080", "*.example", "*",
            "[::1]", "::1", "bücher.example", "bad host", "-bad.example",
            "bad..example", "bad-.example", "a" * 64 + ".example",
            ".".join(["a" * 63] * 4),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                readiness_probe._probe_host(value)

    def test_invalid_entry_can_never_inject_into_selected_host(self) -> None:
        value = "vault.example\r\nX-Injected: yes, second.example"
        self.assertEqual(readiness_probe._probe_host(value), "second.example")


class ProbeHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))
        self.addCleanup(self.client.close)
        for name in ("check_database", "worker_is_healthy", "check_config"):
            patcher = patch(f"app.services.health.{name}", return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch(
            "app.services.health.source_layout.sources_layout_is_ready",
            return_value=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.response_status: int | None = None
        self.response_body = b"private dependency details must not be logged"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.requests.append((self.path, dict(self.headers)))
                if owner.response_status is None:
                    response = owner.client.get(self.path, headers=dict(self.headers))
                    status, body = response.status_code, response.content
                else:
                    status, body = owner.response_status, owner.response_body
                self.send_response(status)
                if status in (301, 302, 307, 308):
                    self.send_header("Location", "http://remote.invalid/health")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup_server() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(cleanup_server)

        def connect(host: str, port: int, *, timeout: float) -> HTTPConnection:
            self.assertEqual((host, port, timeout), ("127.0.0.1", 8080, 3))
            return HTTPConnection(host, server.server_port, timeout=timeout)

        patcher = patch("app.readiness_probe.http.client.HTTPConnection", side_effect=connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_probe(self, allowed_hosts: str | None = "vault.example") -> tuple[int, str]:
        stderr = io.StringIO()
        with patch.dict(os.environ, {
            "HTTP_PROXY": "http://unreachable.invalid:9",
            "HTTPS_PROXY": "http://unreachable.invalid:9",
            "ALL_PROXY": "http://unreachable.invalid:9",
            "NO_PROXY": "",
        }), contextlib.redirect_stderr(stderr):
            if allowed_hosts is None:
                os.environ.pop("ALLOWED_HOSTS", None)
            else:
                os.environ["ALLOWED_HOSTS"] = allowed_hosts
            result = readiness_probe.main()
        return result, stderr.getvalue()

    def test_ready_with_unset_empty_and_multiple_allowed_hosts(self) -> None:
        for allowed_hosts, expected_host in (
            (None, "127.0.0.1"), ("", "127.0.0.1"), (" , ", "127.0.0.1"),
            ("vault.example", "vault.example"),
            (" , Vault.Example, second.example ", "Vault.Example"),
            ("*.example, vault.example", "vault.example"),
        ):
            with self.subTest(allowed_hosts=allowed_hosts), patch(
                "app.main.settings", replace(settings, allowed_hosts=allowed_hosts or "")
            ):
                self.assertEqual(self._run_probe(allowed_hosts), (0, ""))
                path, headers = self.requests[-1]
                self.assertEqual(path, "/ready")
                self.assertEqual(headers["Host"], expected_host)
                for name in ("Cookie", "Authorization", "X-Forwarded-Host", "X-Forwarded-For", "X-Forwarded-Proto"):
                    self.assertNotIn(name, headers)

    def test_database_worker_config_and_sources_503_fail_even_though_liveness_is_ok(self) -> None:
        for dependency in (
            "check_database", "worker_is_healthy", "check_config",
            "source_layout.sources_layout_is_ready",
        ):
            with self.subTest(dependency=dependency), patch(
                "app.main.settings", replace(settings, allowed_hosts="vault.example")
            ), patch(f"app.services.health.{dependency}", return_value=False):
                self.assertEqual(self.client.get("/health", headers={"Host": "vault.example"}).status_code, 200)
                self.assertEqual(self._run_probe(), (1, "Readiness probe failed: HTTP 503\n"))
                self.assertEqual(self.requests[-1][0], "/ready")
        self.assertEqual(len(self.requests), 4)  # No fallback or retries.

    def test_disallowed_hosts_remain_rejected_locally_and_externally(self) -> None:
        with patch("app.main.settings", replace(settings, allowed_hosts="vault.example")):
            for peer in ("127.0.0.1", "203.0.113.42"):
                # No context-manager lifespan: never initialize a database.
                client = TestClient(main.app, client=(peer, 50000))
                try:
                    for path in ("/health", "/ready", "/api/me"):
                        for host in ("127.0.0.1", "localhost", "evil.example"):
                            with self.subTest(peer=peer, path=path, host=host):
                                response = client.get(path, headers={
                                    "Host": host, "X-Forwarded-Host": "vault.example",
                                })
                                self.assertEqual(response.status_code, 400)
                    for path in ("/health", "/ready"):
                        response = client.get(path, headers={"Host": "vault.example"})
                        self.assertEqual(response.status_code, 200)
                finally:
                    client.close()

    def test_probe_with_disallowed_host_fails(self) -> None:
        with patch("app.main.settings", replace(settings, allowed_hosts="different.example")):
            self.assertEqual(self._run_probe(), (1, "Readiness probe failed: HTTP 400\n"))
        self.assertEqual(len(self.requests), 1)

    def test_redirects_and_non_200_statuses_fail_without_following_or_logging_body(self) -> None:
        for status in (204, 301, 302, 307, 308, 400, 401, 403, 404, 500, 503):
            with self.subTest(status=status):
                self.response_status = status
                before = len(self.requests)
                self.assertEqual(self._run_probe(), (1, f"Readiness probe failed: HTTP {status}\n"))
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.requests[-1][0], "/ready")


class ProbeFailureTests(unittest.TestCase):
    def test_invalid_configuration_does_not_connect_or_echo_input(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {"ALLOWED_HOSTS": "user:private-value@invalid"}), patch(
            "app.readiness_probe.http.client.HTTPConnection"
        ) as connect, contextlib.redirect_stderr(stderr):
            self.assertEqual(readiness_probe.main(), 1)
        connect.assert_not_called()
        self.assertEqual(stderr.getvalue(), "Readiness probe failed: no safe Host in ALLOWED_HOSTS\n")

    def test_connection_errors_timeouts_and_malformed_http_fail_without_logging_details(self) -> None:
        for error in (ConnectionRefusedError, socket.timeout, HTTPException):
            for operation in ("request", "getresponse"):
                with self.subTest(error=error, operation=operation):
                    connection = Mock()
                    getattr(connection, operation).side_effect = error("private diagnostic text")
                    stderr = io.StringIO()
                    with patch.dict(os.environ, {"ALLOWED_HOSTS": "vault.example"}), patch(
                        "app.readiness_probe.http.client.HTTPConnection", return_value=connection
                    ), contextlib.redirect_stderr(stderr):
                        self.assertEqual(readiness_probe.main(), 1)
                    connection.close.assert_called_once_with()
                    self.assertEqual(stderr.getvalue(), "Readiness probe failed: local request failed\n")


class ProbePackagingTests(unittest.TestCase):
    def test_module_cli_uses_only_standard_library_and_nonzero_exit(self) -> None:
        environment = os.environ.copy()
        environment["ALLOWED_HOSTS"] = "invalid:private-value@host"
        result = subprocess.run(
            [sys.executable, "-S", "-m", "app.readiness_probe"],
            cwd=REPO_ROOT, env=environment, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "Readiness probe failed: no safe Host in ALLOWED_HOSTS\n")

    def test_image_healthcheck_is_exec_form_and_compose_inherits_it(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text()
        match = re.search(r"(?m)^HEALTHCHECK ([^\n]+)\\\n\s*CMD (\[[^\n]+\])$", dockerfile)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(json.loads(match[2]), ["python", "-m", "app.readiness_probe"])
        for flag in ("--interval=30s", "--timeout=5s", "--start-period=60s", "--retries=3"):
            self.assertIn(flag, match[1])
        for name in ("compose.yaml", "compose.traefik.yaml"):
            service = yaml.safe_load((REPO_ROOT / name).read_text())["services"]["frostvault"]
            self.assertNotIn("healthcheck", service, f"{name} must inherit the image healthcheck")
        self.assertIn("COPY app ./app", dockerfile)


if __name__ == "__main__":
    unittest.main()
