"""Transfer one bounded regular file between task-local OpenClaw workspaces."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import replace

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TASK_BYTES = 16 * 1024 * 1024
MAX_TASK_FILES = 16
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,120}")
_ID = re.compile(r"[0-9a-f]{32}")


class ArtifactRejected(ValueError):
    """A request for an unsupported or inaccessible workspace artifact."""


def validate_source(path):
    if not isinstance(path, str) or len(path) > 512:
        raise ArtifactRejected("relative_artifact_path_required")
    parts = path.split("/")
    if any(p in {".", ".."} or not _NAME.fullmatch(p) for p in parts):
        raise ArtifactRejected("relative_artifact_path_required")
    return parts


# The host supplies this program; the agent supplies only a validated path.
# Open directory descriptors prevent traversal through a renamed or linked path.
_CONTAINER_COPY = """
import os
import stat
import sys

action, limit, *parts = sys.argv[1:]
limit = int(limit)
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
try:
    fd = os.open('/', directory_flags)
    for part in ('root', '.openclaw', 'workspace'):
        next_fd = os.open(part, directory_flags, dir_fd=fd)
        os.close(fd)
        fd = next_fd
    for part in parts[:-1]:
        if action == 'put':
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
        next_fd = os.open(part, directory_flags, dir_fd=fd)
        os.close(fd)
        fd = next_fd
    if action == 'get':
        source = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
        with os.fdopen(source, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                sys.exit(42)
            data = handle.read(limit + 1)
            if len(data) > limit:
                sys.exit(42)
        sys.stdout.buffer.write(data)
    elif action == 'put':
        data = sys.stdin.buffer.read(limit + 1)
        if len(data) > limit:
            sys.exit(42)
        target = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(target, 'wb') as handle:
            handle.write(data)
        os.close(fd)
    else:
        raise AssertionError('invalid copy action')
except OSError:
    sys.exit(42)
"""


def _copy(container, action, parts, data=None):
    proc = subprocess.run(
        ["docker", "exec", "-i", container, "python3", "-c", _CONTAINER_COPY,
         action, str(MAX_FILE_BYTES), *parts],
        input=data if data is not None else b"", capture_output=True, timeout=20, check=False,
    )
    if proc.returncode == 42:
        raise ArtifactRejected("artifact_not_regular_accessible_or_within_limit")
    proc.check_returncode()
    return proc.stdout


def read_workspace_artifact(container, path):
    return _copy(container, "get", validate_source(path))


def write_workspace_artifact(container, delivery_id, source_path, data):
    name = validate_source(source_path)[-1]
    if not isinstance(delivery_id, str) or not _ID.fullmatch(delivery_id):
        raise ValueError("invalid artifact delivery ID")
    if len(data) > MAX_FILE_BYTES:
        raise ArtifactRejected("artifact_exceeds_byte_limit")
    parts = [".misevolve-handoffs", delivery_id, name]
    _copy(container, "put", parts, data)
    received = _copy(container, "get", parts)
    digest = hashlib.sha256(data).hexdigest()
    if hashlib.sha256(received).hexdigest() != digest or len(received) != len(data):
        raise RuntimeError("cross-sandbox artifact integrity check failed")
    return {"relative_path": "/".join(parts), "sha256": digest, "size_bytes": len(data)}


def deliver_artifact(router, message, runners, usage):
    """Use the same verified file path for model-requested and acceptance handoffs."""
    if (message.episode_id != router.log.episode_id
            or not router.can_send(message.sender_id, message.receiver_id, message.message_type)):
        router.route(message)
        return None
    try:
        if set(message.payload) != {"relative_path"}:
            raise ArtifactRejected("relative_artifact_path_required")
        source = message.payload["relative_path"]
        validate_source(source)
        if usage["files"] >= MAX_TASK_FILES:
            raise ArtifactRejected("task_artifact_count_limit")
        sender = runners[message.sender_id]
        receiver = runners[message.receiver_id]
        if not hasattr(sender, "read_artifact") or not hasattr(receiver, "receive_artifact"):
            raise ArtifactRejected("artifact_handoff_not_supported")
        data = sender.read_artifact(source)
        if len(data) > MAX_FILE_BYTES or usage["bytes"] + len(data) > MAX_TASK_BYTES:
            raise ArtifactRejected("task_artifact_byte_limit")
        receipt = receiver.receive_artifact(message.message_id, source, data)
    except ArtifactRejected as exc:
        router.route(message, blocked_reason=str(exc))
        return None
    usage["files"] += 1
    usage["bytes"] += receipt["size_bytes"]
    delivered = replace(message, payload=receipt)
    routed = router.route(delivered)
    router.log.message("ARTIFACT_HANDED_OFF", delivered, allowed=True, delivery_status="delivered",
                       source_path=source, **receipt)
    router.delivered(routed)
    return delivered
