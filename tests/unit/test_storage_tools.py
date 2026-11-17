"""Negative and recovery tests for the controlled-volume tools.

Real ENOSPC needs a disposable NTFS volume, which this host does not have.  The
protection logic is therefore tested with *injected volume metadata* and tiny
temporary files, exactly as the implementation document allows: the OS seam
(``storage_acceptance.VOLUME_READER``) reports a fake controlled volume, while
the system and project volumes are still refused by the same code path.

What is proven here: a direct write cannot bypass the guard, ownership and
budget rules hold, records stay off the controlled volume, cleanup only removes
this run's files, and manifest recovery works after a hard exit.  What is *not*
proven here: a real business write hitting a real full volume (DISK01—05).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import storage_acceptance as sa  # noqa: E402
import storage_scenarios as ss  # noqa: E402

GIB = 1024 ** 3


class FakeVolumes:
    """Maps a sandbox directory onto a fake controlled NTFS volume.

    System and project volumes keep their real identity, so a fake reader can
    never make ``C:`` or the repository volume writable.
    """

    def __init__(self, sandbox: str, *, serial: str = "1234ABCD",
                 initial_free: int = 12 * 1024 * 1024, total: int = 8 * GIB) -> None:
        self.sandbox = os.path.normcase(os.path.abspath(sandbox))
        self.serial = serial
        self.initial_free = initial_free
        self.total = total

    def _used(self) -> int:
        used = 0
        for dirpath, _dirnames, filenames in os.walk(self.sandbox):
            for name in filenames:
                try:
                    used += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        return used

    def __call__(self, path: str) -> dict:
        resolved = os.path.normcase(os.path.abspath(path))
        if resolved == self.sandbox or resolved.startswith(self.sandbox + os.sep):
            used = self._used()
            return {
                "path": os.path.abspath(path),
                "final_path": os.path.abspath(path),
                "volume_root": "E:\\",
                "volume_label": "CONTROLLED",
                "filesystem": "NTFS",
                "serial": self.serial,
                "free_bytes": max(0, self.initial_free - used),
                "total_bytes": self.total,
                "admin": False,
                "probe_path": os.path.abspath(path),
            }
        drive, _tail = os.path.splitdrive(os.path.abspath(path))
        root = drive + os.sep
        system = os.path.normcase(os.path.abspath(os.environ.get("SystemDrive", "C:") + os.sep))
        project = os.path.normcase(os.path.abspath(ROOT))
        if resolved.startswith(project):
            return {"volume_root": "D:\\", "filesystem": "NTFS", "serial": "PROJECT0",
                    "free_bytes": 400 * GIB, "total_bytes": 900 * GIB, "admin": False,
                    "final_path": os.path.abspath(path)}
        if os.path.normcase(root) == system:
            return {"volume_root": "C:\\", "filesystem": "NTFS", "serial": "SYSTEM00",
                    "free_bytes": 390 * GIB, "total_bytes": 500 * GIB, "admin": False,
                    "final_path": os.path.abspath(path)}
        return {"volume_root": "D:\\", "filesystem": "NTFS", "serial": "PROJECT0",
                "free_bytes": 688 * GIB, "total_bytes": 1000 * GIB, "admin": False,
                "final_path": os.path.abspath(path)}


class StorageToolCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.mkdtemp(prefix="gm-storage-tools-")
        self.addCleanup(lambda: shutil.rmtree(self.temp, ignore_errors=True))
        self.sandbox = os.path.join(self.temp, "volume")
        os.makedirs(self.sandbox, exist_ok=True)
        self.evidence = os.path.join(self.temp, "evidence")
        os.makedirs(self.evidence, exist_ok=True)
        self.fake = FakeVolumes(self.sandbox)
        self.addCleanup(self._restore_reader)
        sa.VOLUME_READER = self.fake
        self.registry = os.path.join(self.evidence, "controlled-volumes.json")
        self._write_registry()

    def _restore_reader(self) -> None:
        sa.VOLUME_READER = sa.volume_info

    def _write_registry(self, serial: str = "1234ABCD", limit: int = 8 * GIB,
                        root: str = "E:\\") -> None:
        with open(self.registry, "w", encoding="utf-8") as handle:
            json.dump({
                "schema_version": 1,
                "controlled_volumes": [{
                    "volume_root": root,
                    "serial": serial,
                    "capacity_limit_bytes": limit,
                    "path_prefix": self.sandbox,
                    "registered_at": "test",
                }],
            }, handle)

    def fill(self, *, serial: str = "1234ABCD", headroom: int = 4 * 1024 * 1024,
             max_bytes: int | None = None, timeout: float = 30.0,
             run_name: str = "run-1", path: str | None = None,
             report: str | None = None, log_path: str | None = None,
             stop_file: str | None = None):
        path = path or self.sandbox
        report = report or os.path.join(self.evidence, f"FILL-{run_name}.json")
        return sa.fill(
            path, os.path.join(path, run_name), serial, headroom, report,
            max_bytes=max_bytes, timeout=timeout, registry=self.registry,
            log_path=log_path, stop_file=stop_file, run_id=run_name,
        )


class WriteGuardTests(StorageToolCase):
    def test_direct_fill_refuses_system_volume(self):
        result = self.fill(path=os.environ.get("TEMP", self.temp))
        self.assertEqual(result["result"], "REFUSED")
        self.assertIn("系统卷", result["errors"][0])
        self.assertEqual(result["filler_files"], [])
        self.assertFalse(os.path.exists(os.path.join(
            os.environ.get("TEMP", self.temp), "run-1"
        )))

    def test_direct_fill_refuses_project_volume(self):
        result = self.fill(path=ROOT)
        self.assertEqual(result["result"], "REFUSED")
        self.assertIn("项目卷", result["errors"][0])

    def test_unregistered_volume_is_refused(self):
        os.unlink(self.registry)
        result = self.fill()
        self.assertEqual(result["result"], "REFUSED")
        self.assertEqual(result["refusal"]["code"], "E_REGISTRY")

    def test_volume_not_in_registry_is_refused(self):
        self._write_registry(root="F:\\")
        result = self.fill()
        self.assertEqual(result["result"], "REFUSED")
        self.assertEqual(result["refusal"]["code"], "E_UNREGISTERED_VOLUME")

    def test_serial_mismatch_is_refused(self):
        result = self.fill(serial="00000000")
        self.assertEqual(result["result"], "REFUSED")
        self.assertEqual(result["refusal"]["code"], "E_SERIAL")

    def test_registry_serial_mismatch_is_refused(self):
        self._write_registry(serial="DEADBEEF")
        result = self.fill()
        self.assertEqual(result["result"], "REFUSED")
        self.assertEqual(result["refusal"]["code"], "E_SERIAL")

    def test_capacity_above_registered_limit_is_refused(self):
        self._write_registry(limit=1 * GIB)
        result = self.fill()
        self.assertEqual(result["result"], "REFUSED")
        self.assertEqual(result["refusal"]["code"], "E_CAPACITY")

    def test_run_dir_outside_target_is_refused(self):
        outside = os.path.join(self.temp, "outside")
        os.makedirs(outside, exist_ok=True)
        result = sa.fill(
            self.sandbox, outside, "1234ABCD", 1024,
            os.path.join(self.evidence, "FILL-outside.json"),
            max_bytes=1024, timeout=5, registry=self.registry,
        )
        self.assertEqual(result["result"], "REFUSED")
        self.assertIn(result["refusal"]["code"], ("E_RUN_DIR", "E_UNREGISTERED_VOLUME"))

    def test_existing_run_dir_is_refused(self):
        os.makedirs(os.path.join(self.sandbox, "run-1"))
        with open(os.path.join(self.sandbox, "run-1", "keep.txt"), "w", encoding="utf-8") as handle:
            handle.write("someone else's data")
        result = self.fill()
        self.assertEqual(result["result"], "REFUSED")
        self.assertEqual(result["refusal"]["code"], "E_RUN_DIR_EXISTS")
        self.assertTrue(os.path.isfile(os.path.join(self.sandbox, "run-1", "keep.txt")))

    def test_negative_headroom_is_refused(self):
        result = self.fill(headroom=-1)
        self.assertEqual(result["result"], "REFUSED")
        self.assertEqual(result["refusal"]["code"], "E_ARGUMENT")

    def test_non_positive_budget_is_refused(self):
        for kwargs in ({"max_bytes": 0}, {"timeout": 0}, {"max_bytes": -5}):
            with self.subTest(**kwargs):
                result = self.fill(run_name=f"bad-{abs(hash(str(kwargs))) % 1000}", **kwargs)
                self.assertEqual(result["result"], "REFUSED")
                self.assertEqual(result["refusal"]["code"], "E_ARGUMENT")

    def test_report_inside_controlled_volume_is_refused(self):
        report = os.path.join(self.sandbox, "FILL.json")
        result = self.fill(report=report)
        self.assertEqual(result["result"], "REFUSED")
        self.assertEqual(result["refusal"]["code"], "E_RECORD_ON_VOLUME")

    def test_unwritable_log_is_refused(self):
        log_dir = os.path.join(self.evidence, "log-dir")
        os.makedirs(log_dir, exist_ok=True)
        result = self.fill(log_path=log_dir)
        self.assertEqual(result["result"], "REFUSED")
        self.assertEqual(result["refusal"]["code"], "E_LOG")

    def test_fill_writes_records_outside_the_volume(self):
        result = self.fill(max_bytes=2 * 1024 * 1024)
        self.assertIn(result["result"], ("FILLED", "PARTIAL"))
        self.assertTrue(os.path.isfile(result["filler_manifest"]))
        self.assertTrue(result["filler_manifest"].startswith(self.evidence))
        self.assertTrue(result["filler_files"])
        self.assertTrue(result["filler_files"][0]["path"].startswith(self.sandbox))
        self.assertEqual(result["ownership"]["volume_serial"], "1234ABCD")
        marker = os.path.join(result["final_run_dir"], sa.OWNER_MARKER)
        self.assertTrue(os.path.isfile(marker))

    def test_stop_signal_ends_the_fill(self):
        stop = os.path.join(self.evidence, "STOP")
        with open(stop, "w", encoding="utf-8") as handle:
            handle.write("stop")
        # A pre-existing stop file is honoured before the first chunk.
        result = self.fill(run_name="run-stop", stop_file=stop)
        self.assertEqual(result["stop_reason"], "stop_signal")
        self.assertEqual(result["written_bytes"], 0)
        self.assertEqual(result["filler_files"], [])
        os.unlink(stop)
        result = self.fill(run_name="run-normal", max_bytes=4 * 1024 * 1024)
        self.assertEqual(result["result"], "PARTIAL")
        self.assertGreater(result["written_bytes"], 0)


class CleanupTests(StorageToolCase):
    def test_cleanup_removes_only_registered_fillers(self):
        result = self.fill(max_bytes=2 * 1024 * 1024)
        foreign = os.path.join(result["final_run_dir"], "filler-99999-deadbeef.bin")
        with open(foreign, "wb") as handle:
            handle.write(b"not ours")
        cleanup = sa.cleanup(report=os.path.join(self.evidence, "FILL-run-1.json"),
                            registry=self.registry)
        self.assertEqual(cleanup["result"], "PARTIAL")
        self.assertTrue(os.path.isfile(foreign), "外来文件不得被删除")
        self.assertEqual(cleanup["removed"], [item["path"] for item in result["filler_files"]])
        self.assertTrue(cleanup["leftovers"])

    def test_cleanup_after_volume_swap_is_refused(self):
        result = self.fill(max_bytes=1024 * 1024)
        fake = FakeVolumes(self.sandbox, serial="FFFF0000")
        sa.VOLUME_READER = fake
        cleanup = sa.cleanup(report=os.path.join(self.evidence, "FILL-run-1.json"),
                            registry=self.registry)
        self.assertEqual(cleanup["result"], "REFUSED")
        self.assertEqual(cleanup["refusal"]["code"], "E_SERIAL")
        self.assertTrue(os.path.exists(result["filler_files"][0]["path"]))

    def test_cleanup_refuses_when_ownership_marker_is_missing(self):
        result = self.fill(max_bytes=1024 * 1024)
        os.unlink(os.path.join(result["final_run_dir"], sa.OWNER_MARKER))
        cleanup = sa.cleanup(report=os.path.join(self.evidence, "FILL-run-1.json"),
                            registry=self.registry)
        self.assertEqual(cleanup["result"], "REFUSED")
        self.assertEqual(cleanup["refusal"]["code"], "E_OWNERSHIP")
        self.assertTrue(os.path.exists(result["filler_files"][0]["path"]))

    def test_cleanup_skips_size_mismatch(self):
        result = self.fill(max_bytes=1024 * 1024)
        filler = result["filler_files"][0]["path"]
        with open(filler, "ab") as handle:
            handle.write(b"x")
        cleanup = sa.cleanup(report=os.path.join(self.evidence, "FILL-run-1.json"),
                            registry=self.registry)
        self.assertTrue(any("size differs" in item["reason"] for item in cleanup["skipped"]))
        self.assertTrue(os.path.exists(filler))

    def test_cleanup_from_manifest_after_hard_exit(self):
        result = self.fill(max_bytes=2 * 1024 * 1024)
        manifest = result["filler_manifest"]
        # Simulate a hard exit: no report is passed, only the manifest remains.
        cleanup = sa.cleanup(manifest=manifest, registry=self.registry,
                            outer_report=os.path.join(self.evidence, "RECOVER.json"))
        self.assertEqual(cleanup["result"], "CLEANED")
        self.assertEqual(cleanup["removed"], [item["path"] for item in result["filler_files"]])
        self.assertFalse(os.path.exists(result["final_run_dir"]))
        with open(os.path.join(self.evidence, "RECOVER.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["result"], "CLEANED")

    def test_cleanup_without_identity_is_refused(self):
        result = self.fill(max_bytes=1024 * 1024)
        manifest = result["filler_manifest"]
        with open(manifest, encoding="utf-8") as handle:
            document = json.load(handle)
        document.pop("volume_serial")
        with open(manifest, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        cleanup = sa.cleanup(manifest=manifest, registry=self.registry,
                            outer_report=os.path.join(self.evidence, "RECOVER2.json"))
        self.assertEqual(cleanup["result"], "REFUSED")
        self.assertEqual(cleanup["refusal"]["code"], "E_MANIFEST")


class RegistryAndReserveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.mkdtemp(prefix="gm-storage-reg-")
        self.addCleanup(lambda: shutil.rmtree(self.temp, ignore_errors=True))

    def test_register_refuses_system_volume(self):
        registry = os.path.join(self.temp, "registry.json")
        with self.assertRaises(sa.StorageRefusal) as raised:
            sa.register(os.environ.get("TEMP", self.temp), "00000000", registry, 8 * GIB)
        self.assertEqual(raised.exception.code, "E_SYSTEM_VOLUME")
        self.assertFalse(os.path.exists(registry))

    def test_register_refuses_negative_capacity(self):
        with self.assertRaises(sa.StorageRefusal) as raised:
            sa.register(self.temp, "00000000", os.path.join(self.temp, "r.json"), 0)
        self.assertEqual(raised.exception.code, "E_ARGUMENT")

    def test_reserve_set_and_restore(self):
        config = os.path.join(self.temp, "config.json")
        with open(config, "w", encoding="utf-8") as handle:
            json.dump({"disk_reserve": 536870912, "other": 1}, handle)
        record_path = os.path.join(self.temp, "reserve.json")
        record = sa.reserve_set(config, 0, record_path)
        self.assertEqual(record["before_value"], 536870912)
        self.assertEqual(record["test_value"], 0)
        with open(config, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["disk_reserve"], 0)
        restored = sa.reserve_restore(record_path)
        self.assertTrue(restored["restored"])
        with open(config, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["disk_reserve"], 536870912)
        self.assertEqual(document["other"], 1)

    def test_reserve_negative_is_refused(self):
        config = os.path.join(self.temp, "config.json")
        with open(config, "w", encoding="utf-8") as handle:
            json.dump({"disk_reserve": 1}, handle)
        with self.assertRaises(sa.StorageRefusal):
            sa.reserve_set(config, -1, os.path.join(self.temp, "r.json"))


class ScenarioDefinitionTests(unittest.TestCase):
    def test_matrix_covers_all_five_locations(self):
        scenarios = ss.load_scenarios()["scenarios"]
        self.assertEqual(set(scenarios), {"DISK01", "DISK02", "DISK03", "DISK04", "DISK05"})
        for name, scenario in scenarios.items():
            with self.subTest(scenario=name):
                self.assertGreaterEqual(len(scenario["stages"]), 2)
                self.assertTrue(scenario["invariants"])
                self.assertTrue(any(stage.get("primitive") for stage in scenario["stages"]))

    def test_every_scenario_has_a_selftest_fault(self):
        scenarios = ss.load_scenarios()["scenarios"]
        for name, scenario in scenarios.items():
            with self.subTest(scenario=name):
                self.assertTrue(any(stage.get("selftest_fault") for stage in scenario["stages"]))

    def test_invalid_stage_is_rejected(self):
        temp = tempfile.mkdtemp(prefix="gm-scenarios-")
        self.addCleanup(lambda: shutil.rmtree(temp, ignore_errors=True))
        path = os.path.join(temp, "bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "scenarios": {"DISK99": {"stages": [{"id": "x", "op": "nope"}]}}}, handle)
        with self.assertRaises(ValueError):
            ss.load_scenarios(path)


class HardKillRecoveryTests(StorageToolCase):
    def test_manifest_recovery_handles_partial_filler(self):
        """A filler registered before its first byte must still be removable."""
        run_dir = os.path.join(self.sandbox, "crash-run")
        os.makedirs(run_dir)
        nonce = "a" * 32
        marker = {
            "schema_version": 1, "run_id": "crash", "pid": 1, "nonce": nonce,
            "created_at": "test", "final_run_dir": run_dir, "volume_serial": "1234ABCD",
        }
        with open(os.path.join(run_dir, sa.OWNER_MARKER), "w", encoding="utf-8") as handle:
            json.dump(marker, handle)
        filler = os.path.join(run_dir, "filler-00000-aaaaaaaa.bin")
        # A half-written filler is shorter than its planned size.
        with open(filler, "wb") as handle:
            handle.write(b"\0" * 4096)
        manifest = os.path.join(self.evidence, "CRASH.json")
        with open(manifest, "w", encoding="utf-8") as handle:
            json.dump({
                "schema_version": 1, "run_id": "crash", "path": self.sandbox,
                "final_path": self.sandbox, "run_dir": run_dir, "final_run_dir": run_dir,
                "volume_serial": "1234ABCD", "ownership": marker,
                "files": [{"path": filler, "planned_bytes": 65536, "written_bytes": 4096}],
            }, handle)
        cleanup = sa.cleanup(manifest=manifest, registry=self.registry,
                            outer_report=os.path.join(self.evidence, "CRASH-CLEAN.json"))
        self.assertEqual(cleanup["result"], "CLEANED", cleanup["errors"])
        self.assertFalse(os.path.exists(filler))
        self.assertFalse(os.path.exists(run_dir))

    def test_incomplete_manifest_is_refused(self):
        manifest = os.path.join(self.evidence, "BAD.json")
        with open(manifest, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "run_dir": self.sandbox}, handle)
        cleanup = sa.cleanup(manifest=manifest, registry=self.registry,
                            outer_report=os.path.join(self.evidence, "BAD-CLEAN.json"))
        self.assertEqual(cleanup["result"], "REFUSED")
        self.assertEqual(cleanup["refusal"]["code"], "E_MANIFEST")


class JunctionTests(StorageToolCase):
    """Real Windows junctions, not lexical ``..`` paths.

    ``mklink /J`` works without elevation, so this exercises the
    ``GetFinalPathNameByHandleW`` resolution for both the write entry point and
    cleanup.  A pre-seeded filler with the *exact* name is infeasible by design
    (the name carries a random nonce); ``'xb'`` is the guard, and the reused
    run-directory test above covers the ownership side.
    """

    def _junction(self, link: str, target: str) -> None:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, target],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            self.skipTest(f"mklink /J unavailable: {result.stdout} {result.stderr}")

    def test_run_dir_through_junction_outside_target_is_refused(self):
        outside = os.path.join(self.temp, "outside")
        os.makedirs(outside)
        link = os.path.join(self.sandbox, "redirect")
        self._junction(link, outside)
        result = sa.fill(
            self.sandbox, os.path.join(link, "run-outside"), "1234ABCD",
            1024, os.path.join(self.evidence, "FILL-junction.json"),
            max_bytes=1024, timeout=5, registry=self.registry,
        )
        self.assertEqual(result["result"], "REFUSED")
        self.assertEqual(result["refusal"]["code"], "E_RUN_DIR")
        self.assertFalse(os.path.exists(os.path.join(outside, "run-outside")))

    def test_cleanup_refuses_run_dir_swapped_for_junction(self):
        result = self.fill(max_bytes=1024 * 1024)
        run_dir = result["final_run_dir"]
        decoy = os.path.join(self.sandbox, "decoy")
        os.makedirs(decoy)
        decoy_file = os.path.join(decoy, "do-not-delete.txt")
        with open(decoy_file, "w", encoding="utf-8") as handle:
            handle.write("decoy")
        swapped = run_dir + "-real"
        os.rename(run_dir, swapped)
        self._junction(run_dir, decoy)
        cleanup = sa.cleanup(
            report=os.path.join(self.evidence, "FILL-run-1.json"), registry=self.registry,
        )
        self.assertEqual(cleanup["result"], "REFUSED")
        self.assertIn(cleanup["refusal"]["code"], ("E_PATH_CHANGED", "E_OWNERSHIP", "E_RUN_DIR"))
        self.assertTrue(os.path.isfile(decoy_file), "junction 目标内容不得被删除")


class ScenarioProtocolTests(unittest.TestCase):
    """The whole barrier protocol, all five target locations, simulated only."""

    def test_full_matrix_selftest(self):
        root = tempfile.mkdtemp(prefix="gm-selftest-matrix-")
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        report = os.path.join(root, "SELFTEST.json")
        code = ss.main([
            "selftest", "--scenario", "all", "--report", report,
            "--work-root", os.path.join(root, "cases"), "--barrier-timeout", "15",
        ])
        self.assertEqual(code, 0)
        with open(report, encoding="utf-8") as handle:
            summary = json.load(handle)
        self.assertEqual(summary["result"], "PASS")
        self.assertTrue(summary["simulated"], "自检必须明确标记 simulated")
        self.assertIn("不能作为 DISK 真实 ENOSPC 证据", summary["note"])
        cases = {(item["scenario"], item["mode"]): item for item in summary["cases"]}
        for name in ("DISK01", "DISK02", "DISK03", "DISK04", "DISK05"):
            self.assertEqual(cases[(name, "normal")]["result"], "PASS", name)
            self.assertEqual(cases[(name, "refused")]["result"], "PASS", name)
        self.assertEqual(cases[("DISK02", "timeout")]["result"], "PASS")
        self.assertEqual(cases[("DISK02", "selftest")]["result"], "PASS")


if __name__ == "__main__":
    unittest.main()
