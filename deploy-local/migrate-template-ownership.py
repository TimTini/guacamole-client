#!/usr/bin/env python3
"""One-time, read-only-audited owner migration for the retained v1 template.

This file is intentionally fixed to windows11-v1.  It performs every read-only
check before changing ownership, changes only the template inode owner, and
repeats the hash, metadata, backing, inventory, and dependent-clone checks
afterwards.  It never writes inventory or touches a domain.
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


VERSION = "windows11-v1"
TEMPLATE_ROOT = Path("/var/lib/guacamole-templates")
TEMPLATE_PATH = TEMPLATE_ROOT / f"{VERSION}.qcow2"
INVENTORY_PATH = TEMPLATE_ROOT / "inventory.json"
CLONE_ROOT = Path("/var/lib/guacamole-vms")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def fail(message: str) -> None:
    raise RuntimeError(message)


def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=True,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@dataclass
class MigrationContext:
    """Production defaults plus narrow seams for an isolated fixture."""

    version: str = VERSION
    template_root: Path = TEMPLATE_ROOT
    template_path: Path = TEMPLATE_PATH
    inventory_path: Path = INVENTORY_PATH
    clone_root: Path = CLONE_ROOT
    run_command: Callable[[list[str]], subprocess.CompletedProcess[str]] = run
    fchown: Callable[[int, int, int], None] = os.fchown
    after_chown: Callable[[], None] = lambda: None


def open_verified_directory(path: Path) -> int:
    if not path.is_absolute() or path.anchor != os.sep:
        fail(f"non-canonical directory: {path}")
    descriptor = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            fail(f"template parent is not a directory: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def template_metadata_and_hash(template_path: Path) -> tuple[os.stat_result, str]:
    parent_fd: int | None = None
    descriptor: int | None = None
    try:
        parent_fd = open_verified_directory(template_path.parent)
        descriptor = os.open(
            template_path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            fail("template is not a regular file")
        if (metadata.st_mode & 0o7777) != 0o444:
            fail(f"template mode is not exact 0444: {metadata.st_mode & 0o7777:o}")
        digest = hashlib.sha256()
        duplicate = os.dup(descriptor)
        with os.fdopen(duplicate, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return metadata, digest.hexdigest()
    except FileNotFoundError as exc:
        raise RuntimeError("canonical template is missing") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RuntimeError("template path contains a symlink") from exc
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd is not None:
            os.close(parent_fd)


def load_inventory(inventory_path: Path) -> dict[str, Any]:
    try:
        with inventory_path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("template inventory cannot be read") from exc
    if not isinstance(value, dict):
        fail("template inventory is not an object")
    return value


def expected_hash(inventory: Mapping[str, Any], version: str, template_path: Path) -> str:
    records = [
        item for item in inventory.get("templates", [])
        if isinstance(item, Mapping) and item.get("version") == version
    ]
    if len(records) != 1:
        fail(f"inventory must contain exactly one {version} record")
    record = records[0]
    if record.get("path") != str(template_path):
        fail("inventory template path is not canonical")
    digest = record.get("sha256")
    if not isinstance(digest, str) or not HASH_RE.fullmatch(digest.lower()):
        fail("inventory template SHA-256 is invalid")
    return digest.lower()


def verify_no_backing(template_path: Path, run_command: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> None:
    try:
        payload = json.loads(run_command([
            "qemu-img", "info", "--force-share", "--output=json", str(template_path)
        ]).stdout)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("template qemu metadata is invalid") from exc
    if not isinstance(payload, Mapping):
        fail("template qemu metadata is invalid")
    backing = payload.get("backing-filename")
    if isinstance(backing, str) and backing.strip() and backing.strip().lower() not in {"null", "none"}:
        fail("template has a backing file")
    if payload.get("backing") not in (None, "", False):
        fail("template has a backing file")


def dependent_clones(
    inventory: Mapping[str, Any],
    version: str,
    template_path: Path,
    clone_root: Path,
    run_command: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    for item in inventory.get("clones", []):
        if not isinstance(item, Mapping) or item.get("templateVersion") != version:
            continue
        name = item.get("name")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            fail("dependent clone name is invalid")
        try:
            xml = ET.fromstring(run_command(["virsh", "-c", "qemu:///system", "dumpxml", name]).stdout)
        except (ET.ParseError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"dependent clone XML cannot be read: {name}") from exc
        disk_sources = []
        backing_sources = []
        for disk in xml.findall(".//disk"):
            if disk.get("device") != "disk":
                continue
            source = disk.find("source")
            source_path = source.get("file") if source is not None else None
            if not isinstance(source_path, str) or not source_path.startswith(str(clone_root) + "/"):
                fail(f"dependent clone does not use an independent disk: {name}")
            if source_path == str(template_path):
                fail(f"dependent clone directly uses the template: {name}")
            disk_sources.append(Path(source_path))
            for backing in disk.findall(".//backingStore/source"):
                backing_path = backing.get("file")
                if isinstance(backing_path, str):
                    backing_sources.append(backing_path)
        if not disk_sources:
            fail(f"dependent clone has no disk source: {name}")
        if str(template_path) not in backing_sources:
            fail(f"dependent clone no longer references the retained template: {name}")
        result.append((name, disk_sources[0]))
    return result


def verify_state(context: MigrationContext, inventory: Mapping[str, Any], digest: str, *, after: bool) -> dict[str, Any]:
    metadata, actual_digest = template_metadata_and_hash(context.template_path)
    if actual_digest != digest:
        fail(f"template content hash changed: {actual_digest} != {digest}")
    if after and (metadata.st_uid, metadata.st_gid) != (0, 0):
        fail(f"template owner is not root:root: {metadata.st_uid}:{metadata.st_gid}")
    verify_no_backing(context.template_path, context.run_command)
    clones = dependent_clones(
        inventory,
        context.version,
        context.template_path,
        context.clone_root,
        context.run_command,
    )
    return {
        "owner": f"{metadata.st_uid}:{metadata.st_gid}",
        "mode": f"{metadata.st_mode & 0o7777:o}",
        "sha256": actual_digest,
        "dependentClones": [name for name, _path in clones],
    }


def migrate(context: MigrationContext | None = None) -> dict[str, Any]:
    if context is None:
        context = MigrationContext()
    if os.geteuid() != 0:
        fail("run as root")
    canonical_path = context.template_root / f"{context.version}.qcow2"
    if context.template_path != canonical_path:
        fail("template path is not canonical")
    inventory = load_inventory(context.inventory_path)
    digest = expected_hash(inventory, context.version, context.template_path)
    before = verify_state(context, inventory, digest, after=False)
    parent_fd: int | None = None
    descriptor: int | None = None
    try:
        parent_fd = open_verified_directory(context.template_path.parent)
        descriptor = os.open(
            context.template_path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        context.fchown(descriptor, 0, 0)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd is not None:
            os.close(parent_fd)
    context.after_chown()
    inventory_after = load_inventory(context.inventory_path)
    if expected_hash(inventory_after, context.version, context.template_path) != digest:
        fail("template inventory changed during ownership migration")
    after = verify_state(context, inventory_after, digest, after=True)
    return {"version": context.version, "before": before, "after": after}


if __name__ == "__main__":
    try:
        result = migrate()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"TEMPLATE_OWNERSHIP_MIGRATION_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(2)
    print("TEMPLATE_OWNERSHIP_MIGRATION_OK")
    print(json.dumps(result, sort_keys=True))
