"""Focused stdlib tests for the release verifier and local model guard."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VERIFIER_PATH = ROOT / "scripts" / "verify_release.py"
LOCAL_UP_PATH = ROOT / "scripts" / "local-up.ps1"

SPEC = importlib.util.spec_from_file_location("verify_release", VERIFIER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load {VERIFIER_PATH}")
verify_release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify_release
SPEC.loader.exec_module(verify_release)


class GatePlanTests(unittest.TestCase):
    def test_full_profile_contains_every_required_executable_gate(self) -> None:
        selected, skipped = verify_release.gates_for_profile("full")
        names = {gate.name for gate in selected}
        required = {
            "pointer graph",
            "release verifier unit tests",
            "backend lock is current",
            "backend frozen dependency sync",
            "OpenAPI snapshot drift",
            "backend lint",
            "backend format",
            "backend typecheck",
            "backend full tests",
            "frontend frozen dependency install",
            "generated TypeScript drift",
            "frontend lint",
            "frontend tests",
            "frontend typecheck",
            "frontend production build",
            "PowerShell parser",
            "offline bundle smoke",
        }
        self.assertTrue(required <= names, required - names)
        self.assertEqual(skipped, ())

        rendered = {
            gate.name.removeprefix("Kustomize render: ")
            for gate in selected
            if gate.name.startswith("Kustomize render: ")
        }
        expected = {
            path.parent.relative_to(ROOT).as_posix()
            for path in (ROOT / "deploy" / "k8s").rglob("kustomization.yaml")
        }
        self.assertGreater(len(expected), 0)
        self.assertEqual(rendered, expected)

    def test_quick_profile_marks_full_only_gates_not_executed(self) -> None:
        selected, skipped = verify_release.gates_for_profile("quick")
        selected_names = {gate.name for gate in selected}
        skipped_names = {result.name for result in skipped}

        self.assertIn("backend unit tests (installed environment)", selected_names)
        self.assertIn("OpenAPI snapshot drift (installed environment)", selected_names)
        self.assertIn("backend frozen dependency sync", skipped_names)
        self.assertIn("backend full tests", skipped_names)
        self.assertIn("frontend frozen dependency install", skipped_names)
        self.assertTrue(
            all(
                result.status == verify_release.Status.NOT_EXECUTED
                for result in skipped
            )
        )

    def test_commands_are_argument_tuples_and_subprocess_never_uses_shell(self) -> None:
        for gate in verify_release.build_gates():
            self.assertIsInstance(gate.command, tuple)
            self.assertGreater(len(gate.command), 0)
            self.assertTrue(all(isinstance(argument, str) for argument in gate.command))
        source = VERIFIER_PATH.read_text(encoding="utf-8")
        self.assertIn("shell=False", source)
        self.assertNotIn("shell=True", source)

    def test_exit_codes_separate_failures_pending_evidence_and_pass(self) -> None:
        passed = [verify_release.GateResult("gate", verify_release.Status.PASS)]
        failed = [verify_release.GateResult("gate", verify_release.Status.FAIL)]
        skipped = [
            verify_release.GateResult("gate", verify_release.Status.NOT_EXECUTED)
        ]
        blocking_pending = [
            verify_release.EvidenceGate("EVAL-X", "x", "proposed", True)
        ]
        blocking_passed = [verify_release.EvidenceGate("EVAL-X", "x", "passed", True)]
        nonblocking_pending = [
            verify_release.EvidenceGate("EVAL-Y", "y", "proposed", False)
        ]

        self.assertEqual(verify_release.determine_exit_code(failed, (), "full"), 1)
        self.assertEqual(verify_release.determine_exit_code(skipped, (), "full"), 2)
        self.assertEqual(verify_release.determine_exit_code(passed, (), "quick"), 2)
        self.assertEqual(
            verify_release.determine_exit_code(passed, blocking_pending, "full"), 2
        )
        self.assertEqual(
            verify_release.determine_exit_code(
                passed, (*blocking_passed, *nonblocking_pending), "full"
            ),
            0,
        )


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is required")
class LocalModelGuardTests(unittest.TestCase):
    _fixture_script = r"""
$path = $env:RELEASE_TEST_LOCAL_UP
$caseName = $env:RELEASE_TEST_CASE
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $path,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { exit 90 }
$functionAst = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Assert-OllamaInventory'
}, $true)
if ($null -eq $functionAst) { exit 91 }
Invoke-Expression $functionAst.Extent.Text

$model = 'qwen3.5:4b'
$expected = 'a' * 64
$shouldPass = $caseName -eq 'exact'
switch ($caseName) {
    'exact' {
        $inventory = [pscustomobject]@{ models = @(
            [pscustomobject]@{ name = $model; digest = $expected }
        ) }
    }
    'missing-name' {
        $inventory = [pscustomobject]@{ models = @(
            [pscustomobject]@{ name = 'qwen3.5:9b'; digest = $expected }
        ) }
    }
    'digest-mismatch' {
        $inventory = [pscustomobject]@{ models = @(
            [pscustomobject]@{ name = $model; digest = ('b' * 64) }
        ) }
    }
    'malformed-digest' {
        $inventory = [pscustomobject]@{ models = @(
            [pscustomobject]@{ name = $model; digest = 'not-a-digest' }
        ) }
    }
    'duplicate-name' {
        $inventory = [pscustomobject]@{ models = @(
            [pscustomobject]@{ name = $model; digest = $expected },
            [pscustomobject]@{ name = $model; digest = $expected }
        ) }
    }
    'case-mismatch' {
        $inventory = [pscustomobject]@{ models = @(
            [pscustomobject]@{ name = 'Qwen3.5:4b'; digest = $expected }
        ) }
    }
    'invalid-config-digest' {
        $expected = 'not-a-digest'
        $inventory = [pscustomobject]@{ models = @(
            [pscustomobject]@{ name = $model; digest = ('a' * 64) }
        ) }
    }
    'invalid-config-name' {
        $model = 'qwen model without explicit tag'
        $inventory = [pscustomobject]@{ models = @(
            [pscustomobject]@{ name = $model; digest = $expected }
        ) }
    }
    default { exit 92 }
}

try {
    Assert-OllamaInventory -Inventory $inventory -ExpectedModel $model -ExpectedDigest $expected
    if (-not $shouldPass) { exit 93 }
}
catch {
    if ($shouldPass) {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 94
    }
    exit 0
}
exit 0
""".strip()

    def test_exact_name_and_digest_is_the_only_accepted_inventory(self) -> None:
        for case_name in (
            "exact",
            "missing-name",
            "digest-mismatch",
            "malformed-digest",
            "duplicate-name",
            "case-mismatch",
            "invalid-config-digest",
            "invalid-config-name",
        ):
            with self.subTest(case=case_name):
                completed = subprocess.run(
                    [
                        "pwsh",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        self._fixture_script,
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    shell=False,
                    env={
                        **os.environ,
                        "RELEASE_TEST_LOCAL_UP": str(LOCAL_UP_PATH),
                        "RELEASE_TEST_CASE": case_name,
                    },
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    f"{case_name}: stdout={completed.stdout!r} stderr={completed.stderr!r}",
                )

    def test_inventory_gate_runs_after_pull_and_before_warmup(self) -> None:
        source = LOCAL_UP_PATH.read_text(encoding="utf-8")
        main_start = source.index("Push-Location $RepoRoot")
        pull = source.index('-Name "model-pull"', main_start)
        inventory = source.index("Assert-ConfiguredOllamaModel", pull)
        warmup = source.index('-Name "model-warmup"', inventory)
        self.assertLess(pull, inventory)
        self.assertLess(inventory, warmup)
        self.assertIn("-SkippedPull:$SkipModelPull", source[inventory:warmup])


if __name__ == "__main__":
    unittest.main(verbosity=2)
