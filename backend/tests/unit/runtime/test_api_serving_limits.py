from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def test_api_container_and_kubernetes_cap_concurrent_requests() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    command_match = re.search(r"^CMD\s+(\[.*\])$", dockerfile, flags=re.MULTILINE)
    assert command_match is not None
    command = json.loads(command_match.group(1))
    limit_index = command.index("--limit-concurrency")
    assert command[limit_index + 1] == "32"

    deployment = yaml.safe_load(
        (PROJECT_ROOT / "deploy/k8s/base/api-deployment.yaml").read_text(encoding="utf-8")
    )
    args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
    limit_index = args.index("--limit-concurrency")
    assert args[limit_index + 1] == "32"
