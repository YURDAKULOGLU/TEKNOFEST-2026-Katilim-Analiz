"""Export or verify the deterministic FastAPI OpenAPI contract snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from katilim_analiz.api.app import create_app
from katilim_analiz.application.container import ApplicationContainer
from katilim_analiz.config import AppEnvironment, ModelProfile, Settings

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUTPUT = _REPOSITORY_ROOT / "web" / "openapi" / "openapi.json"


def _canonical_contract() -> str:
    settings = Settings(
        app_name="Katılım Analiz",
        app_env=AppEnvironment.TEST,
        app_allowed_hosts=["testserver"],
        app_cors_origins=[],
        model_profile=ModelProfile.RULES_ONLY,
        ingest_network_enabled=False,
    )
    # Route registration and schema generation do not invoke application ports.
    # The cast keeps the production factory as the single OpenAPI source of truth.
    container = cast(ApplicationContainer, cast(Any, object()))
    schema = create_app(container=container, settings=settings).openapi()
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true", help="fail if the snapshot is stale"
    )
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()

    output = args.output.resolve()
    allowed_root = (_REPOSITORY_ROOT / "web" / "openapi").resolve()
    if output.parent != allowed_root or output.name != "openapi.json":
        parser.error(f"output must be {allowed_root / 'openapi.json'}")

    expected = _canonical_contract()
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != expected:
            raise SystemExit("OpenAPI snapshot is stale; run tools/export_openapi.py")
        print("OpenAPI snapshot is current")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(expected, encoding="utf-8", newline="\n")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
