"""群聊识图：入站图片按原始字节落盘留底，后台静默转述，read_image 按需调用。"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import os
from pathlib import Path
import time
import uuid

from astrbot.api import logger

if __package__ and "." in __package__:
    from ..infra.constants import (
        DISPLAY_NAME,
        LOCALIZED_IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
        LOCALIZED_IMAGE_MAX_BYTES,
    )
    from ..infra.media import (
        image_ref_to_local_path,
        is_image_component,
        is_safe_download_url,
        persist_image_file,
    )
    from ..infra.providers import ProviderResolver
    from ..infra.runtime import RateLimitedLogger
    from .autonomy_budget import CURRENT_RUN, BudgetExceeded
else:
    from infra.constants import (
        DISPLAY_NAME,
        LOCALIZED_IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
        LOCALIZED_IMAGE_MAX_BYTES,
    )
    from infra.media import (
        image_ref_to_local_path,
        is_image_component,
        is_safe_download_url,
        persist_image_file,
    )
    from infra.providers import ProviderResolver
    from infra.runtime import RateLimitedLogger
    from resident.autonomy_budget import CURRENT_RUN, BudgetExceeded

VISION_DIR_NAME = "vision_kept"
VISION_ATTEMPTS_PER_PROVIDER = 2
VISION_WARN_INTERVAL_SECONDS = 60.0
VISION_MAX_CONCURRENCY = 4


class Vision:
    """图片留底与识图模型调用服务。"""

    def __init__(self, service):
        self.service = service
        self.plugin = service.plugin
        self.host = service.host
        self.store = service.store
        self._rate_log = RateLimitedLogger(interval_seconds=VISION_WARN_INTERVAL_SECONDS)
        self._sem = asyncio.Semaphore(VISION_MAX_CONCURRENCY)

    def _warn_throttled(self, key: str, message: str, *args) -> None:
        self._rate_log.warning(key, message, *args)

    def dir(self) -> Path:
        return Path(self.host.plugin_data_dir()) / VISION_DIR_NAME

    @staticmethod
    def _image_ref(component) -> str:
        return str(
            getattr(component, "url", "")
            or getattr(component, "file", "")
            or getattr(component, "path", "")
            or ""
        ).strip()

    async def keep_message_images(
        self, event, activity_id: int, platform_message_id: str = ""
    ) -> None:
        p = self.plugin
        if not p.vision_enabled:
            return
        message_id = str(platform_message_id or "").strip() or self.host.message_id(event)
        if not message_id:
            return
        try:
            images = [c for c in self.host.extract_components(event) if is_image_component(c)]
        except Exception as exc:
            logger.debug("[%s] 识图留底取图片组件失败: %s", DISPLAY_NAME, exc)
            return
        if not images:
            return
        umo = str(event.unified_msg_origin or "").strip()
        if not umo:
            return
        kept = 0
        for idx, component in enumerate(images):
            if kept >= p.vision_image_max:
                break
            ref = self._image_ref(component)
            if not ref:
                continue
            try:
                await self._keep_one(umo, message_id, activity_id, idx, ref)
                kept += 1
            except Exception as exc:
                logger.debug("[%s] 识图留底失败: %s", DISPLAY_NAME, exc)
        if kept:
            self._spawn_caption_task(umo, message_id)

    async def _keep_one(
        self, umo: str, message_id: str, activity_id: int, idx: int, ref: str
    ) -> None:
        local = image_ref_to_local_path(ref)
        source = None
        if local and os.path.exists(local):
            source = local
        else:
            if not is_safe_download_url(ref):
                logger.debug("[%s] 识图拒绝不安全图片地址: %s", DISPLAY_NAME, ref[:120])
                return
            try:
                from astrbot.core.utils.media_utils import MediaResolver

                if MediaResolver is None:
                    return
                async with self._sem:
                    source = await asyncio.wait_for(
                        MediaResolver(ref, media_type="image").to_path(),
                        timeout=LOCALIZED_IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
                    )
            except asyncio.TimeoutError:
                logger.debug("[%s] 识图下载图片超时: %s", DISPLAY_NAME, ref[:120])
                return
            except Exception as exc:
                logger.debug("[%s] 识图下载图片失败: %s", DISPLAY_NAME, exc)
                return
        if not source or not os.path.exists(source):
            return
        try:
            if os.path.getsize(source) > LOCALIZED_IMAGE_MAX_BYTES:
                logger.debug("[%s] 识图图片过大，跳过: %s", DISPLAY_NAME, ref[:120])
                return
        except OSError:
            return
        dest = await asyncio.to_thread(self._persist, source, ref)
        if dest is not None:
            self.store.vision_keep(umo, message_id, activity_id, idx, str(dest))

    def _persist(self, source: str, ref: str) -> Path | None:
        return persist_image_file(source, self.dir(), ref, unique=True)

    def _spawn_caption_task(self, umo: str, message_id: str) -> None:
        ctx = contextvars.copy_context()

        def _create():
            CURRENT_RUN.set(None)
            return asyncio.create_task(self._caption_background(umo, message_id))

        task = ctx.run(_create)
        bg = self.host.bg_tasks
        bg.add(task)
        task.add_done_callback(bg.discard)

    async def _caption_background(self, umo: str, message_id: str) -> None:
        rows = self.store.vision_kept_for(umo, message_id)
        for row in rows:
            if not os.path.exists(row["path"]):
                continue
            key = self._cache_key(row["path"])
            if self.store.vision_caption_get(key):
                continue
            caption = await self.describe_once(row["path"], "", umo=umo)
            if caption:
                self.store.vision_caption_set(key, caption[: self.plugin.vision_brief_max_chars])

    @staticmethod
    def _cache_key(path: str) -> str:
        try:
            digest = hashlib.md5(Path(path).name.encode("utf-8")).hexdigest()
        except Exception:  # pragma: no cover
            digest = str(uuid.uuid4())
        return f"file:{digest}"

    @staticmethod
    def _qa_cache_key(path: str, question: str) -> str:
        base = Vision._cache_key(path)
        q_digest = hashlib.md5(str(question).strip().encode("utf-8")).hexdigest()[:12]
        return f"qa:{base}:{q_digest}"

    def _core_provider_settings(self, umo: str = "") -> dict:
        return ProviderResolver(self.host.context).core_provider_settings(umo)

    def _caption_prompt(self, umo: str = "") -> str:
        custom = str(self._core_provider_settings(umo).get("image_caption_prompt", "") or "").strip()
        if custom:
            return custom
        return "用简洁自然的中文描述图片里的人、物体、场景和主要文字信息，不要编造看不清的内容。"

    async def resolve_provider(self, umo: str = ""):
        resolver = ProviderResolver(self.host.context)
        return await resolver.resolve_ordered(
            list(self.plugin.vision_provider_ids),
            umo=umo,
            core_setting_key="default_image_caption_provider_id",
            fallback_session_default=True,
        )

    async def describe_once(
        self, image_path: str, question: str, umo: str = "", timeout_sec: float = 0
    ) -> str:
        provider = await self.resolve_provider(umo)
        if provider is None:
            self._warn_throttled(
                f"vision-no-provider:{umo or '-'}",
                "[糯小喵] 识图跳过：没有可用模型（会话 %s）",
                umo or "-",
            )
            return ""
        if CURRENT_RUN.get() is not None:
            provider = self.service.autonomy.wrap_provider(provider)
        if not os.path.exists(image_path):
            return ""
        prompt = self._caption_prompt(umo)
        if str(question or "").strip():
            prompt += f"\n额外问题：{question.strip()}"
        timeout = float(timeout_sec or self.plugin.vision_timeout_seconds)
        async with self._sem:
            for _ in range(VISION_ATTEMPTS_PER_PROVIDER):
                try:
                    resp = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=prompt,
                            image_urls=[image_path],
                            session_id=f"nuoxiaomiao_vision_{uuid.uuid4().hex}",
                            persist=False,
                        ),
                        timeout=timeout,
                    )
                    text = ProviderResolver.extract_text(resp)
                    if text:
                        return text
                except BudgetExceeded:
                    raise
                except asyncio.TimeoutError:
                    self._warn_throttled(
                        f"vision-timeout:{umo or '-'}",
                        "[糯小喵] 识图超时（会话 %s）",
                        umo or "-",
                    )
                    return ""
                except Exception as exc:
                    self._warn_throttled(
                        f"vision-call:{umo or '-'}",
                        "[糯小喵] 识图调用失败（会话 %s）: %s",
                        umo or "-",
                        exc,
                    )
        return ""

    async def read_image(
        self, umo: str, message_id: str, index: int, question: str = ""
    ) -> str:
        rows = self.store.vision_kept_for(umo, message_id)
        if not rows:
            return "找不到这张图：留底可能已过期清理，或该消息没有图片。"
        try:
            index = max(0, int(index))
        except (TypeError, ValueError):
            index = 0
        if index >= len(rows):
            return f"这条消息只有 {len(rows)} 张图（序号从 0 开始）。"
        row = rows[index]
        if not os.path.exists(row["path"]):
            return "这张图的留底文件已被清理，无法查看。"
        brief_key = self._cache_key(row["path"])
        caption = self.store.vision_caption_get(brief_key)
        clean_q = str(question or "").strip()
        if caption and not clean_q:
            return caption[: self.plugin.vision_read_max_chars]
        if clean_q:
            qa_key = self._qa_cache_key(row["path"], clean_q)
            qa_cached = self.store.vision_caption_get(qa_key)
            if qa_cached:
                return qa_cached[: self.plugin.vision_read_max_chars]
        answer = await self.describe_once(row["path"], clean_q or caption or "", umo=umo)
        if not answer:
            return caption[: self.plugin.vision_read_max_chars] or "识图模型不可用，暂时无法描述这张图片。"
        if clean_q:
            self.store.vision_caption_set(self._qa_cache_key(row["path"], clean_q), answer)
        elif not caption:
            self.store.vision_caption_set(brief_key, answer[: self.plugin.vision_brief_max_chars])
        return answer[: self.plugin.vision_read_max_chars]

    def cleanup(self) -> tuple[int, int, int]:
        """清理过期留底与孤儿图片；即使关闭开关也允许清理历史遗留。"""
        rows, files, descs = self.store.vision_cleanup(
            self.plugin.image_keep_days, self.plugin.vision_cache_ttl_days
        )
        # 扫盘清理没有 DB 记录且已过期的孤儿文件
        try:
            directory = self.dir()
            if directory.is_dir():
                tracked_names = {
                    Path(r["path"]).name
                    for r in self.store.rows("SELECT path FROM vision_kept_images")
                    if r.get("path")
                }
                cutoff = time.time() - max(1, int(self.plugin.image_keep_days or 1)) * 86400
                for item in directory.iterdir():
                    if item.is_file() and item.name not in tracked_names:
                        try:
                            if item.stat().st_mtime < cutoff:
                                item.unlink()
                                files += 1
                        except OSError:
                            continue
        except Exception:
            pass
        return (rows, files, descs)

    def stats(self) -> tuple[int, int]:
        return self.store.vision_stats()
