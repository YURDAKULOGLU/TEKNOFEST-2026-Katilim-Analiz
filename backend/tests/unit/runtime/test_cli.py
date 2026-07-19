from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_pointer_compatible_module_command_delegates_real_evaluation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    project_root = Path(__file__).resolve().parents[4]
    output = tmp_path / "result.json"
    summary = tmp_path / "summary.md"
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "katilim_analiz",
            "eval",
            "run",
            "--output",
            str(output),
            "--summary",
            str(summary),
            "--allow-incomplete",
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert output.is_file()
    assert summary.is_file()
