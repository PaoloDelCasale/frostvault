"""Structural guarantees for layered CI (issue #13), strict frontend checks
(issue #204), and production-image PostgreSQL backup tooling (issue #7).

Seams under test: workflow YAML and Dependabot config as the public CI contract
contributors rely on — not GitHub's runtime.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
FULL_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _semver(version: str) -> tuple[int, int, int]:
    core = version.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    return (
        int(parts[0]),
        int(parts[1] if len(parts) > 1 else 0),
        int(parts[2] if len(parts) > 2 else 0),
    )


def _workflow_on(workflow: dict) -> dict | list | str:
    # PyYAML 1.1 may parse the key ``on`` as boolean True.
    if "on" in workflow:
        return workflow["on"]
    return workflow[True]


class PullRequestCiContractTests(unittest.TestCase):
    def test_pr_workflow_has_no_aws_oidc_or_static_keys(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        serialized = yaml.safe_dump(workflow)
        self.assertNotIn("aws-actions/configure-aws-credentials", serialized)
        self.assertNotIn("role-to-assume", serialized)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY: ${{", serialized)
        triggers = _workflow_on(workflow)
        self.assertIn("pull_request", triggers)
        job_names = set(workflow["jobs"])
        self.assertIn("sqlite-and-postgresql", job_names)
        self.assertIn("s3-compatible-integrity", job_names)
        self.assertIn("playwright-e2e", job_names)
        start_minio = "\n".join(
            step.get("run", "")
            for step in workflow["jobs"]["s3-compatible-integrity"]["steps"]
        )
        self.assertIn("quay.io/minio/minio:", start_minio)
        self.assertNotIn("minio/minio:", start_minio.replace("quay.io/minio/minio:", ""))

    def test_pr_runs_frontend_checks_in_parallel_jobs(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        self.assertIn("frontend-typecheck", jobs)
        self.assertIn("frontend-lint-test", jobs)
        self.assertIn("frontend-build", jobs)
        self.assertNotIn("frontend-quality", jobs)

        typecheck_runs = [
            step.get("run", "") for step in jobs["frontend-typecheck"]["steps"]
        ]
        lint_runs = [
            step.get("run", "") for step in jobs["frontend-lint-test"]["steps"]
        ]
        build_runs = [
            step.get("run", "") for step in jobs["frontend-build"]["steps"]
        ]
        self.assertTrue(any("npm ci" in block for block in typecheck_runs))
        self.assertTrue(any("npm run typecheck" in block for block in typecheck_runs))
        self.assertTrue(any("npm run lint" in block for block in lint_runs))
        self.assertTrue(any("npm run test" in block for block in lint_runs))
        self.assertTrue(any("npm run build:ci" in block for block in build_runs))
        self.assertFalse(any("node --test" in block for block in lint_runs))
        self.assertFalse(
            any("tsc -b && vite build" in block for block in build_runs)
        )

        lint_node = next(
            step["with"]["node-version"]
            for step in jobs["frontend-lint-test"]["steps"]
            if str(step.get("uses", "")).startswith("actions/setup-node@")
        )
        self.assertEqual(lint_node, "22")

        node24 = jobs["frontend-unit-tests-node24"]
        node24_runs = [step.get("run", "") for step in node24["steps"]]
        node24_version = next(
            step["with"]["node-version"]
            for step in node24["steps"]
            if str(step.get("uses", "")).startswith("actions/setup-node@")
        )
        self.assertEqual(node24_version, "24")
        self.assertTrue(any("npm ci" in block for block in node24_runs))
        self.assertTrue(any("npm run test" in block for block in node24_runs))
        self.assertFalse(any("npm run lint" in block for block in node24_runs))

    def test_frontend_typecheck_refreshes_artifacts_before_strict_checks(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["frontend-typecheck"]["steps"]
        runs = [step.get("run", "") for step in steps]

        def step_index(fragment: str) -> int:
            return next(
                index
                for index, block in enumerate(runs)
                if fragment in block
            )

        generated = step_index("npm run generate:api")
        drift = step_index("git diff --exit-code")
        typecheck = step_index("npm run typecheck")
        self.assertLess(generated, drift)
        self.assertLess(drift, typecheck)
        for artifact in (
            "frontend/openapi.json",
            "frontend/src/api/openapi.generated.ts",
            "frontend/src/api/types.ts",
        ):
            self.assertIn(artifact, runs[drift])

    def test_frontend_typecheck_projects_are_strict_and_environment_scoped(self) -> None:
        frontend = ROOT / "frontend"
        root = json.loads((frontend / "tsconfig.json").read_text(encoding="utf-8"))
        references = {item["path"] for item in root["references"]}
        self.assertEqual(
            references,
            {
                "./tsconfig.app.json",
                "./tsconfig.node.json",
                "./tsconfig.vitest.json",
                "./tsconfig.playwright.json",
            },
        )

        configs = {
            name: json.loads((frontend / name).read_text(encoding="utf-8"))
            for name in references
        }
        for name, config in configs.items():
            self.assertTrue(config["compilerOptions"]["strict"], name)

        app_types = set(configs["./tsconfig.app.json"]["compilerOptions"]["types"])
        node_types = set(configs["./tsconfig.node.json"]["compilerOptions"]["types"])
        vitest_types = set(configs["./tsconfig.vitest.json"]["compilerOptions"]["types"])
        playwright_types = set(
            configs["./tsconfig.playwright.json"]["compilerOptions"]["types"]
        )
        self.assertNotIn("node", app_types)
        self.assertNotIn("vitest/globals", app_types)
        self.assertEqual(node_types, {"node"})
        self.assertIn("node", vitest_types)
        self.assertIn("vitest/globals", vitest_types)
        self.assertNotIn("@playwright/test", vitest_types)
        self.assertEqual(playwright_types, {"node"})
        self.assertNotIn("vitest/globals", playwright_types)
        self.assertIn("tests", configs["./tsconfig.vitest.json"]["include"])
        self.assertIn("e2e", configs["./tsconfig.playwright.json"]["include"])

    def test_frontend_scripts_lint_e2e_and_expose_each_test_typecheck(self) -> None:
        package = json.loads(
            (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
        )
        scripts = package["scripts"]
        self.assertIn("typecheck", scripts)
        self.assertIn("typecheck:vitest", scripts)
        self.assertIn("typecheck:playwright", scripts)
        self.assertIn("e2e", scripts["lint"])
        self.assertIn("git diff --exit-code", scripts["check:generated"])

    def test_pr_isolates_postgresql_tests_from_the_sqlite_unit_job(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        sqlite_job = jobs["sqlite-and-postgresql"]
        python_job = jobs["python-unit-tests"]
        postgres_job = jobs["postgresql-tests"]
        self.assertEqual(
            python_job["strategy"]["matrix"]["shard"],
            [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
        )
        self.assertEqual(sqlite_job["needs"], "python-unit-tests")
        self.assertEqual(sqlite_job["if"], "${{ always() }}")
        python_runs = "\n".join(
            step.get("run", "") for step in python_job["steps"]
        )
        self.assertIn("key=lambda path: (-path.stat().st_size, path.name)", python_runs)
        self.assertIn("assigned[target].append(path)", python_runs)
        self.assertEqual(
            python_job["steps"][-1].get("env", {}).get("SHARD_COUNT"),
            "12",
        )
        self.assertNotIn("postgres", python_job.get("services") or {})
        self.assertNotIn("TEST_POSTGRES_URL", python_job.get("env") or {})
        self.assertIn("postgres", postgres_job.get("services") or {})
        postgres_runs = "\n".join(
            step.get("run", "") for step in postgres_job["steps"]
        )
        self.assertIn("PostgreSQLMigrationTests", postgres_runs)
        self.assertIn("PostgreSQLSharedLookupRateLimitTests", postgres_runs)
        self.assertLess(
            postgres_runs.index("PostgreSQLSharedLookupRateLimitTests"),
            postgres_runs.index("PostgreSQLMigrationTests"),
            "shared rate-limit tests must run before migration fixtures mutate the database",
        )

    def test_playwright_e2e_job_installs_chromium_and_uploads_failures(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        job = workflow["jobs"]["playwright-e2e"]
        self.assertEqual(
            job["strategy"]["matrix"]["project"],
            ["mobile-375", "desktop-1280"],
        )
        runs = [step.get("run", "") for step in job["steps"]]
        self.assertTrue(any("playwright install" in block for block in runs))
        self.assertTrue(any("test:e2e" in block for block in runs))
        self.assertTrue(any("--project" in block for block in runs))
        uses = [step.get("uses", "") for step in job["steps"]]
        self.assertTrue(any(u.startswith("actions/cache@") for u in uses))
        self.assertTrue(any(u.startswith("actions/upload-artifact@") for u in uses))
        serialized = yaml.safe_dump(job)
        self.assertIn("playwright-e2e-failures", serialized)
        self.assertIn("~/.cache/ms-playwright", serialized)
        self.assertIn("E2E_PYTHON", serialized)

class WorkflowHardeningContractTests(unittest.TestCase):
    def test_external_actions_are_pinned_and_checkouts_drop_credentials(self) -> None:
        for path in WORKFLOWS.glob("*.yml"):
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_name, job in workflow["jobs"].items():
                self.assertIn("timeout-minutes", job, f"{path.name}:{job_name}")
                for step in job.get("steps", []):
                    action = step.get("uses")
                    if not action or action.startswith("./"):
                        continue
                    _, separator, revision = action.rpartition("@")
                    self.assertEqual(separator, "@", f"{path.name}: {action}")
                    self.assertRegex(
                        revision,
                        FULL_COMMIT_SHA,
                        f"{path.name}: {action} must use a full commit SHA",
                    )
                    if action.startswith("actions/checkout@"):
                        self.assertFalse(
                            step.get("with", {}).get("persist-credentials", True),
                            f"{path.name}: checkout credentials must not persist",
                        )

    def test_untrusted_pr_events_do_not_cross_privileged_boundaries(self) -> None:
        for path in WORKFLOWS.glob("*.yml"):
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
            triggers = _workflow_on(workflow)
            if isinstance(triggers, dict):
                self.assertNotIn("pull_request_target", triggers, path.name)
                self.assertNotIn("workflow_run", triggers, path.name)

    def test_codeql_uploads_sarif_only_when_public(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "security.yml").read_text(encoding="utf-8"))
        permissions = workflow["jobs"]["codeql"]["permissions"]
        self.assertEqual(permissions["security-events"], "write")
        analyze = next(
            step
            for step in workflow["jobs"]["codeql"]["steps"]
            if step.get("uses", "").startswith("github/codeql-action/analyze")
        )
        self.assertEqual(
            analyze["with"]["upload"],
            "${{ github.event.repository.private == false }}",
        )


class AwsWorkflowContractTests(unittest.TestCase):
    def test_aws_workflow_is_manual_without_a_deployment_environment(self) -> None:
        workflow = yaml.safe_load(
            (WORKFLOWS / "aws-s3-integrity.yml").read_text(encoding="utf-8")
        )
        triggers = _workflow_on(workflow)
        self.assertIn("workflow_dispatch", triggers)
        self.assertNotIn("schedule", triggers)
        self.assertNotIn("pull_request", triggers)
        self.assertNotIn("environment", workflow["jobs"]["aws-integrity"])
        self.assertEqual(workflow["permissions"]["id-token"], "write")
        serialized = yaml.safe_dump(workflow)
        self.assertIn("aws-actions/configure-aws-credentials", serialized)
        self.assertIn("s3_prefix_cleanup_cli", serialized)


class SecurityWorkflowContractTests(unittest.TestCase):
    def test_security_scanners_declare_severity_gates(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "security.yml").read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        self.assertIn("dependency-review", jobs)
        self.assertIn("npm-audit", jobs)
        self.assertIn("codeql", jobs)
        self.assertIn("gitleaks", jobs)
        self.assertIn("sbom-and-image", jobs)
        dep_runs = [step.get("run", "") for step in jobs["dependency-review"]["steps"]]
        self.assertTrue(any("pip-audit" in block for block in dep_runs))
        self.assertTrue(
            any("requirements.txt" in block for block in dep_runs)
        )
        npm_job = jobs["npm-audit"]
        npm_runs = [step.get("run", "") for step in npm_job["steps"]]
        npm_node = next(
            step["with"]["node-version"]
            for step in npm_job["steps"]
            if str(step.get("uses", "")).startswith("actions/setup-node@")
        )
        self.assertEqual(npm_node, "22")
        self.assertTrue(any("npm ci" in block for block in npm_runs))
        self.assertTrue(any("npm audit --json" in block for block in npm_runs))
        self.assertTrue(any("--omit=dev" in block for block in npm_runs))
        self.assertTrue(
            any("scripts/npm-audit-gate.mjs" in block for block in npm_runs)
        )
        self.assertTrue(
            any("npm-audit-exceptions.json" in block for block in npm_runs)
        )
        self.assertTrue(
            any("npm-audit-gate.test.mjs" in block for block in npm_runs)
        )
        self.assertFalse(any("audit fix --force" in block for block in npm_runs))
        npm_uses = [step.get("uses", "") for step in npm_job["steps"]]
        self.assertTrue(any(item.startswith("actions/upload-artifact@") for item in npm_uses))
        npm_serialized = yaml.safe_dump(npm_job)
        self.assertIn("artifacts/npm-audit.json", npm_serialized)
        self.assertIn("artifacts/npm-audit-prod.json", npm_serialized)
        codeql_analyze = next(
            step
            for step in jobs["codeql"]["steps"]
            if step.get("uses", "").startswith("github/codeql-action/analyze")
        )
        self.assertEqual(
            codeql_analyze["with"]["upload"],
            "${{ github.event.repository.private == false }}",
        )
        trivy = next(
            step
            for step in jobs["sbom-and-image"]["steps"]
            if step.get("uses", "").startswith("aquasecurity/trivy-action")
        )
        self.assertEqual(trivy["with"]["exit-code"], "1")
        self.assertIn("CRITICAL", trivy["with"]["severity"])
        self.assertRegex(trivy["uses"].rpartition("@")[2], FULL_COMMIT_SHA)
        gitleaks_runs = [
            step.get("run", "") for step in jobs["gitleaks"]["steps"]
        ]
        self.assertTrue(any("gitleaks detect" in block for block in gitleaks_runs))


class NpmAuditBaselineContractTests(unittest.TestCase):
    """Issue #308: patched npm lockfile copies plus a dated exception process."""

    def test_frontend_overrides_floor_patched_advisory_lines(self) -> None:
        package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        overrides = package["overrides"]
        self.assertEqual(overrides["brace-expansion@1"], "^1.1.18")
        self.assertEqual(overrides["brace-expansion@2"], "^2.1.4")
        self.assertEqual(overrides["brace-expansion@5"], "^5.0.9")
        self.assertEqual(overrides["nanoid@3"], "^3.3.18")
        self.assertEqual(overrides["qs@6"], "^6.16.0")
        self.assertNotIn("qs", overrides)

    def test_lockfile_copies_are_at_or_above_advisory_floors(self) -> None:
        lock = json.loads(
            (ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")
        )
        found = {"brace-expansion": [], "nanoid": [], "qs": []}
        for path, pkg in lock["packages"].items():
            if "node_modules/" not in path:
                continue
            name = path.rsplit("node_modules/", 1)[-1]
            if name in found and pkg.get("version"):
                found[name].append((path, pkg["version"]))
        for name, copies in found.items():
            self.assertTrue(copies, f"expected lockfile copies of {name}")

        for path, version in found["brace-expansion"]:
            major, minor, patch = _semver(version)
            with self.subTest(path=path, version=version):
                if major == 1:
                    self.assertGreaterEqual((minor, patch), (1, 18), path)
                elif major == 2:
                    self.assertGreaterEqual((minor, patch), (1, 4), path)
                elif major == 3:
                    self.assertGreaterEqual((minor, patch), (0, 6), path)
                else:
                    self.assertGreaterEqual((major, minor, patch), (5, 0, 9), path)

        for path, version in found["nanoid"]:
            major, minor, patch = _semver(version)
            with self.subTest(path=path, version=version):
                if major == 3:
                    self.assertGreaterEqual((minor, patch), (3, 18), path)
                else:
                    self.assertGreaterEqual((major, minor, patch), (5, 1, 6), path)

        for path, version in found["qs"]:
            major, minor, patch = _semver(version)
            with self.subTest(path=path, version=version):
                self.assertGreaterEqual((major, minor, patch), (6, 16, 0), path)

    def test_npm_audit_exceptions_are_dated_and_currently_unused(self) -> None:
        payload = json.loads(
            (ROOT / ".github" / "npm-audit-exceptions.json").read_text(encoding="utf-8")
        )
        self.assertIn("exceptions", payload)
        self.assertIsInstance(payload["exceptions"], list)
        seen: set[str] = set()
        for index, entry in enumerate(payload["exceptions"]):
            with self.subTest(index=index):
                identifier = entry["id"]
                self.assertRegex(identifier, r"^(GHSA-[0-9a-z-]+|CVE-\d{4}-\d+)$")
                self.assertNotIn(identifier, seen)
                seen.add(identifier)
                self.assertTrue(str(entry["reason"]).strip())
                self.assertRegex(
                    entry["issue"],
                    r"^https://github\.com/[^/]+/[^/]+/issues/\d+$",
                )
                self.assertIn(entry["exposure"], {"runtime", "build"})
                self.assertRegex(entry["review_by"], r"^\d{4}-\d{2}-\d{2}$")
        # The lockfile floors above should keep this allowlist empty.
        self.assertEqual(payload["exceptions"], [])

    def test_npm_audit_gate_script_is_present(self) -> None:
        script = ROOT / "frontend" / "scripts" / "npm-audit-gate.mjs"
        tests = ROOT / "frontend" / "scripts" / "npm-audit-gate.test.mjs"
        self.assertTrue(script.is_file())
        self.assertTrue(tests.is_file())
        text = script.read_text(encoding="utf-8")
        self.assertIn("review_by", text)
        self.assertIn("runtime", text)
        self.assertIn("build", text)


class TrivyBaselineContractTests(unittest.TestCase):
    def test_runtime_security_pins_and_rclone_checksums_stay_coherent(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        pin = re.search(
            r"^FROM rclone/rclone:(\d+\.\d+\.\d+) AS rclone$",
            dockerfile,
            re.MULTILINE,
        )
        self.assertIsNotNone(pin, "Dockerfile must pin rclone/rclone:<semver> AS rclone")
        self.assertIn("RUN npm run build:ci", dockerfile)
        self.assertRegex(requirements, r"(?m)^msgpack==\d+\.\d+\.\d+\n")
        self.assertRegex(requirements, r"(?m)^setuptools==\d+\.\d+\.\d+\n")
        self.assertIn("apt-get upgrade -y", dockerfile)
        self.assertIn("pip uninstall --yes pip", dockerfile)
        trivyignore = (ROOT / ".trivyignore").read_text(encoding="utf-8")
        exceptions = {
            line.strip()
            for line in trivyignore.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        for cve in exceptions:
            with self.subTest(cve=cve):
                self.assertRegex(cve, r"^CVE-\d{4}-\d+$")
        if exceptions:
            self.assertRegex(
                trivyignore,
                r"Review and remove .+ by \d{4}-\d{2}-\d{2}",
            )
            self.assertRegex(trivyignore, r"https://\S+")

        for path in (
            WORKFLOWS / "migrations.yml",
            WORKFLOWS / "aws-s3-integrity.yml",
        ):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn(
                    "sed -n 's/^FROM rclone\\/rclone:\\([^ ]*\\) AS rclone$/\\1/p' Dockerfile",
                    text,
                )
                self.assertIn("SHA256SUMS", text)
                self.assertIn("sha256sum --check --strict", text)
                self.assertIsNone(
                    re.search(r"RCLONE_VERSION=\d+\.\d+\.\d+", text),
                    "CI must follow the Dockerfile rclone pin, not a hardcoded version",
                )


class ContainerPublishContractTests(unittest.TestCase):
    def test_publish_workflow_pushes_image_to_ghcr(self) -> None:
        path = WORKFLOWS / "publish-image.yml"
        self.assertTrue(path.is_file())
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        self.assertEqual((workflow.get("permissions") or {}).get("packages"), "write")
        self.assertEqual((workflow.get("permissions") or {}).get("checks"), "read")
        self.assertIn("ghcr.io/paolodelcasale/frostvault", text)
        self.assertIn("docker build", text)
        self.assertIn("docker push", text)
        dispatch = _workflow_on(workflow)["workflow_dispatch"]
        self.assertIn("promote_tag", dispatch["inputs"])
        self.assertIn('tags+=("${IMAGE}:${version}" "${IMAGE}:latest")', text)
        self.assertNotIn('refs/heads/main")\n            tags+=("${IMAGE}:latest")', text)
        self.assertIn('docker pull "${IMAGE}:${PROMOTE_TAG}"', text)
        self.assertIn('pushed_digest" != "$source_digest', text)
        # Repo Actions allowlist blocks docker/* marketplace actions.
        self.assertNotRegex(text, r"(?m)^\s*uses:\s*docker/")

    def test_publish_requires_commit_ci_without_privileged_triggers(self) -> None:
        path = WORKFLOWS / "publish-image.yml"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        triggers = _workflow_on(workflow)
        self.assertNotIn("workflow_run", triggers)
        self.assertNotIn("pull_request_target", triggers)
        self.assertIn("require-ci", workflow["jobs"])
        self.assertEqual(workflow["jobs"]["publish"].get("needs"), "require-ci")
        self.assertIn("scripts/require_commit_checks.py", text)
        script = (ROOT / "scripts" / "require_commit_checks.py").read_text(encoding="utf-8")
        for name in (
            "Unit and migration tests",
            "PostgreSQL migration and concurrency tests",
            "S3-compatible integrity (MinIO)",
            "Frontend generate and typecheck",
            "Frontend production build",
        ):
            self.assertIn(name, script)
        self.assertIn('ref: ${{ needs.require-ci.outputs.sha }}', text)
        self.assertIn('git rev-parse "v${PROMOTE_TAG}^{commit}"', text)


class ProductionImagePostgresClientContractTests(unittest.TestCase):
    """CI must keep PostgreSQL client tools in the production image (issue #7)."""

    def test_pr_ci_builds_image_and_exercises_postgres_backup_path(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        self.assertIn(
            "production-image-postgres-backup",
            jobs,
            "PR CI must build the production image and exercise the PG backup path",
        )
        job = jobs["production-image-postgres-backup"]
        self.assertIn("postgres", (job.get("services") or {}))
        self.assertEqual(
            (job["services"]["postgres"].get("image") or ""),
            "postgres:16",
        )
        runs = "\n".join(step.get("run", "") for step in job.get("steps", []))
        self.assertIn("docker build", runs)
        self.assertIn("cache-from type=local", runs)
        ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for item in (".git", "tests", "frontend/node_modules", "frontend/e2e"):
            self.assertIn(item, ignore)
        for tool in ("pg_dump", "pg_restore", "createdb", "dropdb", "psql"):
            self.assertIn(f"{tool} --version", runs)
        self.assertIn("app.backup_upgrade --skip-upgrade", runs)
        self.assertIn("verify_restore_isolated", runs)


class DependabotContractTests(unittest.TestCase):
    def test_codeql_actions_are_grouped_to_prevent_version_mismatches(self) -> None:
        config = yaml.safe_load(
            (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        )
        ecosystems = {item["package-ecosystem"] for item in config["updates"]}
        self.assertEqual(ecosystems, {"pip", "github-actions", "docker", "npm"})
        for item in config["updates"]:
            self.assertEqual(item.get("rebase-strategy"), "auto")
            self.assertEqual(item.get("open-pull-requests-limit"), 3)
            groups = item.get("groups") or {}
            minor_patch = [
                group
                for group in groups.values()
                if group.get("update-types") == ["minor", "patch"]
            ]
            self.assertEqual(len(minor_patch), 1, item["package-ecosystem"])
        npm = next(
            item
            for item in config["updates"]
            if item["package-ecosystem"] == "npm"
        )
        self.assertIn(
            {
                "dependency-name": "eslint",
                "update-types": ["version-update:semver-major"],
            },
            npm.get("ignore") or [],
        )
        actions = next(
            item
            for item in config["updates"]
            if item["package-ecosystem"] == "github-actions"
        )
        self.assertIn(
            "github/codeql-action/*",
            actions["groups"]["codeql-action"]["patterns"],
        )
        self.assertIn(
            "github/codeql-action/*",
            actions["groups"]["actions-minor-and-patch"]["exclude-patterns"],
        )

    def test_maintenance_updates_stale_prs_and_auto_merges_safe_updates(self) -> None:
        path = WORKFLOWS / "dependabot-maintenance.yml"
        text = path.read_text(encoding="utf-8")
        workflow = yaml.safe_load(text)
        triggers = _workflow_on(workflow)
        self.assertIn("workflow_dispatch", triggers)
        self.assertIn("push", triggers)
        self.assertIn("schedule", triggers)
        self.assertNotIn("pull_request", triggers)
        self.assertNotIn("pull_request_target", triggers)
        self.assertEqual(workflow["permissions"]["contents"], "write")
        self.assertEqual(workflow["permissions"]["pull-requests"], "write")
        self.assertIn("--author app/dependabot", text)
        self.assertNotIn("pulls/$number/update-branch", text)
        self.assertNotIn("expected_head_sha", text)
        self.assertIn("@dependabot rebase", text)
        self.assertIn("pulls/$number/commits?per_page=100", text)
        self.assertIn("version-update:semver-(minor|patch)", text)
        self.assertIn("version-update:semver-major", text)
        self.assertIn("update-type: version-update:", text)
        self.assertIn("sleep 15", text)
        self.assertIn('state" == "BEHIND"', text)
        self.assertIn("refresh_all force", text)
        self.assertIn("gh pr merge", text)
        self.assertIn("--auto --squash", text)
        self.assertIn("mergeStateStatus", text)
        self.assertIn("CLEAN", text)
        self.assertIn("refresh_all", text)

class ContributorCiDocsTests(unittest.TestCase):
    def test_ci_status_is_documented(self) -> None:
        docs = (ROOT / "docs" / "ci.md").read_text(encoding="utf-8")
        self.assertIn("Pull request", docs)
        self.assertIn("MinIO", docs)
        self.assertIn("OIDC", docs)
        self.assertIn("Trivy", docs)
        self.assertIn("npm audit", docs)
        self.assertIn("runtime", docs)
        self.assertIn("build", docs)
        self.assertIn("npm-audit-exceptions.json", docs)
        self.assertIn("s3_prefix_cleanup_cli", docs)
        self.assertIn("ghcr.io/paolodelcasale/frostvault", docs)

    def test_readme_mentions_published_image(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("ghcr.io/paolodelcasale/frostvault", readme)


class Epic56AgentPipelineRemovalTests(unittest.TestCase):
    """Issue #86: temporary epic #56 agent pipeline leaves no active artifacts."""

    def test_no_agent_workflow_files_remain(self) -> None:
        """Seam 2: no path matching .github/workflows/agent-*.yml remains."""
        leftovers = sorted(p.name for p in WORKFLOWS.glob("agent-*.yml"))
        self.assertEqual(leftovers, [], f"agent workflow files still present: {leftovers}")

    def test_pipeline_script_and_tests_do_not_remain(self) -> None:
        """Seam 3: agent_pipeline.py and tests/test_agent_pipeline.py are gone."""
        self.assertFalse((ROOT / ".github" / "scripts" / "agent_pipeline.py").exists())
        self.assertFalse((ROOT / "tests" / "test_agent_pipeline.py").exists())

    def test_no_pipeline_label_or_webhook_secret_references(self) -> None:
        """Seam 4: source and docs do not mention agent-pipeline or webhook secrets."""
        # Split literals so this test file is not a self-hit.
        forbidden = (
            "agent" + "-pipeline",
            "CURSOR_EPIC_56_WEBHOOK_",
        )
        skip_parts = {
            "node_modules",
            "dist",
            "__pycache__",
            ".venv",
            ".git",
        }
        scan_roots = (
            ROOT / "app",
            ROOT / "frontend" / "src",
            ROOT / "tests",
            ROOT / "docs",
            ROOT / ".github",
        )
        scan_files = (
            ROOT / "AGENTS.md",
            ROOT / "README.md",
            ROOT / "CONTEXT.md",
            ROOT / ".env.example",
            ROOT / ".env.local.example",
            ROOT / "Dockerfile",
        )
        suffixes = {
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".mjs",
            ".md",
            ".yml",
            ".yaml",
            ".example",
            ".css",
            ".html",
            ".json",
        }
        hits: list[str] = []
        self_path = Path(__file__).resolve()

        def _consider(path: Path) -> None:
            if path.resolve() == self_path:
                return
            if not path.is_file():
                return
            text = path.read_text(encoding="utf-8", errors="replace")
            for fragment in forbidden:
                if fragment in text:
                    hits.append(f"{path.relative_to(ROOT)}:{fragment}")

        for root in scan_roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if any(part in skip_parts for part in path.parts):
                    continue
                if path.suffix.lower() not in suffixes and path.name not in {
                    "AGENTS.md",
                    "README.md",
                    "CONTEXT.md",
                }:
                    continue
                _consider(path)
        for path in scan_files:
            _consider(path)
        self.assertEqual(hits, [], f"pipeline references remain in: {hits}")
