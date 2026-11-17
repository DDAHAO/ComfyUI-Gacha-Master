#!/usr/bin/env python3
r"""Install/update/uninstall ComfyUI Gacha Master into a custom_nodes folder.

The installer never touches models, user workflows, unknown plugin directories,
Gacha Master data, results or exports.  A Windows junction is preferred so the
working tree is live; an explicit copy mode is available.  Copy updates are
staged, hash-verified and swapped with a rollback backup outside custom_nodes
(so a backup can never load as a second plugin).

Usage:
  python tools/install_plugin.py --comfy-root D:\ComfyUI
  python tools/install_plugin.py --comfy-root D:\ComfyUI --mode copy
  python tools/install_plugin.py --comfy-root D:\ComfyUI --mode copy --update
  python tools/install_plugin.py --comfy-root D:\ComfyUI --verify
  python tools/install_plugin.py --comfy-root D:\ComfyUI --uninstall
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST_NAME = "ComfyUI-Gacha-Master"
MARKER = ".gacha_master_install.json"

# Keep the installer, acceptance runner and evidence on one definition of the
# runtime and delivery scopes.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import candidate_manifest as cm  # noqa: E402

# One definition for the installer, the acceptance tool and the evidence.
COPY_EXCLUDES = set(cm.DELIVERY_EXCLUDES)


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_files(root: str) -> list[str]:
    return cm.walk_files(root, COPY_EXCLUDES)


def manifest(root: str) -> dict[str, str]:
    return cm.manifest_for(root, source_files(root))


def tree_hash(files: dict[str, str]) -> str:
    return cm.tree_hash(files)


def installed_manifest(destination: str) -> dict[str, str]:
    """Hash an installed copy, excluding the installer marker itself."""
    rel_paths = [rel for rel in cm.walk_files(destination, COPY_EXCLUDES) if rel != MARKER]
    return cm.manifest_for(destination, rel_paths)


def is_junction(path: str) -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(os.path.isdir(path) and os.path.islink(path)) or bool(
            getattr(os.path, "isjunction", lambda _: False)(path)
        )
    except OSError:
        return False


def link_target(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def marker_path(destination: str) -> str:
    return os.path.join(destination, MARKER)


def read_marker(destination: str) -> dict[str, Any] | None:
    path = marker_path(destination)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def write_marker(destination: str) -> dict[str, Any]:
    files = installed_manifest(destination)
    runtime = cm.runtime_manifest(destination)
    record = {
        "schema_version": 3,
        "name": "ComfyUI Gacha Master",
        "version": "1.0.0",
        "source": PROJECT_ROOT,
        "installed_at": utc_now(),
        "file_count": len(files),
        "tree_sha256": tree_hash(files),
        "runtime_file_count": len(runtime),
        "runtime_tree_sha256": cm.tree_hash(runtime),
        "files": files,
    }
    with open(marker_path(destination), "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    return record


def validate_comfy_root(root: str) -> tuple[str, str]:
    absolute = os.path.abspath(root)
    main_py = os.path.join(absolute, "main.py")
    custom_nodes = os.path.join(absolute, "custom_nodes")
    if not os.path.isfile(main_py):
        raise SystemExit(f"不是有效的 ComfyUI 根目录（缺少 main.py）：{absolute}")
    if not os.path.isdir(custom_nodes):
        raise SystemExit(f"缺少 custom_nodes 目录：{custom_nodes}")
    return absolute, custom_nodes


def create_junction(target: str, destination: str) -> None:
    if os.name != "nt":
        os.symlink(target, destination, target_is_directory=True)
        return
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", destination, target],
        capture_output=True,
        text=True,
        encoding="mbcs" if os.name == "nt" else "utf-8",
        errors="replace",
        shell=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mklink /J failed: {result.stdout} {result.stderr}")


def remove_link(destination: str) -> None:
    if os.name == "nt":
        if os.path.isdir(destination):
            os.rmdir(destination)
        else:
            os.unlink(destination)
    else:
        os.unlink(destination)


def copy_tree(staging: str) -> None:
    shutil.copytree(
        PROJECT_ROOT,
        staging,
        ignore=shutil.ignore_patterns(*COPY_EXCLUDES),
        dirs_exist_ok=False,
    )
    # Never ship interpreter state or test evidence even if ignore patterns
    # differ by platform.
    for dirpath, dirnames, filenames in os.walk(staging):
        for name in list(dirnames):
            if name in COPY_EXCLUDES:
                shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)
                dirnames.remove(name)
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                try:
                    os.unlink(os.path.join(dirpath, name))
                except OSError:
                    pass


def verify_staged(staging: str, expected: dict[str, str]) -> None:
    actual = installed_manifest(staging)
    comparison = cm.compare_manifests(expected, actual)
    runtime = cm.compare_manifests(cm.runtime_manifest(PROJECT_ROOT), cm.runtime_manifest(staging))
    if not comparison["ok"] or not runtime["ok"]:
        raise SystemExit(
            json.dumps(
                {
                    "error": "staged copy does not match source",
                    "missing": comparison["missing"][:20],
                    "extra": comparison["extra"][:20],
                    "changed": comparison["changed"][:20],
                    "runtime_missing": runtime["missing"][:20],
                    "runtime_extra": runtime["extra"][:20],
                    "runtime_changed": runtime["changed"][:20],
                },
                ensure_ascii=False,
                indent=2,
            )
        )


def backup_root(comfy_root: str) -> str:
    # Deliberately outside custom_nodes: a code backup must never be importable
    # as a second ComfyUI plugin.
    path = os.path.join(comfy_root, "user", "gacha_master", "install-backups")
    os.makedirs(path, exist_ok=True)
    return path


def install(comfy_root: str, mode: str, dry_run: bool = False, update: bool = False) -> dict[str, Any]:
    root, custom_nodes = validate_comfy_root(comfy_root)
    destination = os.path.join(custom_nodes, DEST_NAME)
    source_manifest = manifest(PROJECT_ROOT)
    source_runtime = cm.runtime_manifest(PROJECT_ROOT)
    summary: dict[str, Any] = {
        "comfy_root": root,
        "custom_nodes": custom_nodes,
        "source": PROJECT_ROOT,
        "source_file_count": len(source_manifest),
        "source_tree_sha256": tree_hash(source_manifest),
        "source_runtime_file_count": len(source_runtime),
        "source_runtime_tree_sha256": tree_hash(source_runtime),
        "destination": destination,
        "mode": mode,
        "update": update,
        "dry_run": dry_run,
        "action": "none",
    }
    if os.path.exists(destination) or os.path.islink(destination):
        if is_junction(destination) or os.path.islink(destination):
            target = link_target(destination)
            if os.path.normcase(os.path.abspath(target)) != os.path.normcase(PROJECT_ROOT):
                raise SystemExit(f"目标已存在且指向其他位置，不覆盖：{destination} -> {target}")
            summary["action"] = "already_installed_link"
            summary["link_live"] = True
            summary["note"] = "junction 直接指向工作区；更新源文件即更新安装，无需复制更新。"
            return summary
        marker = read_marker(destination)
        if not marker:
            raise SystemExit(f"目标目录已存在且不是本插件安装，拒绝覆盖：{destination}")
        installed = installed_manifest(destination)
        installed_runtime = cm.runtime_manifest(destination)
        summary["installed_file_count"] = len(installed)
        summary["installed_tree_sha256"] = tree_hash(installed)
        summary["installed_runtime_file_count"] = len(installed_runtime)
        summary["installed_runtime_tree_sha256"] = cm.tree_hash(installed_runtime)
        summary["installed_marker_version"] = marker.get("schema_version")
        same = installed == source_manifest and installed_runtime == source_runtime
        summary["matches_source"] = same
        if not update:
            summary["action"] = "already_installed_copy"
            summary["installed_version"] = marker.get("version")
            return summary
        if same:
            summary["action"] = "already_current"
            return summary
        summary["changed_files"] = sorted(
            rel for rel in set(source_manifest) & set(installed)
            if source_manifest[rel] != installed[rel]
        )[:50]
        summary["added_files"] = sorted(set(source_manifest) - set(installed))[:50]
        summary["removed_files"] = sorted(set(installed) - set(source_manifest))[:50]
        if dry_run:
            summary["action"] = "would_update_copy"
            return summary
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        staging = os.path.join(root, f".gacha_master_staging_{stamp}_{uuid.uuid4().hex[:6]}")
        backup = os.path.join(backup_root(root), f"{stamp}_{uuid.uuid4().hex[:6]}")
        try:
            copy_tree(staging)
            verify_staged(staging, source_manifest)
            write_marker(staging)
            # The old copy is moved out of custom_nodes first; if the final
            # rename fails it is restored.  Never delete the old code before
            # the new code is verified.
            os.rename(destination, backup)
            try:
                os.rename(staging, destination)
            except Exception:
                os.rename(backup, destination)
                raise
            installed_after = installed_manifest(destination)
            runtime_after = cm.runtime_manifest(destination)
            if installed_after != source_manifest or runtime_after != source_runtime:
                raise SystemExit("更新后安装源码与候选项不一致，已保留备份供回退。")
            summary["action"] = "updated_copy"
            summary["backup"] = backup
            summary["rollback"] = (
                f"停止 ComfyUI 后删除 {destination}，将 {backup} 移回该路径并重启；"
                "数据库回退需另用升级前备份。"
            )
            return summary
        finally:
            if os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)
    if dry_run:
        summary["action"] = "would_install"
        return summary
    if mode == "link":
        try:
            create_junction(PROJECT_ROOT, destination)
            summary["action"] = "linked"
            return summary
        except Exception as exc:
            summary["link_error"] = f"{type(exc).__name__}: {exc}"
            mode = "copy"
            summary["mode"] = "copy(link failed)"
    staging = os.path.join(root, f".gacha_master_staging_{uuid.uuid4().hex[:8]}")
    try:
        copy_tree(staging)
        verify_staged(staging, source_manifest)
        write_marker(staging)
        os.rename(staging, destination)
    finally:
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
    summary["action"] = "copied"
    return summary


def verify(comfy_root: str) -> dict[str, Any]:
    root, custom_nodes = validate_comfy_root(comfy_root)
    destination = os.path.join(custom_nodes, DEST_NAME)
    source_manifest = manifest(PROJECT_ROOT)
    summary: dict[str, Any] = {
        "comfy_root": root,
        "destination": destination,
        "exists": os.path.exists(destination),
        "source_file_count": len(source_manifest),
        "source_tree_sha256": tree_hash(source_manifest),
        "source_runtime_file_count": len(cm.runtime_manifest(PROJECT_ROOT)),
        "source_runtime_tree_sha256": cm.tree_hash(cm.runtime_manifest(PROJECT_ROOT)),
        "action": "verify",
    }
    if not os.path.exists(destination):
        summary["result"] = "NOT_INSTALLED"
        return summary
    if is_junction(destination) or os.path.islink(destination):
        target = os.path.abspath(link_target(destination))
        summary["link_target"] = target
        summary["result"] = "LINK_OK" if os.path.normcase(target) == os.path.normcase(PROJECT_ROOT) else "LINK_OTHER"
        summary["matches_source"] = summary["result"] == "LINK_OK"
        return summary
    installed = installed_manifest(destination)
    installed_runtime = cm.runtime_manifest(destination)
    source_runtime = cm.runtime_manifest(PROJECT_ROOT)
    delivery = cm.compare_manifests(source_manifest, installed)
    runtime = cm.compare_manifests(source_runtime, installed_runtime)
    summary["installed_file_count"] = len(installed)
    summary["installed_tree_sha256"] = tree_hash(installed)
    summary["installed_runtime_file_count"] = len(installed_runtime)
    summary["installed_runtime_tree_sha256"] = cm.tree_hash(installed_runtime)
    summary["matches_source"] = delivery["ok"] and runtime["ok"]
    summary["delivery_comparison"] = delivery
    summary["runtime_comparison"] = runtime
    summary["mismatches"] = sorted(
        set(delivery["missing"]) | set(delivery["extra"]) | set(delivery["changed"])
    )[:100]
    summary["extra_runtime_files"] = runtime["extra"]
    summary["marker"] = read_marker(destination)
    summary["result"] = "MATCH" if summary["matches_source"] else "MISMATCH"
    return summary


def uninstall(comfy_root: str, dry_run: bool = False) -> dict[str, Any]:
    root, custom_nodes = validate_comfy_root(comfy_root)
    destination = os.path.join(custom_nodes, DEST_NAME)
    summary: dict[str, Any] = {
        "comfy_root": root,
        "destination": destination,
        "dry_run": dry_run,
        "action": "none",
        "data_retained": True,
    }
    if not os.path.exists(destination) and not os.path.islink(destination):
        summary["action"] = "not_installed"
        return summary
    if is_junction(destination) or os.path.islink(destination):
        if dry_run:
            summary["action"] = "would_remove_link"
            return summary
        remove_link(destination)
        summary["action"] = "removed_link"
        return summary
    marker = read_marker(destination)
    if not marker:
        raise SystemExit(f"目标不是本插件安装，拒绝删除：{destination}")
    if dry_run:
        summary["action"] = "would_remove_copy"
        return summary
    shutil.rmtree(destination)
    summary["action"] = "removed_copy"
    summary["note"] = "插件数据、结果、导出与备份默认保留，请按 README 中的位置手动处理。"
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Install/update/uninstall ComfyUI Gacha Master")
    parser.add_argument("--comfy-root", required=True, help="ComfyUI root containing main.py and custom_nodes")
    parser.add_argument("--mode", choices=("link", "copy"), default="link")
    parser.add_argument("--update", action="store_true", help="replace an owned copy with the current source")
    parser.add_argument("--verify", action="store_true", help="report source/installed fingerprints without changes")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.verify:
        result = verify(args.comfy_root)
    elif args.uninstall:
        result = uninstall(args.comfy_root, dry_run=args.dry_run)
    else:
        result = install(args.comfy_root, args.mode, dry_run=args.dry_run, update=args.update)
    result["python"] = sys.executable
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
