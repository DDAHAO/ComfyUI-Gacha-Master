import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from types import SimpleNamespace

from comfyui_gacha_master.backend.backups import BackupManager
from comfyui_gacha_master.backend.db import Database
from comfyui_gacha_master.backend.errors import GachaError
from comfyui_gacha_master.backend.repository import Repository
from comfyui_gacha_master.backend import schema as schema_module
from comfyui_gacha_master.backend.schema import (
    SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, current_schema_version, schema_checksum,
)
from comfyui_gacha_master.backend.util import sha256_file, utc_now


def make_v1(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_V1)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at, checksum) VALUES (1, '2026-01-01T00:00:00Z', ?)",
            (schema_checksum(1),),
        )
        conn.execute(
            """
            INSERT INTO jobs(
                id, display_id, display_name, status, run_intent, protection_mode,
                workflow_ui_path, workflow_api_path, workflow_ui_sha256, workflow_api_sha256,
                bindings_json, warnings_json, plan_version, row_version, recovery_version,
                data_root, results_root, job_dir, result_dir, created_at, updated_at
            ) VALUES ('job-1','JOB-1','legacy','COMPLETED','STOPPED','snapshot',
                '/w/ui.json','/w/api.json','h1','h2','{}','[]',1,3,0,
                '/data','/results','/data/jobs/job-1','/results/JOB-1',
                '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO sources(
                id, job_id, source_order, original_name, size, sha256, width, height,
                image_format, status, result_revision, selection_version, created_at
            ) VALUES ('src-1','job-1',1,'a.png',10,'sha',4,4,'PNG','ACTIVE',3,2,'2026-01-01T00:00:00Z')
            """
        )
        conn.commit()
    finally:
        conn.close()


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gacha-schema-")
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.db_path = os.path.join(self.root, "db", "gacha.sqlite3")

    def test_v1_database_is_upgraded_additively_without_touching_rows(self):
        make_v1(self.db_path)
        db = Database(self.db_path)
        db.open()
        try:
            self.assertEqual(db.schema_version, 4)
            self.assertIn("creation_progress", db.query_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='creation_progress'"
            )[0])
            self.assertIn("review_sessions", db.query_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='review_sessions'"
            )[0])
            self.assertIn("review_session_actions", db.query_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='review_session_actions'"
            )[0])
            job = db.query_one("SELECT * FROM jobs WHERE id='job-1'")
            self.assertEqual(job["status"], "COMPLETED")
            self.assertEqual(int(job["row_version"]), 3)
            source = db.query_one("SELECT * FROM sources WHERE id='src-1'")
            self.assertEqual(int(source["result_revision"]), 3)
            self.assertEqual(int(source["selection_version"]), 2)
            rows = db.query_all("SELECT version, checksum FROM schema_migrations ORDER BY version")
            self.assertEqual([int(row["version"]) for row in rows], [1, 2, 3, 4])
            self.assertEqual(rows[0]["checksum"], schema_checksum(1))
            self.assertEqual(rows[1]["checksum"], schema_checksum(2))
            self.assertEqual(rows[2]["checksum"], schema_checksum(3))
            self.assertEqual(rows[3]["checksum"], schema_checksum(4))
            self.assertEqual(current_schema_version(db._conn), 4)
        finally:
            db.close()

    def test_tampered_old_migration_checksum_refuses_to_start(self):
        make_v1(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE schema_migrations SET checksum='deadbeef' WHERE version=1")
        conn.commit()
        conn.close()
        db = Database(self.db_path)
        with self.assertRaises(GachaError) as ctx:
            db.open()
        self.assertEqual(ctx.exception.code, "DB_READ_ONLY")

    def test_older_verified_backup_is_restored_then_migrated(self):
        old_backup = os.path.join(self.root, "old-backup.sqlite3")
        make_v1(old_backup)
        db = Database(self.db_path)
        db.open()
        try:
            repo = Repository(db)
            paths = SimpleNamespace(
                data_root=self.root,
                results_root=os.path.join(self.root, "results"),
                backups_root=os.path.join(self.root, "backups"),
                db_path=self.db_path,
            )
            manager = BackupManager(
                db=db,
                paths=paths,
                settings=SimpleNamespace(),
                repository=repo,
            )
            checksum, size = sha256_file(old_backup)
            with db.tx() as conn:
                conn.execute(
                    "INSERT INTO backups(id, path, db_version, schema_version, checksum, status, size, created_at) "
                    "VALUES ('old-backup', ?, 1, 1, ?, 'VERIFIED', ?, ?)",
                    (old_backup, checksum, size, utc_now()),
                )
            result = manager.restore("old-backup")
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(db.schema_version, 4)
            self.assertIsNotNone(db.query_one("SELECT id FROM jobs WHERE id='job-1'"))
            self.assertIsNotNone(
                db.query_one("SELECT name FROM sqlite_master WHERE name='creation_progress'")
            )
            self.assertIsNotNone(
                db.query_one("SELECT name FROM sqlite_master WHERE name='review_sessions'")
            )
        finally:
            db.close()

    def test_failed_v3_migration_rolls_back_without_half_tables(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(SCHEMA_V1)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) "
                "VALUES (1, '2026-01-01T00:00:00Z', ?)",
                (schema_checksum(1),),
            )
            conn.executescript(SCHEMA_V2)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) "
                "VALUES (2, '2026-01-01T00:00:00Z', ?)",
                (schema_checksum(2),),
            )
            conn.commit()
        finally:
            conn.close()
        original = schema_module.SCHEMA_SCRIPTS[3]
        schema_module.SCHEMA_SCRIPTS[3] = "CREATE TABLE review_sessions (broken"
        try:
            db = Database(self.db_path)
            with self.assertRaises(GachaError):
                db.open()
        finally:
            schema_module.SCHEMA_SCRIPTS[3] = original
        raw = sqlite3.connect(self.db_path)
        try:
            versions = [
                int(row[0])
                for row in raw.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            tables = {
                row[0]
                for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            raw.close()
        self.assertEqual(versions, [1, 2])
        self.assertNotIn("review_sessions", tables)
        self.assertNotIn("review_session_actions", tables)
        # The untouched v2 database still migrates cleanly afterwards.
        db = Database(self.db_path)
        db.open()
        try:
            self.assertEqual(db.schema_version, 4)
        finally:
            db.close()

    def test_v3_schema_can_be_applied_to_an_empty_database(self):
        db = Database(self.db_path)
        db.open()
        try:
            self.assertEqual(db.schema_version, 4)
            tables = {
                row[0]
                for row in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertTrue({
                "jobs", "sources", "draws", "outputs", "creation_progress",
                "review_sessions", "review_session_actions",
                "output_retention", "cleanup_runs", "cleanup_items",
                "job_storage_state", "maintenance_state",
            } <= tables)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
