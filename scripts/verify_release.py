"""Fail-closed, read-only release verification for the repository.

Executable failures, unexecuted full-profile checks, and human or hardware
evidence are intentionally different states.  This command never changes a
Kubernetes cluster; Kustomize checks are client-side renders only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
WEB = ROOT / "web"
POINTER_FILE = ROOT / "control" / "pointers.json"
EVIDENCE_GATE_POLICY = {
    "EVAL-010": True,
    "EVAL-011": False,
    "EVAL-012": True,
    "EVAL-014": True,
}


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EXECUTED = "NOT_EXECUTED"


@dataclass(frozen=True)
class Gate:
    name: str
    command: tuple[str, ...]
    cwd: Path = ROOT
    full_only: bool = False


@dataclass(frozen=True)
class GateResult:
    name: str
    status: Status
    duration_seconds: float = 0.0
    detail: str = ""


@dataclass(frozen=True)
class EvidenceGate:
    pointer_id: str
    title: str
    status: str
    blocking: bool

    @property
    def is_passed(self) -> bool:
        return self.status == "passed"


def virtualenv_python() -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return BACKEND / ".venv" / directory / executable


def discover_kustomizations() -> tuple[Path, ...]:
    root = ROOT / "deploy" / "k8s"
    return tuple(sorted(path.parent for path in root.rglob("kustomization.yaml")))


def powershell_parser_command() -> tuple[str, ...]:
    script = """
$root = Join-Path (Get-Location).Path 'scripts'
$failures = [System.Collections.Generic.List[string]]::new()
Get-ChildItem -LiteralPath $root -Recurse -Filter '*.ps1' -File | ForEach-Object {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $_.FullName,
        [ref]$tokens,
        [ref]$parseErrors
    ) | Out-Null
    foreach ($parseError in $parseErrors) {
        $failures.Add("$($_.FullName): $($parseError.Message)")
    }
}
if ($failures.Count -gt 0) {
    $failures | ForEach-Object { [Console]::Error.WriteLine($_) }
    exit 1
}
Write-Host 'POWERSHELL_PARSE_OK'
""".strip()
    return ("pwsh", "-NoProfile", "-NonInteractive", "-Command", script)


def build_gates() -> tuple[Gate, ...]:
    venv_python = str(virtualenv_python())
    gates: list[Gate] = [
        Gate("pointer graph", (sys.executable, "tools/check_pointers.py")),
        Gate(
            "release verifier unit tests",
            (sys.executable, "scripts/tests/test-release-verifier.py"),
        ),
        Gate("backend lock is current", ("uv", "lock", "--check"), BACKEND),
        Gate(
            "backend frozen dependency sync",
            ("uv", "sync", "--locked", "--extra", "dev"),
            BACKEND,
            full_only=True,
        ),
        Gate(
            "OpenAPI snapshot drift",
            (
                "uv",
                "run",
                "--locked",
                "--extra",
                "dev",
                "python",
                "../tools/export_openapi.py",
                "--check",
            ),
            BACKEND,
            full_only=True,
        ),
        Gate(
            "OpenAPI snapshot drift (installed environment)",
            (venv_python, "tools/export_openapi.py", "--check"),
        ),
        Gate(
            "backend lint",
            ("uv", "run", "--locked", "--extra", "dev", "ruff", "check", "."),
            BACKEND,
            full_only=True,
        ),
        Gate(
            "backend lint (installed environment)",
            (venv_python, "-m", "ruff", "check", "."),
            BACKEND,
        ),
        Gate(
            "backend format",
            (
                "uv",
                "run",
                "--locked",
                "--extra",
                "dev",
                "ruff",
                "format",
                "--check",
                ".",
            ),
            BACKEND,
            full_only=True,
        ),
        Gate(
            "backend format (installed environment)",
            (venv_python, "-m", "ruff", "format", "--check", "."),
            BACKEND,
        ),
        Gate(
            "backend typecheck",
            ("uv", "run", "--locked", "--extra", "dev", "mypy"),
            BACKEND,
            full_only=True,
        ),
        Gate(
            "backend typecheck (installed environment)",
            (venv_python, "-m", "mypy"),
            BACKEND,
        ),
        Gate(
            "backend full tests",
            ("uv", "run", "--locked", "--extra", "dev", "pytest"),
            BACKEND,
            full_only=True,
        ),
        Gate(
            "backend unit tests (installed environment)",
            (venv_python, "-m", "pytest", "tests/unit"),
            BACKEND,
        ),
        Gate(
            "frontend frozen dependency install",
            ("pnpm", "install", "--frozen-lockfile"),
            WEB,
            full_only=True,
        ),
        Gate("generated TypeScript drift", ("pnpm", "run", "contract:check"), WEB),
        Gate("frontend lint", ("pnpm", "run", "lint"), WEB),
        Gate("frontend tests", ("pnpm", "run", "test"), WEB),
        Gate("frontend typecheck", ("pnpm", "run", "typecheck"), WEB),
        Gate("frontend production build", ("pnpm", "run", "build"), WEB),
    ]

    for directory in discover_kustomizations():
        relative = directory.relative_to(ROOT).as_posix()
        gates.append(
            Gate(f"Kustomize render: {relative}", ("kubectl", "kustomize", relative))
        )

    gates.extend(
        [
            Gate("PowerShell parser", powershell_parser_command()),
            Gate(
                "offline bundle smoke",
                (
                    "pwsh",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    "scripts/tests/test-offline-bundle.ps1",
                ),
            ),
        ]
    )
    return tuple(gates)


def gates_for_profile(profile: str) -> tuple[tuple[Gate, ...], tuple[GateResult, ...]]:
    selected: list[Gate] = []
    skipped: list[GateResult] = []
    installed_names = {
        "OpenAPI snapshot drift (installed environment)",
        "backend lint (installed environment)",
        "backend format (installed environment)",
        "backend typecheck (installed environment)",
        "backend unit tests (installed environment)",
    }

    for gate in build_gates():
        if profile == "quick" and gate.full_only:
            skipped.append(
                GateResult(
                    gate.name,
                    Status.NOT_EXECUTED,
                    detail="full profile is required",
                )
            )
        elif profile == "full" and gate.name in installed_names:
            continue
        else:
            selected.append(gate)
    return tuple(selected), tuple(skipped)


def load_evidence_gates() -> tuple[EvidenceGate, ...]:
    try:
        graph = json.loads(POINTER_FILE.read_text(encoding="utf-8"))
        evaluations = graph["evaluations"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"cannot read evaluation pointers: {exc}") from exc

    gates: list[EvidenceGate] = []
    for pointer_id, expected_blocking in EVIDENCE_GATE_POLICY.items():
        try:
            node = evaluations[pointer_id]
            title = node["title"]
            status = node["status"]
            blocking = node["blocking"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"malformed evaluation pointer {pointer_id}: {exc}"
            ) from exc
        if (
            not isinstance(title, str)
            or not isinstance(status, str)
            or not isinstance(blocking, bool)
        ):
            raise ValueError(
                f"malformed evaluation pointer {pointer_id}: invalid field type"
            )
        if blocking is not expected_blocking:
            raise ValueError(
                f"evaluation pointer {pointer_id}: blocking must be {expected_blocking}"
            )
        gates.append(EvidenceGate(pointer_id, title, status, blocking))
    return tuple(gates)


def _resolve_executable(command: Sequence[str]) -> tuple[str, ...] | None:
    if not command:
        return None
    executable = command[0]
    resolved = shutil.which(executable)
    if resolved is None and Path(executable).is_file():
        resolved = str(Path(executable).resolve())
    if resolved is None:
        return None
    return (resolved, *command[1:])


def run_gate(gate: Gate) -> GateResult:
    resolved = _resolve_executable(gate.command)
    display = subprocess.list2cmdline(list(gate.command))
    print(f"\n--- {gate.name}\n$ {display}", flush=True)
    if resolved is None:
        return GateResult(
            gate.name, Status.FAIL, detail=f"command not found: {gate.command[0]}"
        )
    if not gate.cwd.is_dir():
        return GateResult(
            gate.name, Status.FAIL, detail=f"working directory missing: {gate.cwd}"
        )

    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(resolved), cwd=gate.cwd, check=False, shell=False
        )
    except OSError as exc:
        return GateResult(gate.name, Status.FAIL, time.monotonic() - started, str(exc))
    duration = time.monotonic() - started
    if completed.returncode != 0:
        return GateResult(
            gate.name,
            Status.FAIL,
            duration,
            f"exit code {completed.returncode}",
        )
    return GateResult(gate.name, Status.PASS, duration)


def determine_exit_code(
    results: Sequence[GateResult], evidence: Sequence[EvidenceGate], profile: str
) -> int:
    if any(result.status == Status.FAIL for result in results):
        return 1
    if profile == "quick" or any(
        result.status == Status.NOT_EXECUTED for result in results
    ):
        return 2
    if any(gate.blocking and not gate.is_passed for gate in evidence):
        return 2
    return 0


def print_summary(
    profile: str,
    results: Sequence[GateResult],
    evidence: Sequence[EvidenceGate],
    exit_code: int,
) -> None:
    print("\n=== EXECUTABLE GATES ===")
    for result in results:
        duration = (
            f" ({result.duration_seconds:.1f}s)" if result.duration_seconds else ""
        )
        detail = f" - {result.detail}" if result.detail else ""
        print(f"{result.status.value:12} {result.name}{duration}{detail}")

    print("\n=== HUMAN / HARDWARE EVIDENCE ===")
    for gate in evidence:
        category = "BLOCKING" if gate.blocking else "NON_BLOCKING"
        if gate.is_passed:
            outcome = "PASS"
        elif gate.status == "failed":
            outcome = "FAILED"
        else:
            outcome = "PENDING"
        print(
            f"{outcome:7} {category:12} {gate.pointer_id} [{gate.status}] - {gate.title}"
        )

    if profile == "quick":
        print(
            "\nQUICK PROFILE IS PREFLIGHT ONLY; it never produces a release-pass verdict."
        )
    explanations = {
        0: "RELEASE VERIFIED: executable gates and blocking evidence passed.",
        1: "RELEASE REJECTED: at least one executable gate failed.",
        2: "RELEASE PENDING: executable work was skipped or blocking evidence is not passed.",
    }
    print(f"\n{explanations[exit_code]}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("quick", "full"),
        default="full",
        help="quick is installed-environment preflight; full performs frozen installs and all checks",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    selected, skipped = gates_for_profile(args.profile)
    results: list[GateResult] = []

    try:
        evidence = load_evidence_gates()
    except ValueError as exc:
        evidence = ()
        results.append(
            GateResult("evaluation pointer state", Status.FAIL, detail=str(exc))
        )

    failed = bool(results)
    for gate in selected:
        if failed:
            results.append(
                GateResult(
                    gate.name,
                    Status.NOT_EXECUTED,
                    detail="blocked by an earlier executable failure",
                )
            )
            continue
        result = run_gate(gate)
        results.append(result)
        failed = result.status == Status.FAIL

    results.extend(skipped)
    exit_code = determine_exit_code(results, evidence, args.profile)
    print_summary(args.profile, results, evidence, exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
