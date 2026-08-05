"""Automated deployment/configuration checks for container hardening (issue #9),
read-only-rootfs startup (issue #196), and production image packaging for
PostgreSQL metadata backups (issue #7).

Seam: deployment manifests (Dockerfile, compose.yaml, Traefik reference compose).
Static checks read public config files only. The Compose smoke test builds and
runs only when usable Docker and Docker Compose daemons are available.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from app.database import HEAD_SCHEMA_REVISION

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "compose.yaml"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
ENTRYPOINT_PATH = REPO_ROOT / "docker" / "entrypoint.sh"
TRAEFIK_COMPOSE_PATH = REPO_ROOT / "compose.traefik.yaml"


def _load_compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


PUBLISHED_IMAGE_PREFIX = "ghcr.io/paolodelcasale/frostvault"
DOCKER = shutil.which("docker")


def _docker_available() -> bool:
    if not DOCKER:
        return False
    try:
        completed = subprocess.run(
            [DOCKER, "info"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


DOCKER_AVAILABLE = _docker_available()


def _docker_compose_available() -> bool:
    if not DOCKER_AVAILABLE or not DOCKER:
        return False
    try:
        completed = subprocess.run(
            [DOCKER, "compose", "version"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


DOCKER_COMPOSE_AVAILABLE = _docker_compose_available()


_SHELL_VARIABLE = r"[A-Za-z_][A-Za-z0-9_]*"


def _references_shell_variable(text: str, variable: str) -> bool:
    """Return whether a shell fragment expands ``variable``.

    This intentionally recognizes the simple expansions used by the entrypoint,
    including braced forms with a shell parameter operator. It is a conservative
    static guard, not a replacement for a shell parser.
    """
    escaped = re.escape(variable)
    return bool(
        re.search(
            rf"\$(?:{escaped}\b|\{{{escaped}(?:[^}}]*)\}})",
            text,
        )
    )


def _source_permission_mutations(shell: str) -> list[str]:
    """Find chmod/chown operations that can target ``/sources``.

    Besides literal arguments, follow simple assignment and ``for … in`` flows
    so adding ``/sources`` to a loop or passing it through a variable cannot
    evade the entrypoint hardening check.
    """
    normalized = re.sub(r"\\\n", " ", shell)
    normalized = re.sub(r"(?m)^\s*#.*$", "", normalized)
    assignment_pattern = re.compile(
        rf"(?m)(?:^|[;\n])\s*(?:export\s+|readonly\s+|local\s+)?"
        rf"(?P<name>{_SHELL_VARIABLE})=(?P<value>[^;\n]*)"
    )
    loop_pattern = re.compile(
        rf"\bfor\s+(?P<name>{_SHELL_VARIABLE})\s+in\s+"
        rf"(?P<values>.*?)(?:;\s*|\n\s*)do\b",
        re.DOTALL,
    )
    assignments = list(assignment_pattern.finditer(normalized))
    loops = list(loop_pattern.finditer(normalized))
    source_variables: set[str] = set()

    # Resolve direct source-valued assignments, then aliases and loop variables
    # until no new source-bearing variable is found.
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            value = assignment.group("value")
            if "/sources" in value or any(
                _references_shell_variable(value, variable)
                for variable in source_variables
            ):
                if assignment.group("name") not in source_variables:
                    source_variables.add(assignment.group("name"))
                    changed = True
        for loop in loops:
            values = loop.group("values")
            if "/sources" in values or any(
                _references_shell_variable(values, variable)
                for variable in source_variables
            ):
                if loop.group("name") not in source_variables:
                    source_variables.add(loop.group("name"))
                    changed = True

    mutations: list[str] = []
    for command in re.finditer(
        r"(?m)(?<![A-Za-z0-9_])(?P<command>chown|chmod)\b(?P<args>[^;\n]*)",
        normalized,
    ):
        arguments = command.group("args")
        if "/sources" in arguments or any(
            _references_shell_variable(arguments, variable)
            for variable in source_variables
        ):
            mutations.append(command.group(0).strip())
    return mutations


class ContainerHardeningComposeTests(unittest.TestCase):
    def test_compose_files_reference_published_ghcr_image(self) -> None:
        for path in (COMPOSE_PATH, TRAEFIK_COMPOSE_PATH):
            with self.subTest(path=path.name):
                service = _load_compose(path)["services"]["frostvault"]
                image = str(service.get("image") or "")
                self.assertTrue(
                    image.startswith(PUBLISHED_IMAGE_PREFIX),
                    f"{path.name} should pull {PUBLISHED_IMAGE_PREFIX}, got {image!r}",
                )
                self.assertNotIn("build", service)

    def test_services_drop_capabilities_and_use_read_only_rootfs(self) -> None:
        for path in (COMPOSE_PATH, TRAEFIK_COMPOSE_PATH):
            with self.subTest(path=path.name):
                service = _load_compose(path)["services"]["frostvault"]
                self.assertTrue(service.get("read_only"))
                self.assertIn("ALL", service.get("cap_drop") or [])
                self.assertIn(
                    "no-new-privileges:true",
                    service.get("security_opt") or [],
                )

    def test_services_run_as_configurable_unraid_compatible_identity(self) -> None:
        for path in (COMPOSE_PATH, TRAEFIK_COMPOSE_PATH):
            with self.subTest(path=path.name):
                service = _load_compose(path)["services"]["frostvault"]
                env = service.get("environment") or {}
                if isinstance(env, list):
                    env = dict(
                        item.split("=", 1)
                        for item in env
                        if isinstance(item, str) and "=" in item
                    )

                # Compose may use ${PUID:-99}; the default remains Unraid 99/100.
                self.assertRegex(str(env.get("PUID", "")), r"^(99|\$\{PUID:-99\})$")
                self.assertRegex(str(env.get("PGID", "")), r"^(100|\$\{PGID:-100\})$")
                self.assertEqual(
                    service.get("user"),
                    "${PUID:-99}:${PGID:-100}",
                    "cap_drop: ALL requires Compose to start directly as PUID:PGID",
                )

    def test_writable_mounts_and_tmpfs_match_runtime_identity(self) -> None:
        expected_tmpfs = {
            "/tmp": "/tmp:uid=${PUID:-99},gid=${PGID:-100},mode=1777",
            "/run": "/run:uid=${PUID:-99},gid=${PGID:-100},mode=0755",
        }
        for path in (COMPOSE_PATH, TRAEFIK_COMPOSE_PATH):
            with self.subTest(path=path.name):
                service = _load_compose(path)["services"]["frostvault"]
                volumes = service.get("volumes") or []
                normalized = []
                for volume in volumes:
                    if isinstance(volume, str):
                        parts = volume.split(":")
                        target = parts[1] if len(parts) > 1 else parts[0]
                        mode = parts[2] if len(parts) > 2 else "rw"
                        normalized.append((target, mode))
                    elif isinstance(volume, dict):
                        normalized.append(
                            (
                                volume.get("target"),
                                volume.get("read_only") and "ro" or "rw",
                            )
                        )

                writable = {target for target, mode in normalized if mode != "ro"}
                self.assertEqual(writable, {"/sources", "/data"})
                self.assertIn(("/data", "rw"), normalized)
                self.assertIn(("/config/rclone", "ro"), normalized)

                tmpfs = service.get("tmpfs") or []
                tmpfs_by_target = {
                    item.split(":", 1)[0]: item
                    for item in tmpfs
                    if isinstance(item, str)
                }
                self.assertEqual(tmpfs_by_target, expected_tmpfs)


class DockerfileHardeningTests(unittest.TestCase):
    def test_image_declares_non_root_entrypoint_helper(self) -> None:
        dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?im)^COPY\s+.*entrypoint")
        self.assertRegex(dockerfile, r"(?im)^ENTRYPOINT")
        self.assertNotRegex(
            dockerfile,
            r"(?im)^USER\s+root\b",
            "image must not force USER root; entrypoint switches via PUID/PGID",
        )

    def test_default_unraid_identity_is_baked_into_image(self) -> None:
        dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        self.assertRegex(
            dockerfile,
            r"(?s)useradd\s+--system\s+--uid\s+99\s+--gid\s+100\s+.*?\barchive\b",
        )
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", dockerfile)

    def test_image_installs_postgresql_16_client_tools(self) -> None:
        """Production image must ship PG 16 clients for metadata backup/verify (issue #7)."""
        dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        # Pin major 16 so the image does not rely on an older distro-default pg_dump.
        self.assertRegex(
            dockerfile,
            r"(?i)postgresql-client-16\b",
            "Dockerfile must install postgresql-client-16 for PostgreSQL 16 dumps",
        )


class EntrypointHardeningTests(unittest.TestCase):
    def test_entrypoint_has_valid_posix_shell_syntax(self) -> None:
        completed = subprocess.run(
            ["sh", "-n", str(ENTRYPOINT_PATH)],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_entrypoint_uses_numeric_ids_without_runtime_account_creation(self) -> None:
        entrypoint = ENTRYPOINT_PATH.read_text(encoding="utf-8")
        self.assertIn("validate_numeric_id", entrypoint)
        self.assertIn("RUNTIME_IDENTITY=\"${PUID}:${PGID}\"", entrypoint)
        self.assertIn("exec gosu \"${RUNTIME_IDENTITY}\" \"$0\" \"$@\"", entrypoint)
        self.assertNotRegex(entrypoint, r"(?m)^\s*(?:groupadd|useradd)\b")

    def test_entrypoint_only_prepares_runtime_mounts_not_sources(self) -> None:
        entrypoint = ENTRYPOINT_PATH.read_text(encoding="utf-8")
        self.assertIn("for path in /tmp /run /data", entrypoint)
        self.assertIn("Never change ownership or", entrypoint)
        self.assertEqual(_source_permission_mutations(entrypoint), [])

    def test_source_permission_guard_catches_literals_variables_and_loops(self) -> None:
        unsafe_snippets = (
            "chown 99:100 /sources",
            "chmod 0770 /sources/managed",
            "for path in /tmp /sources /data; do\n  chown 99:100 \"${path}\"\ndone",
            "source_root=/sources\nchmod 0750 \"${source_root}/managed\"",
            "source_paths=\"/data /sources\"\nfor path in ${source_paths}; do\n  chmod g+w \"${path}\"\ndone",
        )
        for snippet in unsafe_snippets:
            with self.subTest(snippet=snippet):
                self.assertTrue(_source_permission_mutations(snippet))


@unittest.skipUnless(
    DOCKER_COMPOSE_AVAILABLE,
    "Docker Compose with a usable Docker daemon is required for this smoke test",
)
class ReadOnlyRootFilesystemSmokeTests(unittest.TestCase):
    """Run the supplied Compose service through the packaged entrypoint and app."""

    _case_number = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.image = f"frostvault-read-only-smoke:{os.getpid()}"
        cls._docker("build", "--tag", cls.image, REPO_ROOT, timeout=900)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._docker("image", "rm", "--force", cls.image, check=False, timeout=60)
        except OSError:
            pass
        finally:
            super().tearDownClass()

    @classmethod
    def _docker(
        cls,
        *args: str | Path,
        check: bool = True,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        assert DOCKER is not None
        try:
            completed = subprocess.run(
                [DOCKER, *(str(arg) for arg in args)],
                check=False,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(f"docker command timed out: {args!r}") from exc
        if check and completed.returncode != 0:
            raise AssertionError(
                "docker command failed: "
                f"{[DOCKER, *(str(arg) for arg in args)]!r}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        return completed

    @classmethod
    def _prepare_host_directory(
        cls,
        path: Path,
        uid: int,
        gid: int,
        mode: int = 0o750,
    ) -> None:
        """Perform the documented host-side preflight before Compose starts."""
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chown(path, uid, gid)
            path.chmod(mode)
        except PermissionError:
            # CI runners may not be able to chown directly. This setup-only
            # helper mutates the bind source before the hardened runtime starts.
            cls._docker(
                "run",
                "--rm",
                "--user",
                "0:0",
                "-v",
                f"{path}:/host:rw",
                "--entrypoint",
                "sh",
                cls.image,
                "-ceu",
                f"chown {uid}:{gid} /host && chmod {mode:o} /host",
            )

    @classmethod
    def _inspect_host_metadata(cls, path: Path) -> tuple[int, int, int]:
        """Read protected bind-source metadata without changing it.

        The smoke runner may not traverse a directory after its documented
        ``PUID:PGID``/``0750`` preflight. A read-only, short-lived root helper
        can inspect the exact bind source without relaxing its permissions.
        """
        completed = cls._docker(
            "run",
            "--rm",
            "--read-only",
            "--user",
            "0:0",
            "-v",
            f"{path}:/host:ro",
            "--entrypoint",
            "sh",
            cls.image,
            "-ceu",
            'stat -c "%u:%g:%a" /host',
        )
        try:
            uid_text, gid_text, mode_text = completed.stdout.strip().split(":")
            uid = int(uid_text, 10)
            gid = int(gid_text, 10)
            mode = int(mode_text, 8)
        except ValueError as exc:
            raise AssertionError(
                f"unexpected metadata helper output for {path}: {completed.stdout!r}"
            ) from exc
        return uid, gid, mode

    @classmethod
    def _inspect_sqlite_database(cls, data_dir: Path) -> tuple[str, tuple[str, int]]:
        """Query a temporary copy of the protected SQLite bind source.

        SQLite uses a WAL sidecar while the application is running. The caller
        stops Compose first; this helper then copies the database and any
        sidecars from a read-only bind into tmpfs, so inspection cannot mutate
        the asserted host directory or its metadata.
        """
        completed = cls._docker(
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp:mode=1777",
            "--user",
            "0:0",
            "-v",
            f"{data_dir}:/data:ro",
            "--entrypoint",
            "python",
            cls.image,
            "-c",
            "\n".join(
                (
                    "import glob",
                    "import json",
                    "import shutil",
                    "import sqlite3",
                    "for source in glob.glob('/data/frostvault.db*'):",
                    "    shutil.copy2(source, '/tmp')",
                    "with sqlite3.connect('file:/tmp/frostvault.db?mode=ro', uri=True) as connection:",
                    "    revision = connection.execute('SELECT version_num FROM alembic_version').fetchone()[0]",
                    "    admin = connection.execute('SELECT username, is_admin FROM users').fetchone()",
                    "print(json.dumps({'revision': revision, 'admin': admin}))",
                )
            ),
        )
        try:
            payload = json.loads(completed.stdout)
            revision = str(payload["revision"])
            username, is_admin = payload["admin"]
            return revision, (str(username), int(is_admin))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AssertionError(
                f"unexpected database helper output: {completed.stdout!r}"
            ) from exc

    @classmethod
    def _cleanup_compose_project(cls, root: Path) -> None:
        """Remove inaccessible test bind sources through a bounded helper."""
        cls._docker(
            "run",
            "--rm",
            "--user",
            "0:0",
            "-v",
            f"{root}:/host:rw",
            "--entrypoint",
            "sh",
            cls.image,
            "-ceu",
            "\n".join(
                (
                    "# /host is one generated TemporaryDirectory, not the repository.",
                    "rm -rf -- /host/data /host/sources /host/config",
                    "rm -f -- /host/compose.yaml /host/override.yaml /host/.env",
                )
            ),
        )

    @classmethod
    def _next_case_names(cls) -> tuple[str, str]:
        cls._case_number += 1
        token = f"{os.getpid()}-{cls._case_number}"
        return f"frostvaultreadonly{os.getpid()}{cls._case_number}", f"frostvault-read-only-{token}"

    @staticmethod
    def _write_environment(root: Path, values: dict[str, str]) -> None:
        (root / ".env").write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )

    def _new_compose_project(
        self,
        uid: int = 99,
        gid: int = 100,
        *,
        source_owner_uid: int | None = None,
        source_gid: int | None = None,
        source_mode: int = 0o750,
        extra_environment: dict[str, str] | None = None,
    ) -> tuple[tempfile.TemporaryDirectory, Path, str, str, Path, Path]:
        temporary = tempfile.TemporaryDirectory(prefix="frostvault-read-only-compose-")
        root = Path(temporary.name)
        shutil.copy2(COMPOSE_PATH, root / "compose.yaml")
        data_dir = root / "data"
        sources_dir = root / "sources"
        config_dir = root / "config"
        self._prepare_host_directory(data_dir, uid, gid)
        self._prepare_host_directory(
            sources_dir,
            uid if source_owner_uid is None else source_owner_uid,
            gid if source_gid is None else source_gid,
            source_mode,
        )
        config_dir.mkdir()
        project_name, container_name = self._next_case_names()
        (root / "override.yaml").write_text(
            "\n".join(
                (
                    "services:",
                    "  frostvault:",
                    f"    image: {self.image}",
                    f"    container_name: {container_name}",
                    '    restart: "no"',
                    "",
                )
            ),
            encoding="utf-8",
        )
        environment = {
            "PUID": str(uid),
            "PGID": str(gid),
            "SOURCES_ROOT": str(sources_dir),
            # Compose `up` does not conflict with an operator's port while the
            # smoke test probes the service with docker exec.
            "APP_PORT": "0",
        }
        if extra_environment:
            environment.update(extra_environment)
        self._write_environment(root, environment)
        return temporary, root, project_name, container_name, data_dir, sources_dir

    def _compose(
        self,
        root: Path,
        project_name: str,
        *args: str,
        check: bool = True,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        assert DOCKER is not None
        environment = os.environ.copy()
        for name in ("PUID", "PGID", "SOURCES_ROOT", "APP_PORT"):
            environment.pop(name, None)
        command = [
            DOCKER,
            "compose",
            "--project-name",
            project_name,
            "-f",
            "compose.yaml",
            "-f",
            "override.yaml",
            *args,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                check=False,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(f"docker compose command timed out: {command!r}: {exc}")
        if check and completed.returncode != 0:
            self.fail(
                "docker compose command failed: "
                f"{command!r}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        return completed

    def _down(self, root: Path, project_name: str) -> None:
        self._compose(
            root,
            project_name,
            "down",
            "--volumes",
            "--remove-orphans",
            check=False,
            timeout=120,
        )

    def _wait_for_ready(self, container_name: str) -> None:
        deadline = time.monotonic() + 60
        last_output = ""
        while time.monotonic() < deadline:
            probe = self._docker(
                "exec",
                container_name,
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "1",
                "http://127.0.0.1:8080/ready",
                check=False,
                timeout=10,
            )
            last_output = probe.stdout + probe.stderr
            if probe.returncode == 0:
                self.assertIn('"status":"ready"', probe.stdout)
                return
            running = self._docker(
                "inspect",
                "--format",
                "{{.State.Running}}",
                container_name,
                check=False,
            )
            if running.stdout.strip() != "true":
                logs = self._docker("logs", container_name, check=False)
                self.fail(
                    "Compose service stopped before readiness. "
                    f"probe={last_output!r}\nlogs:\n{logs.stdout}{logs.stderr}"
                )
            time.sleep(0.5)
        logs = self._docker("logs", container_name, check=False)
        self.fail(
            "Compose service did not become ready. "
            f"last probe={last_output!r}\nlogs:\n{logs.stdout}{logs.stderr}"
        )

    def _run_fresh_compose_application(self, uid: int, gid: int) -> None:
        username = f"smoke-{uid}"
        # Keep the Source Volume root deliberately unlike the runtime account:
        # group access lets the app create /sources/managed, while an accidental
        # chown/chmod by the runtime would be observable in the recorded tuple.
        source_owner_uid = uid + 1
        source_metadata = (source_owner_uid, gid, 0o770)
        self.assertNotEqual(source_owner_uid, uid)
        temporary, root, project_name, container_name, data_dir, sources_dir = (
            self._new_compose_project(
                uid,
                gid,
                source_owner_uid=source_owner_uid,
                source_gid=gid,
                source_mode=0o770,
                extra_environment={
                    "DB_BACKEND": "sqlite",
                    "SQLITE_PATH": "/data/frostvault.db",
                    "AUTO_MIGRATE": "1",
                    "BOOTSTRAP_ADMIN_USERNAME": username,
                    "BOOTSTRAP_ADMIN_PASSWORD": "Smoke-only-password-196",
                    "BOOTSTRAP_ADMIN_DISPLAY_NAME": "SmokeAdmin",
                    "FILESYSTEM_WATCH_ENABLED": "false",
                },
            )
        )
        try:
            # This is the documented preflight, done before `compose up`. Use
            # read-only helpers because a non-root runner cannot inspect its
            # own 0750 PUID:PGID data bind source after preflight.
            data_metadata = (uid, gid, 0o750)
            self.assertEqual(self._inspect_host_metadata(data_dir), data_metadata)
            self.assertEqual(self._inspect_host_metadata(sources_dir), source_metadata)

            self._compose(root, project_name, "up", "--detach", timeout=180)
            self._wait_for_ready(container_name)

            identity = self._docker(
                "exec",
                container_name,
                "sh",
                "-ceu",
                'printf "%s:%s" "$(id -u)" "$(id -g)"',
            )
            self.assertEqual(identity.stdout, f"{uid}:{gid}")
            self._docker(
                "exec",
                container_name,
                "sh",
                "-ceu",
                "test ! -w /etc && test -w /tmp && test -w /run && test -w /data",
            )

            logs = self._docker("logs", container_name)
            combined_logs = logs.stdout + logs.stderr
            # Stop before copying SQLite's WAL database. The read-only helper
            # below uses only a tmpfs copy and never changes host metadata.
            self._down(root, project_name)
            revision, admin = self._inspect_sqlite_database(data_dir)
            self.assertEqual(revision, HEAD_SCHEMA_REVISION)
            self.assertEqual(admin, (username, 1))

            self.assertIn(
                "AUTO_MIGRATE: ensuring database schema is current before uvicorn",
                combined_logs,
            )
            self.assertIn("AUTO_MIGRATE: bootstrapped", combined_logs)
            self.assertIn("Application startup complete.", combined_logs)
            self.assertEqual(self._inspect_host_metadata(data_dir), data_metadata)
            self.assertEqual(
                self._inspect_host_metadata(sources_dir),
                source_metadata,
                "the runtime must not chown or chmod the Source Volume root",
            )
            managed_metadata = self._inspect_host_metadata(sources_dir / "managed")
            self.assertEqual(managed_metadata[:2], (uid, gid))
        finally:
            self._down(root, project_name)
            self._cleanup_compose_project(root)
            temporary.cleanup()

    def _run_compose_identity_case(
        self,
        puid: str,
        pgid: str,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        temporary, root, project_name, _, _, _ = self._new_compose_project()
        try:
            self._write_environment(
                root,
                {
                    "PUID": puid,
                    "PGID": pgid,
                    "SOURCES_ROOT": str(root / "sources"),
                    "APP_PORT": "0",
                },
            )
            rendered = self._compose(root, project_name, "config", check=False)
            rendered_output = rendered.stdout + rendered.stderr
            self.assertEqual(rendered.returncode, 0, rendered_output)
            self.assertRegex(
                rendered.stdout,
                rf"(?m)^\s*user:\s*['\"]?{re.escape(f'{puid}:{pgid}')}",
            )
            completed = self._compose(
                root,
                project_name,
                "run",
                "--rm",
                "--no-deps",
                "frostvault",
                "sh",
                "-c",
                "printf compose-command-ran",
                check=False,
            )
            return completed, completed.stdout + completed.stderr
        finally:
            self._down(root, project_name)
            self._cleanup_compose_project(root)
            temporary.cleanup()

    def test_image_bakes_the_default_identity(self) -> None:
        completed = self._docker(
            "run",
            "--rm",
            "--read-only",
            "--entrypoint",
            "sh",
            self.image,
            "-ceu",
            'test "$(getent passwd archive | cut -d: -f3,4)" = "99:100"',
        )
        self.assertEqual(completed.stdout, "")

    def test_fresh_compose_deployment_runs_the_app_with_default_and_override(self) -> None:
        for uid, gid in ((99, 100), (12345, 12346)):
            with self.subTest(uid=uid, gid=gid):
                self._run_fresh_compose_application(uid, gid)

    def test_compose_rejects_unknown_non_numeric_puid_and_pgid_before_entrypoint(self) -> None:
        cases = (
            ("not-a-number", "100", "user"),
            ("99", "not-a-number", "group"),
        )
        for puid, pgid, principal in cases:
            with self.subTest(PUID=puid, PGID=pgid):
                completed, output = self._run_compose_identity_case(puid, pgid)
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn("compose-command-ran", output)
                self.assertNotIn("ERROR:", output)
                self.assertRegex(
                    output,
                    rf"(?i)(unable to find {principal}|{principal}.*not.*found|no matching entries)",
                )

    def test_compose_root_overrides_reach_entrypoint_rejection(self) -> None:
        for puid, pgid, name in (("0", "100", "PUID"), ("99", "0", "PGID")):
            with self.subTest(PUID=puid, PGID=pgid):
                completed, output = self._run_compose_identity_case(puid, pgid)
                self.assertEqual(completed.returncode, 64, output)
                self.assertIn(
                    f"{name} must be a canonical positive decimal ID from 1 through 2147483647",
                    output,
                )
                self.assertNotIn("compose-command-ran", output)

    def test_compose_rejects_out_of_range_puid_and_pgid_before_entrypoint(self) -> None:
        for puid, pgid in (("2147483648", "100"), ("99", "2147483648")):
            with self.subTest(PUID=puid, PGID=pgid):
                completed, output = self._run_compose_identity_case(puid, pgid)
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn("compose-command-ran", output)
                self.assertNotIn("ERROR:", output)
                self.assertRegex(
                    output,
                    r"(?i)(range|outside.*(?:uid|gid)|invalid.*(?:uid|gid))",
                )


class TraefikReferenceDeploymentTests(unittest.TestCase):
    def test_reference_compose_keeps_app_off_public_ports(self) -> None:
        self.assertTrue(
            TRAEFIK_COMPOSE_PATH.is_file(),
            "reference Traefik compose is required for production deployment",
        )
        compose = _load_compose(TRAEFIK_COMPOSE_PATH)
        services = compose.get("services") or {}
        self.assertIn("frostvault", services)
        app = services["frostvault"]
        self.assertFalse(
            app.get("ports"),
            "reference Traefik deployment must not publish the app directly",
        )
        labels = app.get("labels") or []
        if isinstance(labels, dict):
            label_text = "\n".join(f"{key}={value}" for key, value in labels.items())
        else:
            label_text = "\n".join(str(item) for item in labels)
        self.assertIn("traefik.enable=true", label_text)
        self.assertRegex(label_text, r"traefik\.http\.routers\.[^=]+\.tls")
        self.assertRegex(
            label_text,
            r"stsSeconds|STSSeconds|strict-transport|hsts",
            re.IGNORECASE,
        )


class PermissionAndTraefikDocumentationTests(unittest.TestCase):
    def test_filesystem_permission_recipes_cover_linux_unraid_and_desktop(self) -> None:
        docs = (REPO_ROOT / "docs" / "filesystem-permissions.md").read_text(encoding="utf-8")
        self.assertIn("PUID", docs)
        self.assertIn("99:100", docs)
        self.assertRegex(docs, r"(?i)linux")
        self.assertRegex(docs, r"(?i)unraid")
        self.assertRegex(docs, r"(?i)docker desktop")
        self.assertRegex(docs, r"(?i)never changes ownership|never chown")
        self.assertIn("never creates users or groups at runtime", docs)
        self.assertIn('user: "${PUID:-99}:${PGID:-100}"', docs)
        self.assertIn("mkdir -p ./data", docs)
        self.assertIn('sudo chown "${PUID}:${PGID}" ./data', docs)
        self.assertIn("sudo chmod 0750 ./data", docs)
        self.assertIn("2147483647", docs)
        self.assertRegex(docs, r"(?is)Docker.*before.*entrypoint")

    def test_readme_requires_fresh_data_preflight_before_compose_start(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Compose identity and fresh host data directory", readme)
        self.assertIn("mkdir -p ./data", readme)
        self.assertIn('sudo chown "${PUID}:${PGID}" ./data', readme)
        self.assertIn("never creates an account at runtime", readme)

    def test_traefik_docs_describe_private_upstream(self) -> None:
        docs = (REPO_ROOT / "docs" / "traefik.md").read_text(encoding="utf-8")
        self.assertIn("compose.traefik.yaml", docs)
        self.assertRegex(docs, r"(?i)not.*published|no.*ports")
        self.assertIn("TRUSTED_PROXIES", docs)


if __name__ == "__main__":
    unittest.main()
