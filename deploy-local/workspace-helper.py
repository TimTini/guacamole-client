#!/usr/bin/env python3
"""Discovery, inventory, and guarded golden-template operations.

The template transaction keeps the Task 1 validation, locking, and inventory
contracts compatible with the CLI used by Cockpit.
"""

import argparse
import copy
import dataclasses
import errno
import fcntl
import hashlib
import ipaddress
import inspect
import json
import math
import os
import re
import secrets
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


LIBVIRT_URI = "qemu:///system"
INVENTORY_PATH = Path("/var/lib/guacamole-templates/inventory.json")
TEMPLATES_DIR = Path("/var/lib/guacamole-templates")
VMS_DIR = Path("/var/lib/guacamole-vms")
CLONE_TPM_DIR = Path("/var/lib/libvirt/swtpm")
CLONE_NAME_PREFIX = "windows-template-"
WORKSPACE_OWNERSHIP_SCHEMA = "guacamole-workspace-v1"
WORKSPACE_OWNERSHIP_DIRNAME = ".guacamole-workspace"
WORKSPACE_MARKER_FILENAME = "guacamole-workspace.json"
WORKSPACE_NVRAM_MARKER_SUFFIX = ".guacamole-workspace.json"
LOCK_PATH = Path("/run/lock/guacamole-workspace-helper.lock")
JOB_STATUS_DIR = Path("/var/lib/guacamole-workspaces/jobs")
MAX_JOB_STATUS_BYTES = 65536
JOB_STATUS_SCHEMA = "guacamole-workspace-job-v1"
JOB_STATUS_DIR_MODE = 0o750
JOB_STATUS_FILE_MODE = 0o600
_JOB_SCHEDULE_LOCK = threading.Lock()
HELPER_EXECUTABLE = Path("/usr/local/libexec/guacamole-workspace-helper")
NETWORK_NAME = "guac-nat"
_SOURCE_ROOT = Path(__file__).resolve().parent
_INSTALLED_BUNDLE_ROOT = Path("/usr/local/libexec/guacamole-workspace")
_ASSET_ROOT = _INSTALLED_BUNDLE_ROOT if (_INSTALLED_BUNDLE_ROOT / "compose.yaml").is_file() else _SOURCE_ROOT
COMPOSE_FILE = _ASSET_ROOT / "compose.yaml"
_CANONICAL_WINDOWS_CREDENTIAL_SECRET_PATH = Path(
    "/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password"
)
WINDOWS_CREDENTIAL_MAX_BYTES = 4096


def _configured_windows_credential_secret_path() -> Path:
    """Return the one supported POSIX path inside the H-backed WSL VHDX.

    The production helper deliberately has no environment or bundle-file
    override.  A caller can pass a path to the Python functions only for an
    isolated fixture; the CLI never exposes that seam.  The canonical path is
    deliberately outside /mnt/h (DrvFs), so root ownership and modes are
    enforceable by the Ubuntu ext4 filesystem.
    """

    return _CANONICAL_WINDOWS_CREDENTIAL_SECRET_PATH


WINDOWS_CREDENTIAL_SECRET_PATH = _configured_windows_credential_secret_path()
WINDOWS_CREDENTIAL_USERNAME = "guacadmin"
SOURCE_DISK_PATH = Path("/var/lib/guacamole-vm-windows11/windows11.qcow2")
SOURCE_NVRAM_PATH = Path("/var/lib/guacamole-vm-windows11/libvirt/OVMF_VARS_4M.ms.fd")
SOURCE_TPM_UNIT = "guacamole-vm-windows11-libvirt-tpm.service"
SOURCE_TPM_SOCKET = "/run/guacamole-vm-windows11/swtpm.sock"
_SOURCE_TPM_CONFIG = _INSTALLED_BUNDLE_ROOT / "source-tpm-state.path"
if _SOURCE_TPM_CONFIG.is_file():
    SOURCE_TPM_STATE_PATH = Path(_SOURCE_TPM_CONFIG.read_text(encoding="utf-8").strip())
else:
    SOURCE_TPM_STATE_PATH = _SOURCE_ROOT.parent / "runtime/vm-windows11/tpm"
COMPOSE_NETWORK_NAME = "guacamole-local_default"
CLONE_IP_NETWORK = ipaddress.ip_network("192.168.250.0/24")
CLONE_IP_FIRST = 20
CLONE_IP_LAST = 249
CLONE_RESERVED_IPS = frozenset(("192.168.250.11", "192.168.250.12"))
CLONE_NVRAM_TEMPLATE_PATH = Path("/usr/share/OVMF/OVMF_VARS_4M.ms.fd")
CLONE_NVRAM_DIR = Path("/var/lib/libvirt/qemu/nvram")
CLONE_XML_TEMPLATE_PATH = _ASSET_ROOT / "libvirt/domains/windows-clone.xml.template"
CLONE_RDP_TIMEOUT_SECONDS = 120.0
CLONE_LEASE_TIMEOUT_SECONDS = 120.0
CLONE_MAC_PREFIX = (0x52, 0x54, 0x00)
CLONE_MEMORY_MIN_MIB = 1
CLONE_MEMORY_MAX_MIB = 1048576
CLONE_VCPUS_MIN = 1
CLONE_VCPUS_MAX = 256
CLONE_WAIT_RDP_MINUTES_MAX = 1440.0
CLONE_QEMU_OWNER = "libvirt-qemu"
CLONE_QEMU_GROUP = "libvirt-qemu"
CLONE_NVRAM_MODE = "660"
SOURCE_RDP_ADDRESS = "192.168.250.11"
RDP_PORT = 3389
GUAC_CONNECTION_NAME = "Windows 11"
GUAC_ASSIGNEE_QUERY = (
    "SELECT type::text || E'\\t' || name "
    "FROM guacamole_entity "
    "WHERE type IN ('USER'::guacamole_entity_type, 'USER_GROUP'::guacamole_entity_type) "
    "AND name <> 'guacadmin' "
    "ORDER BY type::text, name;"
)
GUAC_CONNECTION_QUERY = (
    "SELECT c.connection_id||'|'||c.connection_name||'|'||"
    "c.protocol||'|'||"
    "CASE WHEN bool_or(p.parameter_name='hostname' AND p.parameter_value='192.168.250.11') "
    "THEN 'match' ELSE 'mismatch' END||'|'||"
    "CASE WHEN bool_or(p.parameter_name='port' AND p.parameter_value='3389') "
    "THEN 'match' ELSE 'mismatch' END "
    "FROM guacamole_connection c "
    "JOIN guacamole_connection_parameter p USING(connection_id) "
    "WHERE c.connection_name='Windows 11' "
    "GROUP BY c.connection_id,c.connection_name;"
)
SHUTDOWN_TIMEOUT_SECONDS = 120.0
GRACEFUL_SHUTDOWN_SECONDS = 30.0
# A forced poweroff can make Windows spend several minutes in recovery before
# accepting RDP. Clone readiness remains bounded by its separate 120-second
# timeout; source recovery must allow the existing guest to boot fully.
RDP_TIMEOUT_SECONDS = 600.0
PREFLIGHT_TIMEOUT_SECONDS = 30.0
CONVERSION_TIMEOUT_SECONDS = 3600.0
POLL_INTERVAL_SECONDS = 0.25

_VM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ASSIGNEE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")
_SYNC_ATTEMPT_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_ASSIGNEE_TYPES = frozenset(("USER", "USER_GROUP"))


class HelperError(Exception):
    """An expected helper failure that can be returned as safe JSON."""

    default_code = "HELPER_ERROR"
    default_stage = "command"

    def __init__(self, message: str, *, code: str | None = None, stage: str | None = None):
        super().__init__(message)
        self.code = code or self.default_code
        self.stage = stage or self.default_stage
        self.message = message


class ValidationError(HelperError):
    default_code = "NAME_INVALID"
    default_stage = "validate"


class ConflictError(HelperError):
    default_code = "CONFLICT"
    default_stage = "validate"


class InventoryError(HelperError):
    default_code = "INVENTORY_INVALID"
    default_stage = "inventory"

    def __init__(self, message: str, *, replaced: bool = False):
        super().__init__(message)
        self.replaced = replaced


class LockError(HelperError):
    default_code = "LOCK_BUSY"
    default_stage = "lock"


class CredentialSecretError(HelperError):
    default_code = "CREDENTIAL_SECRET_INVALID"
    default_stage = "credentials"


@dataclass(frozen=True)
class WindowsCredential:
    """The shared guest credential, kept out of every public workflow payload."""

    username: str
    password: str

    def to_public_dict(self) -> dict[str, str]:
        return {}


def _credential_secret_path(path: str | os.PathLike[str] | None = None) -> Path:
    secret_path = Path(WINDOWS_CREDENTIAL_SECRET_PATH if path is None else path)
    if not secret_path.is_absolute():
        raise CredentialSecretError("Windows credential secret path must be absolute")
    return secret_path


def _credential_owner_is_root(metadata: os.stat_result) -> bool:
    return os.name != "posix" or metadata.st_uid == 0


def _credential_open_flags(*, directory: bool = False) -> int:
    flags = os.O_RDONLY
    if directory and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _open_credential_parent(path: Path, *, create: bool) -> int:
    """Open the canonical parent through descriptor-relative no-follow steps."""

    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.stat()
        if not stat_module.S_ISDIR(metadata.st_mode):
            raise CredentialSecretError("Windows credential secret parent is not a directory")
        if os.name == "posix" and stat_module.S_IMODE(metadata.st_mode) != 0o700:
            raise CredentialSecretError("Windows credential secret parent mode is invalid")
        return -1

    components = path.parts
    if not path.is_absolute() or not components or components[0] != os.sep:
        raise CredentialSecretError("Windows credential secret path is not canonical")
    descriptor = os.open(os.sep, _credential_open_flags(directory=True))
    try:
        for index, component in enumerate(components[1:]):
            try:
                next_descriptor = os.open(
                    component,
                    _credential_open_flags(directory=True),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise CredentialSecretError(
                        "Windows credential secret is not initialized",
                        code="CREDENTIAL_SECRET_MISSING",
                    )
                os.mkdir(component, 0o700, dir_fd=descriptor)
                next_descriptor = os.open(
                    component,
                    _credential_open_flags(directory=True),
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = next_descriptor
            metadata = os.fstat(descriptor)
            if not stat_module.S_ISDIR(metadata.st_mode) or not _credential_owner_is_root(metadata):
                raise CredentialSecretError("Windows credential secret parent is not a root-owned directory")
            if index == len(components[1:]) - 1 and stat_module.S_IMODE(metadata.st_mode) != 0o700:
                raise CredentialSecretError("Windows credential secret parent mode is invalid")
        metadata = os.fstat(descriptor)
        if stat_module.S_IMODE(metadata.st_mode) != 0o700:
            raise CredentialSecretError("Windows credential secret parent mode is invalid")
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        if exc.errno == errno.ELOOP:
            raise CredentialSecretError("Windows credential secret parent must not contain a symlink") from exc
        raise CredentialSecretError("Windows credential secret parent cannot be opened") from exc
    except BaseException:
        os.close(descriptor)
        raise


def _validate_credential_secret_metadata(path: Path) -> os.stat_result:
    """Validate a file by descriptor, never by a check-then-open pathname."""

    parent_fd = _open_credential_parent(path.parent, create=False)
    if parent_fd < 0:
        try:
            metadata = path.stat()
        except FileNotFoundError as exc:
            raise CredentialSecretError(
                "Windows credential secret is not initialized", code="CREDENTIAL_SECRET_MISSING"
            ) from exc
        if stat_module.S_IMODE(metadata.st_mode) != 0o600 or not _credential_owner_is_root(metadata):
            raise CredentialSecretError("Windows credential secret ownership or mode is invalid")
        if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > WINDOWS_CREDENTIAL_MAX_BYTES:
            raise CredentialSecretError("Windows credential secret size is invalid")
        return metadata
    descriptor = None
    try:
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise CredentialSecretError(
                "Windows credential secret is not initialized", code="CREDENTIAL_SECRET_MISSING"
            ) from exc
        metadata = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(metadata.st_mode)
            or not _credential_owner_is_root(metadata)
            or stat_module.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > WINDOWS_CREDENTIAL_MAX_BYTES
        ):
            raise CredentialSecretError("Windows credential secret ownership, mode, or size is invalid")
        return metadata
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CredentialSecretError("Windows credential secret must not be a symlink") from exc
        raise CredentialSecretError("Windows credential secret cannot be inspected") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def read_windows_credential_secret(path: str | os.PathLike[str] | None = None) -> WindowsCredential:
    """Read and validate the H-backed secret without returning it in a payload."""

    secret_path = _credential_secret_path(path)
    parent_fd = _open_credential_parent(secret_path.parent, create=False)
    descriptor = None
    try:
        if parent_fd < 0:
            with secret_path.open("rb") as stream:
                raw = stream.read(WINDOWS_CREDENTIAL_MAX_BYTES + 1)
        else:
            descriptor = os.open(secret_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            before = os.fstat(descriptor)
            if (
                not stat_module.S_ISREG(before.st_mode)
                or not _credential_owner_is_root(before)
                or stat_module.S_IMODE(before.st_mode) != 0o600
                or before.st_size <= 0
                or before.st_size > WINDOWS_CREDENTIAL_MAX_BYTES
            ):
                raise CredentialSecretError("Windows credential secret ownership, mode, or size is invalid")
            raw = os.read(descriptor, WINDOWS_CREDENTIAL_MAX_BYTES + 1)
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
                raise CredentialSecretError("Windows credential secret changed while it was read")
    except FileNotFoundError as exc:
        raise CredentialSecretError("Windows credential secret is not initialized", code="CREDENTIAL_SECRET_MISSING") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CredentialSecretError("Windows credential secret must not be a symlink") from exc
        raise CredentialSecretError("Windows credential secret cannot be read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)
    if len(raw) > WINDOWS_CREDENTIAL_MAX_BYTES:
        raise CredentialSecretError("Windows credential secret size is invalid")
    try:
        value = raw.decode("utf-8")
    except UnicodeError as exc:
        raise CredentialSecretError("Windows credential secret content is invalid") from exc
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
    if not value or "\r" in value or "\n" in value or "\x00" in value:
        raise CredentialSecretError("Windows credential secret content is invalid")
    return WindowsCredential(WINDOWS_CREDENTIAL_USERNAME, value)


def write_windows_credential_secret(
    password: str,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Atomically replace the root-only H-backed secret from trusted stdin."""

    if not isinstance(password, str) or not password or any(char in password for char in ("\r", "\n", "\x00")):
        raise CredentialSecretError("Windows credential secret content is invalid")
    secret_path = _credential_secret_path(path)
    try:
        encoded = password.encode("utf-8")
    except UnicodeError as exc:
        raise CredentialSecretError("Windows credential secret content is invalid") from exc
    if len(encoded) + 1 > WINDOWS_CREDENTIAL_MAX_BYTES:
        raise CredentialSecretError("Windows credential secret size is invalid")
    parent_fd = _open_credential_parent(secret_path.parent, create=True)
    if parent_fd < 0:
        # The production path is Linux/WSL and always takes the descriptor
        # branch above.  Keep a small Windows-only fixture seam so the source
        # tests can exercise redaction without pretending Windows has
        # openat/O_NOFOLLOW semantics.
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{secret_path.name}.", suffix=".tmp", dir=secret_path.parent
        )
        try:
            os.chmod(temporary_name, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, secret_path)
            temporary_name = None
            os.chmod(secret_path, 0o600)
        except OSError as exc:
            raise CredentialSecretError("Windows credential secret cannot be saved") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
        return secret_path
    descriptor = None
    temporary_name: str | None = None
    try:
        temporary_name = f".{secret_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        temporary_metadata = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(temporary_metadata.st_mode)
            or not _credential_owner_is_root(temporary_metadata)
            or stat_module.S_IMODE(temporary_metadata.st_mode) != 0o600
        ):
            raise CredentialSecretError("Windows credential temporary file metadata is invalid")
        payload = encoded + b"\n"
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        after_write = os.fstat(descriptor)
        if after_write.st_size != len(payload) or (after_write.st_dev, after_write.st_ino) != (
            temporary_metadata.st_dev,
            temporary_metadata.st_ino,
        ):
            raise CredentialSecretError("Windows credential temporary file changed")
        os.close(descriptor)
        descriptor = None
        os.replace(temporary_name, secret_path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
        final_descriptor = os.open(secret_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            final_metadata = os.fstat(final_descriptor)
            if (
                not stat_module.S_ISREG(final_metadata.st_mode)
                or not _credential_owner_is_root(final_metadata)
                or stat_module.S_IMODE(final_metadata.st_mode) != 0o600
                or final_metadata.st_size != len(payload)
                or (final_metadata.st_dev, final_metadata.st_ino)
                != (temporary_metadata.st_dev, temporary_metadata.st_ino)
            ):
                raise CredentialSecretError("Windows credential secret publication was not atomic")
        finally:
            os.close(final_descriptor)
        temporary_name = None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CredentialSecretError("Windows credential secret path must not contain a symlink") from exc
        raise CredentialSecretError("Windows credential secret cannot be saved") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)
    _validate_credential_secret_metadata(secret_path)
    return secret_path


def write_windows_credential_secret_from_stdin(
    stream: Any = None,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Consume one non-echoed password line and never print it."""

    if stream is None:
        stream = sys.stdin
    try:
        value = stream.read(WINDOWS_CREDENTIAL_MAX_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        raise CredentialSecretError("Windows credential secret stdin cannot be read") from exc
    if not isinstance(value, str):
        raise CredentialSecretError("Windows credential secret stdin cannot be read")
    if len(value.encode("utf-8")) > WINDOWS_CREDENTIAL_MAX_BYTES:
        raise CredentialSecretError("Windows credential secret size is invalid")
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n") or value.endswith("\r"):
        value = value[:-1]
    return write_windows_credential_secret(value, path)


@dataclass(init=False)
class TemplateRecord:
    """A published immutable template manifest record.

    The field names match the JSON contract. Snake-case keyword aliases are
    accepted for Python callers so the record remains pleasant to use in code.
    """

    version: str
    sourceDomain: str
    path: str
    sha256: str
    virtualSize: int | None
    createdAt: str

    def __init__(
        self,
        version: str,
        sourceDomain: str | None = None,
        path: str = "",
        sha256: str = "",
        virtualSize: int | None = None,
        createdAt: str = "",
        *,
        source_domain: str | None = None,
        virtual_size: int | None = None,
        created_at: str | None = None,
    ) -> None:
        if sourceDomain is None:
            sourceDomain = source_domain
        if virtual_size is not None:
            virtualSize = virtual_size
        if created_at is not None:
            createdAt = created_at
        if sourceDomain is None:
            raise TypeError("sourceDomain is required")
        self.version = version
        self.sourceDomain = sourceDomain
        self.path = path
        self.sha256 = sha256
        self.virtualSize = virtualSize
        self.createdAt = createdAt

    @property
    def source_domain(self) -> str:
        return self.sourceDomain

    @property
    def virtual_size(self) -> int | None:
        return self.virtualSize

    @property
    def created_at(self) -> str:
        return self.createdAt

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TemplateRecord":
        return cls(
            version=str(value.get("version", "")),
            sourceDomain=value.get("sourceDomain", value.get("source_domain")),
            path=str(value.get("path", "")),
            sha256=str(value.get("sha256", "")),
            virtualSize=value.get("virtualSize", value.get("virtual_size")),
            createdAt=str(value.get("createdAt", value.get("created_at", ""))),
        )


@dataclass(init=False)
class CloneRecord:
    """An inventory record for a VM clone and its optional assignee."""

    name: str
    mac: str
    ip: str
    assigneeType: str | None
    assigneeName: str | None
    status: str
    templateVersion: str | None
    syncAttemptId: str | None

    def __init__(
        self,
        name: str,
        mac: str = "",
        ip: str = "",
        assigneeType: str | None = None,
        assigneeName: str | None = None,
        status: str = "pending",
        templateVersion: str | None = None,
        syncAttemptId: str | None = None,
        *,
        assignee_type: str | None = None,
        assignee_name: str | None = None,
        template_version: str | None = None,
        sync_attempt_id: str | None = None,
    ) -> None:
        if assignee_type is not None:
            assigneeType = assignee_type
        if assignee_name is not None:
            assigneeName = assignee_name
        if template_version is not None:
            templateVersion = template_version
        if sync_attempt_id is not None:
            syncAttemptId = sync_attempt_id
        self.name = name
        self.mac = mac
        self.ip = ip
        self.assigneeType = assigneeType
        self.assigneeName = assigneeName
        self.status = status
        self.templateVersion = templateVersion
        self.syncAttemptId = syncAttemptId

    @property
    def assignee_type(self) -> str | None:
        return self.assigneeType

    @property
    def assignee_name(self) -> str | None:
        return self.assigneeName

    @property
    def template_version(self) -> str | None:
        return self.templateVersion

    @property
    def sync_attempt_id(self) -> str | None:
        return self.syncAttemptId

    def to_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        if payload.get("templateVersion") is None:
            payload.pop("templateVersion", None)
        if payload.get("syncAttemptId") is None:
            payload.pop("syncAttemptId", None)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CloneRecord":
        return cls(
            name=str(value.get("name", "")),
            mac=str(value.get("mac", "")),
            ip=str(value.get("ip", "")),
            assigneeType=value.get("assigneeType", value.get("assignee_type")),
            assigneeName=value.get("assigneeName", value.get("assignee_name")),
            status=str(value.get("status", "pending")),
            templateVersion=value.get("templateVersion", value.get("template_version")),
            syncAttemptId=value.get("syncAttemptId", value.get("sync_attempt_id")),
        )


def validate_vm_name(value: str) -> str:
    """Validate and return a libvirt/hostname-safe VM name."""

    if not isinstance(value, str) or not _VM_NAME_RE.fullmatch(value):
        raise ValidationError("VM name is invalid")
    return value


def validate_assignee(assignee_type: str, assignee_name: str | None = None) -> tuple[str, str]:
    """Validate a Guacamole USER or USER_GROUP assignee."""

    if assignee_type not in _ASSIGNEE_TYPES or not isinstance(assignee_name, str):
        raise ValidationError("Assignee is invalid", code="ASSIGNEE_INVALID")
    if not _ASSIGNEE_NAME_RE.fullmatch(assignee_name):
        raise ValidationError("Assignee is invalid", code="ASSIGNEE_INVALID")
    if assignee_type == "USER" and assignee_name == "guacadmin":
        raise ValidationError("guacadmin cannot be a clone assignee", code="ASSIGNEE_INVALID")
    return assignee_type, assignee_name


def _validate_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValidationError("IP address is invalid", code="IP_INVALID") from exc
    if address.version != 4:
        raise ValidationError("IP address is invalid", code="IP_INVALID")
    return str(address)


def _validate_mac(value: str) -> str:
    if not isinstance(value, str) or not _MAC_RE.fullmatch(value):
        raise ValidationError("MAC address is invalid", code="MAC_INVALID")
    return value.lower()


def _validate_sync_attempt_id(value: str) -> str:
    if not isinstance(value, str) or not _SYNC_ATTEMPT_ID_RE.fullmatch(value):
        raise ValidationError("Guacamole sync ownership token is invalid", code="SYNC_OWNERSHIP_INVALID")
    return str(uuid.UUID(value))


def assert_unique_clone(inventory: Mapping[str, Any], name: str, ip: str, mac: str) -> None:
    """Reject a clone that would reuse an inventory name, IP, or MAC."""

    normalized_name = validate_vm_name(name)
    normalized_ip = _validate_ip(ip)
    normalized_mac = _validate_mac(mac)
    clones = inventory.get("clones", [])
    if not isinstance(clones, list):
        raise InventoryError("Inventory clones must be a list")
    for existing in clones:
        if not isinstance(existing, Mapping):
            raise InventoryError("Inventory clone record is invalid")
        existing_name = existing.get("name")
        existing_ip = existing.get("ip")
        existing_mac = existing.get("mac")
        if existing_name == normalized_name:
            raise ConflictError("Clone name is already present")
        if isinstance(existing_ip, str) and _normalise_ip_for_compare(existing_ip) == normalized_ip:
            raise ConflictError("Clone IP is already present")
        if isinstance(existing_mac, str) and existing_mac.lower() == normalized_mac:
            raise ConflictError("Clone MAC is already present")


GUACAMOLE_SYNC_TIMEOUT_SECONDS = 120.0
GUACAMOLE_SYNC_PARAMETERS = ("hostname", "port", "security", "ignore-cert", "username", "password")
GUAC_MANAGED_ASSIGNEE_ATTRIBUTE = "org.apache.guacamole.workspace.sync.assignee-entity"
GUAC_MANAGED_ATTEMPT_ATTRIBUTE = "org.apache.guacamole.workspace.sync.attempt"
_INVENTORY_SYNCING_STATUS = "syncing"
_POST_COMMIT_FAILURE_CODES = frozenset({
    "SYNC_COMMIT_UNKNOWN",
    "SYNC_INVALID",
    "SYNC_OWNERSHIP_INVALID",
    "SYNC_PERMISSIONS_INVALID",
})


def _coerce_clone_sync_record(value: Mapping[str, Any] | CloneRecord) -> CloneRecord:
    """Return a validated clone record for the Guacamole sync contract."""

    if isinstance(value, CloneRecord):
        record = value
    elif isinstance(value, Mapping):
        record = CloneRecord.from_dict(value)
    else:
        raise ValidationError("Clone record is invalid", code="CLONE_INVALID")
    validate_vm_name(record.name)
    _validate_ip(record.ip)
    _validate_mac(record.mac)
    if record.syncAttemptId is not None:
        record.syncAttemptId = _validate_sync_attempt_id(record.syncAttemptId)
    has_type = record.assigneeType is not None
    has_name = record.assigneeName is not None
    if has_type != has_name:
        raise ValidationError("Assignee is invalid", code="ASSIGNEE_INVALID")
    if has_type:
        validate_assignee(record.assigneeType, record.assigneeName)
    return record


def build_guacamole_sync_sql(
    clone: Mapping[str, Any] | CloneRecord,
    *,
    transaction_end: str = "COMMIT",
    commit: bool | None = None,
    require_new_connection: bool = False,
    connection_id: int | None = None,
    require_owned_connection: bool = False,
    include_windows_credentials: bool = False,
    adopt_existing_connection: bool = False,
) -> str:
    """Build the fixed, parameterized Guacamole connection synchronization SQL.

    Clone and assignee values use validated psql variables. When explicitly
    enabled for a managed clone, credentials are read from a transaction-local
    COPY temp table populated by ``run_psql_sync``; they never occur in SQL
    text. Connection, credential, and permission changes remain atomic.
    ``transaction_end='ROLLBACK'`` is available for disposable integration
    checks.
    """

    record = _coerce_clone_sync_record(clone)
    if record.assigneeType is None or record.assigneeName is None:
        raise ValidationError("Assignee is required", code="ASSIGNEE_INVALID")
    if commit is not None:
        transaction_end = "COMMIT" if commit else "ROLLBACK"
    if transaction_end not in {"COMMIT", "ROLLBACK"}:
        raise ValidationError("Transaction end is invalid", code="SYNC_INVALID")
    if (require_new_connection or require_owned_connection or adopt_existing_connection) and record.syncAttemptId is None:
        raise HelperError(
            "Guacamole sync ownership token is unavailable",
            code="SYNC_OWNERSHIP_UNAVAILABLE",
            stage="sync",
        )
    # A credential-enabled create must insert and claim the one row created by
    # this transaction.  Do this before the managed-existing branch; selecting
    # an existing same-name row would turn a create into an unsafe update.
    if require_new_connection:
        connection_setup_sql = """
WITH inserted AS (
    INSERT INTO guacamole_connection (connection_name, parent_id, protocol)
    SELECT :'sync_name', NULL, 'rdp'
    WHERE NOT EXISTS (
        SELECT 1
        FROM guacamole_connection
        WHERE connection_name = :'sync_name' AND parent_id IS NULL
    )
    RETURNING connection_id
)
INSERT INTO _sync_created (connection_id)
SELECT connection_id FROM inserted;

INSERT INTO _sync_connection (connection_id)
SELECT connection_id FROM _sync_created;
"""
    elif require_owned_connection:
        if require_new_connection:
            raise ValidationError("Guacamole sync ownership mode is invalid", code="SYNC_INVALID", stage="sync")
        if not isinstance(connection_id, int) or isinstance(connection_id, bool) or connection_id <= 0:
            raise ValidationError("Guacamole connection identity is invalid", code="SYNC_OWNERSHIP_INVALID", stage="sync")
    if include_windows_credentials and not (require_new_connection or require_owned_connection) and record.syncAttemptId is None:
        raise HelperError(
            "Windows credentials require a managed Guacamole connection",
            code="SYNC_OWNERSHIP_UNAVAILABLE",
            stage="credentials",
        )
    parameter_values = """        ('hostname', :'sync_ip'),
        ('port', '3389'),
        ('security', 'any'),
        ('ignore-cert', 'true')"""
    if include_windows_credentials:
        parameter_values += """,
        ('username', (SELECT value FROM _sync_secure_values WHERE name = 'username')),
        ('password', (SELECT value FROM _sync_secure_values WHERE name = 'password'))"""
    parameter_allowlist = "'hostname', 'port', 'security', 'ignore-cert'"
    if include_windows_credentials:
        parameter_allowlist += ", 'username', 'password'"
    credential_result_field = """    'credentialsPresent', (
        EXISTS (
            SELECT 1 FROM guacamole_connection_parameter AS credential
            WHERE credential.connection_id = connection.connection_id
              AND credential.parameter_name = 'username'
              AND credential.parameter_value <> ''
        ) AND EXISTS (
            SELECT 1 FROM guacamole_connection_parameter AS credential
            WHERE credential.connection_id = connection.connection_id
              AND credential.parameter_name = 'password'
              AND credential.parameter_value <> ''
        )
    ),""" if include_windows_credentials else ""
    attempt_attribute_sql = "" if not (require_new_connection or adopt_existing_connection) else f"""

INSERT INTO guacamole_connection_attribute (connection_id, attribute_name, attribute_value)
SELECT connection.connection_id,
       '{GUAC_MANAGED_ATTEMPT_ATTRIBUTE}',
       :'sync_attempt_id'
FROM _sync_connection AS connection
ON CONFLICT (connection_id, attribute_name) DO UPDATE
SET attribute_value = EXCLUDED.attribute_value;
"""

    if adopt_existing_connection:
        if require_new_connection or require_owned_connection:
            raise ValidationError("Guacamole adoption mode is invalid", code="SYNC_INVALID", stage="sync")
        if not isinstance(connection_id, int) or isinstance(connection_id, bool) or connection_id <= 0:
            raise ValidationError("Guacamole connection identity is invalid", code="SYNC_OWNERSHIP_INVALID", stage="sync")
        if not include_windows_credentials:
            raise ValidationError("Guacamole adoption requires credentials", code="CREDENTIAL_SYNC_INVALID", stage="credentials")
    if require_new_connection:
        pass
    elif adopt_existing_connection:
        connection_setup_sql = f"""
INSERT INTO _sync_connection (connection_id)
SELECT connection.connection_id
FROM guacamole_connection AS connection
WHERE connection.connection_id = :'sync_connection_id'::integer
  AND connection.connection_name = :'sync_name'
  AND connection.parent_id IS NULL
  AND connection.protocol = 'rdp'
  AND NOT EXISTS (
      SELECT 1 FROM guacamole_connection AS conflict
      WHERE conflict.connection_name = :'sync_name'
        AND conflict.connection_id <> connection.connection_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM guacamole_connection_attribute AS attempt
      WHERE attempt.connection_id = connection.connection_id
        AND attempt.attribute_name = '{GUAC_MANAGED_ATTEMPT_ATTRIBUTE}'
  )
  AND EXISTS (
      SELECT 1
      FROM guacamole_connection_attribute AS assignee_marker
      JOIN guacamole_entity AS assignee
        ON assignee.entity_id = NULLIF(assignee_marker.attribute_value, '')::integer
       AND assignee.type = :'sync_assignee_type'::guacamole_entity_type
       AND assignee.name = :'sync_assignee_name'
      WHERE assignee_marker.connection_id = connection.connection_id
        AND assignee_marker.attribute_name = '{GUAC_MANAGED_ASSIGNEE_ATTRIBUTE}'
  );
"""
    elif require_owned_connection:
        connection_setup_sql = """
INSERT INTO _sync_connection (connection_id)
SELECT connection.connection_id
FROM guacamole_connection AS connection
WHERE connection.connection_id = :'sync_connection_id'::integer
  AND connection.connection_name = :'sync_name'
  AND connection.parent_id IS NULL
  AND connection.protocol = 'rdp'
  AND EXISTS (
      SELECT 1 FROM guacamole_connection_attribute AS attempt
      WHERE attempt.connection_id = connection.connection_id
        AND attempt.attribute_name = 'org.apache.guacamole.workspace.sync.attempt'
        AND attempt.attribute_value = :'sync_attempt_id'
  )
  AND EXISTS (
      SELECT 1
      FROM guacamole_connection_attribute AS assignee_marker
      JOIN guacamole_entity AS assignee
        ON assignee.entity_id = NULLIF(assignee_marker.attribute_value, '')::integer
       AND assignee.type = :'sync_assignee_type'::guacamole_entity_type
       AND assignee.name = :'sync_assignee_name'
      WHERE assignee_marker.connection_id = connection.connection_id
        AND assignee_marker.attribute_name = '{GUAC_MANAGED_ASSIGNEE_ATTRIBUTE}'
  );
"""
    elif include_windows_credentials:
        connection_setup_sql = f"""
INSERT INTO _sync_connection (connection_id)
SELECT connection.connection_id
FROM guacamole_connection AS connection
WHERE connection.connection_name = :'sync_name'
  AND connection.parent_id IS NULL
  AND connection.protocol = 'rdp'
  AND EXISTS (
      SELECT 1
      FROM guacamole_connection_attribute AS attempt
      WHERE attempt.connection_id = connection.connection_id
        AND attempt.attribute_name = '{GUAC_MANAGED_ATTEMPT_ATTRIBUTE}'
        AND attempt.attribute_value = :'sync_attempt_id'
  )
  AND EXISTS (
      SELECT 1
      FROM guacamole_connection_attribute AS assignee_marker
      JOIN guacamole_entity AS assignee
        ON assignee.entity_id = NULLIF(assignee_marker.attribute_value, '')::integer
       AND assignee.type = :'sync_assignee_type'::guacamole_entity_type
       AND assignee.name = :'sync_assignee_name'
      WHERE assignee_marker.connection_id = connection.connection_id
        AND assignee_marker.attribute_name = '{GUAC_MANAGED_ASSIGNEE_ATTRIBUTE}'
  );
"""
    else:
        connection_setup_sql = f"""
WITH inserted AS (
    INSERT INTO guacamole_connection (connection_name, parent_id, protocol)
    SELECT :'sync_name', NULL, 'rdp'
    WHERE NOT EXISTS (
        SELECT 1
        FROM guacamole_connection
        WHERE connection_name = :'sync_name' AND parent_id IS NULL
    )
    RETURNING connection_id
)
INSERT INTO _sync_created (connection_id)
SELECT connection_id FROM inserted;

INSERT INTO _sync_connection (connection_id)
SELECT connection_id
FROM guacamole_connection
WHERE connection_name = :'sync_name'
  AND parent_id IS NULL;
"""

    ownership_contract_sql = ""
    if include_windows_credentials or require_new_connection or require_owned_connection or adopt_existing_connection:
        ownership_contract_sql = f"""
    IF NOT EXISTS (
        SELECT 1
        FROM guacamole_connection_attribute AS attempt
        WHERE attempt.connection_id = (SELECT connection_id FROM _sync_connection)
          AND attempt.attribute_name = '{GUAC_MANAGED_ATTEMPT_ATTRIBUTE}'
          AND attempt.attribute_value = (SELECT attempt_id FROM _sync_contract)
    ) THEN
        RAISE EXCEPTION 'The Guacamole workflow attempt marker is invalid';
    END IF;
"""
    credential_contract_sql = """
    IF (
        SELECT count(*) FROM guacamole_connection_parameter
        WHERE connection_id = (SELECT connection_id FROM _sync_connection)
    ) <> 6 OR EXISTS (
        SELECT 1 FROM guacamole_connection_parameter
        WHERE connection_id = (SELECT connection_id FROM _sync_connection)
          AND parameter_name NOT IN ('hostname', 'port', 'security', 'ignore-cert', 'username', 'password')
    ) OR EXISTS (
        SELECT 1 FROM guacamole_connection_parameter
        WHERE connection_id = (SELECT connection_id FROM _sync_connection)
          AND parameter_name IN ('username', 'password')
          AND parameter_value = ''
    ) THEN
        RAISE EXCEPTION 'The managed Guacamole parameter allowlist is invalid';
    END IF;
""" if include_windows_credentials else ""
    transaction_contract_sql = f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM guacamole_connection AS connection
        WHERE connection.connection_id = (SELECT connection_id FROM _sync_connection)
          AND connection.connection_name = (SELECT connection_name FROM _sync_contract)
          AND connection.parent_id IS NULL
          AND connection.protocol = 'rdp'
    ) THEN
        RAISE EXCEPTION 'The selected Guacamole connection identity is invalid';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM guacamole_connection_attribute AS assignee_marker
        JOIN guacamole_entity AS assignee
          ON assignee.entity_id = NULLIF(assignee_marker.attribute_value, '')::integer
         AND assignee.type = (SELECT assignee_type FROM _sync_contract)
         AND assignee.name = (SELECT assignee_name FROM _sync_contract)
        WHERE assignee_marker.connection_id = (SELECT connection_id FROM _sync_connection)
          AND assignee_marker.attribute_name = '{GUAC_MANAGED_ASSIGNEE_ATTRIBUTE}'
    ) THEN
        RAISE EXCEPTION 'The managed Guacamole assignee marker is invalid';
    END IF;
{ownership_contract_sql}{credential_contract_sql}END
$$;
"""

    return f"""BEGIN;
LOCK TABLE guacamole_connection, guacamole_connection_parameter,
    guacamole_connection_permission, guacamole_connection_attribute
    IN SHARE ROW EXCLUSIVE MODE;

CREATE TEMP TABLE _sync_connection (
    connection_id integer PRIMARY KEY
) ON COMMIT DROP;
CREATE TEMP TABLE _sync_assignee (
    entity_id integer PRIMARY KEY
) ON COMMIT DROP;
CREATE TEMP TABLE _sync_admin (
    entity_id integer PRIMARY KEY
) ON COMMIT DROP;
CREATE TEMP TABLE _sync_previous (
    entity_id integer PRIMARY KEY
) ON COMMIT DROP;
    CREATE TEMP TABLE _sync_created (
        connection_id integer PRIMARY KEY
    ) ON COMMIT DROP;
    CREATE TEMP TABLE _sync_contract (
        connection_name text NOT NULL,
        assignee_type guacamole_entity_type NOT NULL,
        assignee_name text NOT NULL,
        attempt_id text
    ) ON COMMIT DROP;
    INSERT INTO _sync_contract (connection_name, assignee_type, assignee_name, attempt_id)
    VALUES (:'sync_name', :'sync_assignee_type'::guacamole_entity_type,
            :'sync_assignee_name', {":'sync_attempt_id'" if record.syncAttemptId is not None else "NULL"});
{"CREATE TEMP TABLE _sync_secure_values (name text PRIMARY KEY, value text NOT NULL) ON COMMIT DROP;\n-- GUACAMOLE_SECURE_CREDENTIALS" if include_windows_credentials else ""}

{connection_setup_sql}

DO $$
DECLARE
    matching_connections integer;
    matching_rdp_connections integer;
BEGIN
    SELECT count(*) INTO matching_connections FROM _sync_connection;
    SELECT count(*) INTO matching_rdp_connections
    FROM _sync_connection AS selected
    JOIN guacamole_connection AS connection
      ON connection.connection_id = selected.connection_id
    WHERE connection.protocol = 'rdp';
    IF matching_connections <> 1 OR matching_rdp_connections <> 1 THEN
        RAISE EXCEPTION 'A root RDP connection with this name is unavailable';
    END IF;
END
$$;

DO $$
DECLARE
    created_connections integer;
BEGIN
    SELECT count(*) INTO created_connections FROM _sync_created;
    IF {"created_connections <> 1" if require_new_connection else "created_connections > 1"} THEN
        RAISE EXCEPTION 'A Guacamole connection with this workspace name already exists';
    END IF;
END
$$;

INSERT INTO _sync_previous (entity_id)
SELECT NULLIF(attribute_value, '')::integer
FROM guacamole_connection_attribute
WHERE connection_id = (SELECT connection_id FROM _sync_connection)
  AND attribute_name = '{GUAC_MANAGED_ASSIGNEE_ATTRIBUTE}';

INSERT INTO _sync_assignee (entity_id)
SELECT entity_id
FROM guacamole_entity
WHERE type = :'sync_assignee_type'::guacamole_entity_type
  AND name = :'sync_assignee_name';

DO $$
DECLARE
    matching_entities integer;
BEGIN
    SELECT count(*) INTO matching_entities FROM _sync_assignee;
    IF matching_entities <> 1 THEN
        RAISE EXCEPTION 'The declared assignee does not exist';
    END IF;
END
$$;

INSERT INTO _sync_admin (entity_id)
SELECT entity_id
FROM guacamole_entity
WHERE type = 'USER'::guacamole_entity_type AND name = 'guacadmin';

DO $$
DECLARE
    matching_admins integer;
BEGIN
    SELECT count(*) INTO matching_admins FROM _sync_admin;
    IF matching_admins <> 1 THEN
        RAISE EXCEPTION 'The administrator entity does not exist';
    END IF;
END
$$;

DELETE FROM guacamole_connection_parameter
WHERE connection_id = (SELECT connection_id FROM _sync_connection)
  AND parameter_name NOT IN ({parameter_allowlist});

INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, parameter_name, parameter_value
FROM _sync_connection
CROSS JOIN (
    VALUES
{parameter_values}
) AS parameters(parameter_name, parameter_value)
ON CONFLICT (connection_id, parameter_name) DO UPDATE
SET parameter_value = EXCLUDED.parameter_value;

DELETE FROM guacamole_connection_permission
WHERE connection_id = (SELECT connection_id FROM _sync_connection)
  AND entity_id = (SELECT entity_id FROM _sync_previous)
  AND entity_id <> (SELECT entity_id FROM _sync_assignee)
  AND permission = 'READ'::guacamole_object_permission_type;

-- The selected assignee is managed by this workflow.  Remove every direct
-- permission first so a reused USER or USER_GROUP cannot retain UPDATE,
-- DELETE, or ADMINISTER from an earlier assignment.
DELETE FROM guacamole_connection_permission
WHERE connection_id = (SELECT connection_id FROM _sync_connection)
  AND entity_id = (SELECT entity_id FROM _sync_assignee)
  AND permission <> 'READ'::guacamole_object_permission_type;

INSERT INTO guacamole_connection_permission (entity_id, connection_id, permission)
SELECT assignee.entity_id, connection.connection_id,
       'READ'::guacamole_object_permission_type
FROM _sync_assignee AS assignee
CROSS JOIN _sync_connection AS connection
ON CONFLICT (entity_id, connection_id, permission) DO NOTHING;

INSERT INTO guacamole_connection_attribute (connection_id, attribute_name, attribute_value)
SELECT connection.connection_id,
       '{GUAC_MANAGED_ASSIGNEE_ATTRIBUTE}',
       assignee.entity_id::text
FROM _sync_connection AS connection
CROSS JOIN _sync_assignee AS assignee
ON CONFLICT (connection_id, attribute_name) DO UPDATE
SET attribute_value = EXCLUDED.attribute_value;
{attempt_attribute_sql}

INSERT INTO guacamole_connection_permission (entity_id, connection_id, permission)
SELECT admin.entity_id, connection.connection_id, permissions.permission::guacamole_object_permission_type
FROM _sync_admin AS admin
CROSS JOIN _sync_connection AS connection
CROSS JOIN (
    VALUES ('READ'), ('UPDATE'), ('DELETE'), ('ADMINISTER')
) AS permissions(permission)
ON CONFLICT (entity_id, connection_id, permission) DO NOTHING;

DO $$
DECLARE
    assignee_permissions integer;
    admin_permissions integer;
BEGIN
    SELECT count(*) INTO assignee_permissions
    FROM guacamole_connection_permission
    WHERE connection_id = (SELECT connection_id FROM _sync_connection)
      AND entity_id = (SELECT entity_id FROM _sync_assignee)
      AND permission = 'READ'::guacamole_object_permission_type;
    IF assignee_permissions <> 1 OR EXISTS (
        SELECT 1
        FROM guacamole_connection_permission
        WHERE connection_id = (SELECT connection_id FROM _sync_connection)
          AND entity_id = (SELECT entity_id FROM _sync_assignee)
          AND permission <> 'READ'::guacamole_object_permission_type
    ) THEN
        RAISE EXCEPTION 'The declared assignee permissions are invalid';
    END IF;
    SELECT count(*) INTO admin_permissions
    FROM guacamole_connection_permission
    WHERE connection_id = (SELECT connection_id FROM _sync_connection)
      AND entity_id = (SELECT entity_id FROM _sync_admin)
      AND permission = ANY (ARRAY[
          'READ'::guacamole_object_permission_type,
          'UPDATE'::guacamole_object_permission_type,
          'DELETE'::guacamole_object_permission_type,
          'ADMINISTER'::guacamole_object_permission_type
      ]);
    IF admin_permissions <> 4 THEN
        RAISE EXCEPTION 'The administrator permissions are invalid';
    END IF;
END
$$;

{transaction_contract_sql}
SELECT json_build_object(
    'connectionId', connection.connection_id,
    'connectionName', connection.connection_name,
    'created', EXISTS (SELECT 1 FROM _sync_created),
{credential_result_field}
    'permissions', json_build_object(
        'assignee', COALESCE((
            SELECT json_agg(permission::text ORDER BY permission)
            FROM guacamole_connection_permission
            WHERE connection_id = connection.connection_id
              AND entity_id = (SELECT entity_id FROM _sync_assignee)
        ), '[]'::json),
        'guacadmin', COALESCE((
            SELECT json_agg(permission::text ORDER BY permission)
            FROM guacamole_connection_permission
            WHERE connection_id = connection.connection_id
              AND entity_id = (SELECT entity_id FROM _sync_admin)
        ), '[]'::json)
    )
)::text
FROM _sync_connection AS selected
JOIN guacamole_connection AS connection
  ON connection.connection_id = selected.connection_id;

{transaction_end};
"""


def run_psql_sync(
    arguments: Sequence[str],
    sql: str,
    *,
    psql_variables: Mapping[str, str] | None = None,
    secure_values: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute psql with secret values transported as COPY data.

    PostgreSQL receives one generic ``COPY ... FROM STDIN`` statement.  The
    password is in the COPY data stream, never in SQL text, psql ``-v``
    assignments, process argv, or an interpolated statement that a server
    statement logger could record.
    """

    program = sql
    if secure_values is not None:
        if set(secure_values) != {"username", "password"}:
            raise ValidationError("secure psql values are invalid", code="SYNC_INVALID", stage="sync")
        if "-- GUACAMOLE_SECURE_CREDENTIALS" not in program:
            raise ValidationError("secure psql transport marker is missing", code="SYNC_INVALID", stage="sync")

        def csv_value(value: str) -> str:
            if not isinstance(value, str) or any(char in value for char in ("\r", "\n", "\x00")):
                raise CredentialSecretError("Windows credential secret content is invalid")
            return '"' + value.replace('"', '""') + '"'

        copy_program = "COPY _sync_secure_values (name, value) FROM STDIN WITH (FORMAT csv);\n"
        copy_program += f"username,{csv_value(secure_values['username'])}\n"
        copy_program += f"password,{csv_value(secure_values['password'])}\n\\.\n"
        program = program.replace("-- GUACAMOLE_SECURE_CREDENTIALS", copy_program, 1)
    elif "-- GUACAMOLE_SECURE_CREDENTIALS" in program:
        raise CredentialSecretError("Windows credential secret transport is unavailable")
    if psql_variables:
        assignments: list[str] = []
        for name, value in psql_variables.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or not isinstance(value, str):
                raise ValidationError("psql variable is invalid", code="SYNC_INVALID", stage="sync")
            if name in {"sync_username", "sync_password"}:
                raise ValidationError("credential psql variables are not permitted", code="SYNC_INVALID", stage="sync")
            if any(char in value for char in ("\r", "\n", "\x00")):
                raise CredentialSecretError("Windows credential secret content is invalid")
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            assignments.append(f"\\set {name} '{escaped}'")
        # Keep the already-composed COPY block.  Rebuilding from ``sql`` here
        # would silently remove the secure credential transport whenever a
        # caller also supplied ordinary psql variables.
        program = "\n".join(assignments) + "\n" + program

    return subprocess.run(
        list(arguments),
        shell=False,
        check=True,
        input=program,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def _sync_psql_arguments(record: CloneRecord, *, connection_id: int | None = None) -> list[str]:
    """Return the compose/psql argv and keep values out of the SQL text."""

    arguments = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "postgres",
        "psql",
        "-X",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-At",
        "-U",
        "guacamole_user",
        "-d",
        "guacamole_db",
        "-v",
        f"sync_name={record.name}",
        "-v",
        f"sync_ip={record.ip}",
        "-v",
        f"sync_assignee_type={record.assigneeType}",
        "-v",
        f"sync_assignee_name={record.assigneeName}",
    ]
    if record.syncAttemptId is not None:
        arguments.extend(["-v", f"sync_attempt_id={_validate_sync_attempt_id(record.syncAttemptId)}"])
    if connection_id is not None:
        if not isinstance(connection_id, int) or isinstance(connection_id, bool) or connection_id <= 0:
            raise ValidationError("Guacamole connection identity is invalid", code="SYNC_OWNERSHIP_INVALID", stage="sync")
        arguments.extend(["-v", f"sync_connection_id={connection_id}"])
    return arguments


def _parse_sync_result(output: Any) -> dict[str, Any]:
    """Extract the final JSON row emitted by the synchronization SQL."""

    text = _command_output(output)
    results: list[dict[str, Any]] = []
    for line in reversed([item.strip() for item in text.splitlines() if item.strip()]):
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict) and isinstance(result.get("connectionId"), int) and not isinstance(result.get("connectionId"), bool):
            allowed: dict[str, Any] = {"connectionId": result["connectionId"]}
            if isinstance(result.get("connectionName"), str):
                allowed["connectionName"] = result["connectionName"]
            if isinstance(result.get("created"), bool):
                allowed["created"] = result["created"]
            if isinstance(result.get("credentialsPresent"), bool):
                allowed["credentialsPresent"] = result["credentialsPresent"]
            permissions = result.get("permissions")
            if isinstance(permissions, Mapping):
                allowed_permissions: dict[str, list[str]] = {}
                for key in ("assignee", "guacadmin"):
                    values = permissions.get(key)
                    if isinstance(values, list) and all(isinstance(item, str) for item in values):
                        allowed_permissions[key] = list(values)
                if allowed_permissions:
                    allowed["permissions"] = allowed_permissions
            results.append(allowed)
    if len(results) == 1:
        return results[0]
    raise HelperError("Guacamole sync returned no connection result", code="SYNC_INVALID", stage="sync")


def _parse_compensation_result(output: Any) -> dict[str, Any]:
    """Parse the fixed, non-secret result emitted by the compensation SQL."""

    text = _command_output(output)
    for line in reversed([item.strip() for item in text.splitlines() if item.strip()]):
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(result, dict):
            continue
        if set(result) != {"compensated", "deleted", "ownership"}:
            continue
        if (
            isinstance(result.get("compensated"), bool)
            and isinstance(result.get("deleted"), bool)
            and result.get("ownership") in {"owned", "absent", "mismatch"}
            and _is_consistent_compensation_state(
                result["compensated"], result["deleted"], result["ownership"]
            )
        ):
            return {
                "compensated": result["compensated"],
                "deleted": result["deleted"],
                "ownership": result["ownership"],
            }
    raise HelperError("Guacamole compensation result was not confirmed", code="SYNC_COMPENSATION_UNCONFIRMED", stage="rollback")


def _is_consistent_compensation_state(compensated: bool, deleted: bool, ownership: str) -> bool:
    """Return whether compensation fields prove one of the three safe states."""

    return (
        ownership == "owned" and compensated is True and deleted is True
    ) or (
        ownership == "absent" and compensated is True and deleted is False
    ) or (
        ownership == "mismatch" and compensated is False and deleted is False
    )


def build_guacamole_clone_preflight_sql() -> str:
    """Return a read-only identity check that never reads parameter values."""

    return """SELECT count(*)
FROM guacamole_connection
WHERE connection_name = :'sync_name' AND parent_id IS NULL;
"""


def _preflight_guacamole_clone_identity(
    name: str,
    runner,
    *,
    timeout: float = PREFLIGHT_TIMEOUT_SECONDS,
) -> None:
    """Reject a pre-existing connection before a clone can mutate any state."""

    validate_vm_name(name)
    arguments = _sync_psql_arguments(
        CloneRecord(
            name=name,
            mac="52:54:00:00:00:01",
            ip="192.168.250.20",
            assigneeType="USER",
            assigneeName="guacadmin",
            status="preflight",
        )
    )
    try:
        completed = runner(arguments, build_guacamole_clone_preflight_sql(), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HelperError("Guacamole identity preflight timed out", code="COMMAND_TIMEOUT", stage="validate") from exc
    except HelperError:
        raise
    except Exception as exc:
        raise HelperError("Guacamole identity preflight failed", code="COMMAND_FAILED", stage="validate") from exc
    values = [line.strip() for line in _command_output(completed).splitlines() if line.strip()]
    try:
        count = int(values[-1])
    except (IndexError, ValueError) as exc:
        raise HelperError("Guacamole identity preflight returned invalid data", code="GUAC_CONNECTION_INVALID", stage="validate") from exc
    if count != 0:
        raise ConflictError(
            "Guacamole connection or assignment already exists for this workspace name",
            code="GUAC_CONNECTION_CONFLICT",
            stage="validate",
        )


def build_guacamole_probe_sql(
    connection_id: int | None = None,
    *,
    include_windows_credentials: bool = False,
) -> str:
    """Return a read-only post-failure probe bound to this sync attempt."""

    if connection_id is not None:
        if not isinstance(connection_id, int) or isinstance(connection_id, bool) or connection_id <= 0:
            raise ValidationError("Guacamole connection identity is invalid", code="SYNC_OWNERSHIP_INVALID", stage="sync")
        identity_clause = "AND connection.connection_id = :'sync_connection_id'::integer"
    else:
        identity_clause = ""
    credential_field = """\n    'credentialsPresent', EXISTS (
        SELECT 1
        FROM guacamole_connection_parameter AS credential
        WHERE credential.connection_id = connection.connection_id
          AND credential.parameter_name = 'username'
          AND credential.parameter_value <> ''
    ) AND EXISTS (
        SELECT 1
        FROM guacamole_connection_parameter AS credential
        WHERE credential.connection_id = connection.connection_id
          AND credential.parameter_name = 'password'
          AND credential.parameter_value <> ''
    ),""" if include_windows_credentials else ""
    return f"""SELECT json_build_object(
    'connectionId', connection.connection_id,
    'connectionName', connection.connection_name,
    'created', TRUE,
    {credential_field}
    'permissions', json_build_object(
        'assignee', COALESCE((
            SELECT json_agg(permission::text ORDER BY permission)
            FROM guacamole_connection_permission
            WHERE connection_id = connection.connection_id
              AND entity_id = (
                  SELECT entity_id FROM guacamole_entity
                  WHERE type = :'sync_assignee_type'::guacamole_entity_type
                    AND name = :'sync_assignee_name'
              )
        ), '[]'::json),
        'guacadmin', COALESCE((
            SELECT json_agg(permission::text ORDER BY permission)
            FROM guacamole_connection_permission
            WHERE connection_id = connection.connection_id
              AND entity_id = (
                  SELECT entity_id FROM guacamole_entity
                  WHERE type = 'USER'::guacamole_entity_type AND name = 'guacadmin'
              )
        ), '[]'::json)
    )
)::text
FROM guacamole_connection AS connection
JOIN guacamole_connection_attribute AS attempt
  ON attempt.connection_id = connection.connection_id
 AND attempt.attribute_name = 'org.apache.guacamole.workspace.sync.attempt'
 AND attempt.attribute_value = :'sync_attempt_id'
JOIN guacamole_connection_attribute AS assignee_marker
  ON assignee_marker.connection_id = connection.connection_id
 AND assignee_marker.attribute_name = '{GUAC_MANAGED_ASSIGNEE_ATTRIBUTE}'
JOIN guacamole_entity AS assignee
  ON assignee.entity_id = NULLIF(assignee_marker.attribute_value, '')::integer
 AND assignee.type = :'sync_assignee_type'::guacamole_entity_type
 AND assignee.name = :'sync_assignee_name'
WHERE connection.connection_name = :'sync_name'
  AND connection.parent_id IS NULL
  AND connection.protocol = 'rdp'
  {identity_clause};
"""


def _probe_guacamole_sync_state(
    record: CloneRecord,
    runner,
    *,
    timeout: float,
    connection_id: int | None = None,
    include_windows_credentials: bool = False,
) -> dict[str, Any] | None:
    """Find a committed sync after a transport/output failure, without secrets."""

    if record.syncAttemptId is None:
        return None
    try:
        completed = runner(
            _sync_psql_arguments(record, connection_id=connection_id),
            build_guacamole_probe_sql(
                connection_id,
                include_windows_credentials=include_windows_credentials,
            ),
            timeout=timeout,
        )
        result = _parse_sync_result(completed)
        if result.get("created") is not True:
            return None
        _validate_sync_permissions(result)
        if include_windows_credentials and result.get("credentialsPresent") is not True:
            return None
        return result
    except Exception:
        return None


def build_guacamole_restore_sql(record: CloneRecord, connection_id: int) -> str:
    """Build an idempotent delete for a connection created by this clone run."""

    record = _coerce_clone_sync_record(record)
    if record.syncAttemptId is None:
        raise HelperError(
            "Guacamole sync ownership token is unavailable",
            code="SYNC_OWNERSHIP_UNAVAILABLE",
            stage="rollback",
        )
    if not isinstance(connection_id, int) or connection_id <= 0:
        raise ValidationError("Guacamole connection result is invalid", code="SYNC_INVALID")
    return f"""-- GUACAMOLE_RESTORE_CREATED
BEGIN;
LOCK TABLE guacamole_connection, guacamole_connection_parameter,
    guacamole_connection_permission, guacamole_connection_attribute
    IN SHARE ROW EXCLUSIVE MODE;

CREATE TEMP TABLE _sync_target (connection_id integer PRIMARY KEY) ON COMMIT DROP;
INSERT INTO _sync_target (connection_id)
SELECT connection_id
FROM guacamole_connection
WHERE connection_id = :'sync_connection_id'::integer
  AND connection_name = :'sync_name'
  AND parent_id IS NULL
  AND protocol = 'rdp';

CREATE TEMP TABLE _sync_owned (connection_id integer PRIMARY KEY) ON COMMIT DROP;
INSERT INTO _sync_owned (connection_id)
SELECT target.connection_id
FROM _sync_target AS target
JOIN guacamole_connection_attribute AS managed
  ON managed.connection_id = target.connection_id
 AND managed.attribute_name = '{GUAC_MANAGED_ATTEMPT_ATTRIBUTE}'
 AND managed.attribute_value = :'sync_attempt_id'
JOIN guacamole_connection_attribute AS assignee_marker
  ON assignee_marker.connection_id = target.connection_id
 AND assignee_marker.attribute_name = '{GUAC_MANAGED_ASSIGNEE_ATTRIBUTE}'
JOIN guacamole_entity AS assignee
  ON assignee.entity_id = NULLIF(assignee_marker.attribute_value, '')::integer
  AND assignee.type = :'sync_assignee_type'::guacamole_entity_type
  AND assignee.name = :'sync_assignee_name';

DELETE FROM guacamole_connection_attribute
WHERE connection_id = (SELECT connection_id FROM _sync_owned);
DELETE FROM guacamole_connection_permission
WHERE connection_id = (SELECT connection_id FROM _sync_owned);
DELETE FROM guacamole_connection_parameter
WHERE connection_id = (SELECT connection_id FROM _sync_owned);

DELETE FROM guacamole_connection
WHERE connection_id = (SELECT connection_id FROM _sync_owned);

SELECT json_build_object(
    'compensated', NOT EXISTS (SELECT 1 FROM _sync_target)
        OR EXISTS (SELECT 1 FROM _sync_owned),
    'deleted', EXISTS (SELECT 1 FROM _sync_owned),
    'ownership', CASE
        WHEN EXISTS (SELECT 1 FROM _sync_owned) THEN 'owned'
        WHEN NOT EXISTS (SELECT 1 FROM _sync_target) THEN 'absent'
        ELSE 'mismatch'
    END
)::text;

COMMIT;
"""


def build_guacamole_adoption_preflight_sql(connection_id: int) -> str:
    """Read-only proof for the one-time adoption of the retained wtest row."""

    if not isinstance(connection_id, int) or isinstance(connection_id, bool) or connection_id <= 0:
        raise ValidationError("Guacamole connection identity is invalid", code="SYNC_OWNERSHIP_INVALID", stage="validate")
    return f"""WITH target AS (
    SELECT c.connection_id, c.connection_name, c.protocol,
           bool_and(p.parameter_name IN ('hostname', 'port', 'security', 'ignore-cert')) AS only_allowed_parameters,
           bool_or(p.parameter_name = 'hostname' AND p.parameter_value = :'sync_ip') AS hostname_matches,
           bool_or(p.parameter_name = 'port' AND p.parameter_value = '3389') AS port_matches
    FROM guacamole_connection AS c
    LEFT JOIN guacamole_connection_parameter AS p USING (connection_id)
    WHERE c.connection_id = {connection_id}
      AND c.parent_id IS NULL
    GROUP BY c.connection_id, c.connection_name, c.protocol
), assignee AS (
    SELECT COUNT(*) = 1 AS matches
    FROM guacamole_connection_attribute AS marker
    JOIN guacamole_entity AS entity
      ON entity.entity_id = NULLIF(marker.attribute_value, '')::integer
     AND entity.type = :'sync_assignee_type'::guacamole_entity_type
     AND entity.name = :'sync_assignee_name'
    WHERE marker.connection_id = {connection_id}
      AND marker.attribute_name = '{GUAC_MANAGED_ASSIGNEE_ATTRIBUTE}'
), admins AS (
    SELECT COUNT(DISTINCT permission) = 4 AS complete
    FROM guacamole_connection_permission AS permission
    JOIN guacamole_entity AS entity ON entity.entity_id = permission.entity_id
    WHERE permission.connection_id = {connection_id}
      AND entity.type = 'USER'::guacamole_entity_type
      AND entity.name = 'guacadmin'
      AND permission.permission = ANY (ARRAY['READ','UPDATE','DELETE','ADMINISTER']::guacamole_object_permission_type[])
), conflicts AS (
    SELECT COUNT(*) = 0 AS absent
    FROM guacamole_connection
    WHERE connection_name = 'wtest' AND connection_id <> {connection_id}
), attempts AS (
    SELECT COUNT(*) = 0 AS absent
    FROM guacamole_connection_attribute
    WHERE connection_id = {connection_id}
      AND attribute_name = '{GUAC_MANAGED_ATTEMPT_ATTRIBUTE}'
)
SELECT json_build_object(
    'eligible', COALESCE(target.connection_id = {connection_id}, FALSE)
        AND target.connection_name = 'wtest'
        AND target.protocol = 'rdp'
        AND target.only_allowed_parameters
        AND target.hostname_matches
        AND target.port_matches
        AND assignee.matches AND admins.complete AND conflicts.absent AND attempts.absent,
    'connectionId', {connection_id},
    'connectionName', COALESCE(target.connection_name, ''),
    'protocol', COALESCE(target.protocol, ''),
    'hostnameMatches', COALESCE(target.hostname_matches, FALSE),
    'portMatches', COALESCE(target.port_matches, FALSE),
    'assigneeMatches', assignee.matches,
    'adminPermissionsComplete', admins.complete,
    'conflictingConnectionAbsent', conflicts.absent,
    'attemptMarkerAbsent', attempts.absent
)
FROM target
RIGHT JOIN assignee ON TRUE
CROSS JOIN admins
CROSS JOIN conflicts
CROSS JOIN attempts;
"""


def _parse_adoption_preflight_result(output: Any) -> dict[str, Any]:
    text = _command_output(output)
    for line in reversed([item.strip() for item in text.splitlines() if item.strip()]):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("eligible"), bool):
            return {
                key: value.get(key)
                for key in (
                    "eligible", "connectionId", "connectionName", "protocol", "hostnameMatches",
                    "portMatches", "assigneeMatches", "adminPermissionsComplete",
                    "conflictingConnectionAbsent", "attemptMarkerAbsent",
                )
                if key in value
            }
    raise HelperError("Guacamole adoption preflight returned invalid data", code="GUAC_CONNECTION_INVALID", stage="validate")


def adopt_guacamole_connection(
    name: str,
    connection_id: int,
    *,
    inventory_path: str | os.PathLike[str] | None = None,
    preflight_runner=run_psql_sync,
    sync_runner=run_psql_sync,
    timeout: float = GUACAMOLE_SYNC_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Explicitly adopt one audited legacy row, then add marker and credentials."""

    if name != "wtest":
        raise ValidationError("Only the audited wtest connection may be adopted", code="SYNC_OWNERSHIP_INVALID", stage="validate")
    if not isinstance(connection_id, int) or isinstance(connection_id, bool) or connection_id <= 0:
        raise ValidationError("Guacamole connection identity is invalid", code="SYNC_OWNERSHIP_INVALID", stage="validate")
    inventory = load_inventory(inventory_path)
    matches = [item for item in inventory.get("clones", []) if isinstance(item, Mapping) and item.get("name") == name]
    if len(matches) != 1:
        raise HelperError("Audited inventory record is unavailable", code="SYNC_OWNERSHIP_UNAVAILABLE", stage="validate")
    record = _coerce_clone_sync_record(matches[0])
    if record.syncAttemptId is None:
        raise HelperError("Audited inventory sync ownership token is unavailable", code="SYNC_OWNERSHIP_UNAVAILABLE", stage="validate")
    conflicting_ids = [
        item.get("connectionId") for item in inventory.get("clones", [])
        if isinstance(item, Mapping) and item.get("name") != name and item.get("connectionId") == connection_id
    ]
    if conflicting_ids or (isinstance(matches[0].get("connectionId"), int) and matches[0].get("connectionId") != connection_id):
        raise ConflictError("Guacamole connection identity conflicts with inventory", code="GUAC_CONNECTION_CONFLICT", stage="validate")
    preflight = preflight_runner(
        _sync_psql_arguments(record, connection_id=connection_id),
        build_guacamole_adoption_preflight_sql(connection_id),
        timeout=timeout,
    )
    proof = _parse_adoption_preflight_result(preflight)
    if proof.get("eligible") is not True:
        raise ConflictError("Audited Guacamole connection failed the adoption proof", code="GUAC_CONNECTION_CONFLICT", stage="validate")
    # This read happens only after the read-only proof and is required before
    # the atomic marker+parameter transaction can start.
    read_windows_credential_secret()
    result = sync_guacamole(
        record,
        runner=sync_runner,
        verify_live=False,
        connection_id=connection_id,
        include_windows_credentials=True,
        adopt_existing_connection=True,
        timeout=timeout,
    )
    clone = result.get("clone")
    if not isinstance(clone, Mapping) or clone.get("status") != "ready" or clone.get("connectionId") != connection_id:
        raise HelperError("Guacamole adoption did not produce a ready workspace", code="SYNC_INVALID", stage="sync")
    return result


def _guacamole_compensation(
    record: CloneRecord,
    sync_result: Mapping[str, Any],
    runner,
    timeout: float,
):
    """Return an idempotent callback for deleting this run's new connection."""

    if sync_result.get("created") is not True:
        raise ValidationError("Only a newly-created Guacamole connection can be compensated", code="SYNC_INVALID")
    if record.syncAttemptId is None:
        raise HelperError(
            "Guacamole sync ownership token is unavailable",
            code="SYNC_OWNERSHIP_UNAVAILABLE",
            stage="rollback",
        )
    try:
        connection_id = int(sync_result["connectionId"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("Guacamole connection result is invalid", code="SYNC_INVALID") from exc
    sql = build_guacamole_restore_sql(record, connection_id)
    arguments = _sync_psql_arguments(record) + [
        "-v",
        f"sync_connection_id={connection_id}",
    ]
    state = {"done": False}

    def compensate() -> list[BaseException]:
        if state["done"]:
            return []
        try:
            confirmation = _parse_compensation_result(runner(arguments, sql, timeout=timeout))
        except subprocess.TimeoutExpired as exc:
            error = HelperError("Guacamole compensation timed out", code="COMMAND_TIMEOUT", stage="rollback")
            error.__cause__ = exc
            return [error]
        except HelperError as exc:
            return [exc]
        except Exception as exc:
            error = HelperError("Guacamole compensation failed", code="COMMAND_FAILED", stage="rollback")
            error.__cause__ = exc
            return [error]
        if not _is_consistent_compensation_state(
            confirmation["compensated"], confirmation["deleted"], confirmation["ownership"]
        ):
            return [
                HelperError(
                    "Guacamole compensation ownership could not be confirmed",
                    code="SYNC_COMPENSATION_UNCONFIRMED",
                    stage="rollback",
                )
            ]
        if not confirmation["compensated"]:
            return [
                HelperError(
                    "Guacamole compensation ownership could not be confirmed",
                    code="SYNC_COMPENSATION_UNCONFIRMED",
                    stage="rollback",
                )
            ]
        state["done"] = True
        return []

    return compensate


def _raise_after_guacamole_compensation(primary: HelperError, compensation) -> None:
    """Raise the primary error after restoring committed Guacamole state."""

    if compensation is None:
        raise primary
    errors = compensation()
    if errors:
        details = "; ".join(_error_text(error) for error in errors)
        raise HelperError(
            f"{primary.message}; Guacamole compensation failed: {details}",
            code="ROLLBACK_FAILED",
            stage="rollback",
        ) from errors[0]
    raise HelperError(
        f"{primary.message}; committed Guacamole state was compensated",
        code=primary.code,
        stage=primary.stage,
    ) from primary


def _validate_sync_permissions(result: Mapping[str, Any]) -> None:
    """Require the workflow's exact direct permission contract."""

    permissions = result.get("permissions")
    if not isinstance(permissions, Mapping):
        raise HelperError("Guacamole sync returned no permission inventory", code="SYNC_PERMISSIONS_INVALID", stage="permissions")
    assignee_permissions = permissions.get("assignee")
    if not isinstance(assignee_permissions, list) or assignee_permissions != ["READ"]:
        raise HelperError(
            "Selected Guacamole assignee does not have exactly READ permission",
            code="SYNC_PERMISSIONS_INVALID",
            stage="permissions",
        )
    admin_permissions = permissions.get("guacadmin")
    required_admin = {"READ", "UPDATE", "DELETE", "ADMINISTER"}
    if not isinstance(admin_permissions, list) or not required_admin.issubset(set(admin_permissions)):
        raise HelperError(
            "Guacamole administrator permissions are incomplete",
            code="SYNC_PERMISSIONS_INVALID",
            stage="permissions",
        )


def _validate_sync_credentials(result: Mapping[str, Any]) -> None:
    """Require both managed Windows authentication fields without reading values."""

    if result.get("credentialsPresent") is not True:
        raise HelperError(
            "Managed Windows credential parameters are incomplete",
            code="CREDENTIAL_SYNC_INVALID",
            stage="credentials",
        )


def _sync_result_for_admin_only(record: CloneRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "status": "ADMIN_ONLY",
        "connectionId": None,
        "permissions": {},
    }


def _sync_result_for_dry_run(record: CloneRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "status": "ready",
        "connectionId": None,
        "connectionName": record.name,
        "parameters": {
            "hostname": record.ip,
            "port": "3389",
            "security": "any",
            "ignore-cert": "true",
        },
        "assignee": {
            "type": record.assigneeType,
            "name": record.assigneeName,
            "permissions": ["READ"],
        },
        "guacadminPermissions": ["READ", "UPDATE", "DELETE", "ADMINISTER"],
    }


def _verify_clone_live_state(record: CloneRecord, runner=None) -> None:
    """Verify only the declared clone's libvirt identity and DHCP address."""

    if runner is None:
        runner = run_command
    info = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "dominfo", record.name],
        "sync-verify",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    fields = _preflight_field_map(_command_output(info))
    if fields.get("name") != record.name:
        raise HelperError("Inventory domain name does not match", code="CLONE_STATE_INVALID", stage="sync-verify")

    xml_result = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "dumpxml", record.name],
        "sync-verify",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    try:
        root = ET.fromstring(_command_output(xml_result))
    except ET.ParseError as exc:
        raise HelperError("Inventory domain XML is invalid", code="CLONE_STATE_INVALID", stage="sync-verify") from exc
    if root.findtext("name", "").strip() != record.name:
        raise HelperError("Inventory domain XML name does not match", code="CLONE_STATE_INVALID", stage="sync-verify")
    matching_interface = False
    for interface in root.findall("./devices/interface"):
        mac_element = interface.find("mac")
        source = interface.find("source")
        if mac_element is None or source is None:
            continue
        if (mac_element.get("address") or "").lower() == record.mac.lower() and source.get("network") == NETWORK_NAME:
            matching_interface = True
            break
    if not matching_interface:
        raise HelperError("Inventory domain MAC or network does not match", code="CLONE_STATE_INVALID", stage="sync-verify")

    lease_result = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "net-dhcp-leases", NETWORK_NAME],
        "sync-verify",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    if _lease_for_mac(_command_output(lease_result), record.mac) != record.ip:
        raise HelperError("Inventory domain DHCP address does not match", code="CLONE_STATE_INVALID", stage="sync-verify")


def _verify_repair_live_state(
    record: CloneRecord,
    runner=None,
    *,
    inventory_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Prove every durable clone identity before a repair can touch Guacamole."""

    if runner is None:
        runner = run_command
    if record.templateVersion is None or record.syncAttemptId is None:
        raise HelperError(
            "Workspace ownership proof is unavailable",
            code="SYNC_OWNERSHIP_UNAVAILABLE",
            stage="sync-verify",
        )
    inventory = load_inventory(inventory_path)
    current_record = _workspace_inventory_record(record.name, inventory)
    if not isinstance(current_record, Mapping):
        raise HelperError("Workspace record is missing", code="WORKSPACE_NOT_FOUND", stage="sync-verify")
    for key, expected in (
        ("name", record.name),
        ("mac", record.mac),
        ("ip", record.ip),
        ("assigneeType", record.assigneeType),
        ("assigneeName", record.assigneeName),
        ("templateVersion", record.templateVersion),
        ("syncAttemptId", record.syncAttemptId),
    ):
        if current_record.get(key) != expected:
            raise HelperError("Workspace inventory identity changed", code="CLONE_STATE_INVALID", stage="sync-verify")
    template = verify_template_record(_template_record(inventory, record.templateVersion), runner=runner)
    try:
        marker = _read_workspace_marker(_workspace_ownership_path(record.name))
    except HelperError as exc:
        raise HelperError("Workspace ownership marker is invalid", code="CLONE_STATE_INVALID", stage="sync-verify") from exc
    expected_attempt = _validate_sync_attempt_id(record.syncAttemptId)
    if any(
        marker.get(key) != expected
        for key, expected in (
            ("name", record.name),
            ("templateVersion", record.templateVersion),
            ("mac", record.mac.lower()),
            ("ip", record.ip),
            ("syncAttemptId", expected_attempt),
        )
    ):
        raise HelperError("Workspace ownership marker does not match inventory", code="CLONE_STATE_INVALID", stage="sync-verify")
    clone_uuid = marker["cloneUuid"]

    info = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "dominfo", record.name],
        "sync-verify",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    fields = _preflight_field_map(_command_output(info))
    if fields.get("name") != record.name or fields.get("uuid", "").lower() != clone_uuid.lower() or fields.get("state", "").lower() != "running":
        raise HelperError("Inventory domain identity does not match", code="CLONE_STATE_INVALID", stage="sync-verify")

    xml_result = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "dumpxml", record.name],
        "sync-verify",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    try:
        root = ET.fromstring(_command_output(xml_result))
    except ET.ParseError as exc:
        raise HelperError("Inventory domain XML is invalid", code="CLONE_STATE_INVALID", stage="sync-verify") from exc
    if root.findtext("name", "").strip() != record.name or (root.findtext("uuid", "").strip().lower() != clone_uuid.lower()):
        raise HelperError("Inventory domain UUID or name does not match", code="CLONE_STATE_INVALID", stage="sync-verify")
    try:
        metadata = _workspace_xml_metadata(root, record.name)
    except HelperError as exc:
        raise HelperError("Libvirt workspace ownership metadata is invalid", code="CLONE_STATE_INVALID", stage="sync-verify") from exc
    if metadata != {"name": record.name, "templateVersion": record.templateVersion}:
        raise HelperError("Libvirt workspace ownership metadata does not match", code="CLONE_STATE_INVALID", stage="sync-verify")

    overlay_path = VMS_DIR / f"{record.name}.qcow2"
    nvram_path = CLONE_NVRAM_DIR / f"{record.name}_VARS.fd"
    expected_nvram_marker = _workspace_nvram_marker_path(nvram_path)
    for path in (overlay_path, nvram_path, expected_nvram_marker, _workspace_tpm_marker_path(clone_uuid)):
        if not path.is_file():
            raise HelperError("Workspace backing or ownership marker is unavailable", code="CLONE_STATE_INVALID", stage="sync-verify")
    try:
        nvram_marker = _read_workspace_marker(expected_nvram_marker)
        tpm_marker = _read_workspace_marker(_workspace_tpm_marker_path(clone_uuid))
    except HelperError as exc:
        raise HelperError("Workspace NVRAM or TPM ownership marker is invalid", code="CLONE_STATE_INVALID", stage="sync-verify") from exc
    if nvram_marker != marker or tpm_marker != marker:
        raise HelperError("Workspace ownership markers disagree", code="CLONE_STATE_INVALID", stage="sync-verify")

    disk_sources = []
    for disk in root.findall("./devices/disk"):
        source = disk.find("source")
        if source is not None and source.get("file"):
            disk_sources.append(_retirement_path(source.get("file")))
    if _retirement_path(overlay_path) not in disk_sources:
        raise HelperError("Inventory disk does not match the managed overlay", code="CLONE_STATE_INVALID", stage="sync-verify")
    nvram = root.find("./os/nvram")
    if nvram is None or _retirement_path((nvram.text or "").strip()) != _retirement_path(nvram_path):
        raise HelperError("Inventory NVRAM path does not match", code="CLONE_STATE_INVALID", stage="sync-verify")
    matching_interface = False
    for interface in root.findall("./devices/interface"):
        mac_element = interface.find("mac")
        source = interface.find("source")
        if mac_element is not None and source is not None and (mac_element.get("address") or "").lower() == record.mac.lower() and source.get("network") == NETWORK_NAME:
            matching_interface = True
            break
    if not matching_interface:
        raise HelperError("Inventory domain MAC or network does not match", code="CLONE_STATE_INVALID", stage="sync-verify")

    backing = _run_stage(
        runner,
        ["qemu-img", "info", "--force-share", "--output=json", "--backing-chain", str(overlay_path)],
        "sync-verify",
        timeout=CONVERSION_TIMEOUT_SECONDS,
    )
    try:
        backing_payload = json.loads(_command_output(backing))
    except json.JSONDecodeError as exc:
        raise HelperError("Workspace backing chain is invalid", code="CLONE_STATE_INVALID", stage="sync-verify") from exc
    backing_paths: set[str] = set()

    def collect_paths(value: Any) -> None:
        if isinstance(value, Mapping):
            filename = value.get("filename")
            if isinstance(filename, str) and filename:
                backing_paths.add(_retirement_path(filename))
            for child in value.values():
                collect_paths(child)
        elif isinstance(value, list):
            for child in value:
                collect_paths(child)

    collect_paths(backing_payload)
    if _retirement_path(overlay_path) not in backing_paths or _retirement_path(template.path) not in backing_paths:
        raise HelperError("Workspace disk backing does not match its template", code="CLONE_STATE_INVALID", stage="sync-verify")
    lease_result = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "net-dhcp-leases", NETWORK_NAME],
        "sync-verify",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    if _lease_for_mac(_command_output(lease_result), record.mac) != record.ip:
        raise HelperError("Inventory domain DHCP address does not match", code="CLONE_STATE_INVALID", stage="sync-verify")
    wait_for_clone_rdp(record.ip, runner, CLONE_RDP_TIMEOUT_SECONDS)
    return {"cloneUuid": clone_uuid, "syncAttemptId": expected_attempt}


def _raise_unknown_sync_failure(
    record: CloneRecord,
    inventory_path: str | os.PathLike[str],
    inventory: Mapping[str, Any] | None,
    message: str,
    cause: BaseException | None = None,
) -> None:
    """Retain inventory-backed clones before raising an unproven sync result."""

    error = HelperError(message, code="SYNC_COMMIT_UNKNOWN", stage="sync")
    if inventory is not None:
        _mark_clone_sync_failure(record, inventory_path, error)
    if cause is None:
        raise error
    raise error from cause


def sync_guacamole(
    clone: Mapping[str, Any] | CloneRecord | None = None,
    *,
    all_clones: bool = False,
    inventory_path: str | os.PathLike[str] | None = None,
    what_if: bool = False,
    runner=run_psql_sync,
    libvirt_runner=None,
    verify_live: bool = False,
    timeout: float = GUACAMOLE_SYNC_TIMEOUT_SECONDS,
    transaction_end: str = "COMMIT",
    compensation_holder: dict[str, Any] | None = None,
    require_new_connection: bool = False,
    connection_id: int | None = None,
    require_owned_connection: bool = False,
    unknown_commit_on_failure: bool | None = None,
    include_windows_credentials: bool = False,
    adopt_existing_connection: bool = False,
) -> dict[str, Any]:
    """Synchronize one clone or only the clones recorded in inventory.

    ``all_clones`` deliberately reads inventory and never discovers domains.
    A record without an assignee is returned as ``ADMIN_ONLY`` and cannot
    trigger a database command.
    """

    target_inventory_path = Path(INVENTORY_PATH if inventory_path is None else inventory_path)
    if libvirt_runner is None:
        libvirt_runner = run_command
    if unknown_commit_on_failure is None:
        unknown_commit_on_failure = runner is run_psql_sync
    inventory: dict[str, Any] | None = None
    if all_clones or clone is None:
        inventory = load_inventory(target_inventory_path)
        records = [_coerce_clone_sync_record(item) for item in inventory.get("clones", [])]
    else:
        records = [_coerce_clone_sync_record(clone)]

    seen_names: set[str] = set()
    for record in records:
        if record.name in seen_names:
            raise ConflictError(
                "Inventory contains duplicate clone names",
                code="DUPLICATE_CLONE",
                stage="validate",
            )
        seen_names.add(record.name)

    if transaction_end not in {"COMMIT", "ROLLBACK"}:
        raise ValidationError("Transaction end is invalid", code="SYNC_INVALID")
    if require_owned_connection:
        if connection_id is None:
            raise HelperError(
                "Guacamole connection ownership cannot be proven",
                code="SYNC_OWNERSHIP_UNAVAILABLE",
                stage="sync",
            )
        if not isinstance(connection_id, int) or isinstance(connection_id, bool) or connection_id <= 0:
            raise ValidationError("Guacamole connection identity is invalid", code="SYNC_OWNERSHIP_INVALID", stage="sync")
    if adopt_existing_connection:
        if require_new_connection or require_owned_connection:
            raise ValidationError("Guacamole adoption mode is invalid", code="SYNC_INVALID", stage="sync")
        if not include_windows_credentials:
            raise ValidationError("Guacamole adoption requires credentials", code="CREDENTIAL_SYNC_INVALID", stage="credentials")
        if not isinstance(connection_id, int) or isinstance(connection_id, bool) or connection_id <= 0:
            raise ValidationError("Guacamole connection identity is invalid", code="SYNC_OWNERSHIP_INVALID", stage="sync")
    windows_credentials: WindowsCredential | None = None
    if include_windows_credentials and not what_if and any(record.assigneeType is not None for record in records):
        windows_credentials = read_windows_credential_secret()
    results: list[dict[str, Any]] = []
    updated_inventory = dict(inventory) if inventory is not None else None
    if updated_inventory is not None:
        updated_inventory["clones"] = list(updated_inventory.get("clones", []))

    if require_new_connection and not what_if:
        if updated_inventory is None:
            if any(record.syncAttemptId is None for record in records):
                raise HelperError(
                    "Guacamole sync ownership token is unavailable",
                    code="SYNC_OWNERSHIP_UNAVAILABLE",
                    stage="sync",
                )
        else:
            ownership_changed = False
            for index, record in enumerate(records):
                if record.syncAttemptId is not None:
                    continue
                record.syncAttemptId = str(uuid.uuid4())
                original = updated_inventory["clones"][index]
                if not isinstance(original, Mapping):
                    raise InventoryError("Inventory clone record is invalid")
                updated = dict(original)
                updated["syncAttemptId"] = record.syncAttemptId
                updated_inventory["clones"][index] = updated
                ownership_changed = True
            if ownership_changed:
                save_inventory_atomic(updated_inventory, target_inventory_path)

    for index, record in enumerate(records):
        if verify_live:
            _verify_clone_live_state(record, libvirt_runner)
        if record.assigneeType is None:
            result = _sync_result_for_admin_only(record)
        elif what_if:
            result = _sync_result_for_dry_run(record)
        else:
            sql = build_guacamole_sync_sql(
                record,
                transaction_end=transaction_end,
                require_new_connection=require_new_connection,
                connection_id=connection_id,
                require_owned_connection=require_owned_connection,
                include_windows_credentials=include_windows_credentials,
                adopt_existing_connection=adopt_existing_connection,
            )
            compensation = None
            try:
                sync_arguments = _sync_psql_arguments(
                    record,
                    connection_id=connection_id if (require_owned_connection or adopt_existing_connection) else None,
                )
                if include_windows_credentials and windows_credentials is not None and runner is run_psql_sync:
                    completed = runner(
                        sync_arguments,
                        sql,
                        secure_values={
                            "username": windows_credentials.username,
                            "password": windows_credentials.password,
                        },
                        timeout=timeout,
                    )
                else:
                    completed = runner(sync_arguments, sql, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                completed = None
                sync_result = (
                    _probe_guacamole_sync_state(
                        record,
                        runner,
                        timeout=timeout,
                        connection_id=connection_id if (require_owned_connection or adopt_existing_connection) else None,
                        include_windows_credentials=include_windows_credentials,
                    )
                    if transaction_end == "COMMIT"
                    else None
                )
                if sync_result is None:
                    if transaction_end == "COMMIT":
                        _raise_unknown_sync_failure(
                            record,
                            target_inventory_path,
                            updated_inventory,
                            "Guacamole synchronization outcome is unknown; clone was retained for repair",
                            exc,
                        )
                    raise HelperError("Guacamole sync timed out", code="COMMAND_TIMEOUT", stage="sync") from exc
            except Exception as exc:
                sync_result = (
                    _probe_guacamole_sync_state(
                        record,
                        runner,
                        timeout=timeout,
                        connection_id=connection_id if (require_owned_connection or adopt_existing_connection) else None,
                        include_windows_credentials=include_windows_credentials,
                    )
                    if transaction_end == "COMMIT"
                    else None
                )
                if sync_result is None:
                    if transaction_end == "COMMIT":
                        _raise_unknown_sync_failure(
                            record,
                            target_inventory_path,
                            updated_inventory,
                            "Guacamole synchronization outcome is unknown; clone was retained for repair",
                            exc,
                        )
                    if isinstance(exc, HelperError):
                        raise
                    raise HelperError("Guacamole sync failed", code="COMMAND_FAILED", stage="sync") from exc
            else:
                try:
                    sync_result = _parse_sync_result(completed)
                except HelperError as exc:
                    sync_result = (
                        _probe_guacamole_sync_state(
                            record,
                            runner,
                            timeout=timeout,
                            connection_id=connection_id if (require_owned_connection or adopt_existing_connection) else None,
                            include_windows_credentials=include_windows_credentials,
                        )
                        if transaction_end == "COMMIT"
                        else None
                    )
                    if sync_result is None:
                        if transaction_end == "COMMIT":
                            _raise_unknown_sync_failure(
                                record,
                                target_inventory_path,
                                updated_inventory,
                                "Guacamole synchronization outcome is unknown; clone was retained for repair",
                                exc,
                            )
                        raise exc
            if require_new_connection and sync_result.get("created") is not True:
                if transaction_end == "COMMIT":
                    probed = _probe_guacamole_sync_state(
                        record,
                        runner,
                        timeout=timeout,
                        include_windows_credentials=include_windows_credentials,
                    )
                    if probed is None:
                        _raise_unknown_sync_failure(
                            record,
                            target_inventory_path,
                            updated_inventory,
                            "Guacamole synchronization ownership could not be proven; clone was retained for repair",
                        )
                    sync_result = probed
            if (require_owned_connection or adopt_existing_connection) and sync_result.get("connectionId") != connection_id:
                if transaction_end == "COMMIT":
                    # The transaction contract above proves the selected row's
                    # identity while the transaction is still open.  A
                    # mismatching client result therefore cannot be used to
                    # reject an already-committed adoption/repair: retain the
                    # bound identity and let the server-side proof remain the
                    # source of truth.
                    sync_result = dict(sync_result)
                    sync_result["connectionId"] = connection_id
                else:
                    raise HelperError(
                        "Guacamole synchronization returned an unexpected connection identity",
                        code="SYNC_OWNERSHIP_INVALID",
                        stage="sync",
                    )
            if require_new_connection and sync_result.get("created") is not True:
                raise ConflictError(
                    "Guacamole connection or assignment already exists for this workspace name",
                    code="GUAC_CONNECTION_CONFLICT",
                    stage="validate",
                )
            if transaction_end == "COMMIT" and sync_result.get("created") is True:
                try:
                    compensation = _guacamole_compensation(record, sync_result, runner, timeout)
                except HelperError as exc:
                    _raise_unknown_sync_failure(
                        record,
                        target_inventory_path,
                        updated_inventory,
                        f"Guacamole synchronization ownership could not be prepared for repair: {exc.message}",
                        exc,
                    )
            if compensation_holder is not None:
                compensation_holder.clear()
                if compensation is not None:
                    compensation_holder["rollback"] = compensation
            if transaction_end != "COMMIT":
                # The COMMIT path is guarded by transaction_contract_sql,
                # including the exact permissions and credential allowlist,
                # before COMMIT.  Re-validating the psql result here can turn
                # a successful adoption/repair into a post-COMMIT failure if
                # output is truncated or reshaped by the transport.  The
                # disposable ROLLBACK path still validates the returned
                # contract for focused checks and unit tests.
                _validate_sync_permissions(sync_result)
                if include_windows_credentials:
                    _validate_sync_credentials(sync_result)
            result = {
                "name": record.name,
                "status": "ready" if transaction_end == "COMMIT" else "rolled-back",
                "connectionId": sync_result.get("connectionId"),
                "created": sync_result.get("created", False),
                "permissions": sync_result.get("permissions", {}),
            }
            if isinstance(sync_result.get("connectionName"), str):
                result["connectionName"] = sync_result["connectionName"]
        results.append(result)
        if (
            updated_inventory is not None
            and not what_if
            and transaction_end == "COMMIT"
            and result["status"] == "ready"
        ):
            original = updated_inventory["clones"][index]
            if isinstance(original, Mapping):
                replaced = dict(original)
                replaced["status"] = "ready"
                updated_inventory["clones"][index] = replaced
                try:
                    save_inventory_atomic(updated_inventory, target_inventory_path)
                except BaseException as exc:
                    if compensation is None:
                        raise HelperError(
                            "Guacamole sync committed but inventory persistence failed and no new-connection compensation is available",
                            code="ROLLBACK_FAILED",
                            stage="rollback",
                        ) from exc
                    errors = compensation()
                    if errors:
                        details = "; ".join(_error_text(error) for error in errors)
                        raise HelperError(
                            f"Inventory persistence failed; Guacamole compensation failed: {details}",
                            code="ROLLBACK_FAILED",
                            stage="rollback",
                        ) from errors[0]
                    raise HelperError(
                        "Inventory persistence failed; committed Guacamole state was compensated",
                        code="INVENTORY_INVALID",
                        stage="inventory",
                    ) from exc

    payload: dict[str, Any] = {"ok": True, "whatIf": what_if, "clones": results}
    if clone is not None and not all_clones and len(results) == 1:
        payload["clone"] = results[0]
    return payload


RDP_NOT_READY = "RDP_NOT_READY"
CLONE_PROGRESS_STAGES = (
    "validation",
    "disk-overlay",
    "domain",
    "dhcp",
    "rdp",
    "guacamole",
    "permissions",
)


ProgressCallback = Callable[[str, str], None]


def _progress(callback: ProgressCallback | None, stage: str, status: str) -> None:
    if callback is not None:
        callback(stage, status)


def clone_progress(status: str) -> list[dict[str, str]]:
    """Return safe, deterministic stage state for Cockpit and CLI callers."""

    if status == "ready":
        states = {stage: "ready" for stage in CLONE_PROGRESS_STAGES}
    elif status == "waiting-rdp":
        states = {stage: "pending" for stage in CLONE_PROGRESS_STAGES}
        for stage in CLONE_PROGRESS_STAGES[:4]:
            states[stage] = "ready"
        states["rdp"] = "waiting"
    elif status == "what-if":
        states = {stage: "planned" for stage in CLONE_PROGRESS_STAGES}
    else:
        states = {stage: "pending" for stage in CLONE_PROGRESS_STAGES}
    return [{"stage": stage, "status": states[stage]} for stage in CLONE_PROGRESS_STAGES]


_SAFE_ERROR_MESSAGES = {
    "SYNC_COMMIT_UNKNOWN": "Guacamole synchronization outcome is unknown; repair is required",
    "ROLLBACK_FAILED": "Workspace compensation could not be confirmed; repair is required",
    "SYNC_OWNERSHIP_UNAVAILABLE": "Guacamole sync ownership proof is unavailable; repair is required",
    "SYNC_OWNERSHIP_INVALID": "Guacamole sync ownership proof is invalid; repair is required",
    "CREDENTIAL_SECRET_MISSING": "Windows credential secret is not initialized",
    "CREDENTIAL_SECRET_INVALID": "Windows credential secret is unavailable or has unsafe permissions",
    "CREDENTIAL_SYNC_INVALID": "Managed Windows credential parameters are incomplete; repair is required",
}


def _safe_error_message(error: HelperError) -> str:
    """Return a fixed diagnostic that cannot carry subprocess or secret data."""

    return _SAFE_ERROR_MESSAGES.get(error.code, f"{error.stage} failed; repair may be required")


def _safe_compensation_detail(error: BaseException) -> str:
    """Describe compensation failure without exposing runner output."""

    if isinstance(error, HelperError):
        return f"{error.code} at {error.stage}"
    return "COMPENSATION_UNCONFIRMED"


def _mark_clone_sync_failure(
    record: CloneRecord,
    inventory_path: str | os.PathLike[str] | None,
    error: HelperError,
    *,
    ensure_record: bool = False,
) -> bool:
    """Persist a repairable post-VM failure without claiming the clone is ready."""

    target_inventory_path = Path(INVENTORY_PATH if inventory_path is None else inventory_path)
    try:
        inventory = load_inventory(target_inventory_path)
        for index, item in enumerate(inventory.get("clones", [])):
            if not isinstance(item, Mapping) or item.get("name") != record.name:
                continue
            failed = dict(item)
            failed["status"] = "sync-failed"
            failed["errorCode"] = error.code
            failed["errorStage"] = error.stage
            failed["errorMessage"] = _safe_error_message(error)
            inventory["clones"][index] = failed
            save_inventory_atomic(inventory, target_inventory_path)
            return True
        if ensure_record:
            failed = record.to_dict()
            failed["status"] = "sync-failed"
            failed["errorCode"] = error.code
            failed["errorStage"] = error.stage
            failed["errorMessage"] = _safe_error_message(error)
            inventory.setdefault("clones", []).append(failed)
            save_inventory_atomic(inventory, target_inventory_path)
            return True
    except Exception:
        # The original pending record is safer than replacing it with an
        # untracked live VM when durable failure recording is unavailable.
        return False
    return False


def clone_workspace_and_sync(
    name: str,
    assignee_type: str,
    assignee_name: str,
    template_version: str,
    *,
    memory_mib: int = 4096,
    vcpus: int = 2,
    wait_rdp_minutes: float = 20.0,
    runner=None,
    inventory_path: str | os.PathLike[str] | None = None,
    sync_runner=run_psql_sync,
    guacamole_preflight_runner=None,
    unknown_commit_on_failure: bool | None = None,
    what_if: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any] | None:
    """Create a clone, then synchronize its declared Guacamole assignment."""

    if runner is None:
        runner = run_command
    if guacamole_preflight_runner is None and sync_runner is run_psql_sync:
        guacamole_preflight_runner = sync_runner
    rollback_holder: dict[str, Any] = {}
    record = clone_workspace(
        name,
        assignee_type,
        assignee_name,
        template_version,
        memory_mib=memory_mib,
        vcpus=vcpus,
        wait_rdp_minutes=wait_rdp_minutes,
        runner=runner,
        inventory_path=inventory_path,
        guacamole_preflight_runner=guacamole_preflight_runner,
        what_if=what_if,
        progress_callback=progress_callback,
        _rollback_holder=rollback_holder,
    )
    if record is None:
        return {
            "ok": True,
            "whatIf": True,
            "status": "what-if",
            "name": name,
            "template": template_version,
            "progress": clone_progress("what-if"),
        }
    if record.status == "waiting-rdp":
        return {
            "ok": True,
            "status": "waiting-rdp",
            "stage": "rdp",
            "clone": record.to_dict(),
            "progress": clone_progress("waiting-rdp"),
        }

    was_existing_ready = record.status == "ready"
    rollback = rollback_holder.get("rollback") if not was_existing_ready else None
    guacamole_rollback_holder: dict[str, Any] = {}
    _progress(progress_callback, "guacamole", "running")
    failure: HelperError | None = None
    try:
        sync_payload = sync_guacamole(
            record,
            # The final ready inventory write belongs to this orchestration
            # boundary.  A newly-created connection can be deleted by its
            # ownership-checked compensation callback; an unknown COMMIT is
            # retained as sync-failed instead of rolling back the VM.
            inventory_path=None,
            runner=sync_runner,
            libvirt_runner=runner,
            verify_live=True,
            compensation_holder=guacamole_rollback_holder,
            require_new_connection=not was_existing_ready,
            unknown_commit_on_failure=unknown_commit_on_failure,
            include_windows_credentials=True,
        )
        sync_record = sync_payload.get("clone")
        if not isinstance(sync_record, Mapping) or sync_record.get("status") != "ready":
            raise HelperError(
                "Guacamole synchronization did not produce a ready workspace",
                code="SYNC_INVALID",
                stage="sync",
            )
        clone_payload = record.to_dict()
        clone_payload.update(sync_record)
        clone_payload["assignee"] = {
            "type": record.assigneeType,
            "name": record.assigneeName,
        }
        target_inventory_path = Path(INVENTORY_PATH if inventory_path is None else inventory_path)
        inventory = load_inventory(target_inventory_path)
        found = False
        for index, item in enumerate(inventory.get("clones", [])):
            if isinstance(item, Mapping) and item.get("name") == record.name:
                updated = dict(item)
                updated["status"] = "ready"
                if isinstance(sync_record.get("connectionId"), int):
                    updated["connectionId"] = sync_record["connectionId"]
                inventory["clones"][index] = updated
                save_inventory_atomic(inventory, target_inventory_path)
                found = True
                break
        if not found:
            raise HelperError(
                "Clone inventory record disappeared before ready state was saved",
                code="INVENTORY_STALE",
                stage="inventory",
            )
    except HelperError as exc:
        failure = exc
    except Exception as exc:
        failure = HelperError("Post-clone synchronization failed", code="SYNC_FAILED", stage="sync")
        failure.__cause__ = exc

    if failure is None:
        _progress(progress_callback, "guacamole", "ready")
        _progress(progress_callback, "permissions", "ready")
        return {
            "ok": True,
            "status": "ready",
            "stage": "permissions",
            "clone": clone_payload,
            "progress": clone_progress("ready"),
        }

    if was_existing_ready:
        guacamole_rollback = guacamole_rollback_holder.get("rollback")
        if callable(guacamole_rollback):
            compensation_errors = guacamole_rollback()
            if compensation_errors:
                details = "; ".join(_clone_error_message(error) for error in compensation_errors)
                raise HelperError(
                    f"{failure.message}; existing ready clone was preserved but Guacamole compensation failed: {details}",
                    code="ROLLBACK_FAILED",
                    stage="rollback",
                ) from compensation_errors[0]
        raise HelperError(
            f"{failure.message}; existing ready clone was preserved and retry failed",
            code=failure.code,
            stage=failure.stage,
        ) from failure

    if failure.code in {"SYNC_COMMIT_UNKNOWN", "SYNC_OWNERSHIP_UNAVAILABLE", "SYNC_OWNERSHIP_INVALID"}:
        retained = _mark_clone_sync_failure(
            record,
            inventory_path,
            failure,
            ensure_record=True,
        )
        suffix = "; sync-failed repair metadata was saved" if retained else "; repair metadata could not be saved"
        raise HelperError(
            f"{failure.message}{suffix}",
            code=failure.code,
            stage=failure.stage,
        ) from failure

    rollback_errors: list[BaseException] = []
    guacamole_rollback = guacamole_rollback_holder.get("rollback")
    if callable(guacamole_rollback):
        try:
            guacamole_errors = guacamole_rollback()
            if guacamole_errors:
                details = "; ".join(_safe_compensation_detail(error) for error in guacamole_errors)
                compensation_failure = HelperError(
                    f"{failure.message}; Guacamole compensation could not be confirmed: {details}",
                    code="ROLLBACK_FAILED",
                    stage="rollback",
                )
                persisted = _mark_clone_sync_failure(
                    record,
                    inventory_path,
                    compensation_failure,
                    ensure_record=True,
                )
                suffix = "; sync-failed repair metadata was saved" if persisted else "; compensation metadata could not be saved"
                raise HelperError(
                    f"{compensation_failure.message}{suffix}",
                    code=compensation_failure.code,
                    stage=compensation_failure.stage,
                ) from guacamole_errors[0]
        except BaseException as rollback_error:
            compensation_failure = HelperError(
                f"{failure.message}; Guacamole compensation could not be confirmed: {_safe_compensation_detail(rollback_error)}",
                code="ROLLBACK_FAILED",
                stage="rollback",
            )
            persisted = _mark_clone_sync_failure(
                record,
                inventory_path,
                compensation_failure,
                ensure_record=True,
            )
            suffix = "; sync-failed repair metadata was saved" if persisted else "; compensation metadata could not be saved"
            raise HelperError(
                f"{compensation_failure.message}{suffix}",
                code=compensation_failure.code,
                stage=compensation_failure.stage,
            ) from rollback_error
    if callable(rollback):
        try:
            rollback_errors.extend(rollback())
        except BaseException as rollback_error:
            rollback_errors.append(rollback_error)
    else:
        rollback_errors.append(
            HelperError(
                "Clone compensation callback is unavailable",
                code="COMPENSATION_UNAVAILABLE",
                stage="rollback",
            )
        )
    if rollback_errors:
        details = "; ".join(_clone_error_message(error) for error in rollback_errors)
        compensation_failure = HelperError(
            f"{failure.message}; clone compensation failed: {details}",
            code="ROLLBACK_FAILED",
            stage="rollback",
        )
        persisted = _mark_clone_sync_failure(
            record,
            inventory_path,
            compensation_failure,
            ensure_record=True,
        )
        suffix = "; sync-failed repair metadata was saved" if persisted else "; compensation metadata could not be saved"
        raise HelperError(
            f"{compensation_failure.message}{suffix}",
            code=compensation_failure.code,
            stage=compensation_failure.stage,
        ) from rollback_errors[0]
    raise HelperError(
        f"{failure.message}; newly-created clone was rolled back",
        code=failure.code,
        stage=failure.stage,
    ) from failure


def require_root(command: str) -> None:
    """Reject direct unprivileged access to all state-changing commands."""

    if getattr(os, "geteuid", lambda: 0)() != 0:
        raise HelperError(
            f"{command} requires administrative access",
            code="PRIVILEGE_REQUIRED",
            stage="authorize",
        )


class RollbackLedger:
    """LIFO cleanup ledger containing only resources created by this run."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, Any]] = []

    def add(self, label: str, cleanup) -> None:
        self.entries.append((label, cleanup))

    def rollback(self) -> list[BaseException]:
        errors: list[BaseException] = []
        while self.entries:
            _label, cleanup = self.entries.pop()
            try:
                cleanup()
            except BaseException as exc:
                errors.append(exc)
        return errors


def _extract_ipv4_addresses(text: str) -> set[str]:
    addresses: set[str] = set()
    for candidate in re.findall(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:/[0-9]{1,2})?", text):
        try:
            address = ipaddress.ip_interface(candidate).ip if "/" in candidate else ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv4Address):
            addresses.add(str(address))
    return addresses


def _dhcp_reservation_addresses(xml_text: str) -> set[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise HelperError("DHCP network XML is invalid", code="NETWORK_INVALID", stage="allocate-ip") from exc
    return {
        address
        for host in root.findall(".//host")
        if (address := host.get("ip")) is not None
        and address in _extract_ipv4_addresses(address)
    }


def _dhcp_hosts(xml_text: str) -> list[tuple[str, str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise HelperError("DHCP network XML is invalid", code="NETWORK_INVALID", stage="dhcp") from exc
    hosts: list[tuple[str, str, str]] = []
    for host in root.findall(".//host"):
        name, mac, ip = host.get("name"), host.get("mac"), host.get("ip")
        if name and mac and ip:
            hosts.append((name, mac.lower(), ip))
    return hosts


def _read_dhcp_xml(runner, *, inactive: bool = False, stage: str = "dhcp") -> str:
    arguments = ["virsh", "-c", LIBVIRT_URI, "net-dumpxml", NETWORK_NAME]
    if inactive:
        arguments.append("--inactive")
    return _command_output(_run_stage(runner, arguments, stage, timeout=PREFLIGHT_TIMEOUT_SECONDS))


def _capture_dhcp_states(runner, *, stage: str = "dhcp") -> dict[str, list[tuple[str, str, str]]]:
    return {
        "live": _dhcp_hosts(_read_dhcp_xml(runner, stage=stage)),
        "config": _dhcp_hosts(_read_dhcp_xml(runner, inactive=True, stage=stage)),
    }


def _dhcp_state_contains(state: Sequence[tuple[str, str, str]], name: str, mac: str, ip: str) -> bool:
    return (name, mac.lower(), ip) in state


def _dhcp_leases(text: str) -> tuple[set[str], set[str]]:
    addresses = _extract_ipv4_addresses(text)
    macs = {value.lower() for value in re.findall(r"(?i)(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", text)}
    return addresses, macs


def allocate_clone_ip(inventory: Mapping[str, Any], runner=None) -> str:
    """Select the first free workspace IP from the guarded DHCP range."""

    if runner is None:
        runner = run_command
    reservation_results = [
        _run_stage(
            runner,
            ["virsh", "-c", LIBVIRT_URI, "net-dumpxml", NETWORK_NAME],
            "allocate-ip",
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        ),
        _run_stage(
            runner,
            ["virsh", "-c", LIBVIRT_URI, "net-dumpxml", NETWORK_NAME, "--inactive"],
            "allocate-ip",
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        ),
    ]
    lease_result = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "net-dhcp-leases", NETWORK_NAME],
        "allocate-ip",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    used = set(CLONE_RESERVED_IPS)
    for reservation_result in reservation_results:
        used.update(_dhcp_reservation_addresses(_command_output(reservation_result)))
    leased, _ = _dhcp_leases(_command_output(lease_result))
    used.update(leased)
    clones = inventory.get("clones", [])
    if not isinstance(clones, list):
        raise InventoryError("Inventory clones must be a list")
    for clone in clones:
        if not isinstance(clone, Mapping):
            raise InventoryError("Inventory clone record is invalid")
        value = clone.get("ip")
        if isinstance(value, str):
            try:
                used.add(str(ipaddress.ip_address(value)))
            except ValueError as exc:
                raise InventoryError("Inventory clone IP is invalid") from exc
    for last_octet in range(CLONE_IP_FIRST, CLONE_IP_LAST + 1):
        candidate = str(CLONE_IP_NETWORK.network_address + last_octet)
        if candidate not in used:
            return candidate
    raise ConflictError("No free clone IP address is available", code="IP_EXHAUSTED", stage="allocate-ip")


def generate_clone_mac(used_macs: Sequence[str] = ()) -> str:
    """Generate a collision-free locally administered QEMU MAC address."""

    used = {value.lower() for value in used_macs}
    for _ in range(128):
        suffix = secrets.token_bytes(3)
        mac = ":".join(f"{value:02x}" for value in (*CLONE_MAC_PREFIX, *suffix))
        if mac not in used:
            return mac
    raise ConflictError("No free clone MAC address is available", code="MAC_EXHAUSTED", stage="allocate-mac")


def _template_record(inventory: Mapping[str, Any], version: str) -> TemplateRecord:
    for item in inventory.get("templates", []):
        if isinstance(item, Mapping) and item.get("version") == version:
            return TemplateRecord.from_dict(item)
    raise ConflictError("Template version is not present", code="TEMPLATE_MISSING", stage="validate-template")


def _open_verified_directory(path: Path) -> int:
    """Open every directory component with no-follow semantics."""

    if not path.is_absolute() or path.anchor != os.sep:
        raise OSError("template directory must be an absolute POSIX path")
    descriptor = os.open(os.sep, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat_module.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("template directory is not a directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _sha256_descriptor(descriptor: int) -> str:
    """Hash the already-verified file descriptor with bounded memory."""

    digest = hashlib.sha256()
    duplicate = os.dup(descriptor)
    try:
        with os.fdopen(duplicate, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise HelperError("Published template cannot be read", code="TEMPLATE_MISSING", stage="validate-template") from exc
    return digest.hexdigest()


def _verify_template_has_no_backing(path: Path, runner) -> None:
    """Reject a template that is itself backed by another image."""

    result = _run_stage(
        runner,
        ["qemu-img", "info", "--force-share", "--output=json", str(path)],
        "validate-template",
        timeout=CONVERSION_TIMEOUT_SECONDS,
    )
    try:
        payload = json.loads(_command_output(result))
    except json.JSONDecodeError as exc:
        raise HelperError("Template image information is invalid", code="TEMPLATE_INVALID", stage="validate-template") from exc
    if not isinstance(payload, Mapping):
        raise HelperError("Template image information is invalid", code="TEMPLATE_INVALID", stage="validate-template")
    backing = payload.get("backing-filename")
    if isinstance(backing, str) and backing.strip() and backing.strip().lower() not in {"null", "none"}:
        raise HelperError("Template image must not have a backing file", code="TEMPLATE_INVALID", stage="validate-template")
    if payload.get("backing") not in (None, "", False):
        raise HelperError("Template image must not have a backing file", code="TEMPLATE_INVALID", stage="validate-template")


def _verify_template_file(path: Path, expected_sha256: str, runner, *, stage: str) -> None:
    """Verify one canonical template path through no-follow descriptors."""

    directory_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        directory_descriptor = _open_verified_directory(path.parent)
        file_descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        file_stat = os.fstat(file_descriptor)
        if not stat_module.S_ISREG(file_stat.st_mode):
            raise HelperError("Template image is not a regular file", code="TEMPLATE_INVALID", stage=stage)
        if file_stat.st_uid != 0 or file_stat.st_gid != 0:
            raise HelperError("Template image is not root-owned", code="TEMPLATE_INVALID", stage=stage)
        if (file_stat.st_mode & 0o7777) != 0o444:
            raise HelperError("Template image does not have exact 0444 mode", code="TEMPLATE_INVALID", stage=stage)
        digest = _sha256_descriptor(file_descriptor)
    except FileNotFoundError as exc:
        raise HelperError("Template image is unavailable", code="TEMPLATE_MISSING", stage=stage) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise HelperError("Template image path contains a symlink", code="TEMPLATE_INVALID", stage=stage) from exc
        raise HelperError("Template image is unavailable", code="TEMPLATE_MISSING", stage=stage) from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    if digest != expected_sha256.lower():
        raise HelperError("Template hash does not match inventory", code="TEMPLATE_HASH_INVALID", stage=stage)
    _verify_template_has_no_backing(path, runner)


def verify_template_record(record: TemplateRecord, runner=None) -> TemplateRecord:
    """Verify the canonical immutable template and its recorded hash."""

    if runner is None:
        runner = run_command
    try:
        version = validate_vm_name(record.version)
    except ValidationError as exc:
        raise HelperError("Template version is invalid", code="TEMPLATE_INVALID", stage="validate-template") from exc
    canonical = TEMPLATES_DIR / f"{version}.qcow2"
    path = Path(record.path)
    if not path.is_absolute() or path != canonical:
        raise HelperError("Template path is not the canonical version path", code="TEMPLATE_INVALID", stage="validate-template")
    _verify_template_file(canonical, record.sha256, runner, stage="validate-template")
    return record


def _retirement_path(value: str | os.PathLike[str]) -> str:
    """Normalize a local path for safe, read-only ownership comparisons."""

    return os.path.normcase(os.path.abspath(os.fspath(value)))


def _workspace_ownership_root() -> Path:
    """Return the helper-owned durable clone ownership directory."""

    return VMS_DIR / WORKSPACE_OWNERSHIP_DIRNAME


def _workspace_ownership_path(name: str) -> Path:
    validate_vm_name(name)
    return _workspace_ownership_root() / f"{name}.json"


def _workspace_nvram_marker_path(nvram_path: str | os.PathLike[str]) -> Path:
    path = Path(nvram_path)
    return path.with_name(path.name + WORKSPACE_NVRAM_MARKER_SUFFIX)


def _workspace_tpm_marker_path(clone_uuid: str) -> Path:
    return CLONE_TPM_DIR / clone_uuid / WORKSPACE_MARKER_FILENAME


def _workspace_ownership_payload(
    *,
    name: str,
    template_version: str,
    clone_uuid: str,
    mac: str,
    ip: str,
    sync_attempt_id: str,
) -> dict[str, Any]:
    """Build the non-secret ownership marker shared by clone resources."""

    validate_vm_name(name)
    validate_vm_name(template_version)
    canonical_uuid = str(uuid.UUID(clone_uuid))
    canonical_mac = _validate_mac(mac)
    canonical_ip = _validate_ip(ip)
    canonical_attempt = _validate_sync_attempt_id(sync_attempt_id)
    return {
        "schema": WORKSPACE_OWNERSHIP_SCHEMA,
        "name": name,
        "templateVersion": template_version,
        "cloneUuid": canonical_uuid,
        "mac": canonical_mac,
        "ip": canonical_ip,
        "syncAttemptId": canonical_attempt,
    }


def _write_workspace_marker(path: Path, payload: Mapping[str, Any]) -> None:
    """Write one ownership marker atomically without credentials or secrets."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(payload), stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_workspace_marker(path: Path) -> dict[str, Any]:
    """Read and validate a durable marker, failing closed on ambiguity."""

    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise HelperError("Workspace ownership metadata is unreadable", code="RETIRE_CHECK_FAILED", stage="retire-check") from exc
    if not isinstance(value, Mapping) or value.get("schema") != WORKSPACE_OWNERSHIP_SCHEMA:
        raise HelperError("Workspace ownership metadata is invalid", code="RETIRE_CHECK_FAILED", stage="retire-check")
    required = ("name", "templateVersion", "cloneUuid", "mac", "ip", "syncAttemptId")
    if any(not isinstance(value.get(key), str) or not value.get(key) for key in required):
        raise HelperError("Workspace ownership metadata is incomplete", code="RETIRE_CHECK_FAILED", stage="retire-check")
    try:
        payload = _workspace_ownership_payload(
            name=value["name"],
            template_version=value["templateVersion"],
            clone_uuid=value["cloneUuid"],
            mac=value["mac"],
            ip=value["ip"],
            sync_attempt_id=value["syncAttemptId"],
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise HelperError("Workspace ownership metadata is invalid", code="RETIRE_CHECK_FAILED", stage="retire-check") from exc
    return payload


def _managed_clone_identity(mac: str, ip: str) -> bool:
    """Recognize the helper-owned clone address pool without using VM names."""

    try:
        parsed_mac = _validate_mac(mac)
    except ValidationError:
        return False
    if not _managed_clone_ip(ip):
        return False
    prefix = ":".join(f"{value:02x}" for value in CLONE_MAC_PREFIX) + ":"
    return parsed_mac.startswith(prefix)


def _managed_clone_ip(ip: str) -> bool:
    """Recognize the helper-owned clone address pool without a clone name."""

    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if address.version != 4 or address not in CLONE_IP_NETWORK:
        return False
    last_octet = int(str(address).rsplit(".", 1)[1])
    return CLONE_IP_FIRST <= last_octet <= CLONE_IP_LAST and str(address) not in CLONE_RESERVED_IPS


def _workspace_xml_metadata(root: ET.Element, domain_name: str) -> dict[str, str] | None:
    """Extract and validate a managed workspace marker from domain XML."""

    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "workspace":
            continue
        if element.get("managed") != "true":
            continue
        if element.get("schema") != WORKSPACE_OWNERSHIP_SCHEMA:
            raise HelperError("Libvirt workspace metadata is invalid", code="RETIRE_CHECK_FAILED", stage="retire-check")
        owner_name = element.get("name") or domain_name
        template_version = element.get("template")
        if not template_version:
            raise HelperError("Libvirt workspace metadata is incomplete", code="RETIRE_CHECK_FAILED", stage="retire-check")
        try:
            validate_vm_name(owner_name)
            validate_vm_name(template_version)
        except ValidationError as exc:
            raise HelperError("Libvirt workspace metadata is invalid", code="RETIRE_CHECK_FAILED", stage="retire-check") from exc
        if owner_name != domain_name:
            raise HelperError("Libvirt workspace metadata name does not match domain", code="RETIRE_CHECK_FAILED", stage="retire-check")
        return {"name": owner_name, "templateVersion": template_version}
    return None


def _qemu_chain_paths(path: Path, runner) -> set[str]:
    """Return every file named by qemu-img's JSON backing-chain output."""

    result = _run_stage(
        runner,
        ["qemu-img", "info", "--force-share", "--output=json", "--backing-chain", str(path)],
        "retire-check",
        timeout=CONVERSION_TIMEOUT_SECONDS,
    )
    try:
        payload = json.loads(_command_output(result))
    except json.JSONDecodeError as exc:
        raise HelperError("qemu-img backing-chain output is invalid", code="RETIRE_CHECK_FAILED", stage="retire-check") from exc
    paths: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            filename = value.get("filename")
            if isinstance(filename, str) and filename:
                paths.add(_retirement_path(filename))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    if not paths:
        raise HelperError("qemu-img backing-chain output has no file paths", code="RETIRE_CHECK_FAILED", stage="retire-check")
    return paths


def _retirement_psql_arguments() -> list[str]:
    """Return a read-only query argv for non-secret Guacamole references."""

    return [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "postgres",
        "psql",
        "-X",
        "-q",
        "-At",
        "-U",
        "guacamole_user",
        "-d",
        "guacamole_db",
        "-c",
        f"SELECT c.connection_id||E'\\t'||c.connection_name||E'\\t'||c.protocol||E'\\t'||COALESCE(max(CASE WHEN p.parameter_name='hostname' THEN p.parameter_value END),'')||E'\\t'||COALESCE(max(a.attribute_value),'') FROM guacamole_connection c LEFT JOIN guacamole_connection_parameter p USING(connection_id) LEFT JOIN guacamole_connection_attribute a ON a.connection_id=c.connection_id AND a.attribute_name='{GUAC_MANAGED_ATTEMPT_ATTRIBUTE}' WHERE c.parent_id IS NULL AND c.protocol='rdp' GROUP BY c.connection_id,c.connection_name,c.protocol ORDER BY c.connection_id;",
    ]


def _template_retirement_audit(
    version: str,
    template: TemplateRecord,
    inventory: Mapping[str, Any],
    runner,
) -> dict[str, Any]:
    """Inspect all owned runtime state without mutating or deleting anything.

    Clone names are user supplied and therefore are not an ownership boundary.
    The audit joins durable helper markers with the helper-owned DHCP/MAC pool,
    template backing chains, domain metadata, and Guacamole workflow markers.
    """

    template_path = _retirement_path(template.path)
    inventory_clones = inventory.get("clones", [])
    if not isinstance(inventory_clones, list):
        raise InventoryError("Inventory clones must be a list")
    dependent_inventory: list[CloneRecord] = []
    for item in inventory_clones:
        if not isinstance(item, Mapping):
            raise InventoryError("Inventory clone record is invalid")
        record = _coerce_clone_sync_record(item)
        if record.templateVersion == version:
            dependent_inventory.append(record)

    blockers: list[dict[str, str]] = []
    dependent_names = {record.name for record in dependent_inventory}
    dependent_ips = {record.ip for record in dependent_inventory}
    dependent_macs = {record.mac.lower() for record in dependent_inventory}
    dependent_uuids: set[str] = set()
    ownership_by_attempt: dict[str, dict[str, Any]] = {}
    ownership_by_name: dict[str, dict[str, Any]] = {}
    ownership_by_ip: dict[str, dict[str, Any]] = {}
    ownership_by_mac: dict[str, dict[str, Any]] = {}
    ownership_by_uuid: dict[str, dict[str, Any]] = {}

    def _ownership_conflict(message: str) -> None:
        raise HelperError(message, code="RETIRE_CHECK_FAILED", stage="retire-check")

    def register_identity(item: Mapping[str, Any]) -> None:
        """Join one durable owner into every identity it carries."""

        identity_keys = ("name", "templateVersion", "mac", "ip", "cloneUuid", "syncAttemptId")
        for mapping, key in (
            (ownership_by_name, "name"),
            (ownership_by_ip, "ip"),
            (ownership_by_mac, "mac"),
            (ownership_by_uuid, "cloneUuid"),
        ):
            value = item.get(key)
            if not value:
                continue
            normalized = value.lower() if key in {"ip", "mac", "cloneUuid"} else value
            existing = mapping.get(normalized)
            if existing is not None:
                if any(
                    existing.get(identity_key) is not None
                    and item.get(identity_key) is not None
                    and existing.get(identity_key) != item.get(identity_key)
                    for identity_key in identity_keys
                ):
                    _ownership_conflict("Workspace ownership identities disagree")
                merged = dict(existing)
                merged.update({identity_key: value for identity_key, value in item.items() if value is not None})
                mapping[normalized] = merged
            else:
                mapping[normalized] = dict(item)

    def owner_for(*, name: str | None = None, ip: str | None = None, mac: str | None = None, clone_uuid: str | None = None) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for mapping, value, key in (
            (ownership_by_name, name, "name"),
            (ownership_by_ip, ip, "ip"),
            (ownership_by_mac, mac, "mac"),
            (ownership_by_uuid, clone_uuid, "cloneUuid"),
        ):
            if not value:
                continue
            normalized = value.lower() if key != "name" else value
            item = mapping.get(normalized)
            if item is not None and item not in candidates:
                candidates.append(item)
        if not candidates:
            return None
        if any(item != candidates[0] for item in candidates[1:]):
            _ownership_conflict("Workspace ownership identities are ambiguous")
        return candidates[0]

    def add_target_owner(item: Mapping[str, Any], kind: str) -> None:
        if item.get("templateVersion") != version:
            return
        dependent_names.add(str(item["name"]))
        if item.get("ip"):
            dependent_ips.add(str(item["ip"]))
        if item.get("mac"):
            dependent_macs.add(str(item["mac"]).lower())
        if item.get("cloneUuid"):
            dependent_uuids.add(str(item["cloneUuid"]).lower())
        blockers.append({"kind": kind, "name": str(item["name"])})

    def register_ownership(payload: Mapping[str, Any], kind: str) -> None:
        item = dict(payload)
        existing = ownership_by_attempt.get(item["syncAttemptId"])
        if existing is not None and existing != item:
            _ownership_conflict("Workspace ownership markers disagree")
        ownership_by_attempt[item["syncAttemptId"]] = item
        register_identity(item)
        add_target_owner(item, kind)

    # Inventory records are durable ownership evidence even when a runtime
    # marker was lost.  A foreign record must be allowed to coexist with the
    # template under audit; conflicting identities remain fail-closed.
    for item in inventory_clones:
        record = _coerce_clone_sync_record(item)
        if not record.templateVersion:
            continue
        inventory_owner: dict[str, Any] = {
            "name": record.name,
            "templateVersion": record.templateVersion,
            "mac": record.mac.lower(),
            "ip": record.ip,
        }
        if record.syncAttemptId:
            inventory_owner["syncAttemptId"] = record.syncAttemptId
            existing = ownership_by_attempt.get(record.syncAttemptId)
            if existing is not None and any(existing.get(key) != value for key, value in inventory_owner.items()):
                _ownership_conflict("Inventory and workspace ownership disagree")
        register_identity(inventory_owner)

    ownership_root = _workspace_ownership_root()
    if ownership_root.exists():
        if not ownership_root.is_dir():
            raise HelperError("Workspace ownership directory is invalid", code="RETIRE_CHECK_FAILED", stage="retire-check")
        for path in sorted(ownership_root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() != ".json" or path.parent != ownership_root:
                raise HelperError("Workspace ownership directory contains an unexpected file", code="RETIRE_CHECK_FAILED", stage="retire-check")
            payload = _read_workspace_marker(path)
            if path.stem != payload["name"]:
                _ownership_conflict("Workspace ownership filename does not match its name")
            register_ownership(payload, "ownership")

    blockers.extend({"kind": "inventory", "name": record.name} for record in dependent_inventory)
    domain_uuids: set[str] = set()
    domain_candidates: dict[str, dict[str, Any]] = {}
    domain_owner_by_uuid: dict[str, dict[str, Any]] = {}
    domain_nvram_by_path: dict[str, dict[str, Any]] = {}

    domains_output = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "list", "--all", "--name"],
        "retire-check",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    domains = [line.strip() for line in _command_output(domains_output).splitlines() if line.strip()]
    for name in domains:
        xml_result = _run_stage(
            runner,
            ["virsh", "-c", LIBVIRT_URI, "dumpxml", name],
            "retire-check",
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
        try:
            root = ET.fromstring(_command_output(xml_result))
        except ET.ParseError as exc:
            raise HelperError("Libvirt domain XML is invalid during retirement check", code="RETIRE_CHECK_FAILED", stage="retire-check") from exc
        uuid_text = (root.findtext("uuid") or "").strip().lower()
        if uuid_text:
            try:
                uuid_text = str(uuid.UUID(uuid_text))
                domain_uuids.add(uuid_text)
            except ValueError as exc:
                raise HelperError("Libvirt domain UUID is invalid during retirement check", code="RETIRE_CHECK_FAILED", stage="retire-check") from exc
        workspace_xml = _workspace_xml_metadata(root, name)
        disk_paths = {
            _retirement_path(source.get("file"))
            for source in root.findall(".//disk/source")
            if source.get("file")
        }
        backing_paths = {
            _retirement_path(source.get("file"))
            for source in root.findall(".//backingStore/source")
            if source.get("file")
        }
        all_disk_paths = disk_paths | backing_paths
        nvram_paths = {
            _retirement_path(element.text.strip())
            for element in root.findall(".//nvram")
            if element.text and element.text.strip()
        }
        managed_by_metadata = workspace_xml is not None
        scoped = managed_by_metadata or name.startswith(CLONE_NAME_PREFIX) or name in dependent_names or any(
            path.startswith(_retirement_path(VMS_DIR) + os.sep) for path in all_disk_paths
        )
        references_template = template_path in all_disk_paths
        target_metadata = managed_by_metadata and workspace_xml["templateVersion"] == version
        foreign_metadata = managed_by_metadata and not target_metadata
        if uuid_text:
            if managed_by_metadata:
                domain_owner = {
                    "name": name,
                    "templateVersion": workspace_xml["templateVersion"],
                    "cloneUuid": uuid_text,
                }
                register_identity(domain_owner)
                domain_owner_by_uuid[uuid_text] = domain_owner
            elif references_template or (scoped and (name.startswith(CLONE_NAME_PREFIX) or any(
                path.startswith(_retirement_path(VMS_DIR) + os.sep) for path in all_disk_paths
            ))):
                domain_owner = {"name": name, "templateVersion": version, "cloneUuid": uuid_text}
                register_identity(domain_owner)
                domain_owner_by_uuid[uuid_text] = domain_owner
        for nvram_path in nvram_paths:
            domain_nvram_by_path[nvram_path] = {
                "name": name,
                "templateVersion": version if references_template else (
                    workspace_xml["templateVersion"] if managed_by_metadata else None
                ),
                "uuid": uuid_text,
            }
        if references_template or (scoped and not foreign_metadata):
            domain_candidates[name] = {"uuid": uuid_text, "paths": all_disk_paths}
            if uuid_text and (target_metadata or name in dependent_names or references_template):
                dependent_uuids.add(uuid_text)
            if target_metadata or references_template or (scoped and not managed_by_metadata):
                blockers.append({"kind": "domain", "name": name})
        elif foreign_metadata:
            # A durable foreign template marker is evidence that this domain
            # belongs elsewhere and must not block retirement of ``version``.
            domain_candidates[name] = {"uuid": uuid_text, "paths": all_disk_paths}
        elif scoped:
            blockers.append({"kind": "ownership-ambiguous", "name": name})

    overlay_root = _retirement_path(VMS_DIR)
    ownership_root_text = _retirement_path(ownership_root)
    if VMS_DIR.exists():
        for path in sorted(VMS_DIR.rglob("*")):
            if not path.is_file():
                continue
            path_text = _retirement_path(path)
            if path_text == ownership_root_text or path_text.startswith(ownership_root_text + os.sep):
                continue
            name = path.stem.removesuffix(".xml")
            if path.suffix.lower() == ".qcow2":
                chain = _qemu_chain_paths(path, runner)
                if template_path in chain:
                    dependent_names.add(name)
                    blockers.append({"kind": "overlay", "name": name})
            elif path_text.startswith(overlay_root + os.sep) and name in dependent_names:
                blockers.append({"kind": "overlay-metadata", "name": name})

    nvram_root = CLONE_NVRAM_DIR
    if nvram_root.exists():
        if not nvram_root.is_dir():
            raise HelperError("Libvirt NVRAM directory is invalid", code="RETIRE_CHECK_FAILED", stage="retire-check")
        for path in sorted(nvram_root.rglob("*")):
            if not path.is_file():
                continue
            path_text = _retirement_path(path)
            if path.name.endswith(WORKSPACE_NVRAM_MARKER_SUFFIX):
                payload = _read_workspace_marker(path)
                expected_name = path.name[: -len(WORKSPACE_NVRAM_MARKER_SUFFIX)].removesuffix("_VARS.fd")
                if expected_name != payload["name"]:
                    _ownership_conflict("NVRAM ownership filename does not match its name")
                register_ownership(payload, "nvram-ownership")
                continue
            if path.suffix.lower() == ".json":
                raise HelperError("NVRAM ownership metadata has an unexpected filename", code="RETIRE_CHECK_FAILED", stage="retire-check")
            if path.name.endswith("_VARS.fd"):
                name = path.name.removesuffix("_VARS.fd")
                domain_owner = domain_nvram_by_path.get(path_text)
                owner = owner_for(name=name)
                template_owner = owner.get("templateVersion") if owner else (
                    domain_owner.get("templateVersion") if domain_owner else None
                )
                if domain_owner is not None and not domain_owner.get("templateVersion") and not owner:
                    # Persistent libvirt domains with NVRAM recorded in their
                    # XML are known unrelated owners unless helper metadata
                    # explicitly ties them to a template.
                    continue
                if template_owner == version or name in dependent_names:
                    blockers.append({"kind": "nvram", "name": name})
                elif template_owner and template_owner != version:
                    continue
                elif name.startswith(CLONE_NAME_PREFIX):
                    blockers.append({"kind": "nvram", "name": name})
                else:
                    blockers.append({"kind": "ownership-ambiguous", "name": name})
                continue
            # Any other regular file below the helper-owned NVRAM root is
            # retained and inspected as ambiguous state.  Known libvirt
            # domain NVRAM paths are exempted above through domain XML.
            if path_text not in domain_nvram_by_path:
                blockers.append({"kind": "ownership-ambiguous", "name": path.name})

    if CLONE_TPM_DIR.exists():
        if not CLONE_TPM_DIR.is_dir():
            raise HelperError("Libvirt TPM directory is invalid", code="RETIRE_CHECK_FAILED", stage="retire-check")
        marker_paths = sorted(CLONE_TPM_DIR.rglob(WORKSPACE_MARKER_FILENAME))
        for path in sorted(CLONE_TPM_DIR.rglob("*.json")):
            if path.name != WORKSPACE_MARKER_FILENAME:
                raise HelperError("TPM ownership metadata has an unexpected filename", code="RETIRE_CHECK_FAILED", stage="retire-check")
        for marker_path in marker_paths:
            parent = marker_path.parent
            try:
                parent_uuid = str(uuid.UUID(parent.name))
            except ValueError as exc:
                raise HelperError("TPM ownership marker is outside a UUID directory", code="RETIRE_CHECK_FAILED", stage="retire-check") from exc
            payload = _read_workspace_marker(marker_path)
            if payload["cloneUuid"] != parent_uuid:
                raise HelperError("TPM ownership metadata does not match its UUID", code="RETIRE_CHECK_FAILED", stage="retire-check")
            register_ownership(payload, "tpm-ownership")

        tpm2_dirs = sorted(path for path in CLONE_TPM_DIR.rglob("tpm2") if path.is_dir())
        for tpm2_dir in tpm2_dirs:
            uuid_dir = tpm2_dir.parent
            try:
                uuid_text = str(uuid.UUID(uuid_dir.name))
            except ValueError as exc:
                raise HelperError("TPM state directory name is invalid", code="RETIRE_CHECK_FAILED", stage="retire-check") from exc
            owner = owner_for(clone_uuid=uuid_text)
            domain_owner = domain_owner_by_uuid.get(uuid_text)
            template_owner = owner.get("templateVersion") if owner else (
                domain_owner.get("templateVersion") if domain_owner else None
            )
            if uuid_text in dependent_uuids or template_owner == version:
                blockers.append({"kind": "tpm", "name": uuid_text})
            elif template_owner and template_owner != version:
                continue
            elif uuid_text in domain_uuids and uuid_text not in dependent_uuids:
                # A persistent domain without helper metadata is treated as a
                # known unrelated libvirt owner (for example windows11-02).
                continue
            else:
                blockers.append({"kind": "ownership-ambiguous", "name": uuid_text})

    dhcp_states = _capture_dhcp_states(runner, stage="retire-check")
    for scope, hosts in dhcp_states.items():
        for name, mac, ip in hosts:
            owner = owner_for(name=name, mac=mac, ip=ip)
            if owner is not None:
                if owner.get("templateVersion") != version:
                    # This reservation is durably attributed to another
                    # template and must not block this template's retirement.
                    continue
                dependent_names.add(name)
                dependent_ips.add(ip)
                dependent_macs.add(mac)
                blockers.append({"kind": f"dhcp-{scope}", "name": name})
                continue
            if name in dependent_names or mac in dependent_macs or ip in dependent_ips or name.startswith(CLONE_NAME_PREFIX):
                dependent_names.add(name)
                dependent_ips.add(ip)
                dependent_macs.add(mac)
                blockers.append({"kind": f"dhcp-{scope}", "name": name})
            elif _managed_clone_ip(ip) or _managed_clone_identity(mac, ip):
                # Pool-shaped state without a durable owner is unsafe to
                # attribute.  Refuse retirement explicitly and leave it
                # untouched for an operator to reconcile.
                blockers.append({"kind": "ownership-ambiguous", "name": name})

    try:
        guacamole_result = _run_stage(
            runner,
            _retirement_psql_arguments(),
            "retire-check",
            timeout=GUACAMOLE_SYNC_TIMEOUT_SECONDS,
        )
    except HelperError as exc:
        raise HelperError("Guacamole references cannot be inspected", code="RETIRE_CHECK_FAILED", stage="retire-check") from exc
    guacamole: list[dict[str, str]] = []
    for line in _command_output(guacamole_result).splitlines():
        fields = line.split("\t")
        if len(fields) != 5 or not fields[0].isdigit() or not fields[1] or fields[2] != "rdp":
            raise HelperError("Guacamole retirement output is invalid", code="RETIRE_CHECK_FAILED", stage="retire-check")
        marker = fields[4]
        if marker:
            try:
                marker = _validate_sync_attempt_id(marker)
            except ValidationError as exc:
                raise HelperError("Guacamole workflow marker is invalid", code="RETIRE_CHECK_FAILED", stage="retire-check") from exc
        identity_owner = owner_for(name=fields[1], ip=fields[3])
        marker_owner = ownership_by_attempt.get(marker) if marker else None
        if marker_owner is None and marker and identity_owner and identity_owner.get("syncAttemptId") == marker:
            marker_owner = identity_owner
        row = {
            "connectionId": fields[0],
            "name": fields[1],
            "protocol": fields[2],
            "hostname": fields[3],
            "workflowMarker": marker,
        }
        guacamole.append(row)
        if marker and marker_owner is None:
            # Keep the resource visible as a Guacamole workflow reference and
            # add an explicit ambiguity blocker because its template owner
            # cannot be established safely.
            blockers.append({"kind": "guacamole-workflow", "name": row["name"]})
            blockers.append({"kind": "ownership-ambiguous", "name": row["name"]})
        elif marker_owner and marker_owner["templateVersion"] == version:
            dependent_names.add(marker_owner["name"])
            dependent_ips.add(marker_owner["ip"])
            blockers.append({"kind": "guacamole-workflow", "name": row["name"]})
        elif marker_owner and marker_owner["templateVersion"] != version:
            continue
        elif identity_owner and identity_owner.get("templateVersion") == version:
            blockers.append({"kind": "guacamole", "name": row["name"]})
        elif identity_owner and identity_owner.get("templateVersion") != version:
            continue
        elif row["name"] in dependent_names or row["name"].startswith(CLONE_NAME_PREFIX):
            blockers.append({"kind": "guacamole-workflow" if marker else "guacamole", "name": row["name"]})
        elif _managed_clone_ip(row["hostname"]):
            blockers.append({"kind": "ownership-ambiguous", "name": row["name"]})

    unique_blockers: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for blocker in blockers:
        key = (blocker["kind"], blocker["name"])
        if key not in seen:
            seen.add(key)
            unique_blockers.append(blocker)
    return {
        "version": version,
        "templatePath": template.path,
        "inventoryDependents": [record.name for record in dependent_inventory],
        "domains": sorted(domain_candidates),
        "guacamole": guacamole,
        "blockers": unique_blockers,
    }


def check_template_delete(
    version: str,
    runner=None,
    *,
    inventory_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return a read-only retirement decision; this command never deletes."""

    validate_vm_name(version)
    if runner is None:
        runner = run_command
    inventory = load_inventory(inventory_path)
    template = _template_record(inventory, version)
    verify_template_record(template, runner=runner)
    audit = _template_retirement_audit(version, template, inventory, runner)
    blockers = audit["blockers"]
    if blockers:
        ambiguous = any(item.get("kind") == "ownership-ambiguous" for item in blockers)
        return {
            "ok": False,
            "code": "TEMPLATE_RETIREMENT_AMBIGUOUS" if ambiguous else "TEMPLATE_IN_USE",
            "stage": "retire-check",
            "version": version,
            "deletable": False,
            "blockers": blockers,
        }
    return {
        "ok": True,
        "version": version,
        "deletable": True,
        "blockers": [],
    }


def render_clone_xml(
    *,
    name: str,
    clone_uuid: str,
    mac: str,
    disk_path: str | os.PathLike[str],
    nvram_path: str | os.PathLike[str],
    memory_mib: int = 4096,
    vcpus: int = 2,
    template_path: str | os.PathLike[str] | None = None,
    template_version: str = "windows11-v1",
) -> str:
    """Render a clone XML with independent UUID, NVRAM, and managed TPM."""

    validate_vm_name(name)
    validate_vm_name(template_version)
    _validate_mac(mac)
    try:
        parsed_uuid = str(uuid.UUID(clone_uuid))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValidationError("Clone UUID is invalid", code="UUID_INVALID") from exc
    if (
        isinstance(memory_mib, bool)
        or not isinstance(memory_mib, int)
        or not CLONE_MEMORY_MIN_MIB <= memory_mib <= CLONE_MEMORY_MAX_MIB
        or isinstance(vcpus, bool)
        or not isinstance(vcpus, int)
        or not CLONE_VCPUS_MIN <= vcpus <= CLONE_VCPUS_MAX
    ):
        raise ValidationError("Memory and vCPU values are invalid", code="RESOURCES_INVALID")
    source_template = Path(CLONE_XML_TEMPLATE_PATH if template_path is None else template_path)
    try:
        xml = source_template.read_text(encoding="utf-8")
    except OSError as exc:
        raise HelperError("Clone XML template is unavailable", code="XML_TEMPLATE_MISSING", stage="render-xml") from exc
    replacements = {
        "__CLONE_NAME__": name,
        "__CLONE_UUID__": parsed_uuid,
        "__CLONE_MAC__": mac.lower(),
        "__CLONE_DISK_PATH__": str(disk_path),
        "__CLONE_NVRAM_PATH__": str(nvram_path),
        "__CLONE_MEMORY_MIB__": str(memory_mib),
        "__CLONE_VCPUS__": str(vcpus),
        "__CLONE_TEMPLATE_VERSION__": template_version,
    }
    for marker, value in replacements.items():
        xml = xml.replace(marker, value)
    if "__CLONE_" in xml:
        raise HelperError("Clone XML template contains unresolved fields", code="XML_INVALID", stage="render-xml")
    try:
        ET.fromstring(xml)
    except ET.ParseError as exc:
        raise HelperError("Rendered clone XML is invalid", code="XML_INVALID", stage="render-xml") from exc
    return xml


def _normalise_ip_for_compare(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value


def _record_to_dict(value: Any, record_type: type[Any]) -> dict[str, Any]:
    if isinstance(value, record_type):
        return value.to_dict()
    if isinstance(value, Mapping):
        record = record_type.from_dict(value)
        if record_type is CloneRecord:
            payload = {}
            for key, alias in (
                ("name", "name"),
                ("mac", "mac"),
                ("ip", "ip"),
                ("assigneeType", "assignee_type"),
                ("assigneeName", "assignee_name"),
                ("status", "status"),
                ("templateVersion", "template_version"),
                ("syncAttemptId", "sync_attempt_id"),
            ):
                source_key = key if key in value else alias
                if source_key not in value:
                    continue
                item = getattr(record, key)
                if item is not None:
                    payload[key] = item
        else:
            payload = record.to_dict()
        if record_type is CloneRecord:
            try:
                if record.syncAttemptId is not None:
                    payload["syncAttemptId"] = _validate_sync_attempt_id(record.syncAttemptId)
            except ValidationError:
                payload.pop("syncAttemptId", None)
            connection_id = value.get("connectionId")
            if isinstance(connection_id, int) and not isinstance(connection_id, bool) and connection_id > 0:
                payload["connectionId"] = connection_id
            for key in ("errorCode", "errorStage"):
                item = value.get(key)
                if isinstance(item, str) and item in {
                    "SYNC_COMMIT_UNKNOWN",
                    "ROLLBACK_FAILED",
                    "SYNC_OWNERSHIP_UNAVAILABLE",
                    "SYNC_OWNERSHIP_INVALID",
                }:
                    payload[key] = item
            item = value.get("errorMessage")
            if isinstance(item, str) and item in set(_SAFE_ERROR_MESSAGES.values()):
                payload["errorMessage"] = item
        return payload
    raise InventoryError("Inventory record is invalid")


def _normalise_inventory(inventory: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(inventory, Mapping):
        raise InventoryError("Inventory must be a JSON object")
    payload = {
        "templates": inventory.get("templates", []),
        "clones": inventory.get("clones", []),
    }
    templates = payload.get("templates", [])
    clones = payload.get("clones", [])
    if not isinstance(templates, list) or not isinstance(clones, list):
        raise InventoryError("Inventory templates and clones must be lists")
    payload["templates"] = [_record_to_dict(item, TemplateRecord) for item in templates]
    payload["clones"] = [_record_to_dict(item, CloneRecord) for item in clones]
    return payload


def load_inventory(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load inventory without creating files or directories."""

    inventory_path = Path(INVENTORY_PATH if path is None else path)
    if not inventory_path.exists():
        return {"templates": [], "clones": []}
    try:
        with inventory_path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryError("Inventory cannot be read") from exc
    return _normalise_inventory(value)


def save_inventory_atomic(
    inventory: Mapping[str, Any],
    path: str | os.PathLike[str] | None = None,
) -> None:
    """Persist inventory through a sibling file, fsync, and atomic replace."""

    payload = _normalise_inventory(inventory)
    inventory_path = Path(INVENTORY_PATH if path is None else path)
    temporary_path: Path | None = None
    inventory_replaced = False
    try:
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{inventory_path.name}.", suffix=".tmp", dir=inventory_path.parent
        )
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, inventory_path)
        inventory_replaced = True
        temporary_path = None
        try:
            directory_descriptor = os.open(inventory_path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except OSError as exc:
        raise InventoryError("Inventory cannot be saved", replaced=inventory_replaced) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


@contextmanager
def workspace_lock(path: str | os.PathLike[str] = LOCK_PATH) -> Iterator[int]:
    """Hold the non-blocking global helper lock for a mutating operation."""

    lock_path = Path(path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise LockError("Workspace lock cannot be opened") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockError("Another workspace operation is in progress") from exc
        yield descriptor
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


acquire_lock = workspace_lock


_JOB_ACTIVE_STATUSES = frozenset(("queued", "running"))
_JOB_REPAIRABLE_STATUSES = frozenset(("pending", "waiting-rdp", "sync-failed", "failed", "stale"))
_JOB_TERMINAL_STATUSES = frozenset(("ready", "waiting-rdp", "sync-failed", "failed", "stale", "failed-without-inventory"))
_JOB_STATUS_VALUES = frozenset(("pending", "queued", "running", "ready", "waiting-rdp", "sync-failed", "failed", "stale", "failed-without-inventory"))
_JOB_STAGE_VALUES = frozenset(("validation", "disk-overlay", "domain", "dhcp", "rdp", "guacamole", "permissions", "repair", "sync", "command"))
_JOB_PROGRESS_VALUES = frozenset(("pending", "running", "ready", "waiting", "planned", "failed"))
JOB_ACTIVE_MAX_AGE_SECONDS = 6 * 60 * 60
_JOB_REPAIR_BLOCKED_CODES = frozenset((
    "JOB_SYSTEMD_STATE_UNKNOWN",
    "JOB_TIMESTAMP_MISSING",
    "JOB_TIMESTAMP_INVALID",
    "JOB_TIMESTAMP_FUTURE",
))


def _job_id_for_name(name: str) -> str:
    """Return the deterministic unit and ledger identity for one workspace."""

    return f"guacamole-workspace-{validate_vm_name(name)}.service"


def _job_status_path(name: str) -> Path:
    validate_vm_name(name)
    return JOB_STATUS_DIR / f"{name}.json"


def _job_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_progress_value(value: Any) -> list[dict[str, str]]:
    states = {stage: "pending" for stage in CLONE_PROGRESS_STAGES}
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, Mapping):
                continue
            stage = item.get("stage")
            status = item.get("status")
            if stage in states and status in _JOB_PROGRESS_VALUES:
                states[stage] = status
    return [{"stage": stage, "status": states[stage]} for stage in CLONE_PROGRESS_STAGES]


def _job_result_value(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in ("name", "mac", "ip", "assigneeType", "assigneeName", "templateVersion", "syncAttemptId", "status", "connectionId", "connectionName", "cloneUuid"):
        item = value.get(key)
        if key == "connectionId":
            if isinstance(item, int) and not isinstance(item, bool):
                result[key] = item
        elif isinstance(item, str):
            result[key] = item[:256]
    return result or None


def _job_inventory_transition_value(value: Any) -> dict[str, Any] | None:
    """Keep only the durable, secret-free repair inventory transition."""

    if not isinstance(value, Mapping) or value.get("phase") not in {"prepared", "committed", "restored"}:
        return None
    previous = _job_result_value(value.get("previous"))
    desired = _job_result_value(value.get("desired"))
    if previous is None or desired is None:
        return None
    return {"phase": value["phase"], "previous": previous, "desired": desired}


def _job_status_payload(
    name: str,
    value: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
    *,
    touch: bool = True,
) -> dict[str, Any]:
    validate_vm_name(name)
    previous = previous or {}
    status = value.get("status", previous.get("status", "pending"))
    if status not in _JOB_STATUS_VALUES:
        status = "failed"
    stage = value.get("stage", previous.get("stage", "validation"))
    if stage not in _JOB_STAGE_VALUES:
        stage = "command"
    payload: dict[str, Any] = {
        "schema": JOB_STATUS_SCHEMA,
        "jobId": _job_id_for_name(name),
        "name": name,
        "status": status,
        "stage": stage,
        "progress": _job_progress_value(value.get("progress", previous.get("progress"))),
        "createdAt": str(previous.get("createdAt", value.get("createdAt", _job_now())))[:64],
        "updatedAt": _job_now() if touch else str(value.get("updatedAt", previous.get("updatedAt", "")))[:64],
    }
    for key in ("operation", "templateVersion", "assigneeType", "assigneeName"):
        item = value.get(key, previous.get(key))
        if isinstance(item, str) and item:
            payload[key] = item[:256]
    for key in ("memoryMiB", "vcpus"):
        item = value.get(key, previous.get(key))
        if isinstance(item, int) and not isinstance(item, bool):
            payload[key] = item
    result = _job_result_value(value["result"] if "result" in value else previous.get("result"))
    if result is not None:
        payload["result"] = result
        for key in ("mac", "ip", "assigneeType", "assigneeName", "templateVersion", "syncAttemptId", "connectionId"):
            if key not in payload and isinstance(result.get(key), str) and result[key]:
                payload[key] = result[key][:256]
            if key == "connectionId" and key not in payload and isinstance(result.get(key), int) and not isinstance(result[key], bool):
                payload[key] = result[key]
    transition = _job_inventory_transition_value(
        value["inventoryTransition"] if "inventoryTransition" in value else previous.get("inventoryTransition")
    )
    if transition is not None:
        payload["inventoryTransition"] = transition
    if value.get("status") in _JOB_ACTIVE_STATUSES | {"ready"} and value.get("status") != previous.get("status"):
        error = None
    else:
        error = value["error"] if "error" in value else previous.get("error")
    if isinstance(error, Mapping):
        raw_code = str(error.get("code", "JOB_FAILED"))[:64]
        code = raw_code if re.fullmatch(r"[A-Z0-9_]{1,64}", raw_code) else "JOB_FAILED"
        raw_stage = str(error.get("stage", stage))[:64]
        error_stage = raw_stage if raw_stage in _JOB_STAGE_VALUES else "command"
        # Never persist subprocess messages. The fixed text carries enough
        # information for repair while keeping secrets and raw command output
        # out of the durable ledger.
        safe_message = f"{error_stage} failed; repair may be required"
        payload["error"] = {"code": code, "stage": error_stage, "message": safe_message}
        payload["errorCode"] = code
        payload["errorStage"] = error_stage
        payload["errorMessage"] = safe_message
    elif isinstance(value.get("errorMessage", previous.get("errorMessage")), str):
        payload["errorCode"] = "JOB_FAILED"
        payload["errorStage"] = stage
        payload["errorMessage"] = f"{stage} failed; repair may be required"
    return payload


def _job_open_flags(*, directory: bool = False, nofollow: bool = False) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    if nofollow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _job_root_owned() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _validate_job_directory_stat(directory_stat: os.stat_result) -> None:
    if not stat_module.S_ISDIR(directory_stat.st_mode):
        raise OSError("workspace job status path is not a directory")
    if (directory_stat.st_mode & 0o777) != JOB_STATUS_DIR_MODE:
        raise OSError("workspace job status directory mode is unsafe")
    if directory_stat.st_uid != 0 or directory_stat.st_gid != 0:
        raise OSError("workspace job status directory ownership is unsafe")


def _open_verified_job_directory(*, create: bool) -> tuple[int, int]:
    """Open the status directory through stable parent and directory fds."""

    path = Path(JOB_STATUS_DIR)
    if not path.is_absolute():
        raise OSError("workspace job status path must be absolute")
    # Reject a published symlink before opening, while O_NOFOLLOW below closes
    # the final-component substitution race.
    if os.path.realpath(path) != os.path.abspath(path):
        raise OSError("workspace job status path must not be a symlink")
    parent_fd = os.open(path.parent, _job_open_flags(directory=True, nofollow=True))
    directory_fd: int | None = None
    try:
        try:
            directory_fd = os.open(path.name, _job_open_flags(directory=True, nofollow=True), dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(path.name, JOB_STATUS_DIR_MODE, dir_fd=parent_fd)
            directory_fd = os.open(path.name, _job_open_flags(directory=True, nofollow=True), dir_fd=parent_fd)
            os.fchmod(directory_fd, JOB_STATUS_DIR_MODE)
            if _job_root_owned():
                os.fchown(directory_fd, 0, 0)
        _validate_job_directory_stat(os.fstat(directory_fd))
        return parent_fd, directory_fd
    except BaseException:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(parent_fd)
        raise


def _validate_job_file_stat(file_stat: os.stat_result) -> None:
    if not stat_module.S_ISREG(file_stat.st_mode):
        raise OSError("workspace job status file is not regular")
    if (file_stat.st_mode & 0o777) != JOB_STATUS_FILE_MODE:
        raise OSError("workspace job status file mode is unsafe")
    if file_stat.st_uid != 0 or file_stat.st_gid != 0:
        raise OSError("workspace job status file ownership is unsafe")
    if file_stat.st_size > MAX_JOB_STATUS_BYTES:
        raise OSError("workspace job status file is too large")


def _read_job_status_from_directory(name: str, directory_fd: int) -> dict[str, Any] | None:
    filename = f"{validate_vm_name(name)}.json"
    try:
        file_stat = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        _validate_job_file_stat(file_stat)
        descriptor = os.open(filename, _job_open_flags(nofollow=True), dir_fd=directory_fd)
        try:
            opened_stat = os.fstat(descriptor)
            _validate_job_file_stat(opened_stat)
            if (opened_stat.st_dev, opened_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino):
                return None
            raw = b""
            while len(raw) <= MAX_JOB_STATUS_BYTES:
                chunk = os.read(descriptor, MAX_JOB_STATUS_BYTES + 1 - len(raw))
                if not chunk:
                    break
                raw += chunk
            if len(raw) > MAX_JOB_STATUS_BYTES:
                return None
        finally:
            os.close(descriptor)
        value = json.loads(raw.decode("utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    try:
        return _job_status_payload(name, value, value, touch=False)
    except (TypeError, ValueError, ValidationError):
        return None


def read_job_status(name: str) -> dict[str, Any] | None:
    try:
        _parent_fd, directory_fd = _open_verified_job_directory(create=False)
    except (FileNotFoundError, OSError, ValidationError):
        return None
    try:
        return _read_job_status_from_directory(name, directory_fd)
    finally:
        os.close(directory_fd)
        os.close(_parent_fd)


def _create_job_temp_file(directory_fd: int, name: str) -> tuple[int, str]:
    for _ in range(8):
        temporary_name = f".{name}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                JOB_STATUS_FILE_MODE,
                dir_fd=directory_fd,
            )
            return descriptor, temporary_name
        except FileExistsError:
            continue
    raise OSError("workspace job status temporary name is unavailable")


def write_job_status(name: str, updates: Mapping[str, Any]) -> dict[str, Any]:
    """Persist a bounded status document through verified directory fds."""

    previous = read_job_status(name) or {}
    payload = _job_status_payload(name, updates, previous)
    encoded_bytes = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded_bytes) > MAX_JOB_STATUS_BYTES:
        raise HelperError("Workspace job status is too large", code="JOB_STATUS_INVALID", stage="status")
    parent_fd: int | None = None
    directory_fd: int | None = None
    temporary_name: str | None = None
    try:
        parent_fd, directory_fd = _open_verified_job_directory(create=True)
        descriptor, temporary_name = _create_job_temp_file(directory_fd, name)
        try:
            os.fchmod(descriptor, JOB_STATUS_FILE_MODE)
            if _job_root_owned():
                os.fchown(descriptor, 0, 0)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded_bytes)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        os.replace(
            temporary_name,
            f"{validate_vm_name(name)}.json",
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
        _validate_job_directory_stat(os.fstat(directory_fd))
        final_stat = os.stat(f"{validate_vm_name(name)}.json", dir_fd=directory_fd, follow_symlinks=False)
        _validate_job_file_stat(final_stat)
        os.fsync(directory_fd)
    except (OSError, ValidationError) as exc:
        raise HelperError("Workspace job status cannot be saved", code="JOB_STATUS_INVALID", stage="status") from exc
    finally:
        if temporary_name is not None and directory_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)
        if parent_fd is not None:
            os.close(parent_fd)
    return payload


def _systemd_unit_state(unit: str, runner=None) -> dict[str, str]:
    if runner is None:
        runner = run_command
    try:
        result = _run_stage(
            runner,
            [
                "systemctl",
                "show",
                "--no-pager",
                "--property=ActiveState",
                "--property=SubState",
                "--property=ExecMainStatus",
                "--property=Result",
                "--property=LoadState",
                unit,
            ],
            "job-status",
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
    except HelperError as exc:
        cause = exc.__cause__
        output = "\n".join(
            value for value in (
                getattr(cause, "stdout", None),
                getattr(cause, "output", None),
                getattr(cause, "stderr", None),
            ) if value
        )
        values = {
            key: value.strip()
            for line in str(output).splitlines()
            if "=" in line
            for key, value in [line.split("=", 1)]
        }
        if values.get("LoadState") == "not-found" and values.get("ActiveState", "inactive") in {"", "inactive"}:
            values["_query"] = "not-found"
            return values
        return {"_query": "error", "_errorCode": exc.code}
    values: dict[str, str] = {}
    for line in _command_output(result).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip()
    if values.get("LoadState") == "not-found" and values.get("ActiveState", "inactive") in {"", "inactive"}:
        values["_query"] = "not-found"
    elif not {"LoadState", "ActiveState", "SubState", "Result"} <= values.keys():
        values["_query"] = "error"
        values["_errorCode"] = "JOB_SYSTEMD_STATE_UNKNOWN"
    else:
        values["_query"] = "ok"
    return values


def _job_timestamp_state(status: Mapping[str, Any]) -> tuple[str, float | None]:
    value = status.get("updatedAt")
    if not isinstance(value, str) or not value.strip():
        return "missing", None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age = (now - timestamp).total_seconds()
        if age < 0:
            return "future", None
        return "valid", age
    except (TypeError, ValueError):
        return "invalid", None


def _job_age_seconds(status: Mapping[str, Any]) -> float | None:
    state, age = _job_timestamp_state(status)
    return age if state == "valid" else None


def _job_repair_blocked(status: Mapping[str, Any]) -> bool:
    return status.get("errorCode") in _JOB_REPAIR_BLOCKED_CODES


def _job_repair_available(status: Mapping[str, Any]) -> bool:
    return status.get("status") in _JOB_REPAIRABLE_STATUSES and not _job_repair_blocked(status)


def reconcile_job_status(status: Mapping[str, Any], runner=None) -> dict[str, Any]:
    """Reconcile an active ledger with its deterministic transient unit."""

    if runner is None:
        runner = run_command
    current = dict(status)
    if current.get("status") not in _JOB_ACTIVE_STATUSES:
        return current
    name = current.get("name")
    if not isinstance(name, str):
        return current
    unit = _job_id_for_name(name)
    timestamp_state, age = _job_timestamp_state(current)
    if timestamp_state != "valid":
        code = {
            "missing": "JOB_TIMESTAMP_MISSING",
            "invalid": "JOB_TIMESTAMP_INVALID",
            "future": "JOB_TIMESTAMP_FUTURE",
        }[timestamp_state]
        return write_job_status(
            name,
            {
                "status": "stale",
                "stage": "command",
                "error": {"code": code, "stage": "command"},
            },
        )
    if age is not None and age > JOB_ACTIVE_MAX_AGE_SECONDS:
        return write_job_status(
            name,
            {
                "status": "stale",
                "stage": "command",
                "error": {"code": "JOB_STALE", "stage": "command"},
            },
        )
    state = _systemd_unit_state(unit, runner)
    query = state.get("_query") if isinstance(state, Mapping) else "error"
    if query == "error":
        return write_job_status(
            name,
            {
                "status": "stale",
                "stage": "command",
                "error": {"code": "JOB_SYSTEMD_STATE_UNKNOWN", "stage": "command"},
            },
        )
    if query == "not-found":
        return write_job_status(
            name,
            {
                "status": "stale",
                "stage": "command",
                "error": {"code": "JOB_UNIT_MISSING", "stage": "command"},
            },
        )
    # Test doubles from older callers omit the private query marker.  They are
    # still accepted only when all state fields needed below are present.
    if query is None and not {"ActiveState", "SubState", "Result"} <= state.keys():
        query = "error"
    if query == "error":
        return write_job_status(
            name,
            {
                "status": "stale",
                "stage": "command",
                "error": {"code": "JOB_SYSTEMD_STATE_UNKNOWN", "stage": "command"},
            },
        )
    if state.get("ActiveState") in {"active", "activating", "deactivating"}:
        if state.get("Result") not in {None, "", "success", "done"}:
            return write_job_status(
                name,
                {
                    "status": "failed",
                    "stage": "command",
                    "error": {"code": "JOB_UNIT_FAILED", "stage": "command"},
                },
            )
        if state.get("ActiveState") == "active" and state.get("SubState") not in {"running", "start", "start-pre", "start-post", "stop", "stop-sigterm"}:
            return write_job_status(
                name,
                {
                    "status": "stale",
                    "stage": "command",
                    "error": {"code": "JOB_UNIT_TERMINAL", "stage": "command"},
                },
            )
        return current
    if state.get("Result") not in {None, "", "success", "done"}:
        return write_job_status(
            name,
            {
                "status": "failed",
                "stage": "command",
                "error": {"code": "JOB_UNIT_FAILED", "stage": "command"},
            },
        )
    return write_job_status(
        name,
        {
            "status": "stale",
            "stage": "command",
            "error": {"code": "JOB_UNIT_TERMINAL", "stage": "command"},
        },
    )


def list_job_statuses(runner=None) -> list[dict[str, Any]]:
    if runner is None:
        runner = run_command
    try:
        parent_fd, directory_fd = _open_verified_job_directory(create=False)
    except (FileNotFoundError, OSError):
        return []
    statuses: list[dict[str, Any]] = []
    try:
        try:
            names = os.listdir(directory_fd)
        except OSError:
            names = []
        for filename in names:
            if not filename.endswith(".json") or filename.startswith("."):
                continue
            name = filename[:-5]
            try:
                validate_vm_name(name)
            except ValidationError:
                continue
            status = _read_job_status_from_directory(name, directory_fd)
            if status is not None:
                statuses.append(status)
    finally:
        os.close(directory_fd)
        os.close(parent_fd)
    statuses = [reconcile_job_status(status, runner) for status in statuses]
    statuses.sort(key=lambda item: str(item.get("updatedAt", "")), reverse=True)
    return statuses[:100]


def run_command(
    arguments: Sequence[str],
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a fixed argv vector without a shell."""

    return subprocess.run(
        list(arguments),
        shell=False,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def discover_domains(runner=run_command) -> list[str]:
    """Discover persistent libvirt domain names without changing VM state."""

    try:
        result = runner(["virsh", "-c", LIBVIRT_URI, "list", "--all", "--name"])
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HelperError("Libvirt domain discovery failed", code="DISCOVERY_FAILED", stage="discover") from exc
    output = result.stdout if hasattr(result, "stdout") else str(result)
    domains: list[str] = []
    for raw_name in output.splitlines():
        name = raw_name.replace("\x00", "").strip()
        if name and name not in domains:
            domains.append(name)
    return domains


def _assignee_psql_arguments() -> list[str]:
    """Return the fixed read-only Compose/psql argv for entity discovery."""

    return [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "postgres",
        "psql",
        "-X",
        "-q",
        "-At",
        "-U",
        "guacamole_user",
        "-d",
        "guacamole_db",
        "-c",
        GUAC_ASSIGNEE_QUERY,
    ]


def discover_guacamole_assignees(runner=run_command) -> dict[str, list[dict[str, str]]]:
    """Read valid USER and USER_GROUP entities from Guacamole without writes."""

    try:
        try:
            result = runner(_assignee_psql_arguments(), timeout=PREFLIGHT_TIMEOUT_SECONDS)
        except TypeError:
            result = runner(_assignee_psql_arguments())
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise HelperError(
            "Guacamole assignee discovery failed",
            code="ASSIGNEE_DISCOVERY_FAILED",
            stage="discover-assignees",
        ) from exc

    users: list[dict[str, str]] = []
    groups: list[dict[str, str]] = []
    output = _command_output(result)
    for raw_line in output.splitlines():
        parts = raw_line.strip().split("\t", 1)
        if len(parts) != 2:
            continue
        assignee_type, name = parts
        if assignee_type not in _ASSIGNEE_TYPES or not name:
            continue
        try:
            validate_assignee(assignee_type, name)
        except ValidationError:
            continue
        entry = {"type": assignee_type, "name": name, "label": name}
        (users if assignee_type == "USER" else groups).append(entry)
    return {"users": users, "groups": groups}


def build_list_payload(
    *,
    inventory_path: str | os.PathLike[str] = INVENTORY_PATH,
    runner=run_command,
    assignee_runner=run_command,
) -> dict[str, Any]:
    """Build the JSON consumed by Cockpit from inventory, libvirt, and Guacamole."""

    inventory = load_inventory(inventory_path)
    clone_counts: dict[str, int] = {}
    for clone in inventory.get("clones", []):
        if not isinstance(clone, Mapping):
            continue
        version = clone.get("templateVersion", clone.get("template_version"))
        if isinstance(version, str) and version:
            clone_counts[version] = clone_counts.get(version, 0) + 1
    templates: list[dict[str, Any]] = []
    for item in inventory.get("templates", []):
        if not isinstance(item, Mapping):
            continue
        template = dict(item)
        version = template.get("version")
        template["dependentCloneCount"] = clone_counts.get(version, 0) if isinstance(version, str) else 0
        template["hashState"] = "recorded" if isinstance(template.get("sha256"), str) and template["sha256"] else "unknown"
        templates.append(template)
    jobs = list_job_statuses(runner)
    job_by_name = {item.get("name"): item for item in jobs if isinstance(item.get("name"), str)}
    workspaces: list[dict[str, Any]] = []
    seen_workspace_names: set[str] = set()
    for item in inventory.get("clones", []):
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            continue
        name = item["name"]
        view = dict(item)
        job = job_by_name.get(name)
        if job is not None and _ledger_matches_inventory(item, job):
            transition = job.get("inventoryTransition") if isinstance(job.get("inventoryTransition"), Mapping) else None
            transition_desired = transition.get("desired") if isinstance(transition, Mapping) else None
            if not isinstance(transition_desired, Mapping) or transition_desired.get("name") != name:
                transition = None
            transition_phase = transition.get("phase") if isinstance(transition, Mapping) else None
            if transition_phase == "prepared":
                # The ready inventory is a staged DB transaction until the
                # worker records committed.  Keep list consumers from
                # treating the prewrite as a usable connection.
                view["status"] = _INVENTORY_SYNCING_STATUS
                view["jobStatus"] = job.get("status", "running")
                view["stage"] = "sync"
                view["repairAvailable"] = False
            elif transition_phase == "committed":
                view["status"] = "ready"
                view["stage"] = "permissions"
            elif transition_phase == "restored":
                previous = transition.get("previous")
                previous_status = previous.get("status") if isinstance(previous, Mapping) else None
                view["status"] = previous_status if isinstance(previous_status, str) else item.get("status", "pending")
                view["stage"] = job.get("stage", view.get("stage", "sync"))
                view["repairAvailable"] = _job_repair_available(job)
            # Inventory owns identity, assignment, and canonical readiness.
            # A matching ledger contributes only bounded operation metadata.
            elif item.get("status") == "ready":
                view["status"] = "ready"
                view.setdefault("stage", "permissions")
            elif item.get("status") == "pending" and job.get("status") == "ready":
                view["jobStatus"] = "ready"
                view["stage"] = job.get("stage", view.get("stage", "validation"))
            else:
                view["status"] = job.get("status", view.get("status", "pending"))
                for key in ("operation", "jobId", "stage", "progress", "error", "errorCode", "errorStage", "errorMessage", "updatedAt"):
                    if key in job:
                        view[key] = job[key]
        view.setdefault("status", "pending")
        workspaces.append(view)
        seen_workspace_names.add(name)
    for job in jobs:
        name = job.get("name")
        status = job.get("status")
        if not isinstance(name, str) or name in seen_workspace_names:
            continue
        if status not in _JOB_ACTIVE_STATUSES and status not in {"failed", "stale", "failed-without-inventory"}:
            continue
        # An active or diagnostic ledger can be shown so F5 can keep polling
        # and expose a safe action.  It can never promote a job-only row to
        # ready or supply identity fields from a stale result.
        view = {
            key: job[key]
            for key in ("name", "templateVersion", "assigneeType", "assigneeName", "jobId", "operation", "stage", "progress", "error", "errorCode", "errorStage", "errorMessage", "updatedAt")
            if key in job
        }
        if status == "failed":
            view["status"] = "stale"
            view["errorCode"] = view.get("errorCode", "JOB_UNIT_FAILED")
        else:
            view["status"] = status
        view["orphaned"] = True
        workspaces.append(view)
    workspaces.sort(key=lambda item: str(item.get("updatedAt", item.get("name", ""))), reverse=True)
    payload: dict[str, Any] = {
        "ok": True,
        "templates": templates,
        "clones": inventory.get("clones", []),
        "workspaces": workspaces,
        "jobs": jobs,
        "guacamoleAssignees": discover_guacamole_assignees(assignee_runner),
    }
    domains = discover_domains(runner)
    if domains:
        payload["domains"] = domains
    return payload


def _ledger_matches_inventory(item: Mapping[str, Any], job: Mapping[str, Any]) -> bool:
    """Require the durable ledger to agree with inventory before enriching it."""

    if item.get("name") != job.get("name"):
        return False
    for key in ("templateVersion", "syncAttemptId", "mac", "ip", "assigneeType", "assigneeName", "connectionId"):
        inventory_value = item.get(key)
        job_value = job.get(key)
        if inventory_value is not None and job_value is not None and str(inventory_value).lower() != str(job_value).lower():
            return False
    if item.get("templateVersion") is not None and job.get("templateVersion") is None:
        return False
    if item.get("syncAttemptId") is not None and job.get("syncAttemptId") is None:
        return False
    return True


def _workspace_inventory_record(name: str, inventory: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for item in inventory.get("clones", []):
        if isinstance(item, Mapping) and item.get("name") == name:
            return item
    return None


def _job_response(status: Mapping[str, Any], *, repair_available: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "jobId": status.get("jobId"),
        "status": status.get("status", "pending"),
        "stage": status.get("stage"),
        "job": dict(status),
    }
    if isinstance(status.get("result"), Mapping):
        payload["clone"] = dict(status["result"])
    if repair_available and not _job_repair_blocked(status):
        payload["repairAvailable"] = True
    return payload


def _schedule_job(
    name: str,
    operation: str,
    updates: Mapping[str, Any],
    worker_arguments: Sequence[str],
) -> dict[str, Any]:
    """Serialize local callers before the durable unit conflict check."""

    with _JOB_SCHEDULE_LOCK:
        return _schedule_job_locked(name, operation, updates, worker_arguments)


def _schedule_job_locked(
    name: str,
    operation: str,
    updates: Mapping[str, Any],
    worker_arguments: Sequence[str],
) -> dict[str, Any]:
    validate_vm_name(name)
    current = read_job_status(name)
    if current is not None and current.get("status") in _JOB_ACTIVE_STATUSES:
        current = reconcile_job_status(current)
    if current is not None and _job_repair_blocked(current):
        return _job_response(current, repair_available=False)
    if current is not None and current.get("status") in _JOB_ACTIVE_STATUSES:
        return _job_response(current)
    if current is not None and current.get("status") == "ready":
        return _job_response(current)
    job_id = _job_id_for_name(name)
    unit_state = _systemd_unit_state(job_id)
    if unit_state.get("_query") == "error":
        blocked = write_job_status(
            name,
            {
                **updates,
                "jobId": job_id,
                "operation": operation,
                "status": "stale",
                "stage": "command",
                "error": {"code": "JOB_SYSTEMD_STATE_UNKNOWN", "stage": "command"},
            },
        )
        return _job_response(blocked, repair_available=False)
    if unit_state and unit_state.get("ActiveState") in {"active", "activating", "deactivating"}:
        current = write_job_status(name, {**updates, "jobId": job_id, "operation": operation, "status": "running"})
        return _job_response(current)
    queued = dict(updates)
    queued.update({"jobId": job_id, "operation": operation, "status": "queued"})
    queued_status = write_job_status(name, queued)
    argv = [
        "systemd-run",
        "--no-block",
        "--collect",
        f"--unit={job_id}",
        "--service-type=oneshot",
        "--uid=root",
        "--gid=root",
        "--property=UMask=0077",
        "--property=KillMode=control-group",
        str(HELPER_EXECUTABLE),
        operation,
        *worker_arguments,
        "--job-id",
        job_id,
        "--json",
        "--progress",
    ]
    try:
        run_command(argv, timeout=PREFLIGHT_TIMEOUT_SECONDS)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        del exc
        # A launcher failure does not prove that systemd rejected the unit:
        # systemd-run may have accepted it and failed while returning output.
        # Query the authoritative unit state before classifying the ledger as
        # a repairable start failure.  Only LoadState=not-found proves that no
        # worker owns the job.  Every other result fails closed.
        post_launch_state = _systemd_unit_state(job_id)
        post_launch_query = (
            post_launch_state.get("_query")
            if isinstance(post_launch_state, Mapping)
            else None
        )
        if post_launch_query == "ok" and post_launch_state.get("ActiveState") in {
            "active", "activating", "deactivating"
        }:
            running_status = write_job_status(
                name,
                {
                    **updates,
                    "jobId": job_id,
                    "operation": operation,
                    "status": "running",
                },
            )
            return _job_response(running_status)
        authoritative_not_found = (
            post_launch_query == "not-found"
            and post_launch_state.get("LoadState") == "not-found"
            and post_launch_state.get("ActiveState", "inactive") in {"", "inactive"}
        ) if isinstance(post_launch_state, Mapping) else False
        if not authoritative_not_found:
            blocked = write_job_status(
                name,
                {
                    **updates,
                    "jobId": job_id,
                    "operation": operation,
                    "status": "stale",
                    "stage": "command",
                    "error": {"code": "JOB_SYSTEMD_STATE_UNKNOWN", "stage": "command"},
                },
            )
            return _job_response(blocked, repair_available=False)
        failure = {"status": "failed", "stage": "command", "error": {"code": "JOB_START_FAILED", "stage": "command"}}
        failed_status = write_job_status(name, failure)
        return _job_response(failed_status, repair_available=True)
    return _job_response(queued_status)


def _prove_no_clone_artifacts(name: str, runner=None) -> bool:
    """Allow a failed job restart only after a conservative no-artifact proof."""

    if runner is None:
        runner = run_command
    validate_vm_name(name)
    paths = (
        VMS_DIR / f"{name}.qcow2",
        VMS_DIR / f"{name}.xml",
        CLONE_NVRAM_DIR / f"{name}_VARS.fd",
        _workspace_ownership_path(name),
        _workspace_nvram_marker_path(CLONE_NVRAM_DIR / f"{name}_VARS.fd"),
    )
    if any(path.exists() for path in paths):
        return False
    try:
        _run_stage(
            runner,
            ["virsh", "-c", LIBVIRT_URI, "dominfo", name],
            "artifact-check",
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
    except HelperError as exc:
        return _is_verified_domain_not_found(exc)
    return False


def start_workspace_job(
    name: str,
    assignee_type: str,
    assignee_name: str,
    template_version: str,
    *,
    memory_mib: int = 4096,
    vcpus: int = 2,
    wait_rdp_minutes: float = 20.0,
    inventory_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Queue a detached clone whose lifecycle is independent from Cockpit."""

    validate_vm_name(name)
    validate_vm_name(template_version)
    validate_assignee(assignee_type, assignee_name)
    if (
        isinstance(memory_mib, bool)
        or not isinstance(memory_mib, int)
        or not CLONE_MEMORY_MIN_MIB <= memory_mib <= CLONE_MEMORY_MAX_MIB
        or isinstance(vcpus, bool)
        or not isinstance(vcpus, int)
        or not CLONE_VCPUS_MIN <= vcpus <= CLONE_VCPUS_MAX
    ):
        raise ValidationError("Memory and vCPU values are invalid", code="RESOURCES_INVALID")
    try:
        wait_value = float(wait_rdp_minutes)
    except (OverflowError, TypeError, ValueError):
        wait_value = math.nan
    if (
        isinstance(wait_rdp_minutes, bool)
        or not isinstance(wait_rdp_minutes, (int, float))
        or not math.isfinite(wait_value)
        or not 0.0 <= wait_value <= CLONE_WAIT_RDP_MINUTES_MAX
    ):
        raise ValidationError("RDP wait time is invalid", code="RDP_TIMEOUT_INVALID")
    target_inventory_path = Path(INVENTORY_PATH if inventory_path is None else inventory_path)
    inventory = load_inventory(target_inventory_path)
    existing = _workspace_inventory_record(name, inventory)
    if existing is not None:
        status = read_job_status(name)
        if status is not None and status.get("status") in _JOB_ACTIVE_STATUSES:
            status = reconcile_job_status(status)
        if status is None:
            status = _job_status_payload(name, existing, {})
            status["jobId"] = _job_id_for_name(name)
            status["result"] = _job_result_value(existing)
        return _job_response(status, repair_available=_job_repair_available(status))
    status = read_job_status(name)
    if status is not None and status.get("status") in _JOB_ACTIVE_STATUSES:
        status = reconcile_job_status(status)
    if status is not None and _job_repair_blocked(status):
        return _job_response(status, repair_available=False)
    if status is not None and status.get("status") in _JOB_ACTIVE_STATUSES | frozenset(("ready",)):
        return _job_response(status)
    if existing is None and status is not None and status.get("status") in {"failed", "stale"}:
        if not _prove_no_clone_artifacts(name):
            blocked = write_job_status(
                name,
                {
                    "status": "failed-without-inventory",
                    "stage": "command",
                    "error": {"code": "ARTIFACTS_UNVERIFIED", "stage": "command"},
                },
            )
            return _job_response(blocked, repair_available=False)
    updates = {
        "templateVersion": template_version,
        "assigneeType": assignee_type,
        "assigneeName": assignee_name,
        "memoryMiB": memory_mib,
        "vcpus": vcpus,
    }
    return _schedule_job(
        name,
        "clone",
        updates,
        [
            "--name", name,
            "--assign-user" if assignee_type == "USER" else "--assign-group", assignee_name,
            "--template", template_version,
            "--memory-mib", str(memory_mib),
            "--vcpus", str(vcpus),
            "--wait-rdp-minutes", str(wait_rdp_minutes),
        ],
    )


def repair_workspace_job(
    name: str,
    *,
    inventory_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Queue an ownership-safe sync repair for an existing clone."""

    validate_vm_name(name)
    target_inventory_path = Path(INVENTORY_PATH if inventory_path is None else inventory_path)
    inventory = load_inventory(target_inventory_path)
    record = _workspace_inventory_record(name, inventory)
    if record is None:
        current = read_job_status(name)
        if current is not None and current.get("status") in _JOB_ACTIVE_STATUSES:
            current = reconcile_job_status(current)
        if current is not None and _job_repair_blocked(current):
            return _job_response(current, repair_available=False)
        if current is not None and current.get("status") == "failed-without-inventory":
            return _job_response(current, repair_available=False)
        if current is not None and current.get("status") in {"failed", "stale"}:
            if not _prove_no_clone_artifacts(name):
                blocked = write_job_status(
                    name,
                    {
                        "status": "failed-without-inventory",
                        "stage": "command",
                        "error": {"code": "ARTIFACTS_UNVERIFIED", "stage": "command"},
                    },
                )
                return _job_response(blocked, repair_available=False)
            assignee_type = current.get("assigneeType")
            assignee_name = current.get("assigneeName")
            template_version = current.get("templateVersion")
            if assignee_type in _ASSIGNEE_TYPES and isinstance(assignee_name, str) and isinstance(template_version, str):
                return _schedule_job(
                    name,
                    "clone",
                    {
                        "templateVersion": template_version,
                        "assigneeType": assignee_type,
                        "assigneeName": assignee_name,
                    },
                    [
                        "--name", name,
                        "--assign-user" if assignee_type == "USER" else "--assign-group", assignee_name,
                        "--template", template_version,
                    ],
                )
            return _job_response(current, repair_available=False)
        raise ValidationError("Workspace record is missing", code="WORKSPACE_NOT_FOUND")
    current = read_job_status(name)
    if current is not None and current.get("status") in _JOB_ACTIVE_STATUSES:
        current = reconcile_job_status(current)
    if current is not None and _job_repair_blocked(current):
        return _job_response(current, repair_available=False)
    current_status = str(record.get("status", "pending"))
    if current is not None:
        if current.get("status") in _JOB_ACTIVE_STATUSES:
            return _job_response(current)
        if current.get("status") == "failed-without-inventory":
            return _job_response(current, repair_available=False)
        if current.get("status") == "ready" or current.get("status") not in _JOB_REPAIRABLE_STATUSES:
            if current.get("status") == "ready":
                return _job_response(current)
    if current_status == "ready":
        status = _job_status_payload(name, {"status": "ready", "stage": "permissions", "result": record}, current or {})
        return _job_response(status)
    if current_status not in _JOB_REPAIRABLE_STATUSES:
        raise ValidationError("Workspace is not repairable", code="WORKSPACE_NOT_REPAIRABLE")
    return _schedule_job(name, "repair", {"stage": "repair", "result": record}, ["--name", name])


def _command_output(result: Any) -> str:
    """Return stdout from a CompletedProcess-like result."""

    return str(getattr(result, "stdout", result) or "")


def _run_stage(
    runner,
    arguments: Sequence[str],
    stage: str,
    *,
    timeout: float | None = None,
) -> Any:
    """Run one fixed argv command and expose a safe helper error."""

    try:
        accepts_timeout = False
        if timeout is not None:
            try:
                parameters = inspect.signature(runner).parameters.values()
                accepts_timeout = any(parameter.name == "timeout" for parameter in parameters) or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
                )
            except (TypeError, ValueError):
                accepts_timeout = runner is run_command
        if timeout is not None and accepts_timeout:
            return runner(list(arguments), timeout=timeout)
        return runner(list(arguments))
    except subprocess.TimeoutExpired as exc:
        raise HelperError(
            f"{stage} command timed out",
            code="COMMAND_TIMEOUT",
            stage=stage,
        ) from exc
    except HelperError:
        raise
    except Exception as exc:
        raise HelperError(
            f"{stage} command failed",
            code="COMMAND_FAILED",
            stage=stage,
        ) from exc


def _template_paths(version: str) -> tuple[Path, Path]:
    """Return the final and sibling partial paths for a validated version."""

    final_path = TEMPLATES_DIR / f"{version}.qcow2"
    return final_path, final_path.with_name(f"{final_path.name}.partial")


def _assert_template_version_available(version: str) -> tuple[Path, Path, dict[str, Any]]:
    """Reject an existing file, partial, or manifest before touching the source."""

    final_path, partial_path = _template_paths(version)
    inventory = load_inventory()
    for item in inventory.get("templates", []):
        if isinstance(item, Mapping) and item.get("version") == version:
            raise ConflictError("Template version is already present")
    if final_path.exists():
        raise ConflictError("Template version is already present")
    if partial_path.exists():
        raise ConflictError("Template partial image is already present")
    return final_path, partial_path, inventory


def _validate_template_source(source: str, runner) -> None:
    """Confirm the source domain exists and is persistent."""

    result = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "dominfo", source],
        "validate",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    output = _command_output(result)
    fields = {
        line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
        for line in output.splitlines()
        if ":" in line
    }
    if fields.get("name") != source:
        raise ValidationError("Source domain is invalid", code="SOURCE_INVALID")
    if fields.get("persistent", "").lower() != "yes":
        raise ValidationError("Source domain is not persistent", code="SOURCE_NOT_PERSISTENT")


def _preflight_field_map(output: str) -> dict[str, str]:
    return {
        line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
        for line in output.splitlines()
        if ":" in line
    }


def _preflight_source_definition(source: str, source_disk: Path, runner) -> int:
    """Check the persistent source wiring using read-only libvirt queries."""

    xml_result = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "dumpxml", source],
        "preflight",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    try:
        root = ET.fromstring(_command_output(xml_result))
    except ET.ParseError as exc:
        raise HelperError("Source domain XML is invalid", code="SOURCE_INVALID", stage="preflight") from exc

    if root.findtext("name", "").strip() != source:
        raise HelperError("Source domain XML name is invalid", code="SOURCE_INVALID", stage="preflight")

    expected_disk = str(source_disk)
    disk_matches = False
    for disk in root.findall("./devices/disk"):
        driver = disk.find("driver")
        disk_source = disk.find("source")
        if (
            disk.get("device") == "disk"
            and driver is not None
            and driver.get("type") == "qcow2"
            and disk_source is not None
            and disk_source.get("file") == expected_disk
        ):
            disk_matches = True
            break
    if not disk_matches:
        raise HelperError("Source disk wiring is invalid", code="SOURCE_DISK_INVALID", stage="preflight")

    nvram = root.find("./os/nvram")
    if nvram is None or (nvram.text or "").strip() != str(SOURCE_NVRAM_PATH):
        raise HelperError("Source NVRAM wiring is invalid", code="SOURCE_NVRAM_INVALID", stage="preflight")

    tpm_matches = False
    for tpm in root.findall("./devices/tpm"):
        backend = tpm.find("backend")
        tpm_source = backend.find("source") if backend is not None else None
        if (
            backend is not None
            and backend.get("type") == "external"
            and tpm_source is not None
            and tpm_source.get("type") == "unix"
            and tpm_source.get("path") == SOURCE_TPM_SOCKET
        ):
            tpm_matches = True
            break
    if not tpm_matches:
        raise HelperError("Source TPM wiring is invalid", code="SOURCE_TPM_INVALID", stage="preflight")

    network_matches = False
    for interface in root.findall("./devices/interface"):
        network_source = interface.find("source")
        model = interface.find("model")
        if (
            interface.get("type") == "network"
            and network_source is not None
            and network_source.get("network") == NETWORK_NAME
            and model is not None
            and model.get("type") == "e1000e"
        ):
            network_matches = True
            break
    if not network_matches:
        raise HelperError("Source network wiring is invalid", code="SOURCE_NETWORK_INVALID", stage="preflight")

    network_result = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "net-info", NETWORK_NAME],
        "preflight",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    network_fields = _preflight_field_map(_command_output(network_result))
    if network_fields.get("active", "").lower() != "yes" or network_fields.get("persistent", "").lower() != "yes":
        raise HelperError("Source network is not active and persistent", code="SOURCE_NETWORK_INVALID", stage="preflight")

    block_result = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "domblkinfo", source, str(source_disk)],
        "preflight",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    block_fields = _preflight_field_map(_command_output(block_result))
    try:
        capacity = int(block_fields["capacity"].split()[0])
    except (KeyError, ValueError) as exc:
        raise HelperError("Source disk metadata is invalid", code="SOURCE_DISK_INVALID", stage="preflight") from exc
    if capacity <= 0:
        raise HelperError("Source disk capacity is invalid", code="SOURCE_DISK_INVALID", stage="preflight")
    return capacity


def _preflight_tpm(runner) -> None:
    """Verify the dedicated TPM owner, socket, and command contract read-only."""

    state_result = _run_stage(
        runner,
        ["systemctl", "show", "-p", "ActiveState", "--value", SOURCE_TPM_UNIT],
        "preflight",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    if _command_output(state_result).strip().lower() != "active":
        raise HelperError("Dedicated source TPM service is not active", code="TPM_SERVICE_INVALID", stage="preflight")

    try:
        _run_stage(runner, ["test", "-S", SOURCE_TPM_SOCKET], "preflight", timeout=PREFLIGHT_TIMEOUT_SECONDS)
    except HelperError as exc:
        raise HelperError("Dedicated source TPM socket is unavailable", code="TPM_SOCKET_INVALID", stage="preflight") from exc

    try:
        if not SOURCE_TPM_STATE_PATH.is_dir():
            raise OSError("TPM state directory is unavailable")
    except OSError as exc:
        raise HelperError("Dedicated source TPM state is unavailable", code="TPM_STATE_INVALID", stage="preflight") from exc

    unit_result = _run_stage(
        runner,
        ["systemctl", "cat", SOURCE_TPM_UNIT],
        "preflight",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    unit_text = _command_output(unit_result)
    required_contract = (
        "ExecStart=/usr/bin/swtpm socket --tpm2",
        f"--tpmstate dir={SOURCE_TPM_STATE_PATH}",
        f"path={SOURCE_TPM_SOCKET}",
    )
    if any(contract not in unit_text for contract in required_contract):
        raise HelperError("Dedicated source TPM service contract is invalid", code="TPM_SERVICE_INVALID", stage="preflight")


def _preflight_guacamole_connection(runner) -> None:
    """Verify the existing Windows 11 Guacamole connection without writing SQL."""

    result = _run_stage(
        runner,
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-q",
            "-At",
            "-U",
            "guacamole_user",
            "-d",
            "guacamole_db",
            "-c",
            GUAC_CONNECTION_QUERY,
        ],
        "preflight",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    rows = [line.strip() for line in _command_output(result).splitlines() if line.strip()]
    if len(rows) != 1:
        raise HelperError("Windows 11 Guacamole connection is missing or duplicated", code="GUAC_CONNECTION_INVALID", stage="preflight")
    fields = rows[0].split("|")
    if (
        len(fields) != 5
        or fields[1] != GUAC_CONNECTION_NAME
        or fields[2].lower() != "rdp"
        or fields[3] != "match"
        or fields[4] != "match"
    ):
        raise HelperError("Windows 11 Guacamole connection target is invalid", code="GUAC_CONNECTION_INVALID", stage="preflight")


def _preflight_storage(source_disk: Path, final_path: Path, partial_path: Path, virtual_size: int, runner) -> None:
    """Run live-safe storage checks without opening the source disk directly."""

    for path, label in ((source_disk, "source disk"), (SOURCE_NVRAM_PATH, "source NVRAM")):
        try:
            metadata = path.stat()
        except OSError as exc:
            raise HelperError(f"{label} is unavailable", code="SOURCE_ASSET_MISSING", stage="storage") from exc
        if not path.is_file() or metadata.st_size <= 0 or not os.access(path, os.R_OK):
            raise HelperError(f"{label} is unavailable", code="SOURCE_ASSET_MISSING", stage="storage")

    if not TEMPLATES_DIR.is_dir() or not os.access(TEMPLATES_DIR, os.W_OK | os.X_OK):
        raise HelperError("Template storage directory is unavailable", code="STORAGE_INVALID", stage="storage")
    try:
        available = os.statvfs(TEMPLATES_DIR).f_bavail * os.statvfs(TEMPLATES_DIR).f_frsize
    except OSError as exc:
        raise HelperError("Template storage capacity is unavailable", code="STORAGE_INVALID", stage="storage") from exc
    if available < virtual_size:
        raise HelperError("Template storage has insufficient free space", code="STORAGE_FULL", stage="storage")
    if final_path.exists() or partial_path.exists():
        raise ConflictError("Template version is already present")


def _wait_for_domain_shutoff(source: str, runner) -> None:
    """Poll libvirt, then force poweroff if the guest ignores ACPI shutdown."""

    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    graceful_deadline = min(deadline, time.monotonic() + GRACEFUL_SHUTDOWN_SECONDS)
    forced = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HelperError(
                "Source domain did not shut off before the timeout",
                code="SHUTDOWN_TIMEOUT",
                stage="wait-shutoff",
            )
        result = _run_stage(
            runner,
            ["virsh", "-c", LIBVIRT_URI, "domstate", source],
            "wait-shutoff",
            timeout=remaining,
        )
        state = " ".join(_command_output(result).strip().lower().split())
        if state in {"shut off", "shutoff", "off", "inactive"}:
            return
        if not forced and time.monotonic() >= graceful_deadline:
            _run_stage(
                runner,
                ["virsh", "-c", LIBVIRT_URI, "destroy", source],
                "force-shutdown-source",
                timeout=remaining,
            )
            forced = True
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))


def _source_disk_is_held(source_disk: Path) -> bool:
    """Check /proc file descriptors for a process still holding the source disk."""

    if not source_disk.exists():
        return False
    try:
        source_stat = source_disk.stat()
    except OSError:
        return False
    for process_path in Path("/proc").glob("[0-9]*"):
        fd_directory = process_path / "fd"
        try:
            descriptors = list(fd_directory.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                descriptor_stat = descriptor.stat()
            except OSError:
                continue
            if descriptor_stat.st_dev == source_stat.st_dev and descriptor_stat.st_ino == source_stat.st_ino:
                return True
    return False


def _check_source_unlocked(source_disk: Path, runner) -> None:
    """Require no disk holder and an inactive dedicated source TPM unit."""

    if _source_disk_is_held(source_disk):
        raise HelperError(
            "Source disk is still held by a process",
            code="SOURCE_DISK_LOCKED",
            stage="check-unlocked",
        )
    try:
        result = _run_stage(
            runner,
            ["systemctl", "is-active", SOURCE_TPM_UNIT],
            "check-unlocked",
            timeout=SHUTDOWN_TIMEOUT_SECONDS,
        )
    except HelperError as exc:
        cause = exc.__cause__
        if isinstance(cause, subprocess.CalledProcessError):
            state = _command_output(cause).strip().lower()
            if state in {"inactive", "dead"}:
                return
        raise HelperError(
            "Dedicated source TPM unit state cannot be confirmed",
            code="TPM_STATE_UNKNOWN",
            stage="check-unlocked",
        ) from exc
    state = _command_output(result).strip().lower()
    if state == "active":
        _run_stage(
            runner,
            ["systemctl", "stop", SOURCE_TPM_UNIT],
            "stop-source-tpm",
            timeout=SHUTDOWN_TIMEOUT_SECONDS,
        )
        result = _run_stage(
            runner,
            ["systemctl", "is-active", SOURCE_TPM_UNIT],
            "check-unlocked",
            timeout=SHUTDOWN_TIMEOUT_SECONDS,
        )
        state = _command_output(result).strip().lower()
    if state not in {"inactive", "dead"}:
        raise HelperError(
            "Dedicated source TPM unit is still active",
            code="TPM_STILL_ACTIVE",
            stage="check-unlocked",
        )


def _qemu_img_check(path: Path, runner, stage: str) -> None:
    arguments = ["qemu-img", "check", "--read-only", str(path)]
    try:
        _run_stage(runner, arguments, stage, timeout=CONVERSION_TIMEOUT_SECONDS)
    except HelperError as exc:
        cause = exc.__cause__
        stderr = str(getattr(cause, "stderr", "") or "")
        if "--read-only" not in stderr or "unrecognized option" not in stderr:
            raise
        # qemu-img 8.2 (the Ubuntu 24.04 package) has no --read-only flag.
        # check without -r performs no repair and is therefore read-only.
        _run_stage(
            runner,
            ["qemu-img", "check", "-f", "qcow2", str(path)],
            stage,
            timeout=CONVERSION_TIMEOUT_SECONDS,
        )


def _hash_and_virtual_size(path: Path, runner) -> tuple[str, int | None]:
    hash_result = _run_stage(
        runner,
        ["sha256sum", str(path)],
        "hash-temp",
        timeout=CONVERSION_TIMEOUT_SECONDS,
    )
    digest = _command_output(hash_result).strip().split()
    if not digest or not re.fullmatch(r"[0-9a-fA-F]{64}", digest[0]):
        raise HelperError("Template hash output is invalid", code="HASH_INVALID", stage="hash-temp")
    info_result = _run_stage(
        runner,
        ["qemu-img", "info", "--output=json", str(path)],
        "hash-temp",
        timeout=CONVERSION_TIMEOUT_SECONDS,
    )
    try:
        info = json.loads(_command_output(info_result))
        virtual_size = int(info["virtual-size"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HelperError("Template virtual size is invalid", code="IMAGE_INFO_INVALID", stage="hash-temp") from exc
    return digest[0].lower(), virtual_size


def _is_transient_rdp_probe_error(error: HelperError) -> bool:
    """Retry only a failed TCP probe, never a Docker/configuration failure."""

    cause = error.__cause__
    if error.code == "COMMAND_TIMEOUT":
        if isinstance(cause, subprocess.TimeoutExpired):
            diagnostic = " ".join(
                str(value or "")
                for value in (cause.stderr, cause.output)
            ).lower()
            if any(marker in diagnostic for marker in (
                "permission denied",
                "cannot connect to the docker daemon",
                "docker daemon is not running",
                "no such network",
                "network not found",
            )) or re.search(r"\b(?:network|image)\b.*\bnot found\b", diagnostic):
                return False
        return True
    if not isinstance(cause, subprocess.CalledProcessError):
        return False
    diagnostic = " ".join(
        str(value or "")
        for value in (cause.stderr, cause.output)
    ).strip().lower()
    configuration_markers = (
        "no such network",
        "network not found",
        "permission denied",
        "cannot connect to the docker daemon",
        "docker daemon is not running",
        "error during connect",
        "is the docker daemon running",
        "no such image",
        "pull access denied",
        "manifest unknown",
        "error response from daemon",
        "unauthorized",
        "failed to pull",
        "failed to resolve",
        "dial tcp",
        "invalid reference",
        "executable file not found",
    )
    if any(marker in diagnostic for marker in configuration_markers) or re.search(
        r"\b(?:network|image)\b.*\bnot found\b", diagnostic
    ):
        return False
    transient_markers = (
        "connection refused",
        "connection timed out",
        "operation timed out",
        "timed out",
        "refused",
    )
    if any(marker in diagnostic for marker in transient_markers):
        return cause.returncode == 1
    # busybox nc commonly returns 1 with no diagnostic when the TCP port is
    # closed.  Treat that as transient only when the failed argv is the
    # expected TCP probe; an arbitrary silent Docker exit 1 is a runtime
    # failure and must not be hidden as an RDP timeout.
    command = getattr(cause, "cmd", ())
    command_parts = command.split() if isinstance(command, str) else [str(part) for part in command]
    is_tcp_probe = "nc" in command_parts and "-z" in command_parts and "--network" in command_parts
    return cause.returncode == 1 and not diagnostic and is_tcp_probe


def wait_for_rdp(runner=run_command, source: str = "windows11") -> None:
    """Require the source guest RDP port from the Compose network."""

    del source  # The source address is a deployment contract, not user input.
    deadline = time.monotonic() + RDP_TIMEOUT_SECONDS
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        COMPOSE_NETWORK_NAME,
        "busybox:1.36",
        "nc",
        "-z",
        "-w",
        "5",
        SOURCE_RDP_ADDRESS,
        str(RDP_PORT),
    ]
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HelperError(
                "Source RDP did not become ready before the timeout",
                code="RDP_NOT_READY",
                stage="wait-source-rdp",
            )
        try:
            _run_stage(runner, command, "wait-source-rdp", timeout=remaining)
            return
        except HelperError as exc:
            if not _is_transient_rdp_probe_error(exc):
                raise
            if time.monotonic() >= deadline:
                raise HelperError(
                    "Source RDP did not become ready before the timeout",
                    code="RDP_NOT_READY",
                    stage="wait-source-rdp",
                ) from exc
            time.sleep(min(POLL_INTERVAL_SECONDS, remaining))


def _recover_source(source: str, runner) -> None:
    """Poll a requested shutdown to completion, then start and probe RDP."""

    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HelperError(
                "Source domain did not reach shut off during recovery",
                code="RECOVERY_TIMEOUT",
                stage="recovery-state",
            )
        state_result = _run_stage(
            runner,
            ["virsh", "-c", LIBVIRT_URI, "domstate", source],
            "recovery-state",
            timeout=remaining,
        )
        state = " ".join(_command_output(state_result).strip().lower().split())
        if state in {"shut off", "shutoff", "off", "inactive"}:
            break
        if state not in {"running", "idle", "in shutdown", "shutting down"}:
            raise HelperError(
                "Source domain recovery state is unknown",
                code="RECOVERY_STATE_UNKNOWN",
                stage="recovery-state",
            )
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))

    _run_stage(
        runner,
        ["systemctl", "start", SOURCE_TPM_UNIT],
        "start-source-tpm",
        timeout=SHUTDOWN_TIMEOUT_SECONDS,
    )
    _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "start", source],
        "start-source",
        timeout=SHUTDOWN_TIMEOUT_SECONDS,
    )
    wait_for_rdp(runner, source)


def _record_stage(runner, stage: str) -> None:
    """Allow deterministic fake runners to observe local transaction stages."""

    callback = getattr(runner, "record_stage", None)
    if callable(callback):
        callback(stage)


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _remove_published_if_owned(path: Path, identity: tuple[int, int] | None) -> None:
    """Remove a final image only while its inode is this transaction's inode."""

    if identity is None:
        return
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (metadata.st_dev, metadata.st_ino) == identity:
        _remove_if_present(path)


def _error_text(error: BaseException) -> str:
    if isinstance(error, HelperError):
        return error.message
    return str(error) or type(error).__name__


def _restore_inventory(previous_inventory: Mapping[str, Any], existed: bool, path: Path) -> None:
    """Restore the exact pre-transaction inventory presence and records."""

    if existed:
        save_inventory_atomic(previous_inventory, path)
    else:
        _remove_if_present(path)


def create_template(
    source: str,
    version: str,
    runner=run_command,
    *,
    what_if: bool = False,
) -> TemplateRecord | None:
    """Create and publish an immutable qcow2 template transactionally."""

    validate_vm_name(source)
    validate_vm_name(version)
    if source != "windows11":
        raise ValidationError("Source must be windows11", code="SOURCE_INVALID")
    final_path, partial_path, previous_inventory = _assert_template_version_available(version)
    source_disk = SOURCE_DISK_PATH
    _validate_template_source(source, runner)
    source_capacity = _preflight_source_definition(source, source_disk, runner)
    _preflight_tpm(runner)
    _preflight_guacamole_connection(runner)
    _preflight_storage(source_disk, final_path, partial_path, source_capacity, runner)
    if what_if:
        return None

    shutdown_requested = False
    final_published = False
    published_identity: tuple[int, int] | None = None
    inventory_replaced = False
    inventory_path = Path(INVENTORY_PATH)
    inventory_existed = inventory_path.exists()
    primary_error: BaseException | None = None
    recovery_error: BaseException | None = None
    record: TemplateRecord | None = None
    try:
        try:
            shutdown_requested = True
            _run_stage(
                runner,
                ["virsh", "-c", LIBVIRT_URI, "shutdown", source],
                "shutdown-source",
                timeout=SHUTDOWN_TIMEOUT_SECONDS,
            )
            _wait_for_domain_shutoff(source, runner)
            _check_source_unlocked(source_disk, runner)
            _qemu_img_check(source_disk, runner, "qemu-img-check-source")
            _run_stage(
                runner,
                ["qemu-img", "convert", "-p", "-O", "qcow2", str(source_disk), str(partial_path)],
                "convert-temp",
                timeout=CONVERSION_TIMEOUT_SECONDS,
            )
            _qemu_img_check(partial_path, runner, "check-temp")
            digest, virtual_size = _hash_and_virtual_size(partial_path, runner)
            _run_stage(runner, ["chown", "root:root", "--", str(partial_path)], "chown-root")
            _run_stage(runner, ["chmod", "0444", str(partial_path)], "chmod-readonly")
            _verify_template_file(partial_path, digest, runner, stage="rename-publish")
            _run_stage(runner, ["mv", "--no-clobber", "--", str(partial_path), str(final_path)], "rename-publish")
            if partial_path.exists():
                raise ConflictError("Template version appeared during publish")
            try:
                published_metadata = os.stat(final_path, follow_symlinks=False)
            except OSError as exc:
                raise HelperError("Published template cannot be inspected", code="PUBLISH_INVALID", stage="rename-publish") from exc
            if not stat_module.S_ISREG(published_metadata.st_mode):
                raise HelperError("Published template is not a regular file", code="PUBLISH_INVALID", stage="rename-publish")
            published_identity = (published_metadata.st_dev, published_metadata.st_ino)
            final_published = True
            _verify_template_file(final_path, digest, runner, stage="rename-publish")
            created_at = datetime.now(timezone.utc).isoformat()
            record = TemplateRecord(
                version=version,
                sourceDomain=source,
                path=str(final_path),
                sha256=digest,
                virtualSize=virtual_size,
                createdAt=created_at,
            )
            inventory = dict(previous_inventory)
            inventory["templates"] = list(previous_inventory.get("templates", [])) + [record]
            _record_stage(runner, "save-inventory")
            try:
                save_inventory_atomic(inventory)
            except InventoryError as exc:
                inventory_replaced = inventory_replaced or exc.replaced
                raise
            inventory_replaced = True
        except BaseException as exc:
            primary_error = exc
    finally:
        if shutdown_requested:
            try:
                _recover_source(source, runner)
            except BaseException as exc:
                recovery_error = exc

    if primary_error is not None or recovery_error is not None:
        rollback_errors: list[BaseException] = []
        try:
            _remove_if_present(partial_path)
        except Exception as exc:
            rollback_errors.append(exc)
        if final_published:
            try:
                _remove_published_if_owned(final_path, published_identity)
            except Exception as exc:
                rollback_errors.append(exc)
        if inventory_replaced:
            try:
                _restore_inventory(previous_inventory, inventory_existed, inventory_path)
            except Exception as exc:
                rollback_errors.append(exc)

        if rollback_errors:
            details = "; ".join(_error_text(error) for error in rollback_errors)
            context: list[str] = []
            if primary_error is not None:
                context.append("primary=" + _error_text(primary_error))
            if recovery_error is not None:
                context.append("recovery=" + _error_text(recovery_error))
            context_text = "; ".join(context)
            raise HelperError(
                f"Template rollback failed: {details}; {context_text}" if context_text else f"Template rollback failed: {details}",
                code="ROLLBACK_FAILED",
                stage="rollback",
            ) from rollback_errors[0]
        if recovery_error is not None:
            message = "Source recovery failed: " + _error_text(recovery_error)
            if primary_error is not None:
                message = f"Template operation failed: {_error_text(primary_error)}; {message}"
            raise HelperError(message, code="RECOVERY_FAILED", stage="recovery") from recovery_error
        if isinstance(primary_error, HelperError):
            raise primary_error
        if isinstance(primary_error, BaseException) and not isinstance(primary_error, Exception):
            raise primary_error
        raise HelperError("Template creation failed", code="COMMAND_FAILED", stage="command") from primary_error

    return record


def _used_clone_macs(inventory: Mapping[str, Any], runner) -> set[str]:
    used: set[str] = set()
    for clone in inventory.get("clones", []):
        if isinstance(clone, Mapping) and isinstance(clone.get("mac"), str):
            used.add(clone["mac"].lower())
    for command in (
        ["virsh", "-c", LIBVIRT_URI, "net-dumpxml", NETWORK_NAME],
        ["virsh", "-c", LIBVIRT_URI, "net-dumpxml", NETWORK_NAME, "--inactive"],
        ["virsh", "-c", LIBVIRT_URI, "net-dhcp-leases", NETWORK_NAME],
    ):
        result = _run_stage(runner, command, "allocate-mac", timeout=PREFLIGHT_TIMEOUT_SECONDS)
        used.update(_dhcp_leases(_command_output(result))[1])
        try:
            root = ET.fromstring(_command_output(result))
        except ET.ParseError:
            continue
        used.update(
            value.lower()
            for host in root.findall(".//host")
            if (value := host.get("mac")) is not None and _MAC_RE.fullmatch(value)
        )
    return used


def _clone_dhcp_xml(name: str, mac: str, ip: str) -> str:
    return f"<host mac='{mac}' name='{name}' ip='{ip}' />"


def _create_clone_nvram(nvram_path: Path, runner) -> None:
    if not CLONE_NVRAM_DIR.is_dir() or not os.access(CLONE_NVRAM_DIR, os.W_OK | os.X_OK):
        raise HelperError("Libvirt NVRAM directory is unavailable", code="NVRAM_STORAGE_INVALID", stage="create-nvram")
    if not CLONE_NVRAM_TEMPLATE_PATH.is_file() or not os.access(CLONE_NVRAM_TEMPLATE_PATH, os.R_OK):
        raise HelperError("Libvirt NVRAM template is unavailable", code="NVRAM_TEMPLATE_INVALID", stage="create-nvram")
    _run_stage(
        runner,
        [
            "install",
            "-o",
            CLONE_QEMU_OWNER,
            "-g",
            CLONE_QEMU_GROUP,
            "-m",
            CLONE_NVRAM_MODE,
            "--",
            str(CLONE_NVRAM_TEMPLATE_PATH),
            str(nvram_path),
        ],
        "create-nvram",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    metadata = _run_stage(
        runner,
        ["stat", "-c", "%U:%G:%a", str(nvram_path)],
        "verify-nvram",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    expected = f"{CLONE_QEMU_OWNER}:{CLONE_QEMU_GROUP}:{CLONE_NVRAM_MODE}"
    if _command_output(metadata).strip() != expected:
        raise HelperError("Clone NVRAM owner or mode is invalid", code="NVRAM_PERMISSIONS_INVALID", stage="verify-nvram")
    try:
        mode = nvram_path.stat().st_mode & 0o777
    except OSError as exc:
        raise HelperError("Clone NVRAM cannot be inspected", code="NVRAM_PERMISSIONS_INVALID", stage="verify-nvram") from exc
    if mode != int(CLONE_NVRAM_MODE, 8) or not mode & 0o200:
        raise HelperError("Clone NVRAM is not writable", code="NVRAM_PERMISSIONS_INVALID", stage="verify-nvram")


def _update_clone_dhcp(
    name: str,
    mac: str,
    ip: str,
    runner,
    action: str,
    scopes: Sequence[str] = ("--live", "--config"),
) -> None:
    _run_stage(
        runner,
        [
            "virsh",
            "-c",
            LIBVIRT_URI,
            "net-update",
            NETWORK_NAME,
            action,
            "ip-dhcp-host",
            _clone_dhcp_xml(name, mac, ip),
            *scopes,
        ],
        "dhcp",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )


def _clone_domain_exists(name: str, runner) -> bool:
    try:
        _run_stage(
            runner,
            ["virsh", "-c", LIBVIRT_URI, "dominfo", name],
            "validate-domain",
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
    except HelperError as exc:
        if _is_verified_domain_not_found(exc):
            return False
        raise
    return True


def _is_verified_domain_not_found(error: BaseException) -> bool:
    """Return true only for libvirt's explicit missing-domain response."""

    cause: BaseException | None = error.__cause__
    if not isinstance(cause, subprocess.CalledProcessError):
        return False
    diagnostic = " ".join(
        str(value or "")
        for value in (getattr(cause, "stderr", ""), getattr(cause, "stdout", ""), cause)
    ).lower()
    return any(
        marker in diagnostic
        for marker in (
            "no domain with matching name",
            "domain not found",
            "domain does not exist",
            "domain doesn't exist",
        )
    )


def _domain_identities(runner) -> tuple[set[str], set[str], set[str]]:
    """Read current identities and inactive identities for persistent domains."""

    result = _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "list", "--all", "--name"],
        "validate-domain",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    names: set[str] = set()
    uuids: set[str] = set()
    macs: set[str] = set()
    for raw_name in _command_output(result).splitlines():
        domain_name = raw_name.replace("\x00", "").strip()
        if not domain_name:
            continue
        names.add(domain_name)
        domain_info = _run_stage(
            runner,
            ["virsh", "-c", LIBVIRT_URI, "dominfo", domain_name],
            "validate-domain",
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
        persistent = _preflight_field_map(_command_output(domain_info)).get("persistent", "").lower() == "yes"
        xml_commands = [["virsh", "-c", LIBVIRT_URI, "dumpxml", domain_name]]
        if persistent:
            xml_commands.append(["virsh", "-c", LIBVIRT_URI, "dumpxml", domain_name, "--inactive"])
        for xml_arguments in xml_commands:
            xml_result = _run_stage(
                runner,
                xml_arguments,
                "validate-domain",
                timeout=PREFLIGHT_TIMEOUT_SECONDS,
            )
            try:
                root = ET.fromstring(_command_output(xml_result))
            except ET.ParseError as exc:
                raise HelperError("Existing domain XML is invalid", code="DOMAIN_INVALID", stage="validate-domain") from exc
            domain_uuid = root.findtext("uuid")
            if domain_uuid:
                uuids.add(domain_uuid.strip().lower())
            for interface in root.findall("./devices/interface"):
                mac_element = interface.find("mac")
                address = mac_element.get("address") if mac_element is not None else None
                if address and _MAC_RE.fullmatch(address):
                    macs.add(address.lower())
    return names, uuids, macs


def _lease_for_mac(text: str, mac: str) -> str | None:
    normalized = mac.lower()
    for line in text.splitlines():
        if normalized not in line.lower():
            continue
        addresses = _extract_ipv4_addresses(line)
        if addresses:
            return sorted(addresses)[0]
    return None


def wait_for_clone_lease(mac: str, ip: str, runner=run_command, timeout_seconds: float = CLONE_LEASE_TIMEOUT_SECONDS) -> str:
    """Wait for a lease matching both the generated MAC and reserved address."""

    try:
        timeout_value = float(timeout_seconds)
    except (OverflowError, TypeError, ValueError):
        timeout_value = math.nan
    if not math.isfinite(timeout_value) or timeout_value < 0:
        raise ValidationError("RDP wait time is invalid", code="RDP_TIMEOUT_INVALID")
    deadline = time.monotonic() + timeout_value
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HelperError("Clone DHCP lease did not become ready", code=RDP_NOT_READY, stage="wait-lease")
        try:
            result = _run_stage(
                runner,
                ["virsh", "-c", LIBVIRT_URI, "net-dhcp-leases", NETWORK_NAME],
                "wait-lease",
                timeout=remaining,
            )
        except HelperError as exc:
            if isinstance(exc.__cause__, subprocess.TimeoutExpired):
                if time.monotonic() >= deadline:
                    raise HelperError("Clone DHCP lease did not become ready", code=RDP_NOT_READY, stage="wait-lease") from exc
                continue
            raise
        leased_ip = _lease_for_mac(_command_output(result), mac)
        if leased_ip == ip:
            return leased_ip
        if time.monotonic() >= deadline:
            raise HelperError("Clone DHCP lease did not become ready", code=RDP_NOT_READY, stage="wait-lease")
        time.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))


def wait_for_clone_rdp(ip: str, runner=run_command, timeout_seconds: float = CLONE_RDP_TIMEOUT_SECONDS) -> None:
    """Probe a clone's RDP port from the Guacamole Docker network."""

    try:
        timeout_value = float(timeout_seconds)
    except (OverflowError, TypeError, ValueError):
        timeout_value = math.nan
    if not math.isfinite(timeout_value) or timeout_value < 0:
        raise ValidationError("RDP wait time is invalid", code="RDP_TIMEOUT_INVALID")
    deadline = time.monotonic() + timeout_value
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        COMPOSE_NETWORK_NAME,
        "busybox:1.36",
        "nc",
        "-z",
        "-w",
        "5",
        ip,
        str(RDP_PORT),
    ]
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HelperError("Clone RDP did not become ready before the timeout", code=RDP_NOT_READY, stage="wait-rdp")
        try:
            _run_stage(runner, command, "wait-rdp", timeout=remaining)
            return
        except HelperError as exc:
            if not _is_transient_rdp_probe_error(exc):
                raise
            if time.monotonic() >= deadline:
                raise HelperError("Clone RDP did not become ready before the timeout", code=RDP_NOT_READY, stage="wait-rdp") from exc
            time.sleep(min(POLL_INTERVAL_SECONDS, remaining))


def _clone_error_message(error: BaseException) -> str:
    return _error_text(error)


def _stop_clone_if_running(name: str, runner) -> None:
    try:
        result = _run_stage(
            runner,
            ["virsh", "-c", LIBVIRT_URI, "domstate", name],
            "rollback-start",
            timeout=SHUTDOWN_TIMEOUT_SECONDS,
        )
    except HelperError as exc:
        if _is_verified_domain_not_found(exc):
            return
        raise
    state = " ".join(_command_output(result).strip().lower().split())
    if state in {"running", "idle", "in shutdown", "shutting down"}:
        _run_stage(
            runner,
            ["virsh", "-c", LIBVIRT_URI, "destroy", name],
            "rollback-start",
            timeout=SHUTDOWN_TIMEOUT_SECONDS,
        )


def _undefine_clone_if_present(name: str, runner) -> None:
    if not _clone_domain_exists(name, runner):
        return
    _run_stage(
        runner,
        ["virsh", "-c", LIBVIRT_URI, "undefine", name, "--nvram"],
        "rollback-domain",
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )


def _rollback_clone_dhcp(
    name: str,
    mac: str,
    ip: str,
    runner,
    before: Mapping[str, Sequence[tuple[str, str, str]]],
) -> None:
    scopes = (("live", "--live"), ("config", "--config"))
    for state_name, scope in scopes:
        current = _capture_dhcp_states(runner, stage="rollback-dhcp")[state_name]
        if _dhcp_state_contains(current, name, mac, ip) and not _dhcp_state_contains(
            before[state_name], name, mac, ip
        ):
            _update_clone_dhcp(name, mac, ip, runner, "delete", (scope,))
    after = _capture_dhcp_states(runner, stage="rollback-dhcp")
    for state_name, _scope in scopes:
        if _dhcp_state_contains(after[state_name], name, mac, ip) and not _dhcp_state_contains(
            before[state_name], name, mac, ip
        ):
            raise HelperError(
                f"Clone DHCP reservation remains in {state_name} state",
                code="ROLLBACK_FAILED",
                stage="rollback-dhcp",
            )


def clone_workspace(
    name: str,
    assignee_type: str | None,
    assignee_name: str | None,
    template_version: str,
    *,
    memory_mib: int = 4096,
    vcpus: int = 2,
    wait_rdp_minutes: float = 20.0,
    runner=run_command,
    guacamole_preflight_runner=None,
    inventory_path: str | os.PathLike[str] | None = None,
    what_if: bool = False,
    progress_callback: ProgressCallback | None = None,
    _rollback_holder: dict[str, Any] | None = None,
) -> CloneRecord | None:
    """Create one independent libvirt clone and retain only current-run state on failure."""

    validate_vm_name(name)
    validate_vm_name(template_version)
    if assignee_type is None or assignee_name is None:
        raise ValidationError("Assignee is required", code="ASSIGNEE_INVALID")
    validate_assignee(assignee_type, assignee_name)
    if (
        isinstance(memory_mib, bool)
        or not isinstance(memory_mib, int)
        or not CLONE_MEMORY_MIN_MIB <= memory_mib <= CLONE_MEMORY_MAX_MIB
        or isinstance(vcpus, bool)
        or not isinstance(vcpus, int)
        or not CLONE_VCPUS_MIN <= vcpus <= CLONE_VCPUS_MAX
    ):
        raise ValidationError("Memory and vCPU values are invalid", code="RESOURCES_INVALID")
    try:
        wait_rdp_value = float(wait_rdp_minutes)
    except (OverflowError, TypeError, ValueError):
        wait_rdp_value = math.nan
    if (
        isinstance(wait_rdp_minutes, bool)
        or not isinstance(wait_rdp_minutes, (int, float))
        or not math.isfinite(wait_rdp_value)
        or not 0.0 <= wait_rdp_value <= CLONE_WAIT_RDP_MINUTES_MAX
    ):
        raise ValidationError("RDP wait time is invalid", code="RDP_TIMEOUT_INVALID")

    target_inventory_path = Path(INVENTORY_PATH if inventory_path is None else inventory_path)
    inventory = load_inventory(target_inventory_path)
    template = verify_template_record(_template_record(inventory, template_version), runner=runner)
    existing_items = [
        item for item in inventory.get("clones", [])
        if isinstance(item, Mapping) and item.get("name") == name
    ]
    if existing_items:
        if len(existing_items) != 1:
            raise ConflictError("Clone name has duplicate inventory records", code="DUPLICATE_CLONE")
        existing = _coerce_clone_sync_record(existing_items[0])
        if (
            existing.status == "ready"
            and existing.templateVersion == template_version
            and existing.assigneeType == assignee_type
            and existing.assigneeName == assignee_name
        ):
            if what_if:
                return None
            _verify_clone_live_state(existing, runner)
            return existing
        raise ConflictError(
            "Clone name is already present with a different completed or in-progress workspace",
            code="CLONE_CONFLICT",
            stage="validate",
        )
    if guacamole_preflight_runner is not None:
        _preflight_guacamole_clone_identity(name, guacamole_preflight_runner)
    dhcp_states = _capture_dhcp_states(runner, stage="validate-dhcp")
    if any(host[0] == name for state in dhcp_states.values() for host in state):
        raise ConflictError("DHCP reservation name is already present", code="DHCP_CONFLICT", stage="validate-dhcp")
    domain_names, domain_uuids, domain_macs = _domain_identities(runner)
    if name in domain_names:
        raise ConflictError("Clone domain is already present", code="DOMAIN_CONFLICT")
    overlay_path = VMS_DIR / f"{name}.qcow2"
    nvram_path = CLONE_NVRAM_DIR / f"{name}_VARS.fd"
    xml_path = VMS_DIR / f"{name}.xml"
    ownership_path = _workspace_ownership_path(name)
    nvram_marker_path = _workspace_nvram_marker_path(nvram_path)
    if any(path.exists() for path in (overlay_path, nvram_path, xml_path, ownership_path, nvram_marker_path)):
        raise ConflictError("Clone storage is already present", code="STORAGE_CONFLICT")

    ip = allocate_clone_ip(inventory, runner)
    mac = generate_clone_mac((*_used_clone_macs(inventory, runner), *domain_macs))
    clone_uuid = ""
    for _ in range(128):
        candidate_uuid = str(uuid.uuid4())
        if candidate_uuid.lower() not in domain_uuids:
            clone_uuid = candidate_uuid
            break
    if not clone_uuid:
        raise ConflictError("No free clone UUID is available", code="UUID_EXHAUSTED")
    tpm_marker_path = _workspace_tpm_marker_path(clone_uuid)
    if tpm_marker_path.exists():
        raise ConflictError("Clone TPM ownership metadata is already present", code="STORAGE_CONFLICT")
    assert_unique_clone(inventory, name, ip, mac)
    _progress(progress_callback, "validation", "ready")
    if what_if:
        return None

    VMS_DIR.mkdir(parents=True, exist_ok=True)
    ledger = RollbackLedger()
    previous_inventory = inventory
    inventory_existed = target_inventory_path.exists()
    started = False
    record = CloneRecord(
        name=name,
        mac=mac,
        ip=ip,
        assigneeType=assignee_type,
        assigneeName=assignee_name,
        status="pending",
        templateVersion=template_version,
        syncAttemptId=str(uuid.uuid4()),
    )
    if _rollback_holder is not None:
        _rollback_holder["rollback"] = ledger.rollback
        _rollback_holder["record"] = record
    ownership_payload = _workspace_ownership_payload(
        name=name,
        template_version=template_version,
        clone_uuid=clone_uuid,
        mac=mac,
        ip=ip,
        sync_attempt_id=record.syncAttemptId,
    )
    try:
        ledger.add("ownership-metadata", lambda: _remove_if_present(ownership_path))
        _write_workspace_marker(ownership_path, ownership_payload)
        ledger.add("overlay", lambda: _remove_if_present(overlay_path))
        _run_stage(
            runner,
            ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", template.path, str(overlay_path)],
            "create-overlay",
            timeout=CONVERSION_TIMEOUT_SECONDS,
        )
        _progress(progress_callback, "disk-overlay", "ready")
        ledger.add("nvram", lambda: _remove_if_present(nvram_path))
        _create_clone_nvram(nvram_path, runner)
        ledger.add("nvram-ownership", lambda: _remove_if_present(nvram_marker_path))
        _write_workspace_marker(nvram_marker_path, ownership_payload)
        xml = render_clone_xml(
            name=name,
            clone_uuid=clone_uuid,
            mac=mac,
            disk_path=overlay_path,
            nvram_path=nvram_path,
            memory_mib=memory_mib,
            vcpus=vcpus,
            template_version=template_version,
        )
        ledger.add("xml", lambda: _remove_if_present(xml_path))
        xml_path.write_text(xml, encoding="utf-8")

        ledger.add(
            "domain",
            lambda: _undefine_clone_if_present(name, runner),
        )
        _run_stage(runner, ["virsh", "-c", LIBVIRT_URI, "define", str(xml_path)], "define-domain", timeout=PREFLIGHT_TIMEOUT_SECONDS)
        _progress(progress_callback, "domain", "ready")

        ledger.add(
            "dhcp",
            lambda: _rollback_clone_dhcp(name, mac, ip, runner, dhcp_states),
        )
        _update_clone_dhcp(name, mac, ip, runner, "add")
        dhcp_after = _capture_dhcp_states(runner, stage="verify-dhcp")
        if not all(
            _dhcp_state_contains(dhcp_after[state], name, mac, ip)
            for state in ("live", "config")
        ):
            raise HelperError("Clone DHCP reservation was not applied to both states", code="DHCP_INVALID", stage="verify-dhcp")
        _progress(progress_callback, "dhcp", "ready")

        updated_inventory = dict(previous_inventory)
        updated_inventory["clones"] = list(previous_inventory.get("clones", [])) + [record]
        ledger.add(
            "inventory",
            lambda: _restore_inventory(previous_inventory, inventory_existed, target_inventory_path),
        )
        save_inventory_atomic(updated_inventory, target_inventory_path)

        ledger.add(
            "running-domain",
            lambda: _stop_clone_if_running(name, runner),
        )
        ledger.add("tpm-ownership", lambda: _remove_if_present(tpm_marker_path))
        _run_stage(runner, ["virsh", "-c", LIBVIRT_URI, "start", name], "start-domain", timeout=SHUTDOWN_TIMEOUT_SECONDS)
        started = True
        if (tpm_marker_path.parent / "tpm2").is_dir():
            _write_workspace_marker(tpm_marker_path, ownership_payload)
        lease_timeout = max(0.0, float(wait_rdp_minutes) * 60.0)
        wait_for_clone_lease(mac, ip, runner, lease_timeout)
        wait_for_clone_rdp(ip, runner, lease_timeout)
        _progress(progress_callback, "rdp", "ready")
        return record
    except HelperError as exc:
        if exc.code == RDP_NOT_READY and started:
            _progress(progress_callback, "rdp", "waiting")
            record.status = "waiting-rdp"
            waiting_inventory = dict(previous_inventory)
            waiting_inventory["clones"] = list(previous_inventory.get("clones", [])) + [record]
            try:
                save_inventory_atomic(waiting_inventory, target_inventory_path)
            except BaseException as save_error:
                primary_error = save_error
            else:
                return record
        else:
            primary_error = exc
    except BaseException as exc:
        primary_error = exc

    rollback_errors = ledger.rollback()
    if rollback_errors:
        details = "; ".join(_clone_error_message(error) for error in rollback_errors)
        raise HelperError(
            f"Clone rollback failed: {details}; primary={_clone_error_message(primary_error)}",
            code="ROLLBACK_FAILED",
            stage="rollback",
        ) from rollback_errors[0]
    if isinstance(primary_error, HelperError):
        raise primary_error
    raise HelperError("Clone operation failed", code="COMMAND_FAILED", stage="clone") from primary_error


# Stable names for callers that use the task's create/allocate/render vocabulary.
create_clone = clone_workspace
allocate_clone_address = allocate_clone_ip
render_clone_domain_xml = render_clone_xml


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workspace-helper.py")
    commands = parser.add_subparsers(dest="command", required=True)

    list_command = commands.add_parser("list", help="read-only inventory and domain discovery")
    list_command.add_argument("--json", action="store_true", help="emit JSON (the default output)")

    create_command = commands.add_parser("create-template", help="create a golden template")
    create_command.add_argument("--source", required=True)
    create_command.add_argument("--version", required=True)
    create_command.add_argument("--what-if", action="store_true", help="run read-only preflight gates")

    clone_command = commands.add_parser("clone", help="create a workspace clone")
    clone_command.add_argument("--name", required=True)
    assignee_group = clone_command.add_mutually_exclusive_group(required=True)
    assignee_group.add_argument("--assign-user")
    assignee_group.add_argument("--assign-group")
    clone_command.add_argument("--template", required=True)
    clone_command.add_argument("--memory-mib", type=int, default=4096)
    clone_command.add_argument("--vcpus", type=int, default=2)
    clone_command.add_argument("--wait-rdp-minutes", type=float, default=20.0)
    clone_command.add_argument("--json", action="store_true", help="emit JSON (the default output)")
    clone_command.add_argument("--what-if", action="store_true", help="run read-only clone preflight")
    clone_command.add_argument("--progress", action="store_true", help="emit JSON progress events before the final result")
    clone_command.add_argument("--job-id", help=argparse.SUPPRESS)

    start_command = commands.add_parser("start", help="queue a detached workspace clone")
    start_command.add_argument("--name", required=True)
    start_assignee = start_command.add_mutually_exclusive_group(required=True)
    start_assignee.add_argument("--assign-user")
    start_assignee.add_argument("--assign-group")
    start_command.add_argument("--template", required=True)
    start_command.add_argument("--memory-mib", type=int, default=4096)
    start_command.add_argument("--vcpus", type=int, default=2)
    start_command.add_argument("--wait-rdp-minutes", type=float, default=20.0)
    start_command.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    repair_command = commands.add_parser("repair", help="queue an idempotent workspace sync repair")
    repair_command.add_argument("--name", required=True)
    repair_command.add_argument("--job-id", help=argparse.SUPPRESS)
    repair_command.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    repair_command.add_argument("--progress", action="store_true", help=argparse.SUPPRESS)

    sync_command = commands.add_parser("sync", help="synchronize Guacamole records")
    sync_command.add_argument("--all", action="store_true")
    sync_command.add_argument("--what-if", action="store_true", help="print intended changes without writing PostgreSQL")

    credential_command = commands.add_parser(
        "set-windows-credential",
        help="initialize or replace the Windows credential from stdin",
    )
    credential_command.add_argument(
        "--stdin",
        action="store_true",
        required=True,
        help="read exactly one password value from stdin without echoing it",
    )

    adoption_command = commands.add_parser(
        "adopt-guacamole-connection",
        help="explicitly adopt the audited unmarked wtest connection after read-only proof",
    )
    adoption_command.add_argument("--name", required=True)
    adoption_command.add_argument("--connection-id", required=True, type=int)
    adoption_command.add_argument("--json", action="store_true", help="emit JSON (the default output)")

    delete_command = commands.add_parser("check-template-delete", help="check whether a template may be deleted")
    delete_command.add_argument("--version", required=True)
    return parser


def _not_implemented(command: str) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "NOT_IMPLEMENTED",
        "stage": "command",
        "message": f"{command} is reserved for a later task",
    }


def _make_progress_emitter(job_name: str | None = None, job_id: str | None = None) -> ProgressCallback:
    states = {stage: "pending" for stage in CLONE_PROGRESS_STAGES}

    def emit(stage: str, status: str) -> None:
        if stage not in states:
            return
        states[stage] = status
        if job_name is not None:
            overall = "running"
            if stage == "rdp" and status == "waiting":
                overall = "waiting-rdp"
            try:
                write_job_status(
                    job_name,
                    {
                        "jobId": job_id,
                        "status": overall,
                        "stage": stage,
                        "progress": [
                            {"stage": name, "status": states[name]}
                            for name in CLONE_PROGRESS_STAGES
                        ],
                    },
                )
            except HelperError:
                # The VM transaction remains guarded by its normal rollback
                # path. A ledger write failure must not expose command output.
                pass
        print(
            json.dumps(
                {
                    "ok": True,
                    "type": "progress",
                    "stage": stage,
                    "status": status,
                    "progress": [
                        {"stage": name, "status": states[name]}
                        for name in CLONE_PROGRESS_STAGES
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )

    emit.progress = lambda: [
        {"stage": name, "status": states[name]}
        for name in CLONE_PROGRESS_STAGES
    ]
    return emit


def _run_clone_job(arguments: argparse.Namespace, job_id: str) -> dict[str, Any]:
    progress_callback = _make_progress_emitter(arguments.name, job_id)
    progress_callback("validation", "running")
    try:
        with workspace_lock():
            payload = clone_workspace_and_sync(
                arguments.name,
                "USER" if arguments.assign_user is not None else "USER_GROUP",
                arguments.assign_user if arguments.assign_user is not None else arguments.assign_group,
                arguments.template,
                memory_mib=arguments.memory_mib,
                vcpus=arguments.vcpus,
                wait_rdp_minutes=arguments.wait_rdp_minutes,
                progress_callback=progress_callback,
            )
    except HelperError as exc:
        try:
            inventory_state = load_inventory()
            has_inventory = _workspace_inventory_record(arguments.name, inventory_state) is not None
        except HelperError:
            has_inventory = False
        failure_status = "waiting-rdp" if exc.code == RDP_NOT_READY else "sync-failed" if exc.stage in {"sync", "permissions", "rollback"} else "failed"
        if not has_inventory and failure_status == "failed" and not _prove_no_clone_artifacts(arguments.name):
            failure_status = "failed-without-inventory"
        write_job_status(
            arguments.name,
            {
                "jobId": job_id,
                "status": failure_status,
                "stage": exc.stage,
                "error": {"code": exc.code, "stage": exc.stage},
                "progress": progress_callback.progress(),
            },
        )
        raise
    except Exception as exc:
        del exc
        try:
            inventory_state = load_inventory()
            has_inventory = _workspace_inventory_record(arguments.name, inventory_state) is not None
        except HelperError:
            has_inventory = False
        failure_status = "failed"
        failure_code = "COMMAND_FAILED"
        if not has_inventory and not _prove_no_clone_artifacts(arguments.name):
            failure_status = "failed-without-inventory"
            failure_code = "ARTIFACTS_UNVERIFIED"
        write_job_status(
            arguments.name,
            {
                "jobId": job_id,
                "status": failure_status,
                "stage": "command",
                "error": {"code": failure_code, "stage": "command"},
                "progress": progress_callback.progress(),
            },
        )
        raise HelperError("Workspace job failed", code="COMMAND_FAILED", stage="command") from None
    clone = payload.get("clone") if isinstance(payload, Mapping) else None
    status = payload.get("status") if isinstance(payload, Mapping) else "failed"
    write_job_status(
        arguments.name,
        {
            "jobId": job_id,
            "status": "waiting-rdp" if status == "waiting-rdp" else "ready" if status == "ready" else "failed",
            "stage": payload.get("stage", "command") if isinstance(payload, Mapping) else "command",
            "progress": payload.get("progress", progress_callback.progress()) if isinstance(payload, Mapping) else progress_callback.progress(),
            "result": clone,
        },
    )
    return payload


def _inventory_with_clone_record(
    inventory: Mapping[str, Any],
    name: str,
    replacement: Mapping[str, Any],
) -> dict[str, Any]:
    """Return an inventory with exactly one named clone replaced."""

    clones = inventory.get("clones")
    if not isinstance(clones, list):
        raise HelperError("Workspace inventory is invalid", code="INVENTORY_INVALID", stage="inventory")
    updated_clones = list(clones)
    matches = [index for index, item in enumerate(updated_clones) if isinstance(item, Mapping) and item.get("name") == name]
    if len(matches) != 1:
        raise HelperError("Workspace inventory record is not unique", code="INVENTORY_INVALID", stage="inventory")
    updated_clones[matches[0]] = dict(replacement)
    updated = dict(inventory)
    updated["clones"] = updated_clones
    return updated


def _restore_repair_inventory_record(
    name: str,
    previous_record: Mapping[str, Any],
    desired_record: Mapping[str, Any],
) -> None:
    """Restore a prepared repair record while the caller still owns the lock."""

    current_inventory = load_inventory()
    current_record = _workspace_inventory_record(name, current_inventory)
    if isinstance(current_record, Mapping) and dict(current_record) == dict(previous_record):
        return
    if not isinstance(current_record, Mapping) or dict(current_record) != dict(desired_record):
        raise HelperError("Workspace inventory transition is ambiguous", code="INVENTORY_INVALID", stage="inventory")
    save_inventory_atomic(_inventory_with_clone_record(current_inventory, name, previous_record))


def _recover_repair_inventory_transition(
    name: str,
    inventory: Mapping[str, Any],
    job_id: str,
    sync_runner,
) -> Mapping[str, Any]:
    """Resolve a prepared repair before trusting a ready inventory record."""

    status = read_job_status(name) or {}
    transition = status.get("inventoryTransition")
    if not isinstance(transition, Mapping) or transition.get("phase") != "prepared":
        return inventory
    previous_record = transition.get("previous")
    desired_record = transition.get("desired")
    if not isinstance(previous_record, Mapping) or not isinstance(desired_record, Mapping):
        raise HelperError("Workspace inventory transition is invalid", code="INVENTORY_INVALID", stage="inventory")
    current_record = _workspace_inventory_record(name, inventory)
    if isinstance(current_record, Mapping) and dict(current_record) == dict(previous_record):
        write_job_status(
            name,
            {
                "jobId": job_id,
                "status": status.get("status", "sync-failed"),
                "stage": status.get("stage", "sync"),
                "inventoryTransition": {"phase": "restored", "previous": previous_record, "desired": desired_record},
            },
        )
        return inventory
    if not isinstance(current_record, Mapping) or dict(current_record) != dict(desired_record):
        raise HelperError("Workspace inventory transition is ambiguous", code="INVENTORY_INVALID", stage="inventory")
    try:
        record_object = _coerce_clone_sync_record(desired_record)
        connection_id = desired_record.get("connectionId")
        if not isinstance(connection_id, int) or isinstance(connection_id, bool) or connection_id <= 0:
            raise ValueError("repair transition has no connection identity")
        committed = _probe_guacamole_sync_state(
            record_object,
            sync_runner,
            timeout=GUACAMOLE_SYNC_TIMEOUT_SECONDS,
            connection_id=connection_id,
            include_windows_credentials=True,
        )
    except Exception:
        committed = None
    if committed is not None:
        write_job_status(
            name,
            {
                "jobId": job_id,
                "status": "ready",
                "stage": "permissions",
                "result": desired_record,
                "inventoryTransition": {"phase": "committed", "previous": previous_record, "desired": desired_record},
            },
        )
        return inventory
    _restore_repair_inventory_record(name, previous_record, desired_record)
    write_job_status(
        name,
        {
            "jobId": job_id,
            "status": "sync-failed",
            "stage": "sync",
            "error": {"code": "SYNC_COMMIT_UNKNOWN", "stage": "sync"},
            "inventoryTransition": {"phase": "restored", "previous": previous_record, "desired": desired_record},
        },
    )
    raise HelperError("Prepared Guacamole repair did not commit", code="SYNC_COMMIT_UNKNOWN", stage="sync")


def _run_repair_job(
    arguments: argparse.Namespace,
    job_id: str,
    runner=run_command,
    sync_runner=run_psql_sync,
) -> dict[str, Any]:
    try:
        with workspace_lock():
            inventory = load_inventory()
            inventory = _recover_repair_inventory_transition(
                arguments.name,
                inventory,
                job_id,
                sync_runner,
            )
            record = _workspace_inventory_record(arguments.name, inventory)
            if record is None:
                raise ValidationError("Workspace record is missing", code="WORKSPACE_NOT_FOUND")
            if record.get("status") == "ready":
                payload = {"ok": True, "status": "ready", "stage": "permissions", "clone": dict(record), "progress": clone_progress("ready")}
            else:
                record_object = _coerce_clone_sync_record(record)
                _verify_repair_live_state(record_object, runner=runner)
                current = read_job_status(arguments.name) or {}
                connection_id = record.get("connectionId")
                if not isinstance(connection_id, int) or isinstance(connection_id, bool) or connection_id <= 0:
                    result = current.get("result") if isinstance(current.get("result"), Mapping) else {}
                    connection_id = result.get("connectionId") if isinstance(result, Mapping) else None
                owned = None
                if isinstance(connection_id, int) and not isinstance(connection_id, bool) and connection_id > 0:
                    owned = _probe_guacamole_sync_state(record_object, sync_runner, timeout=GUACAMOLE_SYNC_TIMEOUT_SECONDS, connection_id=connection_id)
                if owned is None:
                    owned = _probe_guacamole_sync_state(record_object, sync_runner, timeout=GUACAMOLE_SYNC_TIMEOUT_SECONDS)
                write_job_status(
                    arguments.name,
                    {
                        "jobId": job_id,
                        "status": "running",
                        "stage": "sync",
                        "result": record,
                    },
                )
                # The repair verifier is deliberately repeated immediately
                # before the first Guacamole write.  This closes the window
                # in which the immutable template or any backing identity
                # could be replaced after the initial preflight.
                _verify_repair_live_state(record_object, runner=runner)
                rollback_holder: dict[str, Any] = {}
                if owned is not None:
                    owned_connection_id = owned.get("connectionId") if isinstance(owned, Mapping) else None
                    if not isinstance(owned_connection_id, int) or isinstance(owned_connection_id, bool) or owned_connection_id <= 0:
                        raise HelperError("Guacamole connection identity is invalid", code="SYNC_INVALID", stage="sync")
                    previous_record = dict(record)
                    desired_record = dict(record)
                    desired_record["status"] = "ready"
                    desired_record["connectionId"] = owned_connection_id
                    transition = {
                        "phase": "prepared",
                        "previous": previous_record,
                        "desired": desired_record,
                    }
                    # The ledger is durable before the ready state is exposed.
                    # A restart can therefore distinguish a prepared inventory
                    # from a DB commit that lost its runner response.
                    write_job_status(
                        arguments.name,
                        {
                            "jobId": job_id,
                            "status": "running",
                            "stage": "sync",
                            "result": record,
                            "inventoryTransition": transition,
                        },
                    )
                    try:
                        save_inventory_atomic(_inventory_with_clone_record(inventory, arguments.name, desired_record))
                    except BaseException:
                        _restore_repair_inventory_record(arguments.name, previous_record, desired_record)
                        write_job_status(
                            arguments.name,
                            {
                                "jobId": job_id,
                                "status": "sync-failed",
                                "stage": "inventory",
                                "error": {"code": "INVENTORY_INVALID", "stage": "inventory"},
                                "inventoryTransition": {"phase": "restored", "previous": previous_record, "desired": desired_record},
                            },
                        )
                        raise
                    try:
                        sync_payload = sync_guacamole(
                            record_object,
                            verify_live=False,
                            require_owned_connection=True,
                            connection_id=owned_connection_id,
                            compensation_holder=rollback_holder,
                            runner=sync_runner,
                            include_windows_credentials=True,
                        )
                        synced = sync_payload.get("clone") if isinstance(sync_payload, Mapping) else None
                        if not isinstance(synced, Mapping) or synced.get("status") != "ready":
                            raise HelperError(
                                "Guacamole synchronization did not produce a ready workspace",
                                code="SYNC_INVALID",
                                stage="sync",
                            )
                    except BaseException as sync_error:
                        # A runner can fail after PostgreSQL committed.  Probe
                        # only this existing owned row; once proven committed,
                        # the ready inventory above is left untouched forever.
                        committed = _probe_guacamole_sync_state(
                            record_object,
                            sync_runner,
                            timeout=GUACAMOLE_SYNC_TIMEOUT_SECONDS,
                            connection_id=owned_connection_id,
                            include_windows_credentials=True,
                        )
                        if committed is None:
                            _restore_repair_inventory_record(arguments.name, previous_record, desired_record)
                            write_job_status(
                                arguments.name,
                                {
                                    "jobId": job_id,
                                    "status": "sync-failed",
                                    "stage": "sync",
                                    "error": {"code": "SYNC_COMMIT_UNKNOWN", "stage": "sync"},
                                    "inventoryTransition": {"phase": "restored", "previous": previous_record, "desired": desired_record},
                                },
                            )
                            raise sync_error
                        synced = dict(committed)
                        synced["status"] = "ready"
                    # Mark the durable transition before any final response;
                    # no post-COMMIT path mutates or revalidates inventory.
                    transition["phase"] = "committed"
                    write_job_status(
                        arguments.name,
                        {
                            "jobId": job_id,
                            "status": "running",
                            "stage": "sync",
                            "result": desired_record,
                            "inventoryTransition": transition,
                        },
                    )
                    clone = dict(desired_record)
                    clone.update(synced)
                    payload = {"ok": True, "status": "ready", "stage": "permissions", "clone": clone, "progress": clone_progress("ready")}
                else:
                    sync_payload = sync_guacamole(
                        record_object,
                        verify_live=False,
                        require_new_connection=True,
                        compensation_holder=rollback_holder,
                        runner=sync_runner,
                        include_windows_credentials=True,
                    )
                    synced = sync_payload.get("clone")
                    if not isinstance(synced, Mapping) or synced.get("status") != "ready":
                        raise HelperError("Guacamole synchronization did not produce a ready workspace", code="SYNC_INVALID", stage="sync")
                    updated_inventory = load_inventory()
                    current_record = _workspace_inventory_record(arguments.name, updated_inventory)
                    if not isinstance(current_record, Mapping) or any(
                        current_record.get(key) != expected
                        for key, expected in (
                            ("name", record_object.name),
                            ("mac", record_object.mac),
                            ("ip", record_object.ip),
                            ("assigneeType", record_object.assigneeType),
                            ("assigneeName", record_object.assigneeName),
                            ("templateVersion", record_object.templateVersion),
                            ("syncAttemptId", record_object.syncAttemptId),
                        )
                    ):
                        inventory_error = HelperError(
                            "Workspace inventory changed after Guacamole synchronization",
                            code="INVENTORY_INVALID",
                            stage="inventory",
                        )
                        _raise_after_guacamole_compensation(inventory_error, rollback_holder.get("rollback"))
                    saved_record: dict[str, Any] | None = None
                    for index, item in enumerate(updated_inventory.get("clones", [])):
                        if isinstance(item, Mapping) and item.get("name") == arguments.name:
                            updated = dict(item)
                            updated["status"] = "ready"
                            if isinstance(synced.get("connectionId"), int):
                                updated["connectionId"] = synced["connectionId"]
                            updated_inventory["clones"][index] = updated
                            try:
                                save_inventory_atomic(updated_inventory)
                            except BaseException as exc:
                                inventory_error = HelperError(
                                    "Workspace inventory could not be committed after Guacamole synchronization",
                                    code="INVENTORY_INVALID",
                                    stage="inventory",
                                )
                                _raise_after_guacamole_compensation(inventory_error, rollback_holder.get("rollback"))
                                raise inventory_error from exc
                            saved_record = updated
                            break
                    if saved_record is None:
                        inventory_error = HelperError(
                            "Workspace inventory record disappeared after Guacamole synchronization",
                            code="INVENTORY_INVALID",
                            stage="inventory",
                        )
                        _raise_after_guacamole_compensation(inventory_error, rollback_holder.get("rollback"))
                    clone = dict(saved_record)
                    clone.update(synced)
                    payload = {"ok": True, "status": "ready", "stage": "permissions", "clone": clone, "progress": clone_progress("ready")}
    except HelperError as exc:
        try:
            inventory_state = load_inventory()
            inventory_present = _workspace_inventory_record(arguments.name, inventory_state) is not None
        except HelperError:
            inventory_present = False
        failure_status = "waiting-rdp" if exc.code == RDP_NOT_READY else "sync-failed"
        failure_code = exc.code
        if not inventory_present:
            if _prove_no_clone_artifacts(arguments.name):
                # A missing inventory record is restartable only after the
                # same conservative no-artifact proof used by start.
                failure_status = "failed"
            else:
                failure_status = "failed-without-inventory"
                failure_code = "WORKSPACE_NOT_FOUND" if exc.code == "WORKSPACE_NOT_FOUND" else "ARTIFACTS_UNVERIFIED"
        write_job_status(
            arguments.name,
            {
                "jobId": job_id,
                "status": failure_status,
                "stage": exc.stage,
                "error": {"code": failure_code, "stage": exc.stage},
            },
        )
        raise
    except Exception as exc:
        del exc
        try:
            inventory_state = load_inventory()
            inventory_present = _workspace_inventory_record(arguments.name, inventory_state) is not None
        except HelperError:
            inventory_present = False
        failure_status = "sync-failed"
        failure_code = "COMMAND_FAILED"
        if not inventory_present:
            if _prove_no_clone_artifacts(arguments.name):
                failure_status = "failed"
            else:
                failure_status = "failed-without-inventory"
                failure_code = "ARTIFACTS_UNVERIFIED"
        write_job_status(
            arguments.name,
            {
                "jobId": job_id,
                "status": failure_status,
                "stage": "command",
                "error": {"code": failure_code, "stage": "command"},
            },
        )
        raise HelperError("Workspace repair failed", code="COMMAND_FAILED", stage="command") from None
    write_job_status(
        arguments.name,
        {
            "jobId": job_id,
            "status": "ready",
            "stage": "permissions",
            "progress": payload.get("progress"),
            "result": payload.get("clone"),
        },
    )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command in {"create-template", "clone", "sync", "start", "repair", "set-windows-credential", "adopt-guacamole-connection"}:
            require_root(arguments.command)
        if arguments.command == "list":
            payload = build_list_payload()
        else:
            if arguments.command == "set-windows-credential":
                write_windows_credential_secret_from_stdin()
                payload = {
                    "ok": True,
                    "status": "initialized",
                }
            elif arguments.command == "adopt-guacamole-connection":
                with workspace_lock():
                    payload = adopt_guacamole_connection(arguments.name, arguments.connection_id)
            elif arguments.command == "create-template":
                validate_vm_name(arguments.source)
                validate_vm_name(arguments.version)
                if arguments.what_if:
                    create_template(arguments.source, arguments.version, what_if=True)
                    payload = {
                        "ok": True,
                        "whatIf": True,
                        "source": arguments.source,
                        "version": arguments.version,
                    }
                else:
                    with workspace_lock():
                        record = create_template(arguments.source, arguments.version)
                    payload = {"ok": True, "template": record.to_dict()}
            elif arguments.command == "clone":
                assignee_type = "USER" if arguments.assign_user is not None else "USER_GROUP"
                assignee_name = arguments.assign_user if arguments.assign_user is not None else arguments.assign_group
                if arguments.job_id:
                    payload = _run_clone_job(arguments, arguments.job_id)
                    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                    return 0 if payload.get("ok") else 2
                progress_callback = _make_progress_emitter() if arguments.progress else None
                if progress_callback is not None:
                    progress_callback("validation", "running")
                if arguments.what_if:
                    payload = clone_workspace_and_sync(
                        arguments.name,
                        assignee_type,
                        assignee_name,
                        arguments.template,
                        memory_mib=arguments.memory_mib,
                        vcpus=arguments.vcpus,
                        wait_rdp_minutes=arguments.wait_rdp_minutes,
                        what_if=True,
                        progress_callback=progress_callback,
                    )
                else:
                    with workspace_lock():
                        payload = clone_workspace_and_sync(
                            arguments.name,
                            assignee_type,
                            assignee_name,
                            arguments.template,
                            memory_mib=arguments.memory_mib,
                            vcpus=arguments.vcpus,
                            wait_rdp_minutes=arguments.wait_rdp_minutes,
                            progress_callback=progress_callback,
                        )
            elif arguments.command == "start":
                assignee_type = "USER" if arguments.assign_user is not None else "USER_GROUP"
                assignee_name = arguments.assign_user if arguments.assign_user is not None else arguments.assign_group
                with workspace_lock():
                    payload = start_workspace_job(
                        arguments.name,
                        assignee_type,
                        assignee_name,
                        arguments.template,
                        memory_mib=arguments.memory_mib,
                        vcpus=arguments.vcpus,
                        wait_rdp_minutes=arguments.wait_rdp_minutes,
                    )
            elif arguments.command == "repair":
                if arguments.job_id:
                    payload = _run_repair_job(arguments, arguments.job_id)
                    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                    return 0 if payload.get("ok") else 2
                with workspace_lock():
                    payload = repair_workspace_job(arguments.name)
            elif arguments.command == "check-template-delete":
                validate_vm_name(arguments.version)
                payload = check_template_delete(arguments.version)
            elif arguments.command == "sync":
                if not arguments.all:
                    raise ValidationError("sync requires --all", code="SYNC_INVALID")
                if arguments.what_if:
                    payload = sync_guacamole(all_clones=True, what_if=True, verify_live=True)
                else:
                    with workspace_lock():
                        payload = sync_guacamole(
                            all_clones=True,
                            verify_live=True,
                            include_windows_credentials=True,
                        )
            elif arguments.command != "create-template":
                payload = _not_implemented(arguments.command)
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0 if payload.get("ok") else 2
    except HelperError as exc:
        error_payload = {"ok": False, "code": exc.code, "stage": exc.stage, "message": _safe_error_message(exc)}
        progress_callback = locals().get("progress_callback")
        progress_state = getattr(progress_callback, "progress", None)
        if callable(progress_state):
            error_payload["progress"] = progress_state()
        print(
            json.dumps(
                error_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 2
    except (OSError, ValueError, TypeError) as exc:
        print(
            json.dumps(
                {"ok": False, "code": "COMMAND_FAILED", "stage": "command", "message": "command failed; repair may be required"},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
