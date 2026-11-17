#!/usr/bin/env python3
"""Deterministic candidate manifests for ComfyUI Gacha Master.

The installer, the acceptance runner and the release evidence all need to
compare the same trees.  Keeping the scopes in one module prevents the classic
failure mode where a document explains one hash while the tool that verified
the install used a different file set.

Scopes
------
runtime
    Everything ComfyUI can import or serve: the root ``__init__.py`` and every
    non-bytecode file below ``comfyui_gacha_master/``.  Extra files in this
    scope are dangerous: they can be imported but are not part of the source
    manifest, so they must fail verification.
browser_assets
    Runtime files below ``comfyui_gacha_master/web/`` (JS/CSS/package.json).
delivery
    Files an installer copy is expected to carry: the same rules as
    ``tools/install_plugin.py`` (tests, evidence, virtualenvs and caches are
    excluded).
source
    The whole candidate work tree excluding virtualenvs, evidence and caches.
    This includes tests and tools, which are reported but are not runtime.

Usage:
  python tools/candidate_manifest.py --snapshot-dir test-artifacts/next-.../candidate
  python tools/candidate_manifest.py --installed-root D:\\ComfyUI\\custom_nodes\\ComfyUI-Gacha-Master
  python tools/candidate_manifest.py --check test-artifacts/.../candidate/MANIFEST.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA_VERSION = 1

# Keep in sync with tools/install_plugin.py.  The names are compared exactly
# (case-insensitively on Windows) and at any depth.
BASE_EXCLUDES = {
    ".git",
    ".venv",
    "__pycache__",
    "test-artifacts",
    "node_modules",
    ".pytest_cache",
    "data",
    "tmp",
}
DELIVERY_EXCLUDES = BASE_EXCLUDES | {
    "tests",
    "DEVELOPMENT.md",
}
SOURCE_EXCLUDES = BASE_EXCLUDES
BYTECODE_SUFFIXES = (".pyc", ".pyo")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_excludes(excludes: Iterable[str]) -> set[str]:
    return {name.casefold() for name in excludes}


def walk_files(root: str, excludes: Iterable[str] = ()) -> list[str]:
    """Return sorted, slash-separated relative paths below *root*."""
    root = os.path.abspath(root)
    excluded = _normalise_excludes(excludes)
    result: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name.casefold() not in excluded
        )
        for name in sorted(filenames):
            if name.casefold() in excluded or name.endswith(BYTECODE_SUFFIXES):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            result.append(rel)
    result.sort()
    return result


def manifest_for(root: str, rel_paths: Iterable[str]) -> dict[str, str]:
    root = os.path.abspath(root)
    result: dict[str, str] = {}
    for rel in sorted(rel_paths):
        path = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        result[rel] = sha256_file(path)
    return result


def tree_hash(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(files[rel]))
    return digest.hexdigest()


def runtime_paths(root: str) -> list[str]:
    """Files ComfyUI can load from the plugin directory."""
    root = os.path.abspath(root)
    paths = ["__init__.py"] if os.path.isfile(os.path.join(root, "__init__.py")) else []
    package = os.path.join(root, "comfyui_gacha_master")
    if os.path.isdir(package):
        for rel in walk_files(package):
            paths.append("comfyui_gacha_master/" + rel)
    return sorted(paths)


def runtime_manifest(root: str) -> dict[str, str]:
    return manifest_for(root, runtime_paths(root))


def browser_manifest(root: str) -> dict[str, str]:
    return {
        rel: digest
        for rel, digest in runtime_manifest(root).items()
        if rel.startswith("comfyui_gacha_master/web/")
    }


def delivery_manifest(root: str) -> dict[str, str]:
    return manifest_for(root, walk_files(root, DELIVERY_EXCLUDES))


def source_manifest(root: str) -> dict[str, str]:
    return manifest_for(root, walk_files(root, SOURCE_EXCLUDES))


def scope_document(files: dict[str, str]) -> dict[str, Any]:
    return {
        "file_count": len(files),
        "tree_sha256": tree_hash(files),
        "files": files,
    }


def build_manifest(source_root: str) -> dict[str, Any]:
    source_root = os.path.abspath(source_root)
    runtime = runtime_manifest(source_root)
    browser = browser_manifest(source_root)
    delivery = delivery_manifest(source_root)
    source = source_manifest(source_root)
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "source_root": source_root,
        "scopes": {
            "runtime": scope_document(runtime),
            "browser_assets": scope_document(browser),
            "delivery": scope_document(delivery),
            "source": scope_document(source),
        },
        "categories": {rel: classify(rel) for rel in source},
    }
    document["fingerprint"] = fingerprint(document)
    return document


def classify(rel: str) -> str:
    if rel == "__init__.py" or rel.startswith("comfyui_gacha_master/"):
        return "runtime"
    if rel.startswith("tests/"):
        return "test"
    if rel.startswith("tools/"):
        return "tool"
    if rel.startswith("docs/"):
        return "documentation"
    if rel.startswith("examples/"):
        return "example"
    return "metadata"


def fingerprint(document: dict[str, Any]) -> str:
    """Stable identity of the scopes, independent of timestamps/path noise."""
    payload = {
        name: {
            "file_count": scope["file_count"],
            "tree_sha256": scope["tree_sha256"],
        }
        for name, scope in document["scopes"].items()
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compare_manifests(
    expected: dict[str, str], actual: dict[str, str]
) -> dict[str, Any]:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        rel for rel in set(expected) & set(actual) if expected[rel] != actual[rel]
    )
    return {
        "ok": not (missing or extra or changed),
        "expected_count": len(expected),
        "actual_count": len(actual),
        "missing": missing,
        "extra": extra,
        "changed": changed,
    }


def installed_report(source_root: str, installed_root: str) -> dict[str, Any]:
    """Compare the installed plugin to the candidate.

    The runtime scope is exact-set: a file that exists in the install but not in
    the candidate is reported as ``extra`` and fails the comparison.  The
    delivery scope is reported separately because a link install intentionally
    points at the work tree.
    """
    source_root = os.path.abspath(source_root)
    installed_root = os.path.abspath(installed_root)
    runtime = compare_manifests(runtime_manifest(source_root), runtime_manifest(installed_root))
    delivery = compare_manifests(delivery_manifest(source_root), delivery_manifest(installed_root))
    extra_runtime = runtime["extra"]
    return {
        "ok": runtime["ok"],
        "source_root": source_root,
        "installed_root": installed_root,
        "runtime": runtime,
        "delivery": delivery,
        "extra_runtime_files": extra_runtime,
        "runtime_tree_sha256": tree_hash(runtime_manifest(installed_root)),
    }


def _run_git(root: str, args: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        return {
            "args": args,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except Exception as exc:  # pragma: no cover - git may be unavailable
        return {"args": args, "error": f"{type(exc).__name__}: {exc}"}


def git_snapshot(root: str) -> dict[str, Any]:
    root = os.path.abspath(root)
    head = _run_git(root, ["rev-parse", "HEAD"])
    status = _run_git(root, ["status", "--porcelain=v2", "--untracked-files=all"])
    diff = _run_git(root, ["diff", "--binary", "HEAD"])
    diff_stat = _run_git(root, ["diff", "--stat", "HEAD"])
    untracked = _run_git(root, ["ls-files", "--others", "--exclude-standard"])
    return {
        "captured_at": utc_now(),
        "head": head.get("stdout", "").strip() or None,
        "head_error": head.get("stderr", "").strip() or None,
        "status": status.get("stdout", ""),
        "status_error": status.get("stderr", "").strip() or None,
        "diff": diff.get("stdout", ""),
        "diff_error": diff.get("stderr", "").strip() or None,
        "diff_stat": diff_stat.get("stdout", ""),
        "untracked": untracked.get("stdout", "").splitlines(),
        "untracked_error": untracked.get("stderr", "").strip() or None,
    }


def write_snapshot(root: str, snapshot_dir: str) -> dict[str, Any]:
    """Write a candidate snapshot without touching the work tree."""
    root = os.path.abspath(root)
    os.makedirs(snapshot_dir, exist_ok=True)
    git = git_snapshot(root)
    files = {
        "GIT-HEAD.txt": (git["head"] or "") + "\n",
        "GIT-STATUS.txt": git["status"],
        "GIT-DIFF.patch": git["diff"],
        "GIT-DIFF-STAT.txt": git["diff_stat"],
        "UNTRACKED-FILES.txt": "\n".join(git["untracked"]) + ("\n" if git["untracked"] else ""),
    }
    for name, content in files.items():
        path = os.path.join(snapshot_dir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)

    manifest = build_manifest(root)
    manifest_path = os.path.join(snapshot_dir, "MANIFEST.json")
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    archive_path = os.path.join(snapshot_dir, "candidate-delivery.zip")
    archive_info = write_candidate_archive(root, archive_path, manifest["scopes"]["delivery"]["files"])
    # The delivery zip intentionally drops tests and DEVELOPMENT.md.  Untracked
    # files cannot be rebuilt from a patch plus a name list, so the complete
    # source scope is archived next to it and verified by unpacking.
    source_archive_path = os.path.join(snapshot_dir, "candidate-source.zip")
    source_archive_info = write_candidate_archive(
        root, source_archive_path, manifest["scopes"]["source"]["files"]
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "source_root": root,
        "git": {
            "head": git["head"],
            "dirty": bool(git["status"].strip()),
            "status_error": git["status_error"],
        },
        "manifest_path": manifest_path,
        "manifest_fingerprint": manifest["fingerprint"],
        "scopes": {
            name: {
                "file_count": scope["file_count"],
                "tree_sha256": scope["tree_sha256"],
            }
            for name, scope in manifest["scopes"].items()
        },
        "archive": archive_info,
        "source_archive": source_archive_info,
        "rebuild_command": (
            "python tools/candidate_manifest.py --verify-snapshot " + snapshot_dir
        ),
        "snapshot_files": sorted(
            list(files)
            + ["MANIFEST.json", "candidate-delivery.zip", "candidate-source.zip"]
        ),
    }
    with open(os.path.join(snapshot_dir, "SNAPSHOT.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return summary


def write_candidate_archive(root: str, archive_path: str, files: dict[str, str]) -> dict[str, Any]:
    """Write a byte-stable zip of the delivery scope.

    Fixed timestamps and sorted entry order make repeated generation of the
    same source tree produce the same archive hash on the same zlib build.
    """
    root = os.path.abspath(root)
    os.makedirs(os.path.dirname(os.path.abspath(archive_path)) or ".", exist_ok=True)
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for rel in sorted(files):
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(os.path.join(root, *rel.split("/")), "rb") as handle:
                archive.writestr(info, handle.read())
    return {
        "path": os.path.abspath(archive_path),
        "file_count": len(files),
        "sha256": sha256_file(archive_path),
        "size": os.path.getsize(archive_path),
    }


def check_manifest(source_root: str, manifest_path: str) -> dict[str, Any]:
    with open(manifest_path, encoding="utf-8") as handle:
        previous = json.load(handle)
    current = build_manifest(source_root)
    scopes = {}
    for name, old_scope in previous.get("scopes", {}).items():
        new_scope = current["scopes"].get(name)
        if new_scope is None:
            continue
        scopes[name] = compare_manifests(old_scope["files"], new_scope["files"])
    ok = all(item["ok"] for item in scopes.values())
    return {
        "ok": ok,
        "previous_fingerprint": previous.get("fingerprint"),
        "current_fingerprint": current["fingerprint"],
        "fingerprint_matches": previous.get("fingerprint") == current["fingerprint"],
        "scopes": scopes,
    }


def _safe_extract(archive_path: str, destination: str) -> list[str]:
    """Extract *archive_path* into an empty directory, rejecting odd members."""
    names: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            name = info.filename
            normalised = name.replace("\\", "/")
            parts = normalised.split("/")
            if normalised.startswith("/") or ".." in parts or (len(normalised) > 1 and parts[0].endswith(":")):
                raise ValueError(f"unsafe archive member: {name}")
            if info.is_dir():
                continue
            target = os.path.join(destination, *normalised.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as handle:
                handle.write(source.read())
            names.append(normalised)
    return sorted(names)


def verify_snapshot(snapshot_dir: str, work_dir: str | None = None) -> dict[str, Any]:
    """Unpack a snapshot into fresh directories and rebuild every scope.

    This is the check that a saved candidate can be reconstructed later: the
    archives are opened in new empty directories, the manifests are recomputed
    from the unpacked bytes and the result is compared file by file with the
    stored MANIFEST.  Nothing is read from the live work tree.
    """
    snapshot_dir = os.path.abspath(snapshot_dir)
    with open(os.path.join(snapshot_dir, "MANIFEST.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    base = os.path.abspath(
        work_dir or tempfile.mkdtemp(prefix="gm-rebuild-", dir=tempfile.gettempdir())
    )
    own_base = work_dir is None
    delivery_dir = os.path.join(base, "delivery")
    source_dir = os.path.join(base, "source")
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "snapshot_dir": snapshot_dir,
        "work_dir": base,
        "scopes": {},
        "errors": [],
    }
    try:
        for path in (delivery_dir, source_dir):
            os.makedirs(path)
        delivery_archive = os.path.join(snapshot_dir, "candidate-delivery.zip")
        source_archive = os.path.join(snapshot_dir, "candidate-source.zip")
        delivery_members = _safe_extract(delivery_archive, delivery_dir)
        source_members = _safe_extract(source_archive, source_dir)
        for scope, root, members in (
            ("delivery", delivery_dir, delivery_members),
            ("source", source_dir, source_members),
        ):
            expected = manifest["scopes"][scope]["files"]
            if sorted(members) != sorted(expected):
                result["errors"].append(
                    f"{scope}: archive members differ from MANIFEST files"
                )
            actual = manifest_for(root, walk_files(root))
            comparison = compare_manifests(expected, actual)
            result["scopes"][scope] = {
                "member_count": len(members),
                "file_count": len(actual),
                "tree_sha256": tree_hash(actual),
                **comparison,
            }
            if comparison["ok"] and tree_hash(actual) != manifest["scopes"][scope]["tree_sha256"]:
                result["errors"].append(f"{scope}: tree hash differs from MANIFEST")
        # Runtime and browser scopes are subsets of the unpacked delivery tree,
        # so rebuilding them from the archive is the real runtime check.
        runtime = runtime_manifest(delivery_dir)
        browser = browser_manifest(delivery_dir)
        for name, actual in (("runtime", runtime), ("browser_assets", browser)):
            expected = manifest["scopes"][name]["files"]
            comparison = compare_manifests(expected, actual)
            result["scopes"][name] = {
                "file_count": len(actual),
                "tree_sha256": tree_hash(actual),
                **comparison,
            }
            if not comparison["ok"]:
                result["errors"].append(f"{name}: rebuilt tree differs from MANIFEST")
    except Exception as exc:  # noqa: BLE001 - report every unpacking failure
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    result["ok"] = not result["errors"]
    result["result"] = "PASS" if result["ok"] else "FAIL"
    with open(os.path.join(snapshot_dir, "VERIFY.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    snapshot_path = os.path.join(snapshot_dir, "SNAPSHOT.json")
    if os.path.isfile(snapshot_path):
        try:
            with open(snapshot_path, encoding="utf-8") as handle:
                snapshot = json.load(handle)
            snapshot["verification"] = {
                "ok": result["ok"],
                "verified_at": result["generated_at"],
                "report": os.path.join(snapshot_dir, "VERIFY.json"),
                "scopes": {
                    name: scope["ok"] for name, scope in result["scopes"].items()
                },
            }
            with open(snapshot_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
            result["errors"].append(f"SNAPSHOT.json update failed: {exc}")
    if own_base:
        shutil.rmtree(base, ignore_errors=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Build/verify deterministic candidate manifests")
    parser.add_argument("--source-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--snapshot-dir", default=None)
    parser.add_argument("--output", default=None, help="write MANIFEST.json to this path")
    parser.add_argument("--installed-root", default=None)
    parser.add_argument("--check", default=None, help="compare current tree to a previous MANIFEST.json")
    parser.add_argument(
        "--verify-snapshot",
        default=None,
        help="unpack a saved snapshot into fresh directories and rebuild every scope",
    )
    parser.add_argument(
        "--rebuild-dir",
        default=None,
        help="existing empty directory used for the unpack check (default: new temp dir)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.verify_snapshot:
        result = verify_snapshot(args.verify_snapshot, args.rebuild_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    if args.check:
        result = check_manifest(args.source_root, args.check)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    if args.snapshot_dir:
        result = write_snapshot(args.source_root, args.snapshot_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    manifest = build_manifest(args.source_root)
    if args.installed_root:
        manifest["installed"] = installed_report(args.source_root, args.installed_root)
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
    if not args.quiet:
        print(text)
    return 0 if manifest.get("installed", {}).get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
