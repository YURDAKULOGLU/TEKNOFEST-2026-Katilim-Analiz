from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "generate_sboms.py"
SPEC = importlib.util.spec_from_file_location("generate_sboms", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LicenseExpressionTests(unittest.TestCase):
    def test_accepts_known_spdx_expression(self) -> None:
        MODULE.validate_expression("Apache-2.0 OR MIT", {"Apache-2.0", "MIT"})

    def test_rejects_unknown_identifier(self) -> None:
        with self.assertRaises(MODULE.InventoryError):
            MODULE.validate_expression("MIT OR Mystery-License", {"MIT"})

    def test_rejects_unlicensed_marker(self) -> None:
        with self.assertRaises(MODULE.InventoryError):
            MODULE.validate_expression("UNLICENSED", {"MIT"})


class NormalizationTests(unittest.TestCase):
    def test_scoped_npm_purl_and_opaque_reference_are_stable(self) -> None:
        purl = MODULE.npm_purl("@scope/package", "1.2.3")
        self.assertEqual("pkg:npm/%40scope/package@1.2.3", purl)
        reference = MODULE.deterministic_component_ref("npm", purl)
        self.assertEqual(reference, MODULE.deterministic_component_ref("npm", purl))
        self.assertTrue(reference.startswith("urn:katilim:npm:"))
        self.assertNotIn("%", reference)

    def test_opaque_reference_remap_preserves_purl_and_graph(self) -> None:
        purl = "pkg:deb/debian/example@1%3A2"
        source = {
            "components": [{"bom-ref": purl, "name": "example", "purl": purl}],
            "dependencies": [{"ref": purl, "dependsOn": [purl]}],
        }
        MODULE.use_opaque_component_refs(source, "oci")
        reference = source["components"][0]["bom-ref"]
        self.assertEqual(purl, source["components"][0]["purl"])
        self.assertEqual(reference, source["dependencies"][0]["ref"])
        self.assertEqual([reference], source["dependencies"][0]["dependsOn"])

    def test_normalization_removes_nondeterminism_and_sorts_graph(self) -> None:
        source = {
            "serialNumber": "urn:uuid:random",
            "metadata": {"timestamp": "2026-07-19T00:00:00Z"},
            "components": [
                {
                    "bom-ref": "b",
                    "name": "b",
                    "properties": [{"name": "z", "value": "1"}],
                },
                {"bom-ref": "a", "name": "a"},
            ],
            "dependencies": [
                {"ref": "b", "dependsOn": ["z", "a", "a"]},
                {"ref": "a", "dependsOn": []},
            ],
        }
        normalized = MODULE.normalize_bom(copy.deepcopy(source))
        self.assertNotIn("serialNumber", normalized)
        self.assertNotIn("timestamp", normalized["metadata"])
        self.assertEqual(
            ["a", "b"], [item["bom-ref"] for item in normalized["components"]]
        )
        self.assertEqual(["a", "z"], normalized["dependencies"][1]["dependsOn"])
        first = json.dumps(normalized, sort_keys=True)
        second = json.dumps(
            MODULE.normalize_bom(copy.deepcopy(normalized)), sort_keys=True
        )
        self.assertEqual(first, second)


class PolicyTests(unittest.TestCase):
    def test_named_debian_declaration_is_resolved_but_reported(self) -> None:
        bom = {
            "components": [
                {
                    "bom-ref": "pkg:deb/debian/example@1",
                    "type": "library",
                    "licenses": [{"license": {"name": "custom-debian-license-name"}}],
                }
            ]
        }
        status = MODULE.bom_status(bom)
        self.assertEqual("PASS", status["verdict"])
        self.assertEqual([], status["unresolved_runtime_licenses"])
        self.assertEqual(
            ["custom-debian-license-name"], status["non_spdx_named_declarations"]
        )

    def test_missing_runtime_license_blocks(self) -> None:
        status = MODULE.bom_status(
            {"components": [{"bom-ref": "pkg:pypi/missing@1", "type": "library"}]}
        )
        self.assertEqual("BLOCKED", status["verdict"])


if __name__ == "__main__":
    unittest.main()
