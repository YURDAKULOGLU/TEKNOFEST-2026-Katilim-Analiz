"""Small operator CLI for runtime jobs and pointer-driven evaluation delegation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from katilim_analiz.config import get_settings
from katilim_analiz.demo import seed_demo
from katilim_analiz.export import export_public_dataset
from katilim_analiz.intake import ingest_human_verified
from katilim_analiz.logging import configure_logging
from katilim_analiz.runtime.registry import (
    build_source_request,
    enqueue_source_job,
    load_runtime_registry,
    sync_source_registry,
)
from katilim_analiz.runtime.scanning import (
    CampaignScanResult,
    run_operational_campaign_scan,
    validate_scan_run_id,
)
from katilim_analiz.runtime.worker import run_worker
from katilim_analiz.storage.database import Database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="katilim-analiz")
    commands = parser.add_subparsers(dest="command", required=True)

    worker = commands.add_parser("worker", help="run the durable source worker")
    worker.add_argument("--worker-id", default="worker-1")
    worker.add_argument("--registry", type=Path)
    worker.add_argument("--poll-seconds", type=float, default=2.0)
    worker.add_argument("--once", action="store_true")

    registry = commands.add_parser(
        "registry-sync", help="transactionally seed the versioned BDDK registry"
    )
    registry.add_argument("--registry", type=Path)

    demo = commands.add_parser(
        "demo-seed", help="idempotently seed the versioned evidence-backed demo snapshot"
    )
    demo.add_argument("--dataset", type=Path)
    demo.add_argument("--registry", type=Path)

    intake = commands.add_parser(
        "human-verified-ingest",
        help="upsert human-attested campaign facts for sources the collector cannot reach",
    )
    intake.add_argument("--intake", type=Path, required=True)
    intake.add_argument("--registry", type=Path)

    export = commands.add_parser(
        "dataset-export",
        help="export the latest validated records as a versioned public dataset file",
    )
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--dataset-version", default="1.0.0")
    export.add_argument(
        "--as-of",
        help="timezone-aware ISO-8601 snapshot instant; defaults to the database clock",
    )

    enqueue = commands.add_parser(
        "enqueue-source", help="enqueue one registry-approved official source URL"
    )
    enqueue.add_argument("bank_id")
    enqueue.add_argument("source_url")
    enqueue.add_argument("--campaign-key")
    enqueue.add_argument("--registry", type=Path)

    scan = commands.add_parser(
        "scan", help="discover official campaign detail URLs and enqueue one scan"
    )
    scan.add_argument("--registry", type=Path)
    scan.add_argument("--campaign-registry", type=Path)

    evaluation = commands.add_parser("eval", help="delegate to the versioned eval harness")
    evaluation.add_argument("eval_args", nargs=argparse.REMAINDER)
    return parser


async def _registry_sync(path: Path | None) -> int:
    settings = get_settings()
    configure_logging(settings)
    database = Database.from_settings(settings)
    try:
        registry = load_runtime_registry(path)
        await sync_source_registry(database, registry)
    finally:
        await database.dispose()
    print(
        json.dumps(
            {
                "registry_version": registry.registry_version,
                "source_count": len(registry.banks),
                "status": "synced",
            },
            ensure_ascii=False,
        )
    )
    return 0


async def _enqueue_source(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings)
    database = Database.from_settings(settings)
    try:
        registry = load_runtime_registry(arguments.registry)
        await sync_source_registry(database, registry)
        source = build_source_request(
            registry,
            bank_id=arguments.bank_id,
            source_url=arguments.source_url,
            campaign_key=arguments.campaign_key,
        )
        job_id, created = await enqueue_source_job(database, registry, source)
    finally:
        await database.dispose()
    print(
        json.dumps(
            {
                "job_id": str(job_id),
                "created": created,
                "bank_id": source.bank_id,
                "campaign_key": source.campaign_key,
            },
            ensure_ascii=False,
        )
    )
    return 0


async def _demo_seed(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings)
    database = Database.from_settings(settings)
    try:
        result = await seed_demo(
            database,
            seed_path=arguments.dataset,
            registry_path=arguments.registry,
        )
    finally:
        await database.dispose()
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False))
    return 0


async def _human_verified_ingest(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings)
    database = Database.from_settings(settings)
    try:
        result = await ingest_human_verified(
            database,
            intake_path=arguments.intake,
            registry_path=arguments.registry,
        )
    finally:
        await database.dispose()
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False))
    return 0


def parse_export_as_of(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"--as-of is not a valid ISO-8601 datetime: {raw!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("--as-of must include a timezone offset")
    return value


async def _dataset_export(arguments: argparse.Namespace, as_of: datetime | None) -> int:
    settings = get_settings()
    configure_logging(settings)
    database = Database.from_settings(settings)
    try:
        result = await export_public_dataset(
            database,
            output_path=arguments.output,
            dataset_version=arguments.dataset_version,
            as_of=as_of,
        )
    finally:
        await database.dispose()
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False))
    return 0


def scan_identity_from_environment(environ: Mapping[str, str]) -> tuple[str, str | None]:
    raw_run_id = environ.get("SCAN_RUN_ID")
    if raw_run_id is None:
        raise ValueError("scan requires SCAN_RUN_ID from the Kubernetes Job UID label")
    run_id = validate_scan_run_id(raw_run_id)
    raw_job_name = environ.get("SCAN_JOB_NAME")
    job_name = None if raw_job_name is None else validate_scan_run_id(raw_job_name)
    return run_id, job_name


def _scan_summary(result: CampaignScanResult, scan_job_name: str | None) -> dict[str, object]:
    return {
        "scan_run_id": result.scan_run_id,
        "scan_job_name": scan_job_name,
        "enqueued_jobs": result.enqueued_jobs,
        "deduplicated_jobs": result.deduplicated_jobs,
        "review_required": result.review_required,
        "has_failures": result.has_failures,
        "has_blocked_sources": result.has_blocked_sources,
        "sources": [
            {
                "bank_id": source.bank_id,
                "status": source.status.value,
                "known_targets": source.known_targets,
                "discovered_targets": source.discovered_targets,
                "enqueued_jobs": source.enqueued_jobs,
                "deduplicated_jobs": source.deduplicated_jobs,
                "source_index_changed": source.source_index_changed,
                "review_required": source.review_required,
                "error_code": source.error_code,
            }
            for source in result.sources
        ],
    }


async def _campaign_scan(
    arguments: argparse.Namespace,
    *,
    scan_run_id: str,
    scan_job_name: str | None,
) -> int:
    settings = get_settings()
    configure_logging(settings)
    result = await run_operational_campaign_scan(
        settings,
        scan_run_id=scan_run_id,
        registry_path=arguments.registry,
        campaign_registry_path=arguments.campaign_registry,
    )
    print(json.dumps(_scan_summary(result, scan_job_name), ensure_ascii=False))
    return int(result.has_failures)


def _delegate_eval(arguments: Sequence[str]) -> int:
    if not arguments:
        raise ValueError("eval requires a subcommand such as 'run'")
    completed = subprocess.run(  # noqa: S603 - fixed interpreter/module, no shell
        [sys.executable, "-m", "evals", *arguments],
        check=False,
    )
    return completed.returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "worker":
        settings = get_settings()
        configure_logging(settings)
        asyncio.run(
            run_worker(
                settings,
                worker_id=arguments.worker_id,
                registry_path=(None if arguments.registry is None else str(arguments.registry)),
                poll_seconds=arguments.poll_seconds,
                once=arguments.once,
            )
        )
        return 0
    if arguments.command == "registry-sync":
        return asyncio.run(_registry_sync(arguments.registry))
    if arguments.command == "demo-seed":
        return asyncio.run(_demo_seed(arguments))
    if arguments.command == "human-verified-ingest":
        return asyncio.run(_human_verified_ingest(arguments))
    if arguments.command == "dataset-export":
        try:
            as_of = parse_export_as_of(arguments.as_of)
        except ValueError as exc:
            parser.error(str(exc))
        return asyncio.run(_dataset_export(arguments, as_of))
    if arguments.command == "enqueue-source":
        return asyncio.run(_enqueue_source(arguments))
    if arguments.command == "scan":
        try:
            scan_run_id, scan_job_name = scan_identity_from_environment(os.environ)
        except ValueError as exc:
            parser.error(str(exc))
        return asyncio.run(
            _campaign_scan(
                arguments,
                scan_run_id=scan_run_id,
                scan_job_name=scan_job_name,
            )
        )
    if arguments.command == "eval":
        try:
            return _delegate_eval(arguments.eval_args)
        except ValueError as exc:
            parser.error(str(exc))
    parser.error("unsupported command")
