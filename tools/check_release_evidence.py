#!/usr/bin/env python3
"""Refuse release evidence that does not resolve, agree with its candidate or
match the claims it makes.

The 2026-11-15 final report linked ``SUMMARY.json`` and ``cleanup.json`` from
the evidence root while both files actually lived one directory deeper.  That
class of defect costs little to create and a lot to trust, so the rules are
mechanical here:

Paths
-----
Every path inside a summary or a checked markdown file is either

* repository-relative (the default, ``path_base`` absent or ``"repo"``), or
* relative to the single ``evidence_root`` declared in the same JSON document
  (``path_base: "evidence_root"``).

One base per document.  Mixing bases inside one document is an error, because
that is exactly the ambiguity that produced the broken links.

Checks
------
* JSON is readable and every referenced evidence path exists inside the repo.
* Markdown links in the checked reports resolve.
* Status tokens come from the documented enum.
* Candidate manifest, snapshot and summary agree on the four scopes, the
  fingerprint and the archive hash; the archive file really has that hash.
* Regression counts are non-zero; a failing suite cannot produce ``PASS``.
* ``production_ready`` stays false unless the gates, the work packages and the
  blocking list all agree.
* A case that shows real execution cannot cite fixture or simulated data, and a
  DISK case cannot pass without an observed ENOSPC.

Usage::

  python tools/check_release_evidence.py --summary test-artifacts/<run>/SUMMARY.json
  python tools/check_release_evidence.py --summary ... --markdown test-artifacts/<run>/FINAL-REPORT.md
  python tools/check_release_evidence.py --summary ... --report <run>/EVIDENCE-CHECK.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Iterable

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATUS_VALUES = {
    "PASS",
    "FAIL",
    "PARTIAL",
    "NOT_RUN",
    "STALE",
    "INVALIDATED",
    "BLOCKED",
}
REQUIRED_GATES = (
    "G01_function",
    "G02_creation",
    "G03_scale",
    "G04_real_host",
    "G05_real_execution",
    "G06_reliability",
    "G07_delivery",
    "G08_documentation",
)
REAL_DATA_SOURCES = {"real_gpu", "real_cpu", "real_host", "target"}
SIMULATED_DATA_SOURCES = {"fixture", "simulated", "mock", "injected_fixture"}
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SCOPES = ("runtime", "browser_assets", "delivery", "source")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hash(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(files[rel]))
    return digest.hexdigest()


class Report:
    """Collects checks so one run reports every problem, not the first."""

    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.checked: list[dict[str, Any]] = []

    def error(self, code: str, message: str, **details: Any) -> None:
        self.errors.append({"code": code, "message": message, **details})

    def warn(self, code: str, message: str, **details: Any) -> None:
        self.warnings.append({"code": code, "message": message, **details})

    def ok(self, name: str, **details: Any) -> None:
        self.checked.append({"check": name, "result": "ok", **details})

    def result(self) -> dict[str, Any]:
        return {
            "result": "PASS" if not self.errors else "FAIL",
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": self.errors,
            "warnings": self.warnings,
            "checks": self.checked,
        }


def load_json(path: str, report: Report, code: str = "E_JSON") -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        report.error(code, f"文件不存在: {path}", path=path)
    except (OSError, UnicodeDecodeError) as exc:
        report.error(code, f"文件不可读: {path}", path=path, detail=str(exc))
    except json.JSONDecodeError as exc:
        report.error(code, f"JSON 解析失败: {path}", path=path, detail=str(exc))
    return None


def _within(path: str, root: str) -> bool:
    path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


class PathResolver:
    """One declared base per document, resolved against the repository root."""

    def __init__(self, repo_root: str, base: str, evidence_root: str | None) -> None:
        self.repo_root = os.path.abspath(repo_root)
        self.base = base
        self.evidence_root = evidence_root

    @classmethod
    def from_document(
        cls, repo_root: str, document: dict[str, Any], report: Report, label: str
    ) -> "PathResolver":
        base = document.get("path_base") or "repo"
        evidence_root = document.get("evidence_root")
        if base not in ("repo", "evidence_root"):
            report.error(
                "E_PATH_BASE",
                f"{label}: path_base 必须是 repo 或 evidence_root",
                value=base,
            )
            base = "repo"
        if base == "evidence_root":
            if not isinstance(evidence_root, str) or not evidence_root:
                report.error(
                    "E_PATH_BASE",
                    f"{label}: path_base=evidence_root 但未声明 evidence_root",
                )
                evidence_root = None
            elif os.path.isabs(evidence_root):
                report.error(
                    "E_PATH_BASE",
                    f"{label}: evidence_root 必须是仓库相对路径",
                    value=evidence_root,
                )
        elif evidence_root is not None:
            report.error(
                "E_PATH_BASE",
                f"{label}: 声明了 evidence_root 但 path_base 不是 evidence_root，路径基准不唯一",
            )
        return cls(repo_root, base, evidence_root)

    def resolve(self, value: str) -> str:
        if os.path.isabs(value):
            return os.path.abspath(value)
        root = self.repo_root
        if self.base == "evidence_root" and self.evidence_root:
            root = os.path.join(self.repo_root, *self.evidence_root.split("/"))
        return os.path.abspath(os.path.join(root, *value.split("/")))

    def check(self, value: str, report: Report, label: str, expect_dir: bool = False) -> str | None:
        if not isinstance(value, str) or not value:
            report.error("E_PATH", f"{label}: 路径为空或类型错误", value=value)
            return None
        resolved = self.resolve(value)
        if not _within(resolved, self.repo_root):
            report.error(
                "E_PATH_ESCAPE",
                f"{label}: 路径解析到仓库外",
                value=value,
                resolved=resolved,
            )
            return None
        exists = os.path.isdir(resolved) if expect_dir else os.path.exists(resolved)
        if not exists:
            report.error("E_PATH", f"{label}: 路径不存在", value=value, resolved=resolved)
            return None
        return resolved


def _status_tokens(document: dict[str, Any]) -> Iterable[tuple[str, Any]]:
    """Yield (label, status) for every status-like field we recognise."""
    for key in ("overall_result", "result", "status"):
        if key in document:
            yield key, document[key]
    for key in ("gates",):
        for name, value in (document.get(key) or {}).items():
            yield f"gates.{name}", value
    for key in ("work_packages",):
        for name, entry in (document.get(key) or {}).items():
            if isinstance(entry, dict) and "result" in entry:
                yield f"work_packages.{name}.result", entry["result"]
    for key in ("cases", "items", "disk_cases", "checks"):
        container = document.get(key)
        if isinstance(container, dict):
            entries = container.items()
        elif isinstance(container, list):
            entries = (
                (str(index), entry) for index, entry in enumerate(container)
            )
        else:
            continue
        for name, entry in entries:
            if isinstance(entry, dict) and "result" in entry:
                yield f"{key}.{name}.result", entry["result"]


def check_status_enum(document: dict[str, Any], report: Report) -> None:
    for label, value in _status_tokens(document):
        if isinstance(value, str) and value not in STATUS_VALUES:
            report.error(
                "E_STATUS",
                f"{label}: 状态值不在枚举内",
                value=value,
                allowed=sorted(STATUS_VALUES),
            )
    report.ok("status_enum")


def _scope_counts(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: scope
        for name, scope in (document.get("scopes") or {}).items()
        if isinstance(scope, dict)
    }


def check_candidate(
    summary: dict[str, Any],
    resolver: PathResolver,
    report: Report,
) -> dict[str, Any]:
    candidate = summary.get("candidate") or {}
    manifest_path = resolver.check(candidate.get("manifest", ""), report, "candidate.manifest")
    snapshot_path = resolver.check(candidate.get("snapshot", ""), report, "candidate.snapshot")
    if not manifest_path:
        return {}
    manifest = load_json(manifest_path, report, "E_CANDIDATE_JSON")
    if not isinstance(manifest, dict):
        return {}
    manifest_scopes = _scope_counts(manifest)
    for name in SCOPES:
        if name not in manifest_scopes:
            report.error("E_CANDIDATE", f"MANIFEST 缺少 scope: {name}")
    # Recompute the fingerprint from the manifest scopes: a hand-edited
    # fingerprint otherwise hides a changed tree.
    payload = {
        name: {
            "file_count": scope.get("file_count"),
            "tree_sha256": scope.get("tree_sha256"),
        }
        for name, scope in sorted(manifest_scopes.items())
    }
    expected = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if manifest.get("fingerprint") != expected:
        report.error(
            "E_CANDIDATE",
            "MANIFEST 指纹与其 scope 摘要不一致（被手工编辑或清单损坏）",
            declared=manifest.get("fingerprint"),
            recomputed=expected,
        )
    if candidate.get("manifest_fingerprint") != manifest.get("fingerprint"):
        report.error(
            "E_CANDIDATE",
            "SUMMARY 候选指纹与 MANIFEST 不一致",
            summary=candidate.get("manifest_fingerprint"),
            manifest=manifest.get("fingerprint"),
        )
    summary_scopes = candidate.get("scopes") or {}
    for name, scope in manifest_scopes.items():
        files = scope.get("files") or {}
        if len(files) != scope.get("file_count"):
            report.error(
                "E_CANDIDATE",
                f"MANIFEST {name}: file_count 与 files 数量不一致",
                declared=scope.get("file_count"),
                actual=len(files),
            )
        if files and tree_hash(files) != scope.get("tree_sha256"):
            report.error(
                "E_CANDIDATE",
                f"MANIFEST {name}: tree_sha256 与文件哈希不一致",
                declared=scope.get("tree_sha256"),
                actual=tree_hash(files),
            )
        if scope.get("file_count", 0) <= 0:
            report.error("E_CANDIDATE", f"MANIFEST {name}: 文件数为零")
        declared = summary_scopes.get(name) or {}
        if not declared:
            report.error("E_CANDIDATE", f"SUMMARY 缺少候选 scope: {name}")
            continue
        for field in ("file_count", "tree_sha256"):
            if declared.get(field) != scope.get(field):
                report.error(
                    "E_CANDIDATE",
                    f"SUMMARY 与 MANIFEST 的 {name}.{field} 不一致",
                    summary=declared.get(field),
                    manifest=scope.get(field),
                )
    # Archive: declared hash, real bytes, manifest entry and snapshot entry.
    archive = candidate.get("archive") or {}
    archive_path = resolver.check(archive.get("path", ""), report, "candidate.archive.path")
    archive_sha = archive.get("sha256")
    delivery_files = (manifest_scopes.get("delivery") or {}).get("files") or {}
    name = os.path.basename(archive.get("path", "")) if archive.get("path") else ""
    if name and delivery_files.get(name) and delivery_files[name] != archive_sha:
        report.error(
            "E_ARCHIVE",
            "SUMMARY 交付包摘要与交付 scope 内的同名文件不一致（包已改动）",
            declared=archive_sha,
            manifest=delivery_files[name],
        )
    if archive_path:
        actual = sha256_file(archive_path)
        if archive_sha and actual != archive_sha:
            report.error(
                "E_ARCHIVE",
                "交付包实际 SHA-256 与声明不一致（包已改动）",
                declared=archive_sha,
                actual=actual,
            )
        if archive.get("file_count") and delivery_files and archive["file_count"] != len(delivery_files):
            report.error(
                "E_ARCHIVE",
                "交付包文件数与交付 scope 不一致",
                declared=archive["file_count"],
                manifest=len(delivery_files),
            )
    snapshot = load_json(snapshot_path, report, "E_CANDIDATE_JSON") if snapshot_path else None
    if isinstance(snapshot, dict):
        if snapshot.get("manifest_fingerprint") != manifest.get("fingerprint"):
            report.error(
                "E_CANDIDATE",
                "SNAPSHOT 指纹与 MANIFEST 不一致",
                snapshot=snapshot.get("manifest_fingerprint"),
                manifest=manifest.get("fingerprint"),
            )
        snapshot_scopes = snapshot.get("scopes") or {}
        for name, scope in manifest_scopes.items():
            declared = snapshot_scopes.get(name) or {}
            for field in ("file_count", "tree_sha256"):
                if declared.get(field) != scope.get(field):
                    report.error(
                        "E_CANDIDATE",
                        f"SNAPSHOT 与 MANIFEST 的 {name}.{field} 不一致",
                        snapshot=declared.get(field),
                        manifest=scope.get(field),
                    )
        archive_entry = snapshot.get("archive") or {}
        if archive.get("path") and archive_entry.get("path"):
            if os.path.normcase(resolver.resolve(archive_entry["path"])) != os.path.normcase(
                resolver.resolve(archive["path"])
            ):
                report.error("E_ARCHIVE", "SNAPSHOT 交付包路径与 SUMMARY 不一致")
            if archive_entry.get("sha256") != archive_sha:
                report.error(
                    "E_ARCHIVE",
                    "SNAPSHOT 交付包摘要与 SUMMARY 不一致",
                    snapshot=archive_entry.get("sha256"),
                    summary=archive_sha,
                )
        source_archive = snapshot.get("source_archive") or {}
        if not source_archive:
            report.warn(
                "W_SOURCE_ARCHIVE",
                "SNAPSHOT 未记录完整源码包；未跟踪源码无法从补丁与文件名清单单独重建",
            )
        else:
            source_path = resolver.check(
                source_archive.get("path", ""), report, "candidate.source_archive.path"
            )
            if source_path:
                actual = sha256_file(source_path)
                if actual != source_archive.get("sha256"):
                    report.error(
                        "E_ARCHIVE",
                        "完整源码包实际 SHA-256 与声明不一致",
                        declared=source_archive.get("sha256"),
                        actual=actual,
                    )
                source_files = (manifest_scopes.get("source") or {}).get("files") or {}
                if source_archive.get("file_count") and source_archive["file_count"] != len(source_files):
                    report.error(
                        "E_ARCHIVE",
                        "完整源码包文件数与 source scope 不一致",
                        declared=source_archive.get("file_count"),
                        manifest=len(source_files),
                    )
        verification = snapshot.get("verification")
        if not verification:
            report.warn("W_REBUILD", "SNAPSHOT 未记录解包重建校验（--verify-snapshot）")
        elif not verification.get("ok"):
            report.error("E_REBUILD", "SNAPSHOT 记录的解包重建校验未通过")
    report.ok("candidate_identity")
    return manifest


def check_regression(summary: dict[str, Any], resolver: PathResolver, report: Report) -> None:
    regression = summary.get("regression") or {}
    if not regression:
        report.error("E_REGRESSION", "SUMMARY 缺少 regression 段")
        return
    python_tests = regression.get("python_tests")
    js_tests = regression.get("js_tests")
    if not isinstance(python_tests, int) or python_tests <= 0:
        report.error("E_REGRESSION", "Python 测试数为零或缺失", value=python_tests)
    if not isinstance(js_tests, int) or js_tests <= 0:
        report.error("E_REGRESSION", "JS 测试数为零或缺失", value=js_tests)
    failures = sum(
        int(regression.get(key) or 0)
        for key in ("python_failures", "python_errors", "js_fail")
    )
    claimed = summary.get("overall_result")
    if failures and claimed in ("PASS", None):
        report.error(
            "E_REGRESSION",
            "存在失败用例却声明整体 PASS",
            failures=failures,
            overall_result=claimed,
        )
    if failures and summary.get("production_ready"):
        report.error("E_REGRESSION", "存在失败用例却设置 production_ready=true")
    log = regression.get("log")
    if log:
        resolved = resolver.check(log, report, "regression.log", expect_dir=os.path.splitext(str(log))[1] == "")
        if resolved and os.path.isdir(resolved):
            if not any(name.startswith("REGRESSION") for name in os.listdir(resolved)):
                report.warn("W_REGRESSION_LOG", "回归日志目录没有 REGRESSION.json", path=log)
    report.ok("regression_counts")


def check_gates(summary: dict[str, Any], report: Report) -> None:
    gates = summary.get("gates") or {}
    for name in REQUIRED_GATES:
        if name not in gates:
            report.error("E_GATE", f"缺少门槛: {name}")
    unmet = {name: value for name, value in gates.items() if value != "PASS"}
    packages = summary.get("work_packages") or {}
    unfinished = {
        name: entry.get("result")
        for name, entry in packages.items()
        if isinstance(entry, dict) and entry.get("result") in ("FAIL", "NOT_RUN", "PARTIAL", "STALE")
    }
    blocking = summary.get("blocking_items") or []
    ready = summary.get("production_ready")
    if ready is True:
        if unmet:
            report.error(
                "E_GATE",
                "production_ready=true 但存在未通过门槛",
                unmet=unmet,
            )
        if unfinished:
            report.error(
                "E_GATE",
                "production_ready=true 但存在未完成工作包",
                unfinished=unfinished,
            )
        if blocking:
            report.error(
                "E_GATE",
                "production_ready=true 但仍登记了阻塞项",
                blocking=[entry.get("item") if isinstance(entry, dict) else entry for entry in blocking],
            )
        if not summary.get("support_statement"):
            report.error("E_GATE", "production_ready=true 但缺少支持范围声明")
        for key in ("target_identity",):
            if not summary.get(key):
                report.error("E_GATE", f"production_ready=true 但缺少 {key}")
    if summary.get("overall_result") == "PASS" and unmet:
        report.error(
            "E_GATE",
            "overall_result=PASS 但门槛未全部通过",
            unmet=unmet,
        )
    if ready is True and summary.get("overall_result") != "PASS":
        report.error(
            "E_GATE",
            "production_ready=true 与 overall_result 不一致",
            overall_result=summary.get("overall_result"),
        )
    report.ok("gates", production_ready=ready, unmet=sorted(unmet))


def check_provenance(summary: dict[str, Any], resolver: PathResolver, report: Report) -> None:
    provenance = summary.get("data_provenance") or {}
    real = [str(item) for item in provenance.get("real_gpu") or []]
    fixture = [str(item) for item in provenance.get("fixture") or []]
    for item in real:
        if any(token in item.casefold() for token in ("fixture", "simulated", "mock")):
            report.error("E_PROVENANCE", "真实数据列表包含夹具/模拟条目", item=item)
    for item in fixture:
        if item in real:
            report.error("E_PROVENANCE", "同一条目同时记为真实与夹具", item=item)
    cases = summary.get("cases") or {}
    entries = cases.items() if isinstance(cases, dict) else enumerate(cases)
    for name, entry in entries:
        if not isinstance(entry, dict):
            continue
        case_id = str(entry.get("id") or name)
        source = str(entry.get("data_source") or "").casefold()
        claims_real = bool(entry.get("claims_real_execution"))
        if claims_real and source in SIMULATED_DATA_SOURCES:
            report.error(
                "E_PROVENANCE",
                f"{case_id}: 声明真实执行但数据来源为 {source}",
                case_id=case_id,
            )
        if entry.get("counts_toward_production") and source in SIMULATED_DATA_SOURCES:
            report.error(
                "E_PROVENANCE",
                f"{case_id}: 夹具数据不能计入生产门槛",
                case_id=case_id,
                data_source=source,
            )
        if case_id.upper().startswith("DISK") and entry.get("result") == "PASS":
            if entry.get("enospc_observed") is not True:
                report.error(
                    "E_PROVENANCE",
                    f"{case_id}: DISK PASS 必须记录真实 ENOSPC 观测",
                    case_id=case_id,
                )
            if source and source not in REAL_DATA_SOURCES:
                report.error(
                    "E_PROVENANCE",
                    f"{case_id}: DISK PASS 的数据来源必须是真实业务写入",
                    case_id=case_id,
                    data_source=source,
                )
        evidence = entry.get("evidence") or []
        if entry.get("result") == "PASS" and not evidence:
            report.error("E_PROVENANCE", f"{case_id}: PASS 用例没有证据路径", case_id=case_id)
        for item in evidence:
            resolver.check(item, report, f"cases.{case_id}.evidence")
    report.ok("provenance", real=len(real), fixture=len(fixture))


def check_markdown(path: str, resolver: PathResolver, report: Report) -> None:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        report.error("E_MARKDOWN", f"报告不可读: {path}", detail=str(exc))
        return
    base_dir = os.path.dirname(os.path.abspath(path))
    count = 0
    for match in MARKDOWN_LINK.finditer(text):
        target = match.group(1).strip()
        if not target or target.startswith(("#", "http://", "https://", "mailto:", "data:")):
            continue
        target = target.split("#", 1)[0]
        if not target:
            continue
        decoded = urllib.parse.unquote(target)
        if os.path.isabs(decoded):
            resolved = os.path.abspath(decoded)
        else:
            resolved = os.path.abspath(os.path.join(base_dir, *decoded.split("/")))
        count += 1
        if not os.path.exists(resolved):
            report.error(
                "E_MARKDOWN",
                "报告中链接指向不存在的路径",
                file=os.path.relpath(path, resolver.repo_root),
                link=target,
                resolved=resolved,
            )
    report.ok("markdown_links", file=os.path.relpath(path, resolver.repo_root), link_count=count)


def check_summary(
    repo_root: str,
    summary_path: str,
    markdown: list[str] | None = None,
) -> dict[str, Any]:
    report = Report()
    summary_path = os.path.abspath(summary_path)
    repo_root = os.path.abspath(repo_root)
    summary = load_json(summary_path, report)
    if not isinstance(summary, dict):
        return {
            "schema_version": 1,
            "generated_at": utc_now(),
            "repo_root": repo_root,
            "summary": summary_path,
            **report.result(),
        }
    if summary.get("schema_version") != 1:
        report.error("E_SCHEMA", "SUMMARY schema_version 不是 1", value=summary.get("schema_version"))
    resolver = PathResolver.from_document(repo_root, summary, report, "SUMMARY")
    check_status_enum(summary, report)
    check_candidate(summary, resolver, report)
    check_regression(summary, resolver, report)
    check_gates(summary, report)
    check_provenance(summary, resolver, report)
    markdown_paths = list(markdown or [])
    reported = summary.get("report")
    if isinstance(reported, str) and reported and reported not in markdown_paths:
        markdown_paths.append(reported)
    for value in markdown_paths:
        resolved = resolver.check(value, report, "report")
        if resolved:
            check_markdown(resolved, resolver, report)
    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "repo_root": repo_root,
        "summary": summary_path,
        "path_base": resolver.base,
        "evidence_root": resolver.evidence_root,
        **report.result(),
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check release evidence consistency")
    parser.add_argument("--repo-root", default=PROJECT_ROOT)
    parser.add_argument("--summary", required=True)
    parser.add_argument(
        "--markdown",
        action="append",
        default=[],
        help="额外检查该 markdown 文件的链接（可重复）",
    )
    parser.add_argument("--report", default=None, help="把检查结果写入该 JSON 文件")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    payload = check_summary(args.repo_root, args.summary, args.markdown)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
    if not args.quiet:
        print(text)
    return 0 if payload["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
