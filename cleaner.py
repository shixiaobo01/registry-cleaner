#!/usr/bin/env python3
"""Prune old Docker Distribution tags safely, then run registry GC separately.

The Distribution API deletes manifests by digest, not individual tag names.
Therefore this program never deletes a digest when any of its tags is retained.
This may keep more than KEEP_LAST displayed tags, but prevents accidental loss.
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import logging
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import config
from registry_client import RegistryClient, RegistryError

UNKNOWN_CREATED = datetime.max.replace(tzinfo=timezone.utc)

JSON_CONFIG_FIELDS = {
    "registry_url": ("REGISTRY_URL", str),
    "username": ("USERNAME", str),
    "password_env": ("PASSWORD_ENV", str),
    "verify_tls": ("VERIFY_TLS", (bool, str)),
    "keep_last": ("KEEP_LAST", int),
    "fallback_to_last_modified": ("FALLBACK_TO_LAST_MODIFIED", bool),
    "skip_repository_prefixes": ("SKIP_REPOSITORY_PREFIXES", list),
    "skip_repositories": ("SKIP_REPOSITORIES", list),
    "protected_tag_patterns": ("PROTECTED_TAG_PATTERNS", list),
    "repository_workers": ("REPOSITORY_WORKERS", int),
    "tag_workers": ("TAG_WORKERS", int),
}


@dataclass(frozen=True)
class TagInfo:
    tag: str
    digest: str
    created: datetime
    created_text: str
    protected: bool
    error: str = ""


class State:
    def __init__(self, checkpoint: Path, csv_path: Path):
        self.checkpoint = checkpoint
        self.lock = threading.Lock()
        self.completed: Set[str] = set()
        if checkpoint.exists():
            try:
                data = json.loads(checkpoint.read_text(encoding="utf-8"))
                self.completed = set(data.get("completed_repositories", []))
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError("Cannot read checkpoint %s: %s" % (checkpoint, exc))
        new_file = not csv_path.exists()
        self.csv_handle = csv_path.open("a", newline="", encoding="utf-8")
        self.csv = csv.writer(self.csv_handle)
        if new_file:
            self.csv.writerow(("timestamp_utc", "repository", "tag", "digest", "created", "action", "detail"))
            self.csv_handle.flush()

    def action(self, repo: str, tag: str, digest: str, created: str, action: str, detail="") -> None:
        with self.lock:
            self.csv.writerow((datetime.now(timezone.utc).isoformat(), repo, tag, digest, created, action, detail))
            self.csv_handle.flush()

    def done(self, repository: str) -> None:
        with self.lock:
            self.completed.add(repository)
            tmp = self.checkpoint.with_suffix(self.checkpoint.suffix + ".tmp")
            tmp.write_text(json.dumps({"completed_repositories": sorted(self.completed)}, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.checkpoint)

    def close(self) -> None:
        self.csv_handle.close()


def normalize_verify_tls(value: object) -> object:
    """Accept bool, CA path string, or common truthy/falsey strings from JSON."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"false", "0", "no", "off"}:
            return False
        if lowered in {"true", "1", "yes", "on"}:
            return True
        return value
    raise TypeError("verify_tls must be bool or str")


def load_json_config(path: Path) -> None:
    """Apply customer-safe external JSON configuration to built-in defaults."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Cannot read JSON configuration %s: %s" % (path, exc)) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("JSON configuration %s must contain an object" % path)
    unknown = set(payload) - set(JSON_CONFIG_FIELDS)
    if unknown:
        raise RuntimeError("Unknown JSON configuration key(s): %s" % ", ".join(sorted(unknown)))
    for key, value in payload.items():
        attribute, expected = JSON_CONFIG_FIELDS[key]
        if not isinstance(value, expected):
            expected_name = "/".join(t.__name__ for t in expected) if isinstance(expected, tuple) else expected.__name__
            raise RuntimeError("Configuration %r must be %s" % (key, expected_name))
        if attribute == "VERIFY_TLS":
            value = normalize_verify_tls(value)
        elif attribute == "SKIP_REPOSITORIES":
            value = set(value)
        elif attribute in {"SKIP_REPOSITORY_PREFIXES", "PROTECTED_TAG_PATTERNS"}:
            value = tuple(value)
        setattr(config, attribute, value)


def parse_created(value: object) -> Optional[datetime]:
    """Parse RFC 3339 image config timestamps, including nanoseconds."""
    if not isinstance(value, str) or not value:
        return None
    try:
        # Docker image configs often use 9 fractional-second digits while
        # Python 3.9 can only represent microseconds (6 digits).
        normalized = value.replace("Z", "+00:00")
        normalized = re.sub(r"(\.\d{6})\d+(?=[+-]\d{2}:\d{2}$)", r"\1", normalized)
        return datetime.fromisoformat(normalized).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_http_date(value: object) -> Optional[datetime]:
    """Parse an RFC 7231 Last-Modified header into UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def protected(tag: str) -> bool:
    return any(fnmatch.fnmatchcase(tag, pattern) for pattern in config.PROTECTED_TAG_PATTERNS)


def tag_info(client: RegistryClient, repository: str, tag: str) -> TagInfo:
    try:
        digest, manifest, _ = client.manifest(repository, tag)
        config_desc = manifest.get("config", {}) if isinstance(manifest, dict) else {}
        config_digest = config_desc.get("digest") if isinstance(config_desc, dict) else None
        if not isinstance(config_digest, str):
            # A manifest list/index has no image config. Keeping it is safer than guessing.
            return TagInfo(tag, digest, UNKNOWN_CREATED, "", protected(tag), "no image config (index/list or unsupported manifest)")
        config_json, last_modified = client.blob_json(repository, config_digest)
        created_text = config_json.get("created", "")
        created = parse_created(created_text)
        if created:
            return TagInfo(tag, digest, created, created_text, protected(tag))
        if config.FALLBACK_TO_LAST_MODIFIED:
            fallback = parse_http_date(last_modified)
            if fallback:
                # Preserve the source in the audit CSV so every fallback deletion
                # is obvious and reviewable.
                return TagInfo(tag, digest, fallback, "Last-Modified: " + last_modified, protected(tag))
        detail = "missing or invalid config created time"
        if config.FALLBACK_TO_LAST_MODIFIED:
            detail += "; Registry returned no usable Last-Modified time"
        return TagInfo(tag, digest, UNKNOWN_CREATED, str(created_text), protected(tag), detail)
    except RegistryError as exc:
        return TagInfo(tag, "", UNKNOWN_CREATED, "", protected(tag), str(exc))


def should_skip(repository: str) -> bool:
    return repository in config.SKIP_REPOSITORIES or repository.startswith(tuple(config.SKIP_REPOSITORY_PREFIXES))


def process_repository(client: RegistryClient, state: State, repository: str, args) -> Dict[str, int]:
    log = logging.getLogger(__name__)
    if should_skip(repository):
        log.info("SKIP namespace/exact rule: %s", repository)
        return {"skipped": 1}
    if args.resume and repository in state.completed:
        log.info("RESUME skip completed: %s", repository)
        return {"resumed": 1}
    try:
        tags = list(client.tags(repository, config.TAGS_PAGE_SIZE))
    except RegistryError as exc:
        log.error("%s: cannot list tags: %s", repository, exc)
        return {"failed": 1}
    if len(tags) <= args.keep:
        log.info("%s: %d tag(s), nothing to prune", repository, len(tags))
        state.done(repository)
        return {"completed": 1, "tags": len(tags)}

    infos: List[TagInfo] = []
    with ThreadPoolExecutor(max_workers=args.tag_workers, thread_name_prefix="tag") as pool:
        futures = [pool.submit(tag_info, client, repository, tag) for tag in tags]
        for future in as_completed(futures):
            infos.append(future.result())

    # Unknown metadata/errors are retained intentionally: a failure must not turn into deletion.
    candidates = sorted((x for x in infos if not x.protected and not x.error), key=lambda x: (x.created, x.tag), reverse=True)
    retained: Set[str] = {x.tag for x in infos if x.protected or x.error}
    retained.update(x.tag for x in candidates[:args.keep])
    by_digest: Dict[str, List[TagInfo]] = {}
    for info in infos:
        if info.digest:
            by_digest.setdefault(info.digest, []).append(info)
    deleted = 0
    would_delete = 0
    errors = sum(1 for x in infos if x.error)
    for digest, aliases in by_digest.items():
        alias_tags = {x.tag for x in aliases}
        if alias_tags & retained:
            # Distribution deletes the entire digest's tag links; do not delete shared aliases.
            for info in aliases:
                if info.tag not in retained:
                    state.action(repository, info.tag, digest, info.created_text, "retained_shared_digest", "another tag using this digest is retained")
            continue
        # All aliases are beyond retention. One DELETE removes this digest and every alias.
        display_tags = ",".join(sorted(alias_tags))
        representative = aliases[0]
        if args.delete:
            try:
                client.delete_manifest(repository, digest)
                action = "deleted"
                deleted += len(aliases)
                log.info("DELETED %s: %s", repository, display_tags)
            except RegistryError as exc:
                action = "delete_failed"
                errors += len(aliases)
                log.error("DELETE FAILED %s (%s): %s", repository, digest, exc)
                for info in aliases:
                    state.action(repository, info.tag, digest, info.created_text, action, str(exc))
                continue
        else:
            action = "would_delete"
            would_delete += len(aliases)
            log.info("DRY-RUN %s: would delete %s", repository, display_tags)
        for info in aliases:
            state.action(repository, info.tag, digest, info.created_text, action)
    for info in infos:
        if info.error:
            state.action(repository, info.tag, info.digest, info.created_text, "retained_metadata_error", info.error)
            log.warning("%s:%s retained: %s", repository, info.tag, info.error)
    state.done(repository)
    log.info("%s: tags=%d delete=%d%s errors=%d", repository, len(tags), deleted, (" would_delete=" + str(would_delete)) if not args.delete else "", errors)
    return {"completed": 1, "tags": len(tags), "deleted": deleted, "would_delete": would_delete, "errors": errors}


def setup_logging(log_file: Path, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(threadName)s %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.handlers[:] = [stream, file_handler]


def output_base(args) -> Path:
    """Directory for logs, CSV exports, and checkpoints.

    PyInstaller sets __file__ under _internal/, so never use that path for outputs.
    Prefer the config file directory; otherwise use the current working directory.
    """
    if args.config:
        return Path(args.config).expanduser().resolve().parent
    return Path.cwd()


def write_tag_report(client: RegistryClient, repositories: List[str], args, output: Path) -> int:
    """Write a lightweight inventory without fetching manifests or deleting data."""
    log = logging.getLogger(__name__)

    def count(repository: str):
        try:
            return repository, len(list(client.tags(repository, config.TAGS_PAGE_SIZE))), ""
        except RegistryError as exc:
            return repository, 0, str(exc)

    rows = []
    with ThreadPoolExecutor(max_workers=args.repository_workers, thread_name_prefix="report") as pool:
        for future in as_completed([pool.submit(count, repo) for repo in repositories]):
            rows.append(future.result())
    rows.sort(key=lambda row: (-row[1], row[0]))
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("repository", "tag_count", "error"))
        writer.writerows(rows)
    failures = sum(1 for _, _, error in rows if error)
    log.info("Tag report written to %s: repositories=%d failures=%d", output, len(rows), failures)
    for repository, count_value, error in rows[:20]:
        log.info("TAG-COUNT %7d  %s%s", count_value, repository, ("  ERROR: " + error) if error else "")
    return 1 if failures else 0


def arguments():
    p = argparse.ArgumentParser(description="Safely prune old Docker Registry manifests. Default is dry-run.")
    p.add_argument("--config", metavar="FILE", help="External JSON configuration file (recommended for customer delivery).")
    p.add_argument("--registry-url", help="Override Registry URL from configuration.")
    p.add_argument("--username", help="Override Registry username from configuration.")
    p.add_argument("--password-env", help="Environment variable containing the password (default: REGISTRY_PASSWORD).")
    p.add_argument("--delete", action="store_true", help="Actually DELETE eligible manifests (default only records would_delete).")
    p.add_argument("--resume", action="store_true", help="Skip repositories marked complete in checkpoint.json.")
    p.add_argument("--keep", type=int, help="Number of newest normal tags to retain per repository.")
    p.add_argument("--repository", action="append", help="Only process this exact repository; repeatable.")
    p.add_argument("--repository-prefix", action="append", help="Only process repositories beginning with this prefix; repeatable (for example arm64/).")
    p.add_argument("--report-tags", action="store_true", help="Only write registry-tag-counts.csv, sorted by tag count; no manifests are fetched or deleted.")
    p.add_argument("--repository-workers", type=int, help="Concurrent repository workers.")
    p.add_argument("--tag-workers", type=int, help="Concurrent tag metadata workers per repository.")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = arguments()
    if args.config:
        try:
            load_json_config(Path(args.config).expanduser())
        except RuntimeError as exc:
            raise SystemExit(str(exc))
    if args.registry_url:
        config.REGISTRY_URL = args.registry_url
    if args.username:
        config.USERNAME = args.username
    password_env = args.password_env or config.PASSWORD_ENV
    password = os.environ.get(password_env, "")
    args.keep = config.KEEP_LAST if args.keep is None else args.keep
    args.repository_workers = config.REPOSITORY_WORKERS if args.repository_workers is None else args.repository_workers
    args.tag_workers = config.TAG_WORKERS if args.tag_workers is None else args.tag_workers
    if args.keep < 0 or args.repository_workers < 1 or args.tag_workers < 1:
        raise SystemExit("--keep must be >= 0 and worker counts must be >= 1")
    base = output_base(args)
    setup_logging(base / config.LOG_FILE, args.verbose)
    log = logging.getLogger(__name__)
    tag_counts_path = base / config.TAG_COUNTS_FILE
    if not password:
        log.warning("%s is empty. Set it in the environment before running if authentication is required.", password_env)
    client = RegistryClient(config.REGISTRY_URL, config.USERNAME, password,
                            verify=config.VERIFY_TLS, timeout=config.REQUEST_TIMEOUT_SECONDS,
                            retries=config.RETRY_ATTEMPTS, backoff=config.RETRY_BACKOFF_SECONDS)
    try:
        client.ping()
        repositories = args.repository or list(client.repositories(config.CATALOG_PAGE_SIZE))
    except RegistryError as exc:
        log.error("Cannot connect/list catalog: %s", exc)
        return 2
    if args.repository_prefix:
        prefixes = tuple(args.repository_prefix)
        repositories = [repo for repo in repositories if repo.startswith(prefixes)]
    if args.report_tags:
        return write_tag_report(client, repositories, args, tag_counts_path)
    write_tag_report(client, repositories, args, tag_counts_path)
    state = State(base / config.CHECKPOINT_FILE, base / config.CSV_FILE)
    totals: Dict[str, int] = {}
    try:
        log.info("Mode=%s repositories=%d keep=%d", "DELETE" if args.delete else "DRY-RUN", len(repositories), args.keep)
        with ThreadPoolExecutor(max_workers=args.repository_workers, thread_name_prefix="repo") as pool:
            futures = [pool.submit(process_repository, client, state, repo, args) for repo in repositories]
            for future in as_completed(futures):
                result = future.result()
                for key, value in result.items():
                    totals[key] = totals.get(key, 0) + value
    finally:
        state.close()
    log.info("FINISHED %s", " ".join("%s=%s" % item for item in sorted(totals.items())))
    if args.delete:
        log.warning("Manifest deletes are complete. Disk space is not released until registry garbage-collect runs in a controlled maintenance window.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
