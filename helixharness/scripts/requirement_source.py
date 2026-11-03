#!/usr/bin/env python3
"""Resolve requirement sources, including DingTalk documents read through dws.

The script never calls the network itself: it classifies a source, plans the
exact ``dws`` argv list, maps a failed command to a stable reason code, and
ingests captured output into the private snapshot directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atomic_files import AtomicWriteError, atomic_write_text
from workflow import WorkflowError, feature_name


SNAPSHOT_ROOT = Path(".helixharness") / "sources"
DINGTALK_URL = re.compile(
    r"^https://alidocs\.dingtalk\.com/i/nodes/([A-Za-z0-9]{8,64})(?:[?#].*)?$"
)
CANONICAL_DINGTALK = "https://alidocs.dingtalk.com/i/nodes/{node_id}"
HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)
PATH_LIKE = re.compile(r"^[^\s]+\.(?:md|markdown|txt|adoc)$", re.IGNORECASE)
MAX_MARKDOWN_BYTES = 512 * 1024
COMMENT_LIMIT = 20
COMMAND_TIMEOUT_SECONDS = 30
DWS_VERSION_PATTERN = re.compile(r"dws\s+version\s+(\S+)", re.IGNORECASE)

REASONS = frozenset(
    {
        "requirement_source_url_invalid",
        "requirement_source_dws_missing",
        "requirement_source_auth_required",
        "requirement_source_not_found",
        "requirement_source_forbidden",
        "requirement_source_unsupported_type",
        "requirement_source_too_large",
        "requirement_source_unreadable",
        "requirement_source_path_missing",
        "requirement_source_snapshot_unsafe",
    }
)

_UNSAFE_STDERR = (
    (re.compile(r"not\s+logged\s+in|login\s+required|unauthorized|invalid\s+token", re.I),
     "requirement_source_auth_required", "401"),
    (re.compile(r"token\s+(?:is\s+)?(?:expired|invalid)|re-?authoriz", re.I),
     "requirement_source_auth_required", "401"),
    (re.compile(r"forbidden|permission\s+denied|no\s+permission|access\s+denied", re.I),
     "requirement_source_forbidden", "403"),
    (re.compile(r"not\s+found|no\s+such|不存在|已被删除", re.I),
     "requirement_source_not_found", "404"),
)


class SourceError(ValueError):
    """A source cannot be used as requirement facts."""

    def __init__(self, reason: str, detail: str = "") -> None:
        if reason not in REASONS:
            reason = "requirement_source_unreadable"
        self.reason = reason
        self.detail = detail
        super().__init__(reason if not detail else f"{reason}: {detail}")


def classify_source(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        raise SourceError("requirement_source_unreadable", "empty source")
    match = DINGTALK_URL.fullmatch(text)
    if match:
        node_id = match.group(1)
        return {
            "kind": "dingtalk_doc",
            "normalized": CANONICAL_DINGTALK.format(node_id=node_id),
            "node_id": node_id,
        }
    if HTTP_URL.match(text):
        if "alidocs.dingtalk.com" in text:
            raise SourceError(
                "requirement_source_url_invalid",
                "expected https://alidocs.dingtalk.com/i/nodes/{id}",
            )
        return {"kind": "url", "normalized": text}
    candidate = Path(text).expanduser()
    try:
        if candidate.is_file():
            return {"kind": "file", "normalized": str(candidate.resolve())}
    except OSError as exc:
        raise SourceError("requirement_source_unreadable", "path is unreadable") from exc
    if PATH_LIKE.match(text) and "/" in text:
        raise SourceError("requirement_source_path_missing", "source file is missing")
    return {"kind": "text", "normalized": text}


def _dws_argv(kind: str, normalized: str) -> list[list[dict[str, Any]]]:
    timeout = ["--timeout", str(COMMAND_TIMEOUT_SECONDS)]
    return [
        {"capture": "version", "argv": ["dws", "--version"]},
        {"capture": "auth", "argv": ["dws", "auth", "status", "-f", "json", *timeout]},
        {
            "capture": "info",
            "argv": ["dws", "doc", "info", "--node", normalized, "-f", "json", *timeout],
        },
        {
            "capture": "read",
            "argv": ["dws", "doc", "read", "--node", normalized, "-f", "json", *timeout],
        },
        {
            "capture": "comments",
            "argv": [
                "dws", "-f", "json", *timeout, "doc", "comment", "list",
                "--node", normalized, "--limit", str(COMMENT_LIMIT),
            ],
        },
    ]


def plan_source(raw: str, *, repo: Path, feature: str) -> dict[str, Any]:
    name = feature_name(str(feature or "").strip())
    classified = classify_source(raw)
    snapshot = repo.expanduser().resolve() / SNAPSHOT_ROOT / name
    result: dict[str, Any] = {
        "kind": classified["kind"],
        "normalized": classified["normalized"],
        "feature": name,
        "snapshot_dir": str(snapshot),
        "limits": {
            "max_markdown_bytes": MAX_MARKDOWN_BYTES,
            "comment_limit": COMMENT_LIMIT,
            "timeout_seconds": COMMAND_TIMEOUT_SECONDS,
        },
    }
    if classified["kind"] == "dingtalk_doc":
        result["node_id"] = classified["node_id"]
        result["steps"] = _dws_argv(classified["kind"], classified["normalized"])
    else:
        result["steps"] = []
    return result


def utc_now() -> str:
    """Return a stable UTC timestamp for snapshot provenance."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_doc_url(value: str, node_id: str) -> str:
    """Normalise a DingTalk document URL: strip tracking query and fragment."""

    if node_id and DINGTALK_URL.fullmatch(value or ""):
        return CANONICAL_DINGTALK.format(node_id=node_id)
    if node_id and not value:
        return CANONICAL_DINGTALK.format(node_id=node_id)
    return (value or "").split("?", 1)[0].split("#", 1)[0]


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError(
            "requirement_source_unreadable", f"{label} output is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise SourceError(
            "requirement_source_unreadable", f"{label} output is not an object"
        )
    return payload


def _require_success(payload: dict[str, Any], label: str) -> None:
    if payload.get("success") is False:
        raise SourceError(
            "requirement_source_unreadable", f"{label} reported success=false"
        )


def _extension_label(info: dict[str, Any]) -> str:
    for key in ("extension", "docType", "contentType"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _safe_snapshot_dir(repo: Path, feature: str) -> Path:
    root = repo.expanduser().resolve()
    directory = root / SNAPSHOT_ROOT / feature
    current = root
    for part in (SNAPSHOT_ROOT.parts + (feature,)):
        current = current / part
        if os.path.lexists(current) and (current.is_symlink() or not current.is_dir()):
            raise SourceError(
                "requirement_source_snapshot_unsafe", "snapshot path is unsafe"
            )
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SourceError(
            "requirement_source_snapshot_unsafe", "snapshot directory is unavailable"
        ) from exc
    return directory


def _dws_version(version_file: Path | None) -> str:
    if version_file is None or not version_file.is_file():
        return ""
    try:
        text = version_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = DWS_VERSION_PATTERN.search(text)
    return match.group(1) if match else ""


def ingest_source(
    *,
    repo: Path,
    feature: str,
    info_file: Path,
    read_file: Path,
    comments_file: Path,
    version_file: Path | None = None,
    fetched_at: str | None = None,
    cleanup_inputs: bool = True,
) -> dict[str, Any]:
    name = feature_name(str(feature or "").strip())
    info = _load_json(info_file, "info")
    read = _load_json(read_file, "read")
    comments = _load_json(comments_file, "comments")
    for payload, label in ((info, "info"), (read, "read"), (comments, "comments")):
        _require_success(payload, label)

    extension = _extension_label(info)
    if extension and extension.casefold() not in {"adoc", "alidoc"}:
        raise SourceError("requirement_source_unsupported_type", extension)

    markdown = read.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise SourceError("requirement_source_unreadable", "empty document markdown")
    encoded = markdown.encode("utf-8")
    if len(encoded) > MAX_MARKDOWN_BYTES:
        raise SourceError(
            "requirement_source_too_large",
            f"{len(encoded)} bytes > {MAX_MARKDOWN_BYTES}",
        )

    raw_comments = comments.get("commentList")
    comment_list = raw_comments if isinstance(raw_comments, list) else []
    node_id = str(
        read.get("nodeId") or info.get("nodeId") or info.get("id") or ""
    ).strip()
    title = str(read.get("title") or info.get("name") or "").strip()
    url = canonical_doc_url(
        str(read.get("docUrl") or info.get("docUrl") or "").strip(), node_id
    )

    directory = _safe_snapshot_dir(repo, name)
    digest = hashlib.sha256(encoded).hexdigest()
    try:
        atomic_write_text(directory / "document.md", markdown)
        atomic_write_text(
            directory / "comments.json",
            json.dumps(comments, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        manifest = {
            "schema_version": "1.0",
            "feature": name,
            "source_type": "dingtalk_doc",
            "url": url,
            "node_id": node_id,
            "title": title,
            "fetched_at": str(fetched_at or "").strip() or utc_now(),
            "sha256": digest,
            "bytes": len(encoded),
            "comment_count": len(comment_list),
            "comments_truncated": bool(comments.get("hasMore")),
            "dws_version": _dws_version(version_file),
        }
        atomic_write_text(
            directory / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    except (AtomicWriteError, OSError) as exc:
        raise SourceError(
            "requirement_source_snapshot_unsafe", "snapshot write failed"
        ) from exc

    _discard_captures(
        [info_file, read_file, comments_file, version_file] if cleanup_inputs else []
    )
    return {
        "ok": True,
        "kind": "dingtalk_doc",
        "feature": name,
        "title": title,
        "node_id": node_id,
        "url": url,
        "sha256": digest,
        "bytes": len(encoded),
        "comment_count": len(comment_list),
        "comments_truncated": bool(comments.get("hasMore")),
        "dws_version": manifest["dws_version"],
        "snapshot_dir": str(directory),
        "document_path": str(directory / "document.md"),
    }


def _discard_captures(paths: list[Path | None]) -> None:
    """Remove transient dws capture files once the snapshot is durable."""

    for path in paths:
        if path is None:
            continue
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except OSError:
            continue


def diagnose_failure(stderr: str, exit_code: int = 0) -> dict[str, Any]:
    text = str(stderr or "")
    for pattern, reason, marker in _UNSAFE_STDERR:
        if marker in text or pattern.search(text):
            return {"reason": reason, "detail": text.strip()[:400]}
    if exit_code == 127:
        return {"reason": "requirement_source_dws_missing", "detail": "dws is not on PATH"}
    return {"reason": "requirement_source_unreadable", "detail": text.strip()[:400]}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    classify = sub.add_parser("classify", help="classify a raw requirement source")
    classify.add_argument("--source", required=True)

    plan = sub.add_parser("plan", help="emit the deterministic read plan")
    plan.add_argument("--source", required=True)
    plan.add_argument("--feature", required=True)
    plan.add_argument("--repo-root", type=Path, default=Path.cwd())

    ingest = sub.add_parser("ingest", help="store captured dws output as a snapshot")
    ingest.add_argument("--feature", required=True)
    ingest.add_argument("--repo-root", type=Path, default=Path.cwd())
    ingest.add_argument("--info-file", type=Path, required=True)
    ingest.add_argument("--read-file", type=Path, required=True)
    ingest.add_argument("--comments-file", type=Path, required=True)
    ingest.add_argument("--version-file", type=Path)
    ingest.add_argument("--fetched-at", default="")
    ingest.add_argument(
        "--keep-inputs",
        action="store_true",
        help="keep the transient dws capture files instead of deleting them",
    )

    diagnose = sub.add_parser("diagnose", help="map a failed dws command to a reason")
    diagnose.add_argument("--stderr-file", type=Path)
    diagnose.add_argument("--exit-code", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "classify":
            result = {"ok": True, **classify_source(arguments.source)}
        elif arguments.command == "plan":
            result = {"ok": True, **plan_source(
                arguments.source, repo=arguments.repo_root, feature=arguments.feature
            )}
        elif arguments.command == "ingest":
            result = ingest_source(
                repo=arguments.repo_root,
                feature=arguments.feature,
                info_file=arguments.info_file,
                read_file=arguments.read_file,
                comments_file=arguments.comments_file,
                version_file=arguments.version_file,
                fetched_at=arguments.fetched_at,
                cleanup_inputs=not arguments.keep_inputs,
            )
        else:
            stderr = ""
            if arguments.stderr_file is not None and arguments.stderr_file.is_file():
                stderr = arguments.stderr_file.read_text(encoding="utf-8", errors="replace")
            result = {"ok": False, **diagnose_failure(stderr, arguments.exit_code)}
    except SourceError as exc:
        print(json.dumps(
            {"ok": False, "reason": exc.reason, "detail": exc.detail},
            ensure_ascii=False, sort_keys=True,
        ))
        return 2
    except (WorkflowError, OSError) as exc:
        print(json.dumps(
            {"ok": False, "reason": "requirement_source_unreadable", "detail": str(exc)},
            ensure_ascii=False, sort_keys=True,
        ))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
