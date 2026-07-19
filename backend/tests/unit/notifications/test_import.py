from __future__ import annotations

import subprocess
import sys


def test_notifications_package_is_clean_on_cold_import() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import katilim_analiz.notifications"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
