"""Automated deployment/configuration checks for container hardening (issue #9)
and production image packaging for PostgreSQL metadata backups (issue #7).

Seam: deployment manifests (Dockerfile, compose.yaml, Traefik reference compose).
Tests read public config files only — no Docker daemon required.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "compose.yaml"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
TRAEFIK_COMPOSE_PATH = REPO_ROOT / "compose.traefik.yaml"


def _load_compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


PUBLISHED_IMAGE_PREFIX = "ghcr.io/paolodelcasale/frostvault"


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

    def test_service_drops_capabilities_and_uses_read_only_rootfs(self) -> None:
        compose = _load_compose(COMPOSE_PATH)
        service = compose["services"]["frostvault"]

        self.assertTrue(service.get("read_only"))
        self.assertIn("ALL", service.get("cap_drop") or [])
        self.assertIn("no-new-privileges:true", service.get("security_opt") or [])

    def test_service_runs_as_configurable_unraid_compatible_identity(self) -> None:
        compose = _load_compose(COMPOSE_PATH)
        service = compose["services"]["frostvault"]
        env = service.get("environment") or {}
        if isinstance(env, list):
            env = dict(item.split("=", 1) for item in env if isinstance(item, str) and "=" in item)

        # Compose may use ${PUID:-99}; the default must remain Unraid 99/100.
        self.assertRegex(str(env.get("PUID", "")), r"^(99|\$\{PUID:-99\})$")
        self.assertRegex(str(env.get("PGID", "")), r"^(100|\$\{PGID:-100\})$")

    def test_writable_mounts_are_limited_to_required_volumes(self) -> None:
        compose = _load_compose(COMPOSE_PATH)
        service = compose["services"]["frostvault"]
        volumes = service.get("volumes") or []
        normalized = []
        for volume in volumes:
            if isinstance(volume, str):
                parts = volume.split(":")
                target = parts[1] if len(parts) > 1 else parts[0]
                mode = parts[2] if len(parts) > 2 else "rw"
                normalized.append((target, mode))
            elif isinstance(volume, dict):
                normalized.append((volume.get("target"), volume.get("read_only") and "ro" or "rw"))

        writable = {target for target, mode in normalized if mode != "ro"}
        self.assertEqual(writable, {"/sources", "/data"})
        self.assertIn(("/config/rclone", "ro"), normalized)

        tmpfs = service.get("tmpfs") or []
        tmpfs_targets = {
            item.split(":")[0] if isinstance(item, str) else item.get("target")
            for item in tmpfs
        }
        self.assertIn("/tmp", tmpfs_targets)
        self.assertIn("/run", tmpfs_targets)


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

    def test_image_installs_postgresql_16_client_tools(self) -> None:
        """Production image must ship PG 16 clients for metadata backup/verify (issue #7)."""
        dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        # Pin major 16 so the image does not rely on an older distro-default pg_dump.
        self.assertRegex(
            dockerfile,
            r"(?i)postgresql-client-16\b",
            "Dockerfile must install postgresql-client-16 for PostgreSQL 16 dumps",
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
