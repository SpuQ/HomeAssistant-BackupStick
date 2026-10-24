"""
The USB stick: finding it, and mounting it while it is in use.

No Home Assistant imports. Everything here blocks, so Home Assistant calls it
from its executor.

The stick is mounted only for the moment it is read or written, then flushed
and unmounted. A power cut, a crash or someone pulling the stick out between
backups finds it with nothing half-written on it.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import threading

SUPPORTED_FILESYSTEMS = ("vfat", "exfat", "ext4")

# The folder on the stick that holds the backups. Everything else on the stick
# is left alone.
BACKUP_FOLDER = "HomeAssistant-backups"

BY_UUID = Path("/dev/disk/by-uuid")
BY_LABEL = Path("/dev/disk/by-label")
SYS_BLOCK = Path("/sys/class/block")
MOUNT_ROOT = Path("/run/backup_stick")

# Partition labels of Home Assistant OS itself. A disk carrying any of them is
# the system disk (an SSD on USB, for instance) and is never offered.
SYSTEM_LABEL_PREFIX = "hassos-"


class StickUnavailable(Exception):
    """The stick is not plugged in, or it could not be mounted or unmounted."""


@dataclass(frozen=True)
class StickPartition:
    """A partition on a USB stick that can hold backups."""

    uuid: str
    device: str
    label: str | None
    fs_type: str
    size: int  # bytes

    @property
    def title(self) -> str:
        return self.label or self.uuid


"""
    Finding sticks
"""


def find_sticks() -> list[StickPartition]:
    """Partitions on USB disks with a supported filesystem, system disk excluded."""
    system_disks = {_disk_of(link.resolve().name) for link in BY_LABEL.glob(f"{SYSTEM_LABEL_PREFIX}*")}
    sticks = []
    for link in sorted(BY_UUID.glob("*")):
        device = link.resolve()
        disk = _disk_of(device.name)
        if disk is None or disk in system_disks or not _is_usb(disk):
            continue
        fs_type, label = probe(device)
        if fs_type not in SUPPORTED_FILESYSTEMS:
            continue
        sticks.append(StickPartition(link.name, str(device), label, fs_type, _size(device.name)))
    return sticks


def probe(device: Path) -> tuple[str | None, str | None]:
    """A partition's filesystem type and label, from blkid."""
    result = subprocess.run(["blkid", str(device)], capture_output=True, text=True, timeout=10, check=False)
    fields = dict(re.findall(r'(\w+)="([^"]*)"', result.stdout))
    return fields.get("TYPE"), fields.get("LABEL")


def _disk_of(name: str) -> str | None:
    """The whole disk a block device belongs to: 'sda' for 'sda1'.

    A stick formatted without a partition table is its own disk.
    """
    node = SYS_BLOCK / name
    if not node.exists():
        return None
    if (node / "partition").exists():
        return node.resolve().parent.name
    return name


def _is_usb(disk: str) -> bool:
    return "/usb" in str((SYS_BLOCK / disk).resolve())


def _size(name: str) -> int:
    try:
        return int((SYS_BLOCK / name / "size").read_text()) * 512
    except (OSError, ValueError):
        return 0


"""
    Using a stick
"""


class BackupStick:
    """One stick, identified by its filesystem UUID, mounted only while in use."""

    def __init__(self, uuid: str, fs_type: str, mount_root: Path = MOUNT_ROOT):
        self.uuid = uuid
        self.fs_type = fs_type
        self._mountpoint = mount_root / uuid
        # Several operations can overlap, a download and a listing for example.
        # The first one mounts, the last one to finish unmounts.
        self._lock = threading.Lock()
        self._users = 0

    @property
    def device(self) -> Path:
        return BY_UUID / self.uuid

    def is_present(self) -> bool:
        return self.device.exists()

    @contextlib.contextmanager
    def mounted(self) -> Iterator[Path]:
        """The backup folder on the stick, for as long as the block runs."""
        self._acquire()
        try:
            folder = self._mountpoint / BACKUP_FOLDER
            folder.mkdir(exist_ok=True)
            yield folder
        finally:
            self._release()

    def _acquire(self) -> None:
        with self._lock:
            if self._users == 0:
                if not self.is_present():
                    raise StickUnavailable(f"USB stick {self.uuid} is not plugged in")
                self._mountpoint.mkdir(parents=True, exist_ok=True)
                # Left over from a run that ended mid-operation: start clean.
                if os.path.ismount(self._mountpoint):
                    _run("umount", str(self._mountpoint))
                _run("mount", "-t", self.fs_type, "-o", "noatime", str(self.device), str(self._mountpoint))
            self._users += 1

    def _release(self) -> None:
        with self._lock:
            self._users -= 1
            if self._users == 0:
                _run("sync")
                _run("umount", str(self._mountpoint))


def _run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise StickUnavailable(f"{command[0]} failed: {result.stderr.strip() or result.returncode}")
