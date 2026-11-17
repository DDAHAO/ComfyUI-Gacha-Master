"""Runtime wiring for one data root.

Construction is cheap; opening SQLite, recovery and the scheduler happen on the
first API request (or a delayed startup task registered by the ComfyUI
integration) so importing a custom node never starts a scheduling loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from . import storage
from .adapters.comfy import ComfyAdapter
from .api import Api
from .backups import BackupManager
from .cleanup import StorageCleanupManager
from .config import AppPaths, Settings, default_data_root, default_results_root, load_settings, save_settings, validate_results_location
from .creation import CreationManager
from .db import Database
from .errors import GachaError
from .exports import ExportManager
from .inputs import InputManager
from .recovery import RecoveryManager
from .repository import Repository
from .scheduler import Scheduler
from .service import Service
from .util import FileLock, ensure_dir, path_is_within, utc_now, uuid4, verify_publish_semantics

LOGGER = logging.getLogger("gacha.runtime")

_PROCESS_LOCKS: set[str] = set()
_PROCESS_LOCK_GUARD = threading.RLock()


class Runtime:
    def __init__(
        self,
        *,
        data_root: Optional[str] = None,
        results_root: Optional[str] = None,
        comfy_input_root: Optional[str] = None,
        comfy_output_root: Optional[str] = None,
        base_url: str = "http://127.0.0.1:8188",
        include_metadata: bool = True,
        engine_factory: Optional[Callable[[], Any]] = None,
        api_engine_factory: Optional[Callable[[], Any]] = None,
        prefix: str = "/gacha_master/v1",
    ) -> None:
        self.data_root = os.path.abspath(data_root or default_data_root())
        self.results_root_override = results_root
        self.comfy_input_root = os.path.abspath(comfy_input_root) if comfy_input_root else None
        self.comfy_output_root = os.path.abspath(comfy_output_root) if comfy_output_root else None
        self.base_url = base_url
        self.include_metadata = include_metadata
        self.engine_factory = engine_factory or self._default_engine
        self.api_engine_factory = api_engine_factory
        self.prefix = prefix
        self.lock: Optional[FileLock] = None
        self.db: Optional[Database] = None
        self.paths: Optional[AppPaths] = None
        self.settings: Optional[Settings] = None
        self.repo: Optional[Repository] = None
        self.inputs: Optional[InputManager] = None
        self.backups: Optional[BackupManager] = None
        self.exports: Optional[ExportManager] = None
        self.cleanup: Optional[StorageCleanupManager] = None
        self.service: Optional[Service] = None
        self.creation: Optional[CreationManager] = None
        self.scheduler: Optional[Scheduler] = None
        self.recovery: Optional[RecoveryManager] = None
        self.api_engine: Optional[Any] = None
        self.api: Optional[Api] = None
        self.copy_executor: Optional[ThreadPoolExecutor] = None
        self.boot_id = str(uuid.uuid4())
        self.started = False
        self.start_error: Optional[str] = None
        self._start_lock = threading.RLock()
        self._started_event = threading.Event()
        self._starting = False
        self._process_lock_key: Optional[str] = None

    def _default_engine(self):
        return ComfyAdapter(
            base_url=self.base_url,
            submit_timeout=self.settings.submit_timeout if self.settings else 30.0,
            targeted_cancel_verified=bool(
                self.settings.targeted_cancel_verified if self.settings else False
            ),
        )

    # --------------------------------------------------------------- start
    async def ensure_started(self) -> "Runtime":
        if self.started:
            return self
        wait_event = None
        with self._start_lock:
            if self.started:
                return self
            if self._starting:
                wait_event = self._started_event
            else:
                self._starting = True
        if wait_event is not None:
            if not await asyncio.to_thread(wait_event.wait, 120.0):
                raise GachaError("SERVICE_NOT_READY", "插件启动等待超时。", details={"error": self.start_error})
            if self.start_error and not self.started:
                raise GachaError("SERVICE_NOT_READY", "插件启动失败。", details={"error": self.start_error})
            return self
        try:
            await self._start()
        finally:
            with self._start_lock:
                self._starting = False
                self._started_event.set()
        return self

    async def _start(self) -> None:
        try:
            with _PROCESS_LOCK_GUARD:
                if self.data_root in _PROCESS_LOCKS:
                    raise GachaError(
                        "INSTANCE_LOCKED",
                        "同一数据目录已有插件实例在运行。",
                        details={"data_root": self.data_root},
                    )
                _PROCESS_LOCKS.add(self.data_root)
                self._process_lock_key = self.data_root
            ensure_dir(self.data_root)
            settings = load_settings(os.path.join(self.data_root, "config.json"))
            results_root = (
                self.results_root_override
                or settings.results_root
                or default_results_root(self.data_root)
            )
            paths = AppPaths.build(self.data_root, results_root)
            paths.create()
            validate_results_location(paths)
            settings.results_root = paths.results_root
            if self.comfy_input_root:
                settings.comfy_input_root = self.comfy_input_root
            # Default a safe export root next to ComfyUI output.
            if not settings.allowed_export_roots and self.comfy_output_root:
                export_root = os.path.join(self.comfy_output_root, "gacha_master", "Exports")
                ensure_dir(export_root)
                settings.allowed_export_roots = [export_root]
            if settings.comfy_input_root and settings.comfy_input_root not in settings.allowed_input_roots:
                settings.allowed_input_roots.append(settings.comfy_input_root)
            settings.validate()
            save_settings(paths.config_path, settings)
            lock = FileLock(paths.lock_path)
            if not lock.acquire():
                raise GachaError(
                    "INSTANCE_LOCKED",
                    "另一个进程正在使用同一数据目录。",
                    details={"data_root": paths.data_root},
                )
            self.lock = lock
            db = Database(paths.db_path, busy_timeout=int(settings.sqlite_busy_timeout))
            db.open()
            self.db = db
            self.paths = paths
            self.settings = settings
            repo = Repository(db)
            repo.set_runtime_meta("boot_id", self.boot_id)
            repo.set_runtime_meta("boot_at", utc_now())
            publish = verify_publish_semantics(paths.data_root)
            repo.set_runtime_meta("filesystem_no_replace_publish", "true" if publish["ok"] else "false")
            if not publish["ok"]:
                raise GachaError(
                    "SERVICE_NOT_READY",
                    "本地文件系统未通过无覆盖发布自检，已停止插件。",
                    details=publish,
                )
            self.repo = repo
            inputs = InputManager(paths=paths, settings=settings)
            backups = BackupManager(db=db, paths=paths, settings=settings, repository=repo)
            exports = ExportManager(db=db, repo=repo, settings=settings, paths=paths, backup_manager=backups)
            self.inputs = inputs
            self.backups = backups
            self.exports = exports
            cleanup = StorageCleanupManager(
                db=db, repo=repo, settings=settings, paths=paths
            )
            self.cleanup = cleanup
            self.recovery = RecoveryManager(db=db, repo=repo, settings=settings, inputs=inputs)
            api_engine = (
                self.api_engine_factory()
                if self.api_engine_factory is not None
                else ComfyAdapter(
                    base_url=self.base_url,
                    submit_timeout=settings.submit_timeout,
                )
            )
            self.api_engine = api_engine
            if hasattr(api_engine, "detect"):
                try:
                    await api_engine.detect()
                except Exception as exc:
                    LOGGER.warning("adapter detect failed: %s", exc)
            try:
                caps = await api_engine.capabilities()
                repo.set_runtime_meta("targeted_cancel", str(caps.get("targeted_cancel") or "unverified"))
                repo.set_runtime_meta("comfy_version", str(caps.get("comfy_version") or "unknown"))
                repo.set_runtime_meta("frontend_version", str(caps.get("frontend_version") or "unknown"))
            except Exception:
                repo.set_runtime_meta("targeted_cancel", "unverified")
            # Journal reconciliation must happen before ordinary output
            # verification: a deliberate deletion recovered from an older
            # database must not be reclassified as an accidental MISSING file.
            cleanup_report = cleanup.reconcile_startup()
            LOGGER.info("cleanup reconcile: %s", cleanup_report)
            report = await self.recovery.startup(api_engine, boot_id=self.boot_id)
            LOGGER.info(
                "recovery complete: jobs=%s needs_attention=%s",
                len(report.get("jobs", [])),
                report.get("requires_user_confirmation"),
            )
            scheduler = Scheduler(
                db=db,
                repository=repo,
                settings=settings,
                paths=paths,
                inputs=inputs,
                engine_factory=self.engine_factory,
                backup_manager=backups,
                include_metadata=self.include_metadata,
            )
            self.scheduler = scheduler
            service = Service(
                db=db,
                repo=repo,
                settings=settings,
                paths=paths,
                inputs=inputs,
                backup_manager=backups,
                scheduler=scheduler,
                exports=exports,
                cleanup=cleanup,
                include_metadata=self.include_metadata,
            )
            self.service = service
            creation = CreationManager(service)
            service.creation = creation
            self.creation = creation
            # Creation plans never resume on boot.  This also adopts legacy
            # CREATING jobs from schema v1 using their exact checkpoint and
            # already committed facts.
            creation_report = creation.reconcile_startup()
            LOGGER.info("creation reconcile: %s", creation_report)
            self.copy_executor = ThreadPoolExecutor(
                max_workers=max(1, int(settings.export_workers)), thread_name_prefix="gacha-export"
            )
            self.api = Api(
                service=service,
                repo=repo,
                exports=exports,
                recovery=self.recovery,
                cleanup=cleanup,
                engine_provider=self.engine_provider,
                prefix=self.prefix,
                copy_executor=self.copy_executor,
            )
            self.started = True
            self._started_event.set()
            scheduler.start()
            creation.start()
            LOGGER.info("Gacha Master runtime started, data_root=%s", paths.data_root)
        except Exception as exc:
            self.start_error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("failed to start Gacha Master runtime")
            self.close()
            raise

    async def engine_provider(self):
        await self.ensure_started()
        return self.api_engine

    def close(self) -> None:
        creation_stopped = True
        try:
            if self.creation is not None:
                self.creation.stop(timeout=30.0)
                self.creation = None
                if self.service is not None:
                    self.service.creation = None
        except Exception:
            creation_stopped = False
            LOGGER.exception("creation worker did not stop; keeping the database open")
        try:
            if self.scheduler is not None:
                self.scheduler.stop(timeout=5.0)
                self.scheduler = None
        except Exception:
            pass
        try:
            if self.cleanup is not None:
                self.cleanup.stop(timeout=10.0)
                self.cleanup = None
                if self.service is not None:
                    self.service.cleanup = None
        except Exception:
            pass
        try:
            if self.copy_executor is not None:
                self.copy_executor.shutdown(wait=False, cancel_futures=True)
                self.copy_executor = None
        except Exception:
            pass
        try:
            if self.db is not None and creation_stopped:
                self.db.close()
                self.db = None
        except Exception:
            pass
        try:
            if self.lock is not None:
                self.lock.release()
                self.lock = None
        except Exception:
            pass
        if self._process_lock_key:
            with _PROCESS_LOCK_GUARD:
                _PROCESS_LOCKS.discard(self._process_lock_key)
            self._process_lock_key = None
        self.started = False
        self._started_event.clear()

