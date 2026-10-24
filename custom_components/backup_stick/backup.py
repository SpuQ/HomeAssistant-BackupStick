"""
The USB stick as a backup location.

Backups are stored on the stick as ordinary Home Assistant backup files, named
the way Home Assistant names them, so the stick can be read on any computer and
a backup restored on any Home Assistant by uploading the file. Next to each one
is a small metadata file, which is what this agent lists from.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
import contextlib
import errno
from functools import wraps
import json
import logging
import os
from pathlib import Path
from time import monotonic
from typing import IO, Any, Concatenate, override

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
    OnProgressCallback,
    suggested_filename,
)
from homeassistant.core import HomeAssistant, callback

from . import BackupStickConfigEntry
from .const import DATA_BACKUP_AGENT_LISTENERS, DOMAIN
from .stick import BackupStick, StickUnavailable

_LOGGER = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
METADATA_SUFFIX = ".metadata.json"
# The stick only changes through this agent, unless someone copies files onto
# it by hand; a short-lived cache saves mounting it for every listing.
CACHE_TTL = 300


async def async_get_backup_agents(hass: HomeAssistant, **kwargs: Any) -> list[BackupAgent]:
    entries: list[BackupStickConfigEntry] = hass.config_entries.async_loaded_entries(DOMAIN)
    return [BackupStickAgent(hass, entry) for entry in entries]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant, *, listener: Callable[[], None], **kwargs: Any
) -> Callable[[], None]:
    hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, []).append(listener)

    @callback
    def remove_listener() -> None:
        hass.data[DATA_BACKUP_AGENT_LISTENERS].remove(listener)
        if not hass.data[DATA_BACKUP_AGENT_LISTENERS]:
            del hass.data[DATA_BACKUP_AGENT_LISTENERS]

    return remove_listener


def handle_stick_errors[_R, **P](
    func: Callable[Concatenate[BackupStickAgent, P], Coroutine[Any, Any, _R]],
) -> Callable[Concatenate[BackupStickAgent, P], Coroutine[Any, Any, _R]]:
    """Report a missing stick and disk errors as backup agent errors."""

    @wraps(func)
    async def wrapper(self: BackupStickAgent, *args: P.args, **kwargs: P.kwargs) -> _R:
        try:
            return await func(self, *args, **kwargs)
        except (StickUnavailable, OSError) as err:
            raise _agent_error(err) from err

    return wrapper


def _agent_error(err: Exception) -> BackupAgentError:
    if isinstance(err, OSError) and err.errno == errno.ENOSPC:
        return BackupAgentError("The USB stick is full")
    if isinstance(err, StickUnavailable):
        return BackupAgentError(str(err))
    return BackupAgentError(f"USB stick error: {err}")


def backup_filenames(backup: AgentBackup) -> tuple[str, str]:
    """The backup file and its metadata file, named the way Home Assistant names backups."""
    base = suggested_filename(backup).rsplit(".", 1)[0]
    return f"{base}.tar", f"{base}{METADATA_SUFFIX}"


class BackupStickAgent(BackupAgent):
    domain = DOMAIN

    def __init__(self, hass: HomeAssistant, entry: BackupStickConfigEntry) -> None:
        super().__init__()
        self._hass = hass
        self._stick: BackupStick = entry.runtime_data
        self.name = entry.title
        self.unique_id = entry.unique_id or entry.entry_id
        self._index: dict[str, tuple[AgentBackup, str]] = {}
        self._index_expires = 0.0

    @handle_stick_errors
    @override
    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: OnProgressCallback,
        **kwargs: Any,
    ) -> None:
        tar_name, metadata_name = backup_filenames(backup)
        # suggested_filename does not strip separators; never write outside the folder.
        if Path(tar_name).name != tar_name:
            raise BackupAgentError(f"Refusing unsafe backup file name: {tar_name}")

        stream = await open_stream()
        async with self._mounted() as folder:
            # Each file is written under a temporary name and renamed when
            # complete, so an interrupted upload never looks like a backup. The
            # metadata comes last: without it, the backup isn't listed.
            partial = folder / f".{tar_name}.partial"
            file: IO[bytes] = await self._run(open, partial, "wb")
            try:
                uploaded = 0
                async for chunk in stream:
                    await self._run(file.write, chunk)
                    uploaded += len(chunk)
                    on_progress(bytes_uploaded=uploaded)
                await self._run(_close_durably, file)
                await self._run(os.replace, partial, folder / tar_name)
            except BaseException:
                await self._run(_discard, file, partial)
                raise
            metadata = json.dumps(backup.as_dict()).encode()
            await self._run(_write_durably, folder / metadata_name, metadata)
        self._index_expires = 0.0
        _LOGGER.debug("Stored backup %s as %s", backup.backup_id, tar_name)

    @handle_stick_errors
    @override
    async def async_download_backup(self, backup_id: str, **kwargs: Any) -> AsyncIterator[bytes]:
        _, filename = await self._find(backup_id)
        return self._read(filename)

    async def _read(self, filename: str) -> AsyncIterator[bytes]:
        # The stick stays mounted for as long as the download is being read.
        try:
            async with self._mounted() as folder:
                try:
                    file: IO[bytes] = await self._run(open, folder / filename, "rb")
                except FileNotFoundError as err:
                    raise BackupNotFound(f"{filename} is no longer on the USB stick") from err
                try:
                    while chunk := await self._run(file.read, CHUNK_SIZE):
                        yield chunk
                finally:
                    await self._run(file.close)
        except (StickUnavailable, OSError) as err:
            raise _agent_error(err) from err

    @handle_stick_errors
    @override
    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        _, tar_name = await self._find(backup_id)
        metadata_name = tar_name.removesuffix(".tar") + METADATA_SUFFIX
        async with self._mounted() as folder:
            # Metadata first: a backup without it is no longer listed.
            await self._run((folder / metadata_name).unlink, True)
            await self._run((folder / tar_name).unlink, True)
        self._index_expires = 0.0
        _LOGGER.debug("Deleted %s from the USB stick", tar_name)

    @handle_stick_errors
    @override
    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        return [backup for backup, _ in (await self._read_index()).values()]

    @handle_stick_errors
    @override
    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        backup, _ = await self._find(backup_id)
        return backup

    async def _find(self, backup_id: str) -> tuple[AgentBackup, str]:
        if found := (await self._read_index()).get(backup_id):
            return found
        raise BackupNotFound(f"Backup {backup_id} not found on the USB stick")

    async def _read_index(self) -> dict[str, tuple[AgentBackup, str]]:
        if monotonic() < self._index_expires:
            return self._index
        async with self._mounted() as folder:
            self._index = await self._run(_scan, folder)
        self._index_expires = monotonic() + CACHE_TTL
        return self._index

    @contextlib.asynccontextmanager
    async def _mounted(self) -> AsyncIterator[Path]:
        mount = self._stick.mounted()
        folder = await self._run(mount.__enter__)
        try:
            yield folder
        finally:
            await self._run(mount.__exit__, None, None, None)

    async def _run[_T](self, target: Callable[..., _T], *args: Any) -> _T:
        return await self._hass.async_add_executor_job(target, *args)


def _scan(folder: Path) -> dict[str, tuple[AgentBackup, str]]:
    """Every complete backup in the folder, by backup ID."""
    index: dict[str, tuple[AgentBackup, str]] = {}
    for metadata_path in folder.glob(f"*{METADATA_SUFFIX}"):
        tar_name = metadata_path.name.removesuffix(METADATA_SUFFIX) + ".tar"
        if not (folder / tar_name).is_file():
            continue
        try:
            backup = AgentBackup.from_dict(json.loads(metadata_path.read_text()))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as err:
            _LOGGER.warning("Skipping unreadable metadata %s on the USB stick: %s", metadata_path.name, err)
            continue
        index[backup.backup_id] = (backup, tar_name)
    return index


def _close_durably(file: IO[bytes]) -> None:
    file.flush()
    os.fsync(file.fileno())
    file.close()


def _write_durably(path: Path, data: bytes) -> None:
    partial = path.with_name(f".{path.name}.partial")
    with open(partial, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(partial, path)


def _discard(file: IO[bytes], path: Path) -> None:
    with contextlib.suppress(OSError):
        file.close()
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
