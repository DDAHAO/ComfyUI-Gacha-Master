"""Configuration and resolved storage locations.

Data (SQLite, snapshots, receipts) and generated results are kept apart so
updating or removing the plugin never deletes completed work.  The active
SQLite database must live on a local filesystem; network shares are rejected
for the database path.
"""

from __future__ import annotations

import copy
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .errors import GachaError
from .util import atomic_write_json, ensure_dir, path_is_within, utc_now

MIB = 1024 * 1024
GIB = 1024 * MIB

DEFAULT_SETTINGS: dict[str, Any] = {
    "schema_version": 1,
    "protection_mode": "snapshot",
    "max_inflight": 1,
    "max_active_jobs": 1,
    "auto_resume_on_restart": False,
    "auto_retry_draw": False,
    "seed_domain_max": 9007199254740991,
    "plan_limit": 100000,
    "plan_insert_batch": 500,
    "upload_max_file": 100 * MIB,
    "decode_max_pixels": 100_000_000,
    "thumbnail_edge": 320,
    "page_size": 50,
    "page_size_max": 200,
    "submit_timeout": 30.0,
    "status_poll": 1.0,
    "long_execution_warning": 1800,
    "backup_draws": 20,
    "backup_interval": 600,
    "backup_retention": 5,
    "disk_reserve": 1024 * MIB,
    "thumbnail_workers": 2,
    "export_workers": 1,
    "sqlite_busy_timeout": 5000,
    "export_layout": "FLAT",
    "targeted_cancel_verified": False,
    "allowed_input_roots": [],
    "allowed_export_roots": [],
    "results_root": None,
    "comfy_input_root": None,
}

PROTECTION_MODES = {"snapshot", "reference"}


@dataclass
class Settings:
    schema_version: int = 1
    protection_mode: str = "snapshot"
    max_inflight: int = 1
    max_active_jobs: int = 1
    auto_resume_on_restart: bool = False
    auto_retry_draw: bool = False
    seed_domain_max: int = 9007199254740991
    plan_limit: int = 100000
    plan_insert_batch: int = 500
    upload_max_file: int = 100 * MIB
    decode_max_pixels: int = 100_000_000
    thumbnail_edge: int = 320
    page_size: int = 50
    page_size_max: int = 200
    submit_timeout: float = 30.0
    status_poll: float = 1.0
    long_execution_warning: int = 1800
    backup_draws: int = 20
    backup_interval: int = 600
    backup_retention: int = 5
    disk_reserve: int = 1024 * MIB
    thumbnail_workers: int = 2
    export_workers: int = 1
    sqlite_busy_timeout: int = 5000
    export_layout: str = "FLAT"
    targeted_cancel_verified: bool = False
    allowed_input_roots: list[str] = field(default_factory=list)
    allowed_export_roots: list[str] = field(default_factory=list)
    results_root: Optional[str] = None
    comfy_input_root: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        merged = copy.deepcopy(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in (data or {}).items() if k in DEFAULT_SETTINGS})
        settings = cls(**merged)
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.protection_mode not in PROTECTION_MODES:
            raise GachaError("BAD_REQUEST", "protection_mode 只能是 snapshot 或 reference。")
        hard = {
            "max_inflight": (1, 1, "V1.0 只允许一个插件执行在途。"),
            "max_active_jobs": (1, 1, "V1.0 只允许一个运行意图的 JOB。"),
            "plan_limit": (1, 1_000_000, "计划上限超出允许范围。"),
            "plan_insert_batch": (1, 5000, "入库批量超出允许范围。"),
            "upload_max_file": (1024, 2 * 1024 * MIB, "上传大小限制超出允许范围。"),
            "decode_max_pixels": (1_000_000, 500_000_000, "像素上限超出允许范围。"),
            "thumbnail_edge": (64, 1024, "缩略图尺寸超出允许范围。"),
            "page_size": (1, 200, "分页大小超出允许范围。"),
            "backup_retention": (1, 50, "备份保留数超出允许范围。"),
            "disk_reserve": (0, 50 * GIB, "磁盘保留空间超出允许范围。"),
            "submit_timeout": (1.0, 300.0, "提交超时超出允许范围。"),
            "status_poll": (0.01, 10.0, "状态轮询间隔超出允许范围。"),
            "sqlite_busy_timeout": (100, 60000, "SQLite 忙等待超出允许范围。"),
        }
        for name, (low, high, message) in hard.items():
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value < low or value > high:
                raise GachaError("BAD_REQUEST", f"{name}: {message}", details={"value": value, "min": low, "max": high})
        if self.auto_resume_on_restart:
            raise GachaError("BAD_REQUEST", "V1.0 不允许重启后自动恢复提交。")
        if self.auto_retry_draw:
            raise GachaError("BAD_REQUEST", "V1.0 不允许自动重试 DRAW。")
        if self.export_layout not in {"FLAT", "GROUPED"}:
            raise GachaError("BAD_REQUEST", "export_layout 只能是 FLAT 或 GROUPED。")
        if not isinstance(self.targeted_cancel_verified, bool):
            raise GachaError("BAD_REQUEST", "targeted_cancel_verified 必须是布尔值。")
        for root in self.allowed_input_roots:
            if not isinstance(root, str) or not root.strip():
                raise GachaError("BAD_REQUEST", "输入根目录不能为空。")
        for root in self.allowed_export_roots:
            if not isinstance(root, str) or not root.strip():
                raise GachaError("BAD_REQUEST", "导出根目录不能为空。")


@dataclass
class AppPaths:
    data_root: str
    results_root: str
    config_path: str
    db_path: str
    backups_root: str
    inbox_root: str
    jobs_root: str
    logs_root: str
    lock_path: str
    pid_path: str
    thumbnails_root: str

    @classmethod
    def build(cls, data_root: str, results_root: str) -> "AppPaths":
        data_root = os.path.abspath(data_root)
        results_root = os.path.abspath(results_root)
        return cls(
            data_root=data_root,
            results_root=results_root,
            config_path=os.path.join(data_root, "config.json"),
            db_path=os.path.join(data_root, "db", "gacha.sqlite3"),
            backups_root=os.path.join(data_root, "backups"),
            inbox_root=os.path.join(data_root, "inbox"),
            jobs_root=os.path.join(data_root, "jobs"),
            logs_root=os.path.join(data_root, "logs"),
            lock_path=os.path.join(data_root, "gacha.lock"),
            pid_path=os.path.join(data_root, "gacha.pid"),
            thumbnails_root=os.path.join(results_root, "thumbnails"),
        )

    def create(self) -> None:
        for path in (
            self.data_root,
            os.path.dirname(self.db_path),
            self.backups_root,
            self.inbox_root,
            self.jobs_root,
            self.logs_root,
            self.results_root,
            self.thumbnails_root,
        ):
            ensure_dir(path)


def default_data_root() -> str:
    explicit = os.environ.get("GACHA_DATA_ROOT")
    if explicit:
        return os.path.abspath(explicit)
    return os.path.join(os.path.expanduser("~"), ".comfyui-gacha-master")


def default_results_root(data_root: str) -> str:
    explicit = os.environ.get("GACHA_RESULTS_ROOT")
    if explicit:
        return os.path.abspath(explicit)
    return os.path.join(data_root, "results")


def load_settings(config_path: str) -> Settings:
    if not os.path.exists(config_path):
        return Settings()
    try:
        import json

        with open(config_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return Settings.from_dict(data)
    except GachaError:
        raise
    except Exception as exc:
        raise GachaError(
            "BAD_REQUEST",
            "配置文件损坏，已停止加载。",
            details={"path": config_path, "error": str(exc)},
        )


def save_settings(config_path: str, settings: Settings) -> None:
    settings.validate()
    payload = settings.to_dict()
    payload["updated_at"] = utc_now()
    atomic_write_json(config_path, payload)


def validate_results_location(paths: AppPaths) -> None:
    db_dir = os.path.dirname(paths.db_path)
    if paths.db_path.startswith("\\\\"):
        raise GachaError("PATH_NOT_ALLOWED", "SQLite 数据库不能放在 UNC 网络共享上。")
    if os.name == "nt":
        drive, _ = os.path.splitdrive(db_dir)
        if drive.startswith("\\\\"):
            raise GachaError("PATH_NOT_ALLOWED", "SQLite 数据库不能放在网络共享上。")
    # The results root must not live inside the database directory (or vice
    # versa); that keeps backups and job data separate.
    if path_is_within(db_dir, paths.results_root) or path_is_within(paths.results_root, db_dir):
        raise GachaError(
            "PATH_NOT_ALLOWED",
            "结果目录不能与数据库目录重叠。",
            details={"db_dir": db_dir, "results_root": paths.results_root},
        )
