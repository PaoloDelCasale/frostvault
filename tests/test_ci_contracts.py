"""Structural guarantees for layered CI (issue #13), strict frontend checks
(issue #204), and production-image PostgreSQL backup tooling (issue #7).

Seams under test: workflow YAML and Dependabot config as the public CI contract
contributors rely on — not GitHub's runtime.
"""

from __future__ import annotations

import datetime
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


def _override_floor(spec: str) -> tuple[int, int, int]:
    """Lower bound of an override range such as ``^10.7.5`` or ``>=2.4.0``."""
    return _semver(spec.lstrip("^~>=< "))


def _workflow_on(workflow: dict) -> dict | list | str:
    # PyYAML 1.1 may parse the key ``on`` as boolean True.
    if "on" in workflow:
        return workflow["on"]
    return workflow[True]


class PythonRuntimeAlignmentContractTests(unittest.TestCase):
    """Issue #553: ship only the Python line exercised by the complete suite."""

    def test_final_image_uses_versioned_official_python_312_index_pin(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        stages = re.findall(r"(?mi)^FROM\s+(.+)$", dockerfile)
        self.assertRegex(
            stages[-1],
            r"^python:3\.12\.\d+-slim-trixie@sha256:[0-9a-f]{64}$",
        )
        # A literal tag+digest stays discoverable by root Docker Dependabot;
        # a build ARG or floating tag must not bypass the runtime contract.
        self.assertEqual(sum(stage.startswith("python:") for stage in stages), 1)

    def test_all_ci_python_setups_and_host_commands_use_312(self) -> None:
        configured = set()
        host_python = re.compile(
            r"(?m)^\s*(?:python(?:3(?:\.\d+)?)?|pip3?|pip-audit)(?:\s|$)"
        )
        for path in WORKFLOWS.glob("*.yml"):
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
            for name, job in workflow["jobs"].items():
                setup_seen = False
                for step in job.get("steps", []):
                    if step.get("uses", "").startswith("actions/setup-python@"):
                        with self.subTest(workflow=path.name, job=name):
                            self.assertEqual(step["with"]["python-version"], "3.12")
                            self.assertNotIn("if", step)
                            self.assertFalse(step.get("continue-on-error", False))
                        setup_seen = True
                        configured.add((path.name, name))
                    if host_python.search(step.get("run", "")):
                        self.assertTrue(setup_seen, f"{path.name}:{name} uses runner Python")
        self.assertTrue({
            ("migrations.yml", "python-unit-tests"),
            ("migrations.yml", "postgresql-tests"),
            ("migrations.yml", "s3-compatible-integrity"),
            ("migrations.yml", "playwright-e2e"),
            ("security.yml", "dependency-review"),
            ("security.yml", "codeql"),
            ("publish-image.yml", "require-ci"),
            ("aws-s3-integrity.yml", "aws-integrity"),
        }.issubset(configured))

    def test_playwright_uses_the_setup_python_interpreter(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        step = next(step for step in workflow["jobs"]["playwright-e2e"]["steps"]
                    if "npm run test:e2e" in step.get("run", ""))
        self.assertEqual(step["env"]["E2E_PYTHON"], "python")
        config = (ROOT / "frontend/e2e/playwright.config.ts").read_text(encoding="utf-8")
        self.assertIn("if (process.env.E2E_PYTHON) return process.env.E2E_PYTHON", config)
        self.assertIn("command: `${resolvePython()}", config)

    def test_built_image_checks_python_line_before_backup(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["production-image-postgres-backup"]["steps"]
        index = next(i for i, step in enumerate(steps)
                     if step.get("name") == "Verify Python runtime in image")
        probe = steps[index]
        self.assertNotIn("if", probe)
        self.assertFalse(probe.get("continue-on-error", False))
        self.assertIn('docker run --rm --entrypoint python "$IMAGE"', probe["run"])
        self.assertIn("assert sys.version_info[:2] == (3, 12)", probe["run"])
        self.assertLess(index, next(i for i, step in enumerate(steps)
                                   if "run_metadata_backup" in step.get("run", "")))


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
        self.assertIn("unit-and-migration-tests", job_names)
        self.assertIn("s3-compatible-integrity", job_names)
        self.assertIn("playwright-e2e", job_names)
        s3_job_runs = "\n".join(
            step.get("run", "")
            for step in workflow["jobs"]["s3-compatible-integrity"]["steps"]
        )
        # The S3-compatible endpoint comes from a pinned PyPI package, not a
        # container registry: MinIO community images are no longer published.
        self.assertRegex(s3_job_runs, r'"moto\[server\]==\d+\.\d+\.\d+"')
        self.assertIn("moto_server -H 127.0.0.1 -p 9000", s3_job_runs)
        self.assertNotIn("minio/minio:", s3_job_runs)
        self.assertNotIn("docker run", s3_job_runs)

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
        self.assertTrue(any("cmp src/vendor/shadcn/LICENSE.md dist/licenses/shadcn.txt" in block for block in build_runs))
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

    def test_pr_isolates_postgresql_tests_from_the_postgresql_unit_job(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        postgresql_job = jobs["unit-and-migration-tests"]
        python_job = jobs["python-unit-tests"]
        postgres_job = jobs["postgresql-tests"]
        self.assertEqual(
            python_job["strategy"]["matrix"]["shard"],
            [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
        )
        self.assertEqual(postgresql_job["needs"], ["python-unit-tests", "postgresql-tests"])
        self.assertEqual(postgresql_job["if"], "${{ always() }}")
        python_runs = "\n".join(
            step.get("run", "") for step in python_job["steps"]
        )
        self.assertIn("key=lambda path: (-path.stat().st_size, path.name)", python_runs)
        self.assertIn("assigned[target].append(path)", python_runs)
        self.assertEqual(
            python_job["steps"][-1].get("env", {}).get("SHARD_COUNT"),
            "12",
        )
        self.assertIn("postgres", python_job.get("services") or {})
        self.assertIn("TEST_POSTGRES_URL", python_job.get("env") or {})
        for name in ("python-unit-tests", "postgresql-tests", "s3-compatible-integrity", "playwright-e2e"):
            self.assertEqual(jobs[name]["services"]["postgres"]["image"], "postgres:16")
            self.assertIn("TEST_POSTGRES_URL", jobs[name]["env"])
        self.assertIn('if path.stem not in {"test_database", "test_aggregate_deadlocks"}', python_runs)
        self.assertIn("postgres", postgres_job.get("services") or {})
        postgres_runs = "\n".join(
            step.get("run", "") for step in postgres_job["steps"]
        )
        self.assertIn("tests.test_database", postgres_runs)
        self.assertIn("tests.test_aggregate_deadlocks", postgres_runs)

    def test_upload_stress_is_bounded_only_on_prs_and_attested_on_main(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        triggers = _workflow_on(workflow)
        self.assertEqual(triggers["push"]["branches"], ["main"])
        self.assertNotIn("paths", triggers["pull_request"] or {})
        job = workflow["jobs"]["postgresql-tests"]
        self.assertEqual(job["env"]["FROSTVAULT_STRESS_PROFILE"],
                         "${{ github.event_name == 'pull_request' && 'pr' || 'full' }}")
        self.assertNotIn("if", job)
        self.assertFalse(job.get("continue-on-error", False))
        stress = next(step for step in job["steps"] if step.get("id") == "stress")
        self.assertNotIn("if", stress)
        self.assertFalse(stress.get("continue-on-error", False))
        self.assertIn("tests.test_aggregate_deadlocks", stress["run"])
        guard = next(step for step in job["steps"]
                     if step.get("name") == "PostgreSQL schema guard and baseline tests")
        self.assertEqual(guard["run"], "python -m unittest tests.test_database -v")
        self.assertNotIn("if", guard)
        self.assertLess(job["steps"].index(guard), job["steps"].index(stress))
        output = 'echo "profile=$FROSTVAULT_STRESS_PROFILE" >> "$GITHUB_OUTPUT"'
        self.assertIn(output, stress["run"])
        self.assertLess(stress["run"].index("python -m unittest"), stress["run"].index(output))
        self.assertNotIn("shell", stress)  # GitHub's default bash -e is required.
        self.assertNotIn("||", stress["run"])
        self.assertNotIn("set +e", stress["run"])
        self.assertEqual(job["outputs"]["profile"], "${{ steps.stress.outputs.profile }}")
        full = workflow["jobs"]["full-postgresql-stress"]
        self.assertEqual(full["name"], "Full PostgreSQL concurrency stress")
        self.assertEqual(full["needs"], ["postgresql-tests"])
        self.assertEqual(full["if"], "${{ always() && github.event_name == 'push' && github.ref == 'refs/heads/main' }}")
        step = full["steps"][0]
        self.assertEqual(step["env"]["RESULT"], "${{ needs.postgresql-tests.result }}")
        self.assertEqual(step["env"]["PROFILE"], "${{ needs.postgresql-tests.outputs.profile }}")
        self.assertIn('test "$RESULT" = success', step["run"])
        self.assertIn('test "$PROFILE" = full', step["run"])
        import os
        import subprocess
        for result, profile, expected in (
            ("success", "full", 0), ("success", "pr", 1),
            ("success", "", 1), ("skipped", "full", 1),
            ("failure", "full", 1), ("cancelled", "full", 1),
        ):
            with self.subTest(result=result, profile=profile):
                command = subprocess.run(["bash", "-e", "-c", step["run"]],
                                         env={**os.environ, "RESULT": result, "PROFILE": profile})
                self.assertEqual(command.returncode, expected)

    def test_every_python_module_is_selected_once_without_importing_database_tests(self) -> None:
        # Execute the actual workflow selector for all shards: a newly added
        # regression must not disappear or run repeatedly due to sharding.
        import contextlib
        import io
        import os
        from collections import Counter
        from unittest.mock import patch

        workflow = yaml.safe_load((WORKFLOWS / "migrations.yml").read_text(encoding="utf-8"))
        job = workflow["jobs"]["python-unit-tests"]
        run = job["steps"][-1]["run"]
        selector = run.split("python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
        selected = []
        for shard in job["strategy"]["matrix"]["shard"]:
            out = io.StringIO()
            with patch.dict(os.environ, {"SHARD_INDEX": str(shard), "SHARD_COUNT": "12"}), contextlib.redirect_stdout(out):
                exec(compile(selector, "workflow-selector", "exec"), {})
            selected.extend(out.getvalue().splitlines())
        dedicated = {"tests.test_database", "tests.test_aggregate_deadlocks"}
        expected = {f"tests.{path.stem}" for path in (ROOT / "tests").glob("test_*.py")} - dedicated
        self.assertEqual(Counter(selected), Counter({module: 1 for module in expected}))
        postgres = workflow["jobs"]["postgresql-tests"]
        runs = "\n".join(step.get("run", "") for step in postgres["steps"])
        self.assertIn("tests.test_database", runs)
        self.assertIn("tests.test_aggregate_deadlocks", runs)

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


class AuditGatesDoNotBlockUnrelatedMergesTests(unittest.TestCase):
    """Advisories published after a PR opens must not block unrelated merges.

    Pull requests audit in baseline mode (only findings the change introduces
    fail); main and the daily schedule stay strict and keep a tracking issue.
    """

    def setUp(self) -> None:
        self.workflow = yaml.safe_load(
            (WORKFLOWS / "security.yml").read_text(encoding="utf-8")
        )
        self.jobs = self.workflow["jobs"]

    def test_schedule_is_daily(self) -> None:
        crons = [item["cron"] for item in _workflow_on(self.workflow)["schedule"]]
        self.assertEqual(len(crons), 1)
        minute, hour, dom, month, dow = crons[0].split()
        self.assertEqual((dom, month, dow), ("*", "*", "*"))

    def test_each_audit_has_baseline_mode_and_issue_tracking(self) -> None:
        for job_name, gate_script, baseline_flag in (
            ("dependency-review", "scripts/pip_audit_gate.py", "--baseline"),
            ("npm-audit", "scripts/npm-audit-gate.mjs", "--baseline-full"),
        ):
            with self.subTest(job=job_name):
                job = self.jobs[job_name]
                self.assertEqual(job["permissions"].get("issues"), "write")
                steps = job["steps"]
                gate = next(step for step in steps if step.get("id") == "gate")
                self.assertIn(gate_script, gate["run"])
                self.assertIn(baseline_flag, gate["run"])
                self.assertIn('"$GITHUB_EVENT_NAME" = "pull_request"', gate["run"])
                self.assertIn("origin/${BASE_REF}", gate["run"])
                checkout = next(
                    step
                    for step in steps
                    if str(step.get("uses", "")).startswith("actions/checkout@")
                )
                self.assertEqual(checkout["with"]["fetch-depth"], 0)
                self.assertIs(checkout["with"]["persist-credentials"], False)
                tracker = next(
                    step for step in steps if "ci_audit_issue.sh" in step.get("run", "")
                )
                self.assertIn("github.event_name != 'pull_request'", tracker["if"])
                self.assertIn("steps.gate.outcome", tracker["env"]["GATE_OUTCOME"])

    def test_issue_tracker_script_is_one_self_closing_issue(self) -> None:
        script = (ROOT / "scripts" / "ci_audit_issue.sh").read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", script)
        self.assertIn("gh issue edit", script)
        self.assertIn("gh issue create", script)
        self.assertIn("gh issue close", script)

    def test_docs_describe_the_two_modes(self) -> None:
        docs = (ROOT / "docs" / "ci.md").read_text(encoding="utf-8")
        self.assertIn("baseline mode", docs)
        self.assertIn("Security audit failing on main", docs)


class NpmAuditBaselineContractTests(unittest.TestCase):
    """Issues #308/#405: patched remaining copies and retired toolchain findings."""

    def test_frontend_overrides_floor_patched_advisory_lines(self) -> None:
        package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        overrides = package["overrides"]
        # Floors, not exact pins: a Dependabot/advisory bump to a newer patched
        # release must not fail this contract. Each override must still require
        # at least the first patched release of its advisory line.
        floors = {
            "body-parser": (2, 4, 0),
            "brace-expansion@1": (1, 1, 21),
            "brace-expansion@2": (2, 1, 7),
            "brace-expansion@5": (5, 0, 12),
            "fast-uri@3": (3, 1, 8),
            "ip-address@10": (10, 7, 5),
            "nanoid@3": (3, 3, 18),
            "qs@6": (6, 16, 0),
            "undici@7": (7, 29, 1),
        }
        # The CLI-only packages left the graph in #405; unused overrides are
        # not required. Keep floors for any override that is retained/re-added,
        # and verify all remaining copies independently below.
        for name in ("brace-expansion@1", "brace-expansion@2", "brace-expansion@5", "nanoid@3"):
            self.assertIn(name, overrides)
        for name, floor in floors.items():
            if name in overrides:
                with self.subTest(override=name):
                    self.assertGreaterEqual(_override_floor(overrides[name]), floor, name)
        self.assertNotIn("qs", overrides)

    def test_lockfile_copies_are_at_or_above_advisory_floors(self) -> None:
        lock = json.loads(
            (ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")
        )
        found = {
            "body-parser": [],
            "brace-expansion": [],
            "fast-uri": [],
            "glob": [],
            "ip-address": [],
            "nanoid": [],
            "qs": [],
            "undici": [],
        }
        for path, pkg in lock["packages"].items():
            if "node_modules/" not in path:
                continue
            name = path.rsplit("node_modules/", 1)[-1]
            if name in found and pkg.get("version"):
                found[name].append((path, pkg["version"]))
        # glob left with Workbox (#415); body-parser, fast-uri, ip-address and
        # qs left with the shadcn CLI (#405). Do not require absent packages to
        # return merely to test them. Floors below still cover every copy if
        # any future dependency reintroduces them.
        for name in ("brace-expansion", "nanoid", "undici"):
            self.assertTrue(found[name], f"expected lockfile copies of {name}")

        for path, version in found["brace-expansion"]:
            major, minor, patch = _semver(version)
            with self.subTest(path=path, version=version):
                if major == 1:
                    self.assertGreaterEqual((minor, patch), (1, 21), path)
                elif major == 2:
                    self.assertGreaterEqual((minor, patch), (1, 7), path)
                elif major == 3:
                    self.assertGreaterEqual((minor, patch), (0, 6), path)
                else:
                    self.assertGreaterEqual((major, minor, patch), (5, 0, 12), path)

        for path, version in found["fast-uri"]:
            major, minor, patch = _semver(version)
            with self.subTest(path=path, version=version):
                if major == 3:
                    self.assertGreaterEqual((minor, patch), (1, 8), path)
                else:
                    self.assertGreaterEqual(major, 4, path)

        for path, version in found["ip-address"]:
            major, minor, patch = _semver(version)
            with self.subTest(path=path, version=version):
                self.assertGreaterEqual((major, minor, patch), (10, 7, 5), path)

        for path, version in found["body-parser"]:
            major, minor, patch = _semver(version)
            with self.subTest(path=path, version=version):
                self.assertGreaterEqual((major, minor, patch), (2, 4, 0), path)

        for path, version in found["glob"]:
            major, minor, patch = _semver(version)
            with self.subTest(path=path, version=version):
                self.assertGreaterEqual((major, minor, patch), (14, 0, 2), path)

        for path, version in found["undici"]:
            major, minor, patch = _semver(version)
            with self.subTest(path=path, version=version):
                if major == 7:
                    self.assertGreaterEqual((minor, patch), (29, 1), path)
                else:
                    self.assertGreaterEqual(major, 8, path)

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

    def test_issue_405_vulnerable_graph_and_exceptions_are_gone(self) -> None:
        package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
        self.assertNotIn("shadcn", package["devDependencies"])
        self.assertNotIn("shadcn", lock["packages"][""]["devDependencies"])
        removed = {"shadcn", "@shadcn/registry", "deepmerge", "ts-morph", "@ts-morph/common"}
        minimatch_versions = set()
        for path, pkg in lock["packages"].items():
            name = path.rsplit("node_modules/", 1)[-1]
            self.assertNotIn(name, removed, path)
            if name == "minimatch":
                version = _semver(pkg["version"])
                minimatch_versions.add(version[0])
                # GHSA-g3hv-75wg-m249 affects >=7 <11.0.1, not 3/5.
                self.assertTrue(version[0] < 7 or version >= (11, 0, 1), path)
        self.assertTrue({3, 5, 11}.issubset(minimatch_versions))
        overrides = package["overrides"]
        self.assertNotIn("minimatch", overrides)
        self.assertEqual([key for key in overrides if key.startswith("minimatch")], ["minimatch@10"])
        self.assertGreaterEqual(_override_floor(overrides["minimatch@10"]), (11, 0, 1))
        self.assertEqual(_override_floor(overrides["minimatch@10"])[0], 11)
        exceptions = json.loads((ROOT / ".github" / "npm-audit-exceptions.json").read_text(encoding="utf-8"))
        retired = {"GHSA-gcqx-w48m-w2h4", "GHSA-g3hv-75wg-m249"}
        self.assertFalse(retired.intersection(entry["id"] for entry in exceptions["exceptions"]))

    def test_npm_audit_exceptions_are_dated(self) -> None:
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
                # Only unfixable dev-toolchain advisories may be excepted, and
                # never past their review date.
                self.assertEqual(entry["exposure"], "build")
                self.assertGreaterEqual(
                    entry["review_by"], datetime.date.today().isoformat()
                )

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
    def test_runtime_security_pins_and_sdk_only_image_stay_coherent(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("rclone", dockerfile.lower())
        self.assertNotIn("FROM golang:", dockerfile)
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
            "Full PostgreSQL concurrency stress",
            "S3-compatible integrity (MinIO)",
            "Frontend generate and typecheck",
            "Frontend production build",
        ):
            self.assertIn(name, script)
        require_steps = workflow["jobs"]["require-ci"]["steps"]
        gate_step = next(step for step in require_steps
                         if "scripts/require_commit_checks.py" in step.get("run", ""))
        self.assertNotIn("if", gate_step)
        self.assertFalse(gate_step.get("continue-on-error", False))
        self.assertEqual(gate_step["run"].strip(),
                         'python scripts/require_commit_checks.py --sha "${{ steps.resolve.outputs.sha }}"')
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
        self.assertIn("run_metadata_backup", runs)
        self.assertIn("verify_restore_isolated", runs)


class DependabotContractTests(unittest.TestCase):
    def test_root_docker_updates_keep_python_digest_and_patch_refreshes_enabled(self) -> None:
        config = yaml.safe_load(
            (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        )
        docker = next(item for item in config["updates"]
                      if item["package-ecosystem"] == "docker")
        self.assertEqual(docker["directory"], "/")
        self.assertEqual(docker["schedule"]["interval"], "weekly")
        self.assertFalse(docker.get("ignore"))
        self.assertFalse(docker.get("allow"))
        self.assertEqual(docker["groups"]["docker-minor-and-patch"]["update-types"],
                         ["minor", "patch"])

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
        ignored_majors = npm.get("ignore") or []
        for name in (
            "eslint",
            "@eslint/js",
            "eslint-plugin-react-hooks",
            "typescript",
        ):
            self.assertIn(
                {
                    "dependency-name": name,
                    "update-types": ["version-update:semver-major"],
                },
                ignored_majors,
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

    def test_maintenance_auto_merges_safe_updates(self) -> None:
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
        # A comment from GITHUB_TOKEN is refused by Dependabot ("only users with
        # push access can use that command"), so the workflow must not rely on it.
        self.assertNotIn("@dependabot rebase", text)
        self.assertIn("pulls/$number/commits?per_page=100", text)
        self.assertIn("version-update:semver-(minor|patch)", text)
        self.assertIn("version-update:semver-major", text)
        self.assertIn("update-type: version-update:", text)
        self.assertIn("gh pr merge", text)
        self.assertIn("--auto --squash", text)
        self.assertIn("mergeStateStatus", text)
        self.assertIn("CLEAN", text)

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
