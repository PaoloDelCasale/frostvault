"""Structural guarantees for layered CI (issue #13) and production-image
PostgreSQL backup tooling (issue #7).

Seams under test: workflow YAML and Dependabot config as the public CI contract
contributors rely on — not GitHub's runtime.
"""

from __future__ import annotations

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

    def test_pr_unit_job_runs_js_tests(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["sqlite-and-postgresql"]["steps"]
        run_blocks = [step.get("run", "") for step in steps]
        self.assertTrue(any("node --test" in block for block in run_blocks))

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
    def test_aws_workflow_is_manual_or_weekly_not_on_pull_request(self) -> None:
        workflow = yaml.safe_load(
            (WORKFLOWS / "aws-s3-integrity.yml").read_text(encoding="utf-8")
        )
        triggers = _workflow_on(workflow)
        self.assertIn("workflow_dispatch", triggers)
        self.assertIn("schedule", triggers)
        self.assertNotIn("pull_request", triggers)
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
