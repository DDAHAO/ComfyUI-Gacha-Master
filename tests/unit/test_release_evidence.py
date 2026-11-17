"""Tests for tools/check_release_evidence.py.

The checker exists because evidence that points at the wrong file, or a claim
that outruns its candidate, is indistinguishable from a real pass unless the
reference and the identity are machine-checked.  Each test mutates one field of
a valid fixture and asserts the specific refusal.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import candidate_manifest as cm  # noqa: E402
import check_release_evidence as ce  # noqa: E402


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class EvidenceFixture:
    """A miniature repository with one frozen candidate and one summary."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="gm-evidence-")
        self.repo = os.path.join(self.root, "repo")
        write(os.path.join(self.repo, "__init__.py"), "VALUE = 1\n")
        write(os.path.join(self.repo, "comfyui_gacha_master", "backend", "x.py"), "X = 1\n")
        write(os.path.join(self.repo, "comfyui_gacha_master", "web", "a.js"), "export const a = 1;\n")
        write(os.path.join(self.repo, "tools", "t.py"), "T = 1\n")
        write(os.path.join(self.repo, "tests", "unit", "test_x.py"), "X = 1\n")
        write(os.path.join(self.repo, "docs", "d.md"), "# d\n")
        write(os.path.join(self.repo, "README.md"), "# readme\n")
        self.run_dir = os.path.join(self.repo, "test-artifacts", "run-1")
        self.snapshot = os.path.join(self.run_dir, "candidate")
        self.snapshot_doc = cm.write_snapshot(self.repo, self.snapshot)
        cm.verify_snapshot(self.snapshot)
        self.report = os.path.join(self.run_dir, "FINAL-REPORT.md")
        write(self.report, "# report\n\n- [SUMMARY.json](SUMMARY.json)\n")
        self.summary_path = os.path.join(self.run_dir, "SUMMARY.json")

    def summary(self) -> dict:
        scopes = {
            name: {
                "file_count": scope["file_count"],
                "tree_sha256": scope["tree_sha256"],
            }
            for name, scope in self.snapshot_doc["scopes"].items()
        }
        return {
            "schema_version": 1,
            "path_base": "repo",
            "overall_result": "PARTIAL",
            "production_ready": False,
            "candidate": {
                "manifest": "test-artifacts/run-1/candidate/MANIFEST.json",
                "snapshot": "test-artifacts/run-1/candidate/SNAPSHOT.json",
                "manifest_fingerprint": self.snapshot_doc["manifest_fingerprint"],
                "scopes": scopes,
                "archive": {
                    "path": "test-artifacts/run-1/candidate/candidate-delivery.zip",
                    "sha256": self.snapshot_doc["archive"]["sha256"],
                    "file_count": self.snapshot_doc["archive"]["file_count"],
                },
            },
            "regression": {
                "result": "PASS",
                "python_tests": 12,
                "python_skipped": 1,
                "python_failures": 0,
                "python_errors": 0,
                "js_tests": 5,
                "js_fail": 0,
                "log": "test-artifacts/run-1/candidate",
            },
            "work_packages": {
                "PU01": {"result": "PASS"},
                "PU02": {"result": "PASS"},
                "PU03": {"result": "NOT_RUN"},
            },
            "gates": {name: "PARTIAL" for name in ce.REQUIRED_GATES},
            "cases": {
                "DISK01": {
                    "id": "DISK01",
                    "result": "NOT_RUN",
                    "data_source": "real",
                    "evidence": ["test-artifacts/run-1/FINAL-REPORT.md"],
                }
            },
            "data_provenance": {"real_gpu": ["1x1"], "fixture": ["5000 sources"]},
            "blocking_items": [{"item": "DISK01", "reason": "no controlled volume"}],
            "report": "test-artifacts/run-1/FINAL-REPORT.md",
        }

    def check(self, summary: dict | None = None) -> dict:
        payload = summary if summary is not None else self.summary()
        write(self.summary_path, json.dumps(payload, ensure_ascii=False, indent=2))
        return ce.check_summary(self.repo, self.summary_path)

    def codes(self, result: dict) -> set[str]:
        return {error["code"] for error in result["errors"]}

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = EvidenceFixture()
        self.addCleanup(self.fixture.cleanup)

    def mutate(self) -> dict:
        return copy.deepcopy(self.fixture.summary())

    def test_valid_partial_summary_passes(self):
        result = self.fixture.check()
        self.assertEqual(result["result"], "PASS", result["errors"])
        self.assertEqual(result["path_base"], "repo")

    def test_broken_markdown_link_is_refused(self):
        write(
            self.fixture.report,
            "# report\n\n- [cleanup](cleanup.json)\n- [summary](SUMMARY.json)\n",
        )
        result = self.fixture.check()
        self.assertEqual(result["result"], "FAIL")
        self.assertIn("E_MARKDOWN", self.fixture.codes(result))
        self.assertTrue(any("cleanup.json" in error.get("link", "") for error in result["errors"]))

    def test_missing_evidence_path_is_refused(self):
        summary = self.mutate()
        summary["cases"]["DISK01"]["evidence"] = ["test-artifacts/run-1/nope.json"]
        result = self.fixture.check(summary)
        self.assertIn("E_PATH", self.fixture.codes(result))

    def test_path_escaping_repository_is_refused(self):
        summary = self.mutate()
        summary["cases"]["DISK01"]["evidence"] = ["../../etc/passwd"]
        result = self.fixture.check(summary)
        self.assertIn("E_PATH_ESCAPE", self.fixture.codes(result))

    def test_mixed_path_base_is_refused(self):
        summary = self.mutate()
        summary["evidence_root"] = "test-artifacts/run-1"
        result = self.fixture.check(summary)
        self.assertIn("E_PATH_BASE", self.fixture.codes(result))

    def test_unknown_evidence_root_needs_declaration(self):
        summary = self.mutate()
        summary["path_base"] = "evidence_root"
        result = self.fixture.check(summary)
        self.assertIn("E_PATH_BASE", self.fixture.codes(result))

    def test_evidence_root_base_resolves_relative_to_evidence(self):
        summary = self.mutate()
        summary["path_base"] = "evidence_root"
        summary["evidence_root"] = "test-artifacts/run-1"
        summary["report"] = "FINAL-REPORT.md"
        summary["candidate"]["manifest"] = "candidate/MANIFEST.json"
        summary["candidate"]["snapshot"] = "candidate/SNAPSHOT.json"
        summary["candidate"]["archive"]["path"] = "candidate/candidate-delivery.zip"
        summary["regression"]["log"] = "candidate"
        summary["cases"]["DISK01"]["evidence"] = ["FINAL-REPORT.md"]
        result = self.fixture.check(summary)
        self.assertEqual(result["result"], "PASS", result["errors"])

    def test_invalid_status_token_is_refused(self):
        summary = self.mutate()
        summary["work_packages"]["PU02"]["result"] = "OK"
        result = self.fixture.check(summary)
        self.assertIn("E_STATUS", self.fixture.codes(result))

    def test_tampered_archive_is_refused(self):
        archive = os.path.join(
            self.fixture.repo, "test-artifacts", "run-1", "candidate", "candidate-delivery.zip"
        )
        with open(archive, "ab") as handle:
            handle.write(b"tamper")
        result = self.fixture.check()
        self.assertIn("E_ARCHIVE", self.fixture.codes(result))

    def test_tampered_manifest_fingerprint_is_refused(self):
        manifest_path = os.path.join(self.fixture.snapshot, "MANIFEST.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest["fingerprint"] = "0" * 64
        write(manifest_path, json.dumps(manifest, indent=2))
        result = self.fixture.check()
        self.assertIn("E_CANDIDATE", self.fixture.codes(result))

    def test_scope_count_mismatch_is_refused(self):
        summary = self.mutate()
        summary["candidate"]["scopes"]["runtime"]["file_count"] += 1
        result = self.fixture.check(summary)
        self.assertIn("E_CANDIDATE", self.fixture.codes(result))

    def test_zero_tests_is_refused(self):
        summary = self.mutate()
        summary["regression"]["python_tests"] = 0
        result = self.fixture.check(summary)
        self.assertIn("E_REGRESSION", self.fixture.codes(result))

    def test_failures_cannot_claim_pass(self):
        summary = self.mutate()
        summary["overall_result"] = "PASS"
        summary["regression"]["js_fail"] = 2
        result = self.fixture.check(summary)
        self.assertIn("E_REGRESSION", self.fixture.codes(result))
        self.assertIn("E_GATE", self.fixture.codes(result))

    def test_production_ready_requires_every_gate(self):
        summary = self.mutate()
        summary["overall_result"] = "PASS"
        summary["production_ready"] = True
        summary["gates"] = {name: "PASS" for name in ce.REQUIRED_GATES}
        summary["work_packages"] = {"PU01": {"result": "PASS"}}
        summary["blocking_items"] = []
        summary["support_statement"] = {"can_claim": ["x"]}
        summary["target_identity"] = {"os": "Windows 11"}
        ok = self.fixture.check(summary)
        self.assertEqual(ok["result"], "PASS", ok["errors"])
        # One unmet gate collapses the claim.
        summary["gates"]["G06_reliability"] = "PARTIAL"
        bad = self.fixture.check(summary)
        self.assertIn("E_GATE", self.fixture.codes(bad))

    def test_production_ready_with_unfinished_package_is_refused(self):
        summary = self.mutate()
        summary["overall_result"] = "PASS"
        summary["production_ready"] = True
        summary["gates"] = {name: "PASS" for name in ce.REQUIRED_GATES}
        summary["support_statement"] = {"can_claim": ["x"]}
        summary["target_identity"] = {"os": "Windows 11"}
        result = self.fixture.check(summary)
        self.assertIn("E_GATE", self.fixture.codes(result))

    def test_fixture_case_cannot_count_toward_production(self):
        summary = self.mutate()
        summary["cases"]["DISK02"] = {
            "id": "DISK02",
            "result": "PASS",
            "data_source": "fixture",
            "counts_toward_production": True,
            "enospc_observed": True,
            "evidence": ["test-artifacts/run-1/FINAL-REPORT.md"],
        }
        result = self.fixture.check(summary)
        self.assertIn("E_PROVENANCE", self.fixture.codes(result))

    def test_disk_pass_requires_enospc_observation(self):
        summary = self.mutate()
        summary["cases"]["DISK03"] = {
            "id": "DISK03",
            "result": "PASS",
            "data_source": "real",
            "evidence": ["test-artifacts/run-1/FINAL-REPORT.md"],
        }
        result = self.fixture.check(summary)
        self.assertIn("E_PROVENANCE", self.fixture.codes(result))

    def test_real_execution_cannot_cite_fixture(self):
        summary = self.mutate()
        summary["cases"]["HOST14"] = {
            "id": "HOST14",
            "result": "PASS",
            "claims_real_execution": True,
            "data_source": "fixture",
            "evidence": ["test-artifacts/run-1/FINAL-REPORT.md"],
        }
        result = self.fixture.check(summary)
        self.assertIn("E_PROVENANCE", self.fixture.codes(result))

    def test_same_item_as_real_and_fixture_is_refused(self):
        summary = self.mutate()
        summary["data_provenance"]["fixture"].append("1x1")
        result = self.fixture.check(summary)
        self.assertIn("E_PROVENANCE", self.fixture.codes(result))

    def test_missing_source_archive_is_a_warning(self):
        manifest_dir = os.path.join(self.fixture.snapshot)
        snapshot_path = os.path.join(manifest_dir, "SNAPSHOT.json")
        with open(snapshot_path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        snapshot.pop("source_archive", None)
        snapshot.pop("verification", None)
        write(snapshot_path, json.dumps(snapshot, indent=2))
        result = self.fixture.check()
        self.assertIn("W_SOURCE_ARCHIVE", {warning["code"] for warning in result["warnings"]})
        self.assertIn("W_REBUILD", {warning["code"] for warning in result["warnings"]})


if __name__ == "__main__":
    unittest.main()
