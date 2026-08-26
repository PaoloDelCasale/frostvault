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

    def test_pr_runs_frontend_quality_in_a_dedicated_parallel_job(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        self.assertIn("frontend-quality", jobs)
        run_blocks = [
            step.get("run", "") for step in jobs["frontend-quality"]["steps"]
        ]
        self.assertTrue(any("npm ci" in block for block in run_blocks))
        self.assertTrue(any("npm run test" in block for block in run_blocks))
        self.assertTrue(any("npm run lint" in block for block in run_blocks))
        self.assertTrue(any("npm run build" in block for block in run_blocks))
        self.assertFalse(any("node --test" in block for block in run_blocks))

    def test_frontend_quality_refreshes_artifacts_before_strict_checks(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["frontend-quality"]["steps"]
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

        for command in ("npm run lint", "npm run test", "npm run build"):
            self.assertGreater(step_index(command), typecheck)

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
            [0, 1, 2, 3],
        )
        self.assertEqual(sqlite_job["needs"], "python-unit-tests")
        self.assertEqual(sqlite_job["if"], "${{ always() }}")
        python_runs = "\n".join(
            step.get("run", "") for step in python_job["steps"]
        )
        self.assertIn("index % shard_count == shard_index", python_runs)
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
        runs = [step.get("run", "") for step in job["steps"]]
        self.assertTrue(any("playwright install" in block for block in runs))
        self.assertTrue(any("test:e2e" in block for block in runs))
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
        self.assertIn("codeql", jobs)
        self.assertIn("gitleaks", jobs)
        self.assertIn("sbom-and-image", jobs)
        dep_runs = [step.get("run", "") for step in jobs["dependency-review"]["steps"]]
        self.assertTrue(any("pip-audit" in block for block in dep_runs))
        self.assertTrue(
            any("requirements.txt" in block for block in dep_runs)
        )
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


class TrivyBaselineContractTests(unittest.TestCase):
    def test_runtime_security_pins_and_rclone_checksums_stay_coherent(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("FROM rclone/rclone:1.75.0 AS rclone", dockerfile)
        self.assertIn("msgpack==1.2.1\n", requirements)
        self.assertIn("setuptools==83.0.0\n", requirements)
        self.assertIn("apt-get upgrade -y", dockerfile)
        self.assertIn("pip uninstall --yes pip", dockerfile)

        rclone_checksum = (
            "aa2804e08f48250e71009c727124b6341cd0288465804a9a09d14663cabafbaa"
        )
        for path in (
            WORKFLOWS / "migrations.yml",
            WORKFLOWS / "aws-s3-integrity.yml",
        ):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn("RCLONE_VERSION=1.75.0", text)
                self.assertIn(rclone_checksum, text)
                self.assertNotIn("1.74.4", text)


class ContainerPublishContractTests(unittest.TestCase):
    def test_publish_workflow_pushes_image_to_ghcr(self) -> None:
        path = WORKFLOWS / "publish-image.yml"
        self.assertTrue(path.is_file())
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        self.assertEqual((workflow.get("permissions") or {}).get("packages"), "write")
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
        for tool in ("pg_dump", "pg_restore", "createdb", "dropdb", "psql"):
            self.assertIn(f"{tool} --version", runs)
        self.assertIn("app.backup_upgrade --skip-upgrade", runs)
        self.assertIn("verify_restore_isolated", runs)


class DependabotContractTests(unittest.TestCase):
    def test_dependabot_has_no_auto_merge(self) -> None:
        text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        config = yaml.safe_load(text)
        for update in config["updates"]:
            self.assertNotIn("auto-merge", update)
            self.assertFalse(update.get("automerged", False))
        ecosystems = {item["package-ecosystem"] for item in config["updates"]}
        self.assertEqual(ecosystems, {"pip", "github-actions", "docker"})
        self.assertIn("proposals only", text.lower())

class ContributorCiDocsTests(unittest.TestCase):
    def test_ci_status_is_documented(self) -> None:
        docs = (ROOT / "docs" / "ci.md").read_text(encoding="utf-8")
        self.assertIn("Pull request", docs)
        self.assertIn("MinIO", docs)
        self.assertIn("OIDC", docs)
        self.assertIn("Trivy", docs)
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
