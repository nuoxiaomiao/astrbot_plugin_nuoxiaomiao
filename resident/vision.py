"""群聊识图：群图留底、自动转述、read_image 工具的数据层与模型调用。

行为参考：astrbot_plugin_astrmia（Kalo & Qingyi）公开文档描述的能力——群聊里
发来的图片被留存并在后台转述，模型可凭「会话 + 编号 + 序号」追问任意一张图的
细节。本模块是独立实现：模型来源与提示词与 AstrBot 主对话的图片转述共用同一套
核心配置，不复制任何第三方源码。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time
import uuid
from pathlib import Path

from astrbot.api import logger

# 单张图片描述失败后重试次数（不含超时吞掉的那次）。
VISION_ATTEMPTS_PER_PROVIDER = 2
# 默认提示词：仅在 AstrBot 核心没有配置 image_caption_prompt 时使用。
VISION_DEFAULT_CAPTION_PROMPT = (
    "请用中文仔细描述这张图片的内容，包括主要物体、人物动作、场景与"
    "任何可见文字。如果图里有值得注意的细节，请一并说明。"
)


class Vision:
    """图片留底、转述缓存与 read_image 的数据服务。"""

    def __init__(self, service):
        self.service = service
        self.plugin = service.plugin
        self.store = service.store

    # ------------------------------------------------------------------
    # 留底目录与图片定位
    # ------------------------------------------------------------------

    def dir(self) -> Path:
        """图片留底的持久目录（活动库同目录下的 vision_kept/）。"""
        return self.plugin._plugin_data_dir() / "vision_kept"

    @staticmethod
    def _image_ref(component) -> str:
        return str(
            getattr(component, "url", "")
            or getattr(component, "file", "")
            or getattr(component, "path", "")
            or ""
        ).strip()

    async def keep_message_images(self, event, activity_id: int, message_id: str) -> None:
        """把入站消息里的图片留底并登记索引，随后后台转述。

        任何一步失败都不阻断聊天：识图是顺手的增值功能。
        """
        if not self.plugin.vision_enabled:
            return
        try:
            components = self.plugin._extract_components(event)
            images = [
                component
                for component in components
                if self.plugin._is_image_component(component)
            ]
        except Exception as exc:
            logger.debug("[糯小喵] 识图留底取图片组件失败: %s", exc)
            return
        if not images:
            return
        umo = str(event.unified_msg_origin or "").strip()
        if not umo:
            return
        kept = 0
        for idx, component in enumerate(images):
            if kept >= self.plugin.vision_image_max:
                break
            ref = self._image_ref(component)
            if not ref:
                continue
            try:
                await self._keep_one(umo, message_id, activity_id, idx, ref)
                kept += 1
            except Exception as exc:
                logger.debug("[糯小喵] 识图留底失败: %s", exc)
        if kept:
            self._spawn_caption_task(umo, message_id)

    async def _keep_one(
        self, umo: str, message_id: str, activity_id: int, idx: int, ref: str
    ) -> None:
        """定位图片字节（本地已存在则直接用，否则下载）并持久化。"""
        local = self.plugin._image_ref_to_local_path(ref)
        source = None
        if local and os.path.exists(local):
            source = local
        else:
            try:
                from astrbot.core.utils.media_utils import MediaResolver

                if MediaResolver is None:  # pragma: no cover - 旧版核心兜底
                    return
                source = await MediaResolver(ref, media_type="image").to_path()
            except Exception as exc:
                logger.debug("[糯小喵] 识图下载图片失败: %s", exc)
                return
        if not source or not os.path.exists(source):
            return
        dest = await asyncio.to_thread(self._persist, source, ref)
        if dest is not None:
            self.store.vision_keep(umo, message_id, activity_id, idx, str(dest))

    def _persist(self, source: str, ref: str) -> Path | None:
        """把图片字节拷贝到留底目录，返回目标路径。"""
        try:
            target_dir = self.dir()
            target_dir.mkdir(parents=True, exist_ok=True)
            suffix = Path(source).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".gif"}:
                suffix = ".jpg"
            digest = hashlib.md5(ref.encode("utf-8")).hexdigest()[:10]
            stamp = time.strftime("%Y%m%d%H%M%S")
            pid = os.getpid() & 0xFFFF
            dest = target_dir / f"img_{stamp}_{pid}_{digest}{suffix}"
            shutil.copy2(source, dest)
            return dest
        except Exception as exc:
            logger.debug("[糯小喵] 识图图片落盘失败: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 后台转述
    # ------------------------------------------------------------------

    def _spawn_caption_task(self, umo: str, message_id: str) -> None:
        """启动一条后台转述任务；引用挂到插件的后台任务集合防 GC。"""
        task = asyncio.create_task(self._caption_background(umo, message_id))
        self.plugin._resident_bg_tasks.add(task)
        task.add_done_callback(self.plugin._resident_bg_tasks.discard)

    async def _caption_background(self, umo: str, message_id: str) -> None:
        """把这条消息里的每张留底图依次转述并写入缓存（自动转述用简短版）。"""
        rows = self.store.vision_kept_for(umo, message_id)
        for row in rows:
            if not os.path.exists(row["path"]):
                continue
            key = self._cache_key(row["path"])
            if self.store.vision_caption_get(key):
                continue
            caption = await self.describe_once(row["path"], "")
            if caption:
                self.store.vision_caption_set(
                    key, caption[: self.plugin.vision_brief_max_chars]
                )

    # ------------------------------------------------------------------
    # 模型调用
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_key(path: str) -> str:
        try:
            digest = hashlib.md5(Path(path).name.encode("utf-8")).hexdigest()
        except Exception:  # pragma: no cover - 防御性
            digest = str(uuid.uuid4())
        return f"file:{digest}"

    def _core_provider_settings(self, umo: str = "") -> dict:
        """读取 AstrBot 主对话的 provider_settings（图片转述配置的来源）。"""
        try:
            cfg = self.plugin.context.get_config(umo=umo or None)
            if isinstance(cfg, dict):
                return cfg.get("provider_settings", {}) or {}
        except Exception:
            pass
        return {}

    def _caption_prompt(self, umo: str = "") -> str:
        prompt = str(
            self._core_provider_settings(umo).get("image_caption_prompt", "") or ""
        ).strip()
        return prompt or VISION_DEFAULT_CAPTION_PROMPT

    async def resolve_provider(self, umo: str = ""):
        """按链解析识图模型：插件指定链 → 核心默认识图模型 → 会话默认模型。"""
        manager = getattr(self.plugin.context, "provider_manager", None)
        if manager is None:
            return None
        for provider_id in self.plugin.vision_provider_ids:
            try:
                provider = await manager.get_provider_by_id(provider_id)
            except Exception:
                provider = None
            if provider is not None and callable(getattr(provider, "text_chat", None)):
                return provider
        core_default = str(
            self._core_provider_settings(umo).get("default_image_caption_provider_id", "")
            or ""
        ).strip()
        if core_default:
            try:
                provider = await manager.get_provider_by_id(core_default)
            except Exception:
                provider = None
            if provider is not None and callable(getattr(provider, "text_chat", None)):
                return provider
        try:
            get_async = getattr(manager, "get_using_provider_async", None)
            if callable(get_async):
                provider = await get_async(umo=umo or None)
            else:
                from astrbot.core.provider.entities import ProviderType

                provider = manager.get_using_provider(ProviderType.CHAT_COMPLETION)
        except Exception:
            provider = None
        if provider is not None and callable(getattr(provider, "text_chat", None)):
            return provider
        return None

    async def describe_once(
        self, image_path: str, question: str, umo: str = "", timeout_sec: float = 0
    ) -> str:
        """对单张图片跑一次识图模型；失败或超时返回空串。"""
        provider = await self.resolve_provider(umo)
        if provider is None:
            return ""
        if not os.path.exists(image_path):
            return ""
        prompt = self._caption_prompt(umo)
        if str(question or "").strip():
            prompt += f"\n额外问题：{question.strip()}"
        timeout = float(timeout_sec or self.plugin.vision_timeout_seconds)
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
                text = str(getattr(resp, "completion_text", "") or "").strip()
                if text:
                    return text
            except asyncio.TimeoutError:
                logger.debug("[糯小喵] 识图超时")
                return ""
            except Exception as exc:
                logger.debug("[糯小喵] 识图调用失败: %s", exc)
        return ""

    async def read_image(
        self, umo: str, message_id: str, index: int, question: str = ""
    ) -> str:
        """read_image 工具的数据路径：按会话 + 平台消息号 + 序号取图回答。"""
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
        caption = self.store.vision_caption_get(self._cache_key(row["path"]))
        if caption and not str(question or "").strip():
            return caption[: self.plugin.vision_read_max_chars]
        answer = await self.describe_once(row["path"], question or caption or "")
        if not answer:
            return (
                caption[: self.plugin.vision_read_max_chars]
                or "识图模型不可用，暂时无法描述这张图片。"
            )
        if str(question or "").strip():
            self.store.vision_caption_set(self._cache_key(row["path"]), answer)
        return answer[: self.plugin.vision_read_max_chars]

    # ------------------------------------------------------------------
    # 清理与统计
    # ------------------------------------------------------------------

    def cleanup(self) -> tuple[int, int, int]:
        """按保留天数清理过期留底行、文件与描述缓存。"""
        if not self.plugin.vision_enabled:
            return (0, 0, 0)
        return self.store.vision_cleanup(
            self.plugin.image_keep_days, self.plugin.vision_cache_ttl_days
        )

    def stats(self) -> tuple[int, int]:
        """返回 (留底图数量, 描述缓存数量)。"""
        return self.store.vision_stats()
