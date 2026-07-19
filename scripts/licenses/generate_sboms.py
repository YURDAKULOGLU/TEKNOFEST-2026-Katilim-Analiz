#!/usr/bin/env python3
"""Generate deterministic CycloneDX inventories from locked runtime inputs.

The script intentionally uses only the Python standard library plus the
repository's package-manager CLIs.  Python license data comes from PyPI's
official JSON API.  Frontend license data comes from the exact package.json
files installed by the frozen pnpm lock.  OCI package discovery is delegated
to a caller-supplied, pinned Syft executable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SBOM_DIR = REPO_ROOT / "artifacts" / "sbom"
LICENSE_DIR = REPO_ROOT / "artifacts" / "licenses"
OVERRIDES_PATH = LICENSE_DIR / "python-license-overrides.json"
PYPI_CACHE_PATH = LICENSE_DIR / "pypi-runtime-metadata.json"
NPM_CACHE_PATH = LICENSE_DIR / "npm-runtime-metadata.json"
SPDX_CACHE_PATH = LICENSE_DIR / "spdx-license-ids-3.28.0.json"
INVENTORY_PATH = LICENSE_DIR / "runtime-license-inventory.json"

SPDX_LIST_VERSION = "3.28.0"
SPDX_LIST_URL = (
    "https://raw.githubusercontent.com/spdx/license-list-data/"
    f"v{SPDX_LIST_VERSION}/json/licenses.json"
)
PYPI_JSON_URL = "https://pypi.org/pypi/{name}/{version}/json"
MODEL_NAME = "qwen3.5:4b"
MODEL_DIGEST = "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd"

FORBIDDEN_LICENSE_MARKERS = {
    "UNLICENSED",
    "PROPRIETARY",
    "NOASSERTION",
    "NONE",
}
EXPRESSION_OPERATORS = {"AND", "OR", "WITH"}


class InventoryError(RuntimeError):
    """Raised when a fail-closed inventory invariant is violated."""


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(rendered, encoding="utf-8", newline="\n")


def run(command: list[str], *, cwd: Path = REPO_ROOT) -> str:
    executable = command[0]
    if not Path(executable).is_file():
        resolved = shutil.which(executable)
        if not resolved:
            raise InventoryError(f"required executable not found: {executable}")
        command = [resolved, *command[1:]]
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise InventoryError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{detail}"
        )
    return completed.stdout


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "katilim-analiz-sbom/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS origins
        return json.load(response)


def load_spdx_ids(*, refresh: bool) -> set[str]:
    if refresh or not SPDX_CACHE_PATH.exists():
        source = fetch_json(SPDX_LIST_URL)
        payload = {
            "license_list_version": SPDX_LIST_VERSION,
            "source_url": SPDX_LIST_URL,
            "license_ids": sorted(item["licenseId"] for item in source["licenses"]),
        }
        write_json(SPDX_CACHE_PATH, payload)
    cached = json.loads(SPDX_CACHE_PATH.read_text(encoding="utf-8"))
    if cached.get("license_list_version") != SPDX_LIST_VERSION:
        raise InventoryError("SPDX cache version does not match the pinned version")
    return set(cached["license_ids"])


def expression_ids(expression: str) -> set[str]:
    tokens = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]*", expression))
    return tokens - EXPRESSION_OPERATORS


def validate_expression(expression: str, spdx_ids: set[str]) -> None:
    normalized = expression.strip()
    if not normalized or normalized.upper() in FORBIDDEN_LICENSE_MARKERS:
        raise InventoryError(f"forbidden or empty license expression: {expression!r}")
    unknown = sorted(
        token for token in expression_ids(normalized) if token not in spdx_ids
    )
    if unknown:
        raise InventoryError(
            f"license expression {expression!r} has non-SPDX identifiers: {', '.join(unknown)}"
        )


def license_choice(expression: str) -> list[dict[str, Any]]:
    if len(expression_ids(expression)) == 1 and not any(
        operator in expression.split() for operator in EXPRESSION_OPERATORS
    ):
        return [{"license": {"id": expression}}]
    return [{"expression": expression}]


def base_bom(root_component: dict[str, Any]) -> dict[str, Any]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.7",
        "version": 1,
        "metadata": {
            "component": root_component,
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "katilim-license-generator",
                        "version": "1.0.0",
                    }
                ]
            },
        },
    }


def component_sort_key(component: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(component.get("bom-ref", "")),
        str(component.get("purl", "")),
        str(component.get("name", "")),
        str(component.get("version", "")),
    )


def normalize_bom(payload: dict[str, Any]) -> dict[str, Any]:
    payload.pop("serialNumber", None)
    metadata = payload.get("metadata", {})
    metadata.pop("timestamp", None)

    payload["components"] = sorted(
        payload.get("components", []), key=component_sort_key
    )
    for component in payload["components"]:
        for field, key in (
            ("licenses", lambda item: json.dumps(item, sort_keys=True)),
            ("hashes", lambda item: (item.get("alg", ""), item.get("content", ""))),
            ("properties", lambda item: (item.get("name", ""), item.get("value", ""))),
            (
                "externalReferences",
                lambda item: (item.get("type", ""), item.get("url", "")),
            ),
        ):
            if field in component:
                component[field] = sorted(component[field], key=key)

    dependencies = payload.get("dependencies", [])
    for dependency in dependencies:
        dependency["dependsOn"] = sorted(set(dependency.get("dependsOn", [])))
    payload["dependencies"] = sorted(dependencies, key=lambda item: item.get("ref", ""))
    return payload


def runtime_python_packages() -> tuple[dict[str, dict[str, Any]], set[str]]:
    lock_path = REPO_ROOT / "backend" / "uv.lock"
    with lock_path.open("rb") as stream:
        lock = tomllib.load(stream)
    packages = {canonical_name(item["name"]): item for item in lock["package"]}

    exported = run(
        [
            "uv",
            "export",
            "--project",
            "backend",
            "--locked",
            "--no-dev",
            "--no-hashes",
            "--no-emit-project",
            "--format",
            "requirements-txt",
        ]
    )
    runtime_names: set[str] = set()
    for line in exported.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        candidate = candidate.split(";", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s]+)$", candidate)
        if not match:
            raise InventoryError(f"unexpected uv export line: {line!r}")
        name, version = canonical_name(match.group(1)), match.group(2)
        locked = packages.get(name)
        if not locked or locked.get("version") != version:
            raise InventoryError(
                f"uv export is not aligned with uv.lock for {name}=={version}"
            )
        runtime_names.add(name)
    return packages, runtime_names


def load_python_overrides() -> dict[tuple[str, str], dict[str, str]]:
    payload = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    overrides: dict[tuple[str, str], dict[str, str]] = {}
    for item in payload["overrides"]:
        key = (canonical_name(item["name"]), item["version"])
        if key in overrides:
            raise InventoryError(f"duplicate Python license override: {key}")
        overrides[key] = item
    return overrides


def python_metadata(
    packages: dict[str, dict[str, Any]],
    runtime_names: set[str],
    *,
    refresh: bool,
) -> dict[tuple[str, str], dict[str, Any]]:
    expected = {(name, packages[name]["version"]) for name in runtime_names}
    if not refresh and PYPI_CACHE_PATH.exists():
        cached_payload = json.loads(PYPI_CACHE_PATH.read_text(encoding="utf-8"))
        cached = {
            (canonical_name(item["name"]), item["version"]): item
            for item in cached_payload["packages"]
        }
        if set(cached) == expected:
            return cached

    records: list[dict[str, Any]] = []
    for name, version in sorted(expected):
        source_url = PYPI_JSON_URL.format(
            name=urllib.parse.quote(name), version=urllib.parse.quote(version)
        )
        info = fetch_json(source_url)["info"]
        records.append(
            {
                "name": name,
                "version": version,
                "license_expression": info.get("license_expression"),
                "license": info.get("license"),
                "license_classifiers": sorted(
                    item
                    for item in info.get("classifiers", [])
                    if item.startswith("License ::")
                ),
                "project_url": info.get("project_url"),
                "source_url": source_url,
            }
        )
    payload = {"source": "PyPI JSON API", "packages": records}
    write_json(PYPI_CACHE_PATH, payload)
    return {(item["name"], item["version"]): item for item in records}


def resolve_python_license(
    name: str,
    version: str,
    metadata: dict[tuple[str, str], dict[str, Any]],
    overrides: dict[tuple[str, str], dict[str, str]],
    spdx_ids: set[str],
) -> tuple[str, str]:
    record = metadata[(name, version)]
    expression = record.get("license_expression")
    source = record["source_url"]
    if not expression:
        override = overrides.get((name, version))
        if not override:
            raise InventoryError(
                f"{name}=={version} has no PEP 639 license expression and no reviewed override"
            )
        expression = override["expression"]
        source = override["source_url"]
    validate_expression(expression, spdx_ids)
    return expression, source


def dependency_names(package: dict[str, Any], runtime_names: set[str]) -> set[str]:
    names: set[str] = set()
    for dependency in package.get("dependencies", []):
        name = canonical_name(dependency["name"])
        if name in runtime_names:
            names.add(name)
    for optional_group in package.get("optional-dependencies", {}).values():
        for dependency in optional_group:
            name = canonical_name(dependency["name"])
            if name in runtime_names:
                names.add(name)
    return names


def generate_backend_bom(
    packages: dict[str, dict[str, Any]],
    runtime_names: set[str],
    metadata: dict[tuple[str, str], dict[str, Any]],
    overrides: dict[tuple[str, str], dict[str, str]],
    spdx_ids: set[str],
) -> dict[str, Any]:
    root_ref = "pkg:pypi/katilim-analiz@0.1.0"
    root = {
        "bom-ref": root_ref,
        "type": "application",
        "name": "katilim-analiz",
        "version": "0.1.0",
        "licenses": license_choice("Apache-2.0"),
        "purl": root_ref,
    }
    bom = base_bom(root)
    components: list[dict[str, Any]] = []
    references: dict[str, str] = {}
    for name in sorted(runtime_names):
        package = packages[name]
        version = package["version"]
        expression, source = resolve_python_license(
            name, version, metadata, overrides, spdx_ids
        )
        purl = f"pkg:pypi/{name}@{version}"
        references[name] = purl
        properties = []
        markers = sorted(
            {
                dependency["marker"]
                for dependency in package.get("dependencies", [])
                if dependency.get("marker")
            }
        )
        properties.extend(
            {"name": "katilim:environment-marker", "value": marker}
            for marker in markers
        )
        components.append(
            {
                "bom-ref": purl,
                "type": "library",
                "name": name,
                "version": version,
                "scope": "required",
                "licenses": license_choice(expression),
                "purl": purl,
                "externalReferences": [
                    {
                        "type": "distribution",
                        "url": metadata[(name, version)]["source_url"],
                    },
                    {"type": "license", "url": source},
                ],
                **({"properties": properties} if properties else {}),
            }
        )

    dependencies = []
    root_package = packages["katilim-analiz"]
    dependencies.append(
        {
            "ref": root_ref,
            "dependsOn": sorted(
                references[name]
                for name in dependency_names(root_package, runtime_names)
            ),
        }
    )
    for name in sorted(runtime_names):
        dependencies.append(
            {
                "ref": references[name],
                "dependsOn": sorted(
                    references[child]
                    for child in dependency_names(packages[name], runtime_names)
                ),
            }
        )
    bom["components"] = components
    bom["dependencies"] = dependencies
    return normalize_bom(bom)


def repository_url(package_json: dict[str, Any]) -> str | None:
    repository = package_json.get("repository")
    if isinstance(repository, dict):
        value = repository.get("url")
    elif isinstance(repository, str):
        value = repository
    else:
        value = None
    if not isinstance(value, str) or not value:
        return None
    if not value.startswith(("https://", "http://", "git://", "git+https://")):
        return None
    return value


def pnpm_runtime_tree() -> tuple[
    dict[str, set[str]], dict[str, dict[str, Any]], set[str]
]:
    output = run(
        ["pnpm", "--dir", "web", "list", "--prod", "--depth", "Infinity", "--json"]
    )
    roots = json.loads(output)
    if len(roots) != 1:
        raise InventoryError("expected one pnpm project root")
    root = roots[0]
    edges: dict[str, set[str]] = defaultdict(set)
    metadata: dict[str, dict[str, Any]] = {}
    root_dependencies: set[str] = set()
    queue: deque[tuple[str | None, str, dict[str, Any]]] = deque()
    for dependency_name, node in root.get("dependencies", {}).items():
        queue.append((None, dependency_name, node))

    expanded: set[tuple[str, str]] = set()
    while queue:
        parent_key, name, node = queue.popleft()
        version = str(node.get("version", ""))
        path_text = node.get("path")
        if not version or not path_text:
            raise InventoryError(f"pnpm tree node lacks exact version/path: {name}")
        key = f"{name}@{version}"
        if parent_key is None:
            root_dependencies.add(key)
        else:
            edges[parent_key].add(key)
        package_path = Path(path_text) / "package.json"
        package_json = json.loads(package_path.read_text(encoding="utf-8"))
        license_value = package_json.get("license")
        if not isinstance(license_value, str) or not license_value.strip():
            raise InventoryError(
                f"{key} has no scalar license in published package.json"
            )
        record = {
            "name": name,
            "version": version,
            "license_expression": license_value.strip(),
            "homepage": package_json.get("homepage"),
            "repository": repository_url(package_json),
            "source_path": package_path.resolve().relative_to(REPO_ROOT).as_posix(),
        }
        previous = metadata.setdefault(key, record)
        if previous != record:
            raise InventoryError(f"conflicting installed metadata for {key}")
        expansion_key = (key, str(Path(path_text)))
        if expansion_key in expanded:
            continue
        expanded.add(expansion_key)
        for child_name, child_node in node.get("dependencies", {}).items():
            queue.append((key, child_name, child_node))
    return edges, metadata, root_dependencies


def npm_purl(name: str, version: str) -> str:
    encoded_name = urllib.parse.quote(name, safe="/")
    return f"pkg:npm/{encoded_name}@{version}"


def deterministic_component_ref(namespace: str, identifier: str) -> str:
    """Return an opaque, stable CycloneDX reference for dependency graph edges."""
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    return f"urn:katilim:{namespace}:{digest}"


def generate_frontend_bom(spdx_ids: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    edges, metadata, root_dependencies = pnpm_runtime_tree()
    records: list[dict[str, Any]] = []
    references: dict[str, str] = {}
    components: list[dict[str, Any]] = []
    for key in sorted(metadata):
        record = metadata[key]
        expression = record["license_expression"]
        validate_expression(expression, spdx_ids)
        name, version = record["name"], record["version"]
        purl = npm_purl(name, version)
        component_ref = deterministic_component_ref("npm", purl)
        references[key] = component_ref
        external_references = [
            {
                "type": "distribution",
                "url": (
                    "https://www.npmjs.com/package/"
                    f"{urllib.parse.quote(name, safe='@/')}/v/{urllib.parse.quote(version)}"
                ),
            }
        ]
        if record.get("repository"):
            external_references.append({"type": "vcs", "url": record["repository"]})
        components.append(
            {
                "bom-ref": component_ref,
                "type": "library",
                "name": name,
                "version": version,
                "scope": "required",
                "licenses": license_choice(expression),
                "purl": purl,
                "externalReferences": external_references,
            }
        )
        records.append(record)

    root_ref = "pkg:npm/katilim-analiz-web@0.1.0"
    root = {
        "bom-ref": root_ref,
        "type": "application",
        "name": "katilim-analiz-web",
        "version": "0.1.0",
        "licenses": license_choice("Apache-2.0"),
        "purl": root_ref,
    }
    bom = base_bom(root)
    bom["components"] = components
    dependencies = [
        {
            "ref": root_ref,
            "dependsOn": sorted(references[key] for key in root_dependencies),
        }
    ]
    for key in sorted(metadata):
        dependencies.append(
            {
                "ref": references[key],
                "dependsOn": sorted(
                    references[child] for child in edges.get(key, set())
                ),
            }
        )
    bom["dependencies"] = dependencies
    cache = {
        "source": "frozen pnpm production tree and published package.json",
        "packages": records,
    }
    return normalize_bom(bom), cache


def generate_model_bom() -> dict[str, Any]:
    root_ref = f"urn:sha256:{MODEL_DIGEST}"
    root = {
        "bom-ref": root_ref,
        "type": "machine-learning-model",
        "name": MODEL_NAME,
        "version": MODEL_DIGEST,
        "hashes": [{"alg": "SHA-256", "content": MODEL_DIGEST}],
        "licenses": license_choice("Apache-2.0"),
        "externalReferences": [
            {"type": "distribution", "url": "https://ollama.com/library/qwen3.5:4b"},
            {"type": "documentation", "url": "https://huggingface.co/Qwen/Qwen3.5-4B"},
        ],
        "properties": [
            {"name": "katilim:ollama-manifest-digest", "value": MODEL_DIGEST},
            {"name": "katilim:quantization", "value": "Q4_K_M"},
        ],
    }
    return normalize_bom(base_bom(root))


def replace_bom_ref(payload: dict[str, Any], old_ref: str, new_ref: str) -> None:
    for dependency in payload.get("dependencies", []):
        if dependency.get("ref") == old_ref:
            dependency["ref"] = new_ref
        dependency["dependsOn"] = [
            new_ref if item == old_ref else item
            for item in dependency.get("dependsOn", [])
        ]
    for composition in payload.get("compositions", []):
        if composition.get("bom-ref") == old_ref:
            composition["bom-ref"] = new_ref


def use_opaque_component_refs(payload: dict[str, Any], namespace: str) -> None:
    """Decouple graph identifiers from encoded Package URLs while retaining purl fields."""
    mapping: dict[str, str] = {}
    for component in payload.get("components", []):
        old_ref = str(component.get("bom-ref", ""))
        if not old_ref:
            raise InventoryError("CycloneDX component is missing bom-ref")
        identifier = str(component.get("purl") or old_ref)
        new_ref = deterministic_component_ref(namespace, identifier)
        previous = mapping.setdefault(old_ref, new_ref)
        if previous != new_ref:
            raise InventoryError(f"conflicting CycloneDX reference: {old_ref}")
        component["bom-ref"] = new_ref

    for dependency in payload.get("dependencies", []):
        dependency["ref"] = mapping.get(dependency.get("ref"), dependency.get("ref"))
        dependency["dependsOn"] = [
            mapping.get(item, item) for item in dependency.get("dependsOn", [])
        ]
    for composition in payload.get("compositions", []):
        composition["bom-ref"] = mapping.get(
            composition.get("bom-ref"), composition.get("bom-ref")
        )


def image_identity(image: str) -> tuple[str, str]:
    output = run(["docker", "image", "inspect", image])
    inspected = json.loads(output)[0]
    image_id = str(inspected["Id"])
    repo_digests = sorted(inspected.get("RepoDigests") or [])
    digest_reference = repo_digests[0] if repo_digests else image_id
    digest = digest_reference.rsplit("@sha256:", 1)[-1].removeprefix("sha256:")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise InventoryError(f"cannot determine immutable image digest for {image}")
    return digest_reference, digest


def enrich_oci_python_licenses(
    payload: dict[str, Any],
    metadata: dict[tuple[str, str], dict[str, Any]],
    overrides: dict[tuple[str, str], dict[str, str]],
    spdx_ids: set[str],
) -> None:
    for component in payload.get("components", []):
        purl = str(component.get("purl", ""))
        if not purl.startswith("pkg:pypi/"):
            continue
        name = canonical_name(component["name"])
        version = str(component["version"])
        key = (name, version)
        if key not in metadata:
            # Base-image tools such as pip are not application dependencies. Keep
            # the exact declaration detected from their installed metadata.
            continue
        expression, source = resolve_python_license(
            name, version, metadata, overrides, spdx_ids
        )
        component["licenses"] = license_choice(expression)
        component.setdefault("externalReferences", []).append(
            {"type": "license", "url": source}
        )


def generate_oci_bom(
    image: str,
    syft_executable: Path,
    metadata: dict[tuple[str, str], dict[str, Any]],
    overrides: dict[tuple[str, str], dict[str, str]],
    spdx_ids: set[str],
) -> tuple[dict[str, Any], dict[str, str]]:
    if not syft_executable.is_file():
        raise InventoryError(f"Syft executable not found: {syft_executable}")
    version_output = run([str(syft_executable), "version"])
    if "Version:       1.48.0" not in version_output:
        raise InventoryError("OCI generation requires pinned Syft 1.48.0")
    digest_reference, digest = image_identity(image)
    with tempfile.TemporaryDirectory(prefix="katilim-sbom-") as temp_dir:
        raw_path = Path(temp_dir) / "oci.raw.cdx.json"
        run(
            [
                str(syft_executable),
                f"docker:{image}",
                "--override-default-catalogers",
                "dpkg-db-cataloger,python-installed-package-cataloger",
                "--select-catalogers",
                "-file",
                "-o",
                f"cyclonedx-json={raw_path}",
            ]
        )
        payload = json.loads(raw_path.read_text(encoding="utf-8"))

    root = payload["metadata"]["component"]
    old_ref = root["bom-ref"]
    new_ref = f"urn:sha256:{digest}"
    root["bom-ref"] = new_ref
    root["version"] = digest
    root["hashes"] = [{"alg": "SHA-256", "content": digest}]
    root.setdefault("properties", []).extend(
        [
            {"name": "katilim:image-reference", "value": image},
            {"name": "katilim:image-digest-reference", "value": digest_reference},
            {"name": "katilim:syft-version", "value": "1.48.0"},
        ]
    )
    replace_bom_ref(payload, old_ref, new_ref)
    enrich_oci_python_licenses(payload, metadata, overrides, spdx_ids)
    use_opaque_component_refs(payload, "oci")
    return normalize_bom(payload), {
        "requested_reference": image,
        "digest_reference": digest_reference,
        "sha256": digest,
        "syft_version": "1.48.0",
    }


def license_declarations(component: dict[str, Any]) -> list[str]:
    declarations: list[str] = []
    for choice in component.get("licenses", []):
        if choice.get("expression"):
            declarations.append(choice["expression"])
            continue
        license_data = choice.get("license", {})
        value = license_data.get("id") or license_data.get("name")
        if value:
            declarations.append(value)
    return declarations


def bom_status(
    payload: dict[str, Any], *, ignore_aggregate_os: bool = False
) -> dict[str, Any]:
    unresolved: list[str] = []
    incompatible: list[str] = []
    non_spdx_named: set[str] = set()
    for component in payload.get("components", []):
        if ignore_aggregate_os and component.get("type") == "operating-system":
            continue
        ref = component.get("purl") or component.get("bom-ref") or component.get("name")
        declarations = license_declarations(component)
        if not declarations:
            unresolved.append(str(ref))
            continue
        for choice in component.get("licenses", []):
            expression = choice.get("expression")
            license_data = choice.get("license", {})
            declaration = (
                expression or license_data.get("id") or license_data.get("name")
            )
            if not declaration:
                continue
            if declaration.upper() in FORBIDDEN_LICENSE_MARKERS:
                incompatible.append(f"{ref}: {declaration}")
            if license_data.get("name") and not license_data.get("id"):
                non_spdx_named.add(declaration)
    return {
        "component_count": len(payload.get("components", [])),
        "unresolved_runtime_licenses": sorted(unresolved),
        "incompatible_runtime_licenses": sorted(incompatible),
        "non_spdx_named_declarations": sorted(non_spdx_named),
        "verdict": "PASS" if not unresolved and not incompatible else "BLOCKED",
    }


def data_inventory() -> list[dict[str, Any]]:
    return [
        {
            "scope": "team-authored schemas, normalization/comparison/security cases and annotations",
            "license": "Apache-2.0",
            "status": "licensed",
            "source": "LICENSE and datasets/PROVENANCE.md",
        },
        {
            "scope": "BDDK registry facts, bank names, canonical URLs and live coverage observations",
            "license": None,
            "status": "third-party-rights-retained",
            "source": "https://www.bddk.org.tr/Kurulus/Liste/77",
        },
        {
            "scope": "short bank-site evidence excerpts in derived, gold and demo datasets",
            "license": None,
            "status": "third-party-rights-retained-not-relicensed",
            "source": "datasets/PROVENANCE.md and per-record source_url fields",
        },
        {
            "scope": "full fetched HTML and cleaned page text",
            "license": None,
            "status": "private-runtime-only-not-distributed",
            "source": "docs/legal/data-boundary.md",
        },
    ]


def write_checksums() -> None:
    checksum_path = LICENSE_DIR / "SHA256SUMS"
    candidates = sorted(
        [*SBOM_DIR.glob("*.json"), *LICENSE_DIR.glob("*.json")],
        key=lambda path: path.relative_to(REPO_ROOT).as_posix(),
    )
    lines = [
        f"{sha256_file(path)}  {path.relative_to(REPO_ROOT).as_posix()}"
        for path in candidates
    ]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-metadata", action="store_true")
    parser.add_argument("--image", help="local OCI image reference to scan")
    parser.add_argument(
        "--syft-executable", type=Path, help="path to pinned Syft 1.48.0"
    )
    parser.add_argument("--require-image", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.require_image and not args.image:
        raise InventoryError("--require-image requires --image")
    if args.image and not args.syft_executable:
        raise InventoryError("--image requires --syft-executable")

    SBOM_DIR.mkdir(parents=True, exist_ok=True)
    LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    spdx_ids = load_spdx_ids(refresh=args.refresh_metadata)
    packages, runtime_names = runtime_python_packages()
    overrides = load_python_overrides()
    pypi_metadata = python_metadata(
        packages, runtime_names, refresh=args.refresh_metadata
    )

    backend_bom = generate_backend_bom(
        packages, runtime_names, pypi_metadata, overrides, spdx_ids
    )
    frontend_bom, npm_cache = generate_frontend_bom(spdx_ids)
    model_bom = generate_model_bom()
    write_json(SBOM_DIR / "backend.cdx.json", backend_bom)
    write_json(SBOM_DIR / "frontend.cdx.json", frontend_bom)
    write_json(SBOM_DIR / "model.cdx.json", model_bom)
    write_json(NPM_CACHE_PATH, npm_cache)

    oci_status: dict[str, Any]
    image_identity_data: dict[str, str] | None = None
    if args.image:
        oci_bom, image_identity_data = generate_oci_bom(
            args.image,
            args.syft_executable.resolve(),
            pypi_metadata,
            overrides,
            spdx_ids,
        )
        write_json(SBOM_DIR / "oci.cdx.json", oci_bom)
        oci_status = bom_status(oci_bom, ignore_aggregate_os=True)
        oci_status["artifact"] = "artifacts/sbom/oci.cdx.json"
        oci_status["image"] = image_identity_data
        oci_status["policy_basis"] = (
            "Debian main package declarations plus exact Python metadata; the aggregate OS "
            "component intentionally has no single license."
        )
    else:
        oci_status = {
            "verdict": "NOT_GENERATED",
            "artifact": None,
            "unresolved_runtime_licenses": [],
            "incompatible_runtime_licenses": [],
        }

    inventory = {
        "schema_version": "1.0",
        "policy": {
            "fail_closed_on_missing_or_forbidden_runtime_license": True,
            "forbidden_markers": sorted(FORBIDDEN_LICENSE_MARKERS),
            "legal_advice": False,
        },
        "inputs": {
            "backend_uv_lock_sha256": sha256_file(REPO_ROOT / "backend" / "uv.lock"),
            "frontend_pnpm_lock_sha256": sha256_file(
                REPO_ROOT / "web" / "pnpm-lock.yaml"
            ),
            "model_manifest_sha256": MODEL_DIGEST,
            **({"oci": image_identity_data} if image_identity_data else {}),
        },
        "inventories": {
            "backend": {
                **bom_status(backend_bom),
                "artifact": "artifacts/sbom/backend.cdx.json",
            },
            "frontend": {
                **bom_status(frontend_bom),
                "artifact": "artifacts/sbom/frontend.cdx.json",
            },
            "model": {
                "name": MODEL_NAME,
                "digest": MODEL_DIGEST,
                "license": "Apache-2.0",
                "verdict": "PASS",
                "artifact": "artifacts/sbom/model.cdx.json",
                "official_sources": [
                    "https://ollama.com/library/qwen3.5:4b",
                    "https://huggingface.co/Qwen/Qwen3.5-4B",
                ],
            },
            "data": {
                "verdict": "PASS_WITH_RIGHTS_BOUNDARY",
                "items": data_inventory(),
            },
            "oci": oci_status,
        },
    }
    blocking = []
    for name in ("backend", "frontend", "oci"):
        verdict = inventory["inventories"][name]["verdict"]
        if verdict == "BLOCKED" or (args.require_image and verdict == "NOT_GENERATED"):
            blocking.append(name)
    inventory["release_license_gate"] = {
        "verdict": "PASS" if not blocking else "BLOCKED",
        "blocking_inventories": blocking,
        "note": "PASS is an inventory/policy result, not legal advice or an obligation waiver.",
    }
    write_json(INVENTORY_PATH, inventory)
    write_checksums()
    return 0 if not blocking else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InventoryError as error:
        print(f"LICENSE_INVENTORY_BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
