"""Automated deployment/configuration checks for container hardening (issue #9),
read-only-rootfs startup (issue #196), and production image packaging for
PostgreSQL metadata backups (issue #7).

Seam: deployment manifests (Dockerfile, compose.yaml, Traefik reference compose).
Static checks read public config files only. The image smoke test builds and runs
only when a usable Docker daemon is available.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

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
        self.assertNotRegex(entrypoint, r"(?m)^\s*chown\s+.*?/sources")


@unittest.skipUnless(
    DOCKER_AVAILABLE,
    "Docker daemon unavailable; read-only image smoke test skipped",
)
class ReadOnlyRootFilesystemSmokeTests(unittest.TestCase):
    """Exercise the packaged entrypoint with the same hardening as Compose."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.image = f"frostvault-read-only-smoke:{os.getpid()}"
        cls._temporary_dir = tempfile.TemporaryDirectory(
            prefix="frostvault-read-only-smoke-"
        )
        try:
            cls._docker("build", "--tag", cls.image, REPO_ROOT, timeout=900)
        except BaseException:
            cls._temporary_dir.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._docker(
                "image",
                "rm",
                "--force",
                cls.image,
                check=False,
                timeout=60,
            )
        except OSError:
            pass
        finally:
            cls._temporary_dir.cleanup()
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

    def _runtime_args(
        self,
        uid: int,
        gid: int,
        data_dir: Path,
        sources_dir: Path,
    ) -> list[str]:
        return [
            "run",
            "--rm",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            f"{uid}:{gid}",
            "--tmpfs",
            f"/tmp:uid={uid},gid={gid},mode=1777",
            "--tmpfs",
            f"/run:uid={uid},gid={gid},mode=0755",
            "-v",
            f"{data_dir}:/data:rw",
            "-v",
            f"{sources_dir}:/sources:rw",
            "-e",
            f"PUID={uid}",
            "-e",
            f"PGID={gid}",
            "-e",
            "AUTO_MIGRATE=0",
            "-e",
            "DB_BACKEND=sqlite",
            "-e",
            "SQLITE_PATH=/data/frostvault.db",
        ]

    def _run_runtime_case(self, uid: int, gid: int) -> None:
        case_root = Path(self._temporary_dir.name) / f"{uid}-{gid}"
        data_dir = case_root / "data"
        sources_dir = case_root / "sources"
        data_dir.mkdir(parents=True)
        sources_dir.mkdir()
        source_stat = sources_dir.stat()
        source_metadata = (
            source_stat.st_uid,
            source_stat.st_gid,
            stat.S_IMODE(source_stat.st_mode),
        )

        # Bind mounts arrive with host ownership. Prepare only /data using a
        # separate privileged setup container; the runtime itself drops every
        # capability and must not change /sources.
        self._docker(
            "run",
            "--rm",
            "-v",
            f"{data_dir}:/data:rw",
            "--entrypoint",
            "sh",
            self.image,
            "-ceu",
            f"chown {uid}:{gid} /data",
        )

        runtime_args = self._runtime_args(uid, gid, data_dir, sources_dir)
        self._docker(
            *runtime_args,
            self.image,
            "sh",
            "-ceu",
            (
                f'test "$(id -u)" = "{uid}"\n'
                f'test "$(id -g)" = "{gid}"\n'
                "test ! -w /etc\n"
                "touch /tmp/frostvault-smoke /run/frostvault-smoke\n"
                f'printf "%s:%s\\n" "$(id -u)" "$(id -g)" > /data/runtime-{uid}-{gid}\n'
            ),
        )
        self._docker(
            *runtime_args,
            self.image,
            "python",
            "-m",
            "alembic",
            "upgrade",
            "head",
            timeout=240,
        )

        self.assertEqual(
            (data_dir / f"runtime-{uid}-{gid}").read_text(encoding="utf-8"),
            f"{uid}:{gid}\n",
        )
        self.assertTrue((data_dir / "frostvault.db").is_file())
        after_source_stat = sources_dir.stat()
        self.assertEqual(
            (
                after_source_stat.st_uid,
                after_source_stat.st_gid,
                stat.S_IMODE(after_source_stat.st_mode),
            ),
            source_metadata,
            "runtime must not change Source Volume ownership or modes",
        )

    def _run_root_entrypoint_case(self, uid: int, gid: int) -> None:
        case_root = Path(self._temporary_dir.name) / f"root-{uid}-{gid}"
        data_dir = case_root / "data"
        sources_dir = case_root / "sources"
        data_dir.mkdir(parents=True)
        sources_dir.mkdir()
        source_stat = sources_dir.stat()
        source_metadata = (
            source_stat.st_uid,
            source_stat.st_gid,
            stat.S_IMODE(source_stat.st_mode),
        )

        # This is the direct-docker-run path: the entrypoint starts as root,
        # then drops to a numeric ID without writing /etc on a read-only root.
        self._docker(
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/run",
            "-v",
            f"{data_dir}:/data:rw",
            "-v",
            f"{sources_dir}:/sources:rw",
            "-e",
            f"PUID={uid}",
            "-e",
            f"PGID={gid}",
            "-e",
            "AUTO_MIGRATE=0",
            self.image,
            "sh",
            "-ceu",
            (
                f'test "$(id -u)" = "{uid}"\n'
                f'test "$(id -g)" = "{gid}"\n'
                "test ! -w /etc\n"
                "touch /tmp/frostvault-root-smoke /run/frostvault-root-smoke\n"
                f'printf "%s:%s\\n" "$(id -u)" "$(id -g)" > /data/root-{uid}-{gid}\n'
            ),
        )

        self.assertEqual(
            (data_dir / f"root-{uid}-{gid}").read_text(encoding="utf-8"),
            f"{uid}:{gid}\n",
        )
        after_source_stat = sources_dir.stat()
        self.assertEqual(
            (
                after_source_stat.st_uid,
                after_source_stat.st_gid,
                stat.S_IMODE(after_source_stat.st_mode),
            ),
            source_metadata,
            "root entrypoint must not change Source Volume ownership or modes",
        )

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

    def test_default_and_overridden_identities_work_with_read_only_rootfs(self) -> None:
        for uid, gid in ((99, 100), (12345, 12346)):
            with self.subTest(uid=uid, gid=gid):
                self._run_runtime_case(uid, gid)

    def test_root_entrypoint_supports_default_and_overridden_identities_read_only(self) -> None:
        for uid, gid in ((99, 100), (12345, 12346)):
            with self.subTest(uid=uid, gid=gid):
                self._run_root_entrypoint_case(uid, gid)

    def test_invalid_override_fails_before_starting_the_command(self) -> None:
        completed = self._docker(
            "run",
            "--rm",
            "--read-only",
            "-e",
            "PUID=not-a-number",
            "-e",
            "PGID=100",
            self.image,
            "sh",
            "-c",
            "true",
            check=False,
        )
        self.assertEqual(completed.returncode, 64)
        self.assertIn(
            "PUID must be a canonical positive decimal ID from 1 through 2147483647",
            completed.stderr,
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

    def test_traefik_docs_describe_private_upstream(self) -> None:
        docs = (REPO_ROOT / "docs" / "traefik.md").read_text(encoding="utf-8")
        self.assertIn("compose.traefik.yaml", docs)
        self.assertRegex(docs, r"(?i)not.*published|no.*ports")
        self.assertIn("TRUSTED_PROXIES", docs)


if __name__ == "__main__":
    unittest.main()
