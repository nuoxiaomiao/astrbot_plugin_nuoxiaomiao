"""Shared image validation, SSRF safety checks, localization, and disk GC."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable
from urllib.parse import unquote, urlparse
import uuid

from astrbot.api import logger

try:
    from astrbot.core.utils.media_utils import MediaResolver
except Exception:  # pragma: no cover
    MediaResolver = None  # type: ignore[assignment]

if __package__ and "." in __package__:
    from .constants import (
        DISPLAY_NAME,
        IMAGE_MAGIC_PREFIXES,
        IMAGE_SUFFIXES,
        LOCALIZED_IMAGE_CLEANUP_TTL_SECONDS,
        LOCALIZED_IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
        LOCALIZED_IMAGE_MAX_BYTES,
        LOCALIZED_IMAGE_NAME_RE,
        LOCALIZED_IMAGE_SWEEP_INTERVAL_SECONDS,
        LOCALIZED_IMAGE_SWEEP_MAX_AGE_SECONDS,
        SSRF_PRIVATE_NETWORKS,
    )
else:
    from infra.constants import (
        DISPLAY_NAME,
        IMAGE_MAGIC_PREFIXES,
        IMAGE_SUFFIXES,
        LOCALIZED_IMAGE_CLEANUP_TTL_SECONDS,
        LOCALIZED_IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
        LOCALIZED_IMAGE_MAX_BYTES,
        LOCALIZED_IMAGE_NAME_RE,
        LOCALIZED_IMAGE_SWEEP_INTERVAL_SECONDS,
        LOCALIZED_IMAGE_SWEEP_MAX_AGE_SECONDS,
        SSRF_PRIVATE_NETWORKS,
    )


def is_image_component(component: Any) -> bool:
    name = type(component).__name__
    if name == "Image":
        return True
    comp_type = getattr(component, "type", None)
    if getattr(comp_type, "value", None) == "Image":
        return True
    return str(comp_type) in {"Image", "ComponentType.Image"}


def image_ref_to_local_path(image_ref: str) -> str | None:
    value = str(image_ref or "").strip()
    if not value:
        return None
    if value.lower().startswith("file://"):
        parsed = urlparse(value)
        path = unquote(parsed.path or "")
        if parsed.netloc:
            path = f"//{parsed.netloc}{path}"
        elif path.startswith("//"):
            path = f"/{path.lstrip('/')}"
        return path or None
    if os.path.isabs(value):
        return value
    return None


def is_private_ip(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip.split("%")[0].strip())
    except ValueError:
        return False
    if getattr(address, "ipv4_mapped", None) is not None:
        address = address.ipv4_mapped
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return True
    for net, prefix in SSRF_PRIVATE_NETWORKS:
        try:
            network = ipaddress.ip_network(f"{net}/{prefix}")
            if address.version == network.version and address in network:
                return True
        except ValueError:
            continue
    return False


def is_safe_download_url(image_ref: str) -> bool:
    value = str(image_ref or "").strip()
    if not value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return False
    if host.endswith(".local") or host.endswith(".localhost"):
        return False
    return not is_private_ip(host)


def looks_like_image(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    if not head:
        return False
    for magic in IMAGE_MAGIC_PREFIXES:
        if head.startswith(magic):
            if magic == b"RIFF" and head[8:12] != b"WEBP":
                continue
            return True
    return False


def persist_image_file(
    source_path: str | Path,
    target_dir: Path,
    ref: str,
    *,
    unique: bool = True,
    prefix: str = "img",
) -> Path | None:
    try:
        src_path = Path(source_path)
        if not looks_like_image(str(src_path)):
            logger.warning(
                "[%s] localize: source is not an image, skip: %s",
                DISPLAY_NAME,
                source_path,
            )
            return None
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = src_path.suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            suffix = ".jpg"
        digest = hashlib.md5(ref.encode("utf-8")).hexdigest()[:10]
        ts = time.strftime("%Y%m%d%H%M%S")
        pid = os.getpid() & 0xFFFF
        uid = f"_{uuid.uuid4().hex[:8]}" if unique else ""
        dest = target_dir / f"{prefix}_{ts}_{pid}_{digest}{uid}{suffix}"
        shutil.copy2(src_path, dest)
        return dest
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[%s] localize image persist failed, keep original: ref=%s err=%s",
            DISPLAY_NAME,
            ref,
            exc,
        )
        return None


def rewrite_image_component(component: Any, local_path: Path) -> Any:
    file_uri = local_path.resolve().as_uri()
    try:
        component.file = file_uri
        component.url = file_uri
        component.path = str(local_path.resolve())
    except Exception:  # noqa: BLE001
        return component
    return component


class MediaLocalizer:
    """Download/persist inbound images safely for merge batches with epoch cleanup."""

    def __init__(self, dir_resolver: Callable[[], Path]) -> None:
        self._dir_resolver = dir_resolver
        self._tracked: dict[tuple[str, int], list[str]] = {}
        self._tracked_ts: dict[tuple[str, int], float] = {}
        self._cleanup_lock = asyncio.Lock()
        self._last_sweep_ts: float = 0.0

    def is_in_persist_dir(self, local_path: str) -> bool:
        try:
            persist_dir = str(self._dir_resolver().resolve())
            return os.path.abspath(local_path).startswith(persist_dir + os.sep)
        except Exception:  # noqa: BLE001
            return False

    async def download_and_persist(
        self,
        ref: str,
        *,
        resolver_cls: Any = None,
        timeout: float = LOCALIZED_IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
        max_bytes: int = LOCALIZED_IMAGE_MAX_BYTES,
    ) -> Path | None:
        resolver = resolver_cls if resolver_cls is not None else MediaResolver
        local_path = image_ref_to_local_path(ref)
        target_dir = self._dir_resolver()
        if local_path:
            if os.path.exists(local_path):
                if self.is_in_persist_dir(local_path):
                    return None
                return await asyncio.to_thread(persist_image_file, local_path, target_dir, ref)
            if not is_safe_download_url(ref):
                return None
        elif not is_safe_download_url(ref):
            logger.warning("[%s] localize: refused unsafe image ref: %s", DISPLAY_NAME, ref[:120])
            return None
        if resolver is None:
            logger.warning("[%s] localize: MediaResolver unavailable, skip", DISPLAY_NAME)
            return None
        try:
            downloaded = await asyncio.wait_for(
                resolver(ref, media_type="image").to_path(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("[%s] localize download timed out: ref=%s", DISPLAY_NAME, ref[:120])
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] localize download failed: ref=%s err=%s", DISPLAY_NAME, ref[:120], exc)
            return None
        if not downloaded or not os.path.exists(downloaded):
            return None
        try:
            if os.path.getsize(downloaded) > max_bytes:
                logger.warning("[%s] localize download too large: ref=%s", DISPLAY_NAME, ref[:120])
                return None
        except OSError:
            return None
        return await asyncio.to_thread(persist_image_file, downloaded, target_dir, ref)

    async def localize_components(
        self,
        components: list[Any],
        *,
        enabled: bool = True,
        session_key: str = "",
        epoch: int = 0,
        resolver_cls: Any = None,
    ) -> tuple[list[Any], list[str]]:
        if not enabled or not components:
            return components, []
        image_count = sum(1 for c in components if is_image_component(c))
        if image_count == 0:
            return components, []
        localized: list[Any] = []
        created_paths: list[str] = []
        for component in components:
            if not is_image_component(component):
                localized.append(component)
                continue
            ref = str(
                getattr(component, "url", "")
                or getattr(component, "file", "")
                or getattr(component, "path", "")
                or ""
            ).strip()
            if not ref:
                localized.append(component)
                continue
            old_path = str(getattr(component, "path", "") or "")
            dest = await self.download_and_persist(ref, resolver_cls=resolver_cls)
            if dest is not None:
                rewritten = rewrite_image_component(component, dest)
                new_path = str(getattr(rewritten, "path", "") or "")
                if new_path and new_path != old_path:
                    created_paths.append(new_path)
                    if session_key:
                        self.track(session_key, epoch, new_path)
                    localized.append(rewritten)
                    continue
            localized.append(component)
        return localized, created_paths

    def track(self, session_key: str, epoch: int, file_path: str) -> None:
        if not session_key or not file_path:
            return
        key = (session_key, int(epoch or 0))
        files = self._tracked.setdefault(key, [])
        if file_path not in files:
            files.append(file_path)
        self._tracked_ts[key] = time.monotonic()

    def reassign_epoch(self, session_key: str, old_epoch: int, new_epoch: int, paths: list[str]) -> None:
        """Move newly localized files from a stale epoch to the freshly created batch epoch."""
        if not session_key or not paths or old_epoch == new_epoch:
            return
        old_key = (session_key, int(old_epoch or 0))
        new_key = (session_key, int(new_epoch or 0))
        old_list = self._tracked.get(old_key)
        if old_list:
            self._tracked[old_key] = [p for p in old_list if p not in paths]
            if not self._tracked[old_key]:
                self._tracked.pop(old_key, None)
                self._tracked_ts.pop(old_key, None)
        new_list = self._tracked.setdefault(new_key, [])
        for p in paths:
            if p not in new_list:
                new_list.append(p)
        self._tracked_ts[new_key] = time.monotonic()

    async def cleanup_batch(self, session_key: str, epoch: int = 0) -> None:
        composite = (session_key, int(epoch or 0))
        async with self._cleanup_lock:
            files = self._tracked.pop(composite, [])
            self._tracked_ts.pop(composite, None)
        if not files:
            return
        await asyncio.to_thread(self._unlink_files, files, composite)

    @staticmethod
    def _unlink_files(files: list[str], composite: tuple[str, int]) -> int:
        removed = 0
        for file_path in files:
            try:
                if file_path and os.path.isfile(file_path):
                    os.remove(file_path)
                    removed += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("[%s] failed to remove localized image %s: %s", DISPLAY_NAME, file_path, exc)
        if removed:
            logger.info("[%s] cleaned up %d localized image(s) for batch=%s", DISPLAY_NAME, removed, composite)
        return removed

    async def cleanup_expired(self) -> None:
        now = time.monotonic()
        expired = [
            key
            for key, ts in self._tracked_ts.items()
            if now - ts > LOCALIZED_IMAGE_CLEANUP_TTL_SECONDS
        ]
        for session_key, epoch in expired:
            await self.cleanup_batch(session_key, epoch)
        if now - self._last_sweep_ts >= LOCALIZED_IMAGE_SWEEP_INTERVAL_SECONDS:
            self._last_sweep_ts = now
            await asyncio.to_thread(self.sweep_orphans, LOCALIZED_IMAGE_SWEEP_MAX_AGE_SECONDS)

    async def cleanup_all(self) -> None:
        for session_key, epoch in list(self._tracked.keys()):
            await self.cleanup_batch(session_key, epoch)

    def sweep_orphans(self, max_age_seconds: float) -> int:
        removed = 0
        try:
            directory = self._dir_resolver()
            if not directory.is_dir():
                return 0
            now = time.time()
            for path in directory.iterdir():
                try:
                    if not path.is_file():
                        continue
                    if not LOCALIZED_IMAGE_NAME_RE.match(path.name):
                        continue
                    if now - path.stat().st_mtime <= max_age_seconds:
                        continue
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] 清扫孤儿图片失败: %s", DISPLAY_NAME, exc)
        if removed:
            logger.info(
                "[%s] 清扫孤儿图片 %d 个（超过 %.0f 分钟无人认领）",
                DISPLAY_NAME,
                removed,
                max_age_seconds / 60,
            )
        return removed
