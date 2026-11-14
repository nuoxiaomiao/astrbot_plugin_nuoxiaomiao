"""Smart debounce merging, image localization epoch isolation, and synthetic resubmission."""

from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any, Callable

from astrbot.api import logger
from astrbot.core.message.components import At, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.star_tools import StarTools

if __package__ and "." in __package__:
    from ..infra.constants import (
        DISPLAY_NAME,
        MERGE_FAILURE_MESSAGE,
        MERGED_EVENT_MAX_ATTEMPTS,
        MERGED_EVENT_RETRY_DELAY_SECONDS,
        SYNTHETIC_EVENT_EXTRA,
        SYNTHETIC_ID_TTL_SECONDS,
        MergeSession,
    )
    from ..infra.media import MediaLocalizer
    from ..qq_official import apply_content_mode, is_mergeable_at, is_qq_official, sanitize_outbound_components
else:
    from infra.constants import (
        DISPLAY_NAME,
        MERGE_FAILURE_MESSAGE,
        MERGED_EVENT_MAX_ATTEMPTS,
        MERGED_EVENT_RETRY_DELAY_SECONDS,
        SYNTHETIC_EVENT_EXTRA,
        SYNTHETIC_ID_TTL_SECONDS,
        MergeSession,
    )
    from infra.media import MediaLocalizer
    from qq_official import apply_content_mode, is_mergeable_at, is_qq_official, sanitize_outbound_components


class DebounceMerger:
    """Buffer rapid multi-part user messages and re-submit merged events cleanly."""

    def __init__(
        self,
        plugin: Any,
        guard: Any,
        media: MediaLocalizer,
        send_observer: Any,
        send_order: Any,
        *,
        platform_inst_by_id_fn: Callable[[str], Any | None],
    ) -> None:
        self.plugin = plugin
        self.guard = guard
        self.media = media
        self.send_observer = send_observer
        self.send_order = send_order
        self._platform_inst_by_id = platform_inst_by_id_fn
        self._merge_lock = asyncio.Lock()
        self._merges: dict[str, MergeSession] = {}
        self._merge_monitors: dict[str, asyncio.Task[None]] = {}
        self._skip_merge_ids: set[str] = set()
        self._skip_merge_timestamps: dict[str, float] = {}
        self._synthetic_source_ids: dict[str, set[str]] = {}
        self._synthetic_epochs: dict[str, int] = {}
        self._epoch_counter = 0

    @staticmethod
    def merge_key(session_key: str, sender_id: str) -> str:
        return f"{session_key}:{sender_id}"

    def next_epoch(self) -> int:
        self._epoch_counter += 1
        return self._epoch_counter

    def debounce_applies(self, event: Any, sender_id: str) -> bool:
        p = self.plugin
        if not p.debounce_enabled:
            return False
        try:
            if event.is_private_chat():
                if not p.debounce_private_enabled:
                    return False
            elif event.get_group_id():
                if not p.debounce_group_enabled:
                    return False
                if not self.guard.event_group_allowed(
                    event, p.debounce_group_whitelist, p.debounce_group_blacklist
                ):
                    return False
            else:
                return False
        except Exception:
            return False
        if p.debounce_user_whitelist and sender_id not in p.debounce_user_whitelist:
            return False
        return True

    def dynamic_idle_seconds(self, text: str, keyword_woken: bool = False) -> float | None:
        p = self.plugin
        if not p.debounce_dynamic_enabled:
            return None
        stripped = (text or "").strip()
        if not stripped:
            return p.debounce_slow_idle_seconds
        if len(stripped) >= p.debounce_long_chars:
            return p.debounce_fast_idle_seconds
        if p.debounce_end_punctuation and stripped[-1] in p.debounce_end_punctuation:
            return p.debounce_fast_idle_seconds
        if keyword_woken and self.is_bare_wake_call(stripped):
            return p.debounce_wake_idle_seconds
        return p.debounce_slow_idle_seconds

    def is_bare_wake_call(self, text: str) -> bool:
        p = self.plugin
        if not p.keyword_wake_list:
            return False
        remainder = text
        for keyword in p.keyword_wake_list:
            if keyword:
                remainder = remainder.replace(keyword, "")
        return not remainder.strip(" \t\r\n，,。.！!？?~～、")

    @staticmethod
    def has_mergeable_content(event: Any, text: str, components: list[Any]) -> bool:
        if text:
            return True
        self_id = str(event.get_self_id() or "")
        for component in components:
            cname = type(component).__name__
            if cname == "Plain":
                continue
            if cname == "At" and is_mergeable_at(component, event):
                continue
            if cname == "Reply" and (
                is_qq_official(event) or str(getattr(component, "sender_id", "")) == self_id
            ):
                continue
            return True
        return False

    async def has_active_merge(self, key: str) -> bool:
        async with self._merge_lock:
            return key in self._merges

    async def cancel_merge(self, key: str) -> None:
        async with self._merge_lock:
            self._merges.pop(key, None)
            monitor = self._merge_monitors.pop(key, None)
        if monitor and not monitor.done():
            monitor.cancel()

    async def consume_synthetic_id(self, message_id: str) -> tuple[set[str] | None, int]:
        async with self._merge_lock:
            if message_id not in self._skip_merge_ids:
                return None, 0
            self._skip_merge_ids.discard(message_id)
            self._skip_merge_timestamps.pop(message_id, None)
            src_ids = self._synthetic_source_ids.pop(message_id, set())
            epoch = self._synthetic_epochs.pop(message_id, 0)
            return src_ids, epoch

    def is_synthetic_id(self, message_id: str) -> bool:
        return bool(message_id) and message_id in self._skip_merge_ids

    async def mark_synthetic_id(self, message_id: str, source_ids: set[str] | None = None, epoch: int = 0) -> None:
        async with self._merge_lock:
            self._skip_merge_ids.add(message_id)
            self._skip_merge_timestamps[message_id] = time.monotonic()
            self._synthetic_source_ids[message_id] = set(source_ids or set())
            if epoch:
                self._synthetic_epochs[message_id] = epoch

    async def discard_synthetic_id(self, message_id: str) -> None:
        async with self._merge_lock:
            self._skip_merge_ids.discard(message_id)
            self._skip_merge_timestamps.pop(message_id, None)
            self._synthetic_source_ids.pop(message_id, None)
            self._synthetic_epochs.pop(message_id, None)

    async def collect_merge(
        self,
        event: Any,
        session_key: str,
        sender_id: str,
        text: str,
        components: list[Any],
        message_id: str,
        is_trigger: bool,
        keyword_woken: bool = False,
    ) -> str:
        p = self.plugin
        key = self.merge_key(session_key, sender_id)
        now = time.monotonic()
        idle = self.dynamic_idle_seconds(text, keyword_woken=keyword_woken)

        async with self._merge_lock:
            existing_ms = self._merges.get(key)
        if existing_ms is None and (not is_trigger or (idle is not None and idle <= 0)):
            return ""

        pre_epoch = existing_ms.epoch if existing_ms is not None else self.next_epoch()
        localized_components, created_paths = await self.media.localize_components(
            components,
            enabled=p.merge_localize_images,
            session_key=key,
            epoch=pre_epoch,
        )

        action = ""
        async with self._merge_lock:
            ms = self._merges.get(key)
            if ms is not None:
                if ms.epoch != pre_epoch and created_paths:
                    self.media.reassign_epoch(key, pre_epoch, ms.epoch, created_paths)
                ms.parts.append(text)
                ms.component_parts.append(list(localized_components))
                ms.message_ids.append(message_id)
                ms.last_ts = now
                if ms.origin_event is None:
                    ms.origin_event = event
                if idle is not None:
                    ms.idle_seconds = idle
                if p.debounce_max_parts and len(ms.parts) >= p.debounce_max_parts:
                    ms.idle_seconds = 0.0
                action = "appended"
            elif is_trigger and not (idle is not None and idle <= 0):
                # 原批在图片下载期间已放行：必须分配全新 epoch，避免旧批清理误删本批图片。
                batch_epoch = pre_epoch if existing_ms is None else self.next_epoch()
                if batch_epoch != pre_epoch and created_paths:
                    self.media.reassign_epoch(key, pre_epoch, batch_epoch, created_paths)
                self._merges[key] = MergeSession(
                    session_key=session_key,
                    sender_id=sender_id,
                    parts=[text],
                    component_parts=[list(localized_components)],
                    message_ids=[message_id],
                    start_ts=now,
                    last_ts=now,
                    origin_event=event,
                    idle_seconds=idle,
                    epoch=batch_epoch,
                )
                monitor = self._merge_monitors.get(key)
                if monitor is None or monitor.done():
                    self._merge_monitors[key] = asyncio.create_task(self.monitor_merge(key))
                action = "started"

        if not action and created_paths:
            await self.media.cleanup_batch(key, pre_epoch)
        return action

    async def monitor_merge(self, key: str) -> None:
        p = self.plugin
        try:
            while True:
                async with self._merge_lock:
                    ms = self._merges.get(key)
                    if not ms:
                        return
                    now = time.monotonic()
                    idle = float(p.merge_idle_seconds if ms.idle_seconds is None else ms.idle_seconds)
                    max_total = float(p.merge_max_total_seconds)
                    remain_max = max_total - (now - ms.start_ts)
                    remain_idle = idle - (now - ms.last_ts)
                    is_typing = self.guard.is_typing(key, now)

                if remain_max <= 0:
                    await self.release_merge(key, reason="max_total")
                    return
                if is_typing:
                    await asyncio.sleep(0.5)
                    continue
                if idle <= 0 or remain_idle <= 0:
                    await self.release_merge(key, reason="idle")
                    return
                await asyncio.sleep(max(0.05, min(remain_idle, remain_max)))
        except asyncio.CancelledError:
            pass
        finally:
            async with self._merge_lock:
                if self._merge_monitors.get(key) is asyncio.current_task():
                    self._merge_monitors.pop(key, None)

    async def release_merge(self, key: str, reason: str = "") -> None:
        p = self.plugin
        async with self._merge_lock:
            ms = self._merges.pop(key, None)
            task = self._merge_monitors.pop(key, None)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if not ms or not (any(part.strip() for part in ms.parts) or any(cp for cp in ms.component_parts)):
            return
        full_text = p.merge_join_separator.join(part for part in ms.parts if part).strip()
        logger.info(
            "[%s] merge release (%s): key=%s parts=%d len=%d",
            DISPLAY_NAME,
            reason,
            key,
            len(ms.parts),
            len(full_text),
        )
        origin = ms.origin_event
        if origin is None:
            return
        now = time.monotonic()
        if self.guard.guard_scope_applies(origin) and self.guard.should_check_user(ms.sender_id):
            should_block, should_send_meme = await self.guard.record_and_decide(
                ms.session_key, ms.sender_id, now
            )
            if should_block:
                await self.guard.apply_guard_block(origin, ms.session_key, ms.sender_id, should_send_meme)
                return
        try:
            await self.send_merged_event(ms, full_text)
        except Exception as exc:
            logger.error("[%s] failed to release merge: %s\n%s", DISPLAY_NAME, exc, traceback.format_exc())
            await self.notify_merge_failure(origin)

    async def notify_merge_failure(self, event: Any) -> None:
        try:
            chain = MessageChain().message(MERGE_FAILURE_MESSAGE)
            if is_qq_official(event) and getattr(self.plugin, "qq_official_content_mode", True):
                apply_content_mode(chain, True)
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            async with self.send_order.one_shot(umo, event=event):
                if is_qq_official(event):
                    delivered = await self.send_observer.qq_official_send(event, chain)
                    if delivered is False:
                        logger.warning("[%s] QQ官方合并失败通知未发出: session=%s", DISPLAY_NAME, umo)
                else:
                    await event.send(chain)
        except Exception as exc:
            logger.warning("[%s] failed to send merge failure notice: %s", DISPLAY_NAME, exc)

    def build_merged_components(self, ms: MergeSession) -> list[Any]:
        """保留图文交错的原始组件顺序合并多段消息。"""
        merged: list[Any] = []
        has_text = False
        for index, text in enumerate(ms.parts):
            source_components = ms.component_parts[index] if index < len(ms.component_parts) else []
            has_plain = any(isinstance(c, Plain) for c in source_components)
            if source_components and has_plain:
                plain_indices = [i for i, c in enumerate(source_components) if isinstance(c, Plain)]
                # 单段消息内若没有图文穿插只合并连续 Plain；有穿插时各自保留文本位置。
                if len(plain_indices) == 1:
                    inserted = False
                    text_val = f"{self.plugin.merge_join_separator if has_text else ''}{text}" if text else ""
                    for comp in source_components:
                        if isinstance(comp, Plain):
                            if text_val and not inserted:
                                merged.append(Plain(text_val))
                                inserted = True
                        else:
                            merged.append(comp)
                else:
                    first_plain = True
                    for comp in source_components:
                        if isinstance(comp, Plain):
                            t = str(getattr(comp, "text", "") or "")
                            if first_plain and has_text and t:
                                t = f"{self.plugin.merge_join_separator}{t}"
                            if t:
                                merged.append(Plain(t))
                            first_plain = False
                        else:
                            merged.append(comp)
                if text:
                    has_text = True
            else:
                if has_text and text:
                    merged.append(Plain(f"{self.plugin.merge_join_separator}{text}"))
                elif text:
                    merged.append(Plain(text))
                for comp in source_components:
                    if not isinstance(comp, Plain):
                        merged.append(comp)
                if text:
                    has_text = True
        return merged

    async def send_merged_event(self, ms: MergeSession, full_text: str) -> None:
        origin = ms.origin_event
        if origin is None:
            return
        release_text = full_text
        new_components = self.build_merged_components(ms)
        if is_qq_official(origin):
            new_components = sanitize_outbound_components(new_components, origin)
        self_id = str(origin.get_self_id() or "")
        has_wake_component = any(
            (isinstance(c, At) and str(getattr(c, "qq", "")) == self_id)
            or (isinstance(c, Reply) and str(getattr(c, "sender_id", "")) == self_id)
            for c in new_components
        )
        if origin.get_group_id() and not has_wake_component:
            wake_prefix = self.guard.effective_wake_prefix(origin)
            if wake_prefix and not release_text.startswith(wake_prefix):
                release_text = f"{wake_prefix}{release_text}"
                new_components.insert(0, Plain(wake_prefix))
        if not new_components and release_text:
            new_components.append(Plain(release_text))

        type_val = "FriendMessage"
        try:
            t = getattr(getattr(origin, "message_obj", None), "type", None)
            if t is not None:
                type_val = str(getattr(t, "value", t) or type_val)
        except Exception:
            pass
        session_id = (
            getattr(origin, "session_id", None)
            or getattr(getattr(origin, "message_obj", None), "session_id", "")
            or ""
        )
        sender = getattr(getattr(origin, "message_obj", None), "sender", None)
        platform = ""
        try:
            platform = origin.get_platform_id() or ""
        except Exception:
            pass
        if not platform:
            try:
                platform = origin.get_platform_name() or ""
            except Exception:
                pass

        qq_official_origin = False
        raw_message_origin = None
        origin_message_id = ""
        if is_qq_official(origin):
            mo = getattr(origin, "message_obj", None)
            raw_message_origin = getattr(mo, "raw_message", None)
            origin_message_id = str(getattr(mo, "message_id", "") or "")
            qq_official_origin = raw_message_origin is not None
            if not qq_official_origin:
                logger.warning("[%s] QQ官方合并事件缺少原始消息对象，回复可能无法送达: session=%s", DISPLAY_NAME, session_id)

        source_inbox_ids = list(origin.get_extra("nuoxiaomiao_inbox_ids") or [])

        for attempt in range(1, MERGED_EVENT_MAX_ATTEMPTS + 1):
            new_message = await StarTools.create_message(
                type=type_val,
                self_id=origin.get_self_id(),
                session_id=session_id,
                sender=sender,
                message=list(new_components),
                message_str=release_text,
                group_id=origin.get_group_id() or "",
                raw_message=raw_message_origin if qq_official_origin else None,
                message_id=origin_message_id if qq_official_origin else "",
            )
            mid = str(getattr(new_message, "message_id", None) or "")
            if mid:
                await self.mark_synthetic_id(
                    mid,
                    {m for m in ms.message_ids if m},
                    epoch=ms.epoch,
                )
            try:
                adapter = self._platform_inst_by_id(platform)
                if adapter is not None and callable(getattr(adapter, "create_event", None)) and callable(getattr(adapter, "commit_event", None)):
                    synthetic_event = adapter.create_event(new_message)
                    synthetic_event.is_wake = True
                    synthetic_event.is_at_or_wake_command = True
                    synthetic_event.set_extra(SYNTHETIC_EVENT_EXTRA, True)
                    synthetic_event.set_extra(
                        "nuoxiaomiao_merged_source_ids",
                        sorted(m for m in ms.message_ids if m),
                    )
                    if source_inbox_ids:
                        synthetic_event.set_extra("nuoxiaomiao_inbox_ids", source_inbox_ids)
                    if ms.epoch:
                        synthetic_event.set_extra("nuoxiaomiao_merge_epoch", ms.epoch)
                    adapter.commit_event(synthetic_event)
                    committed = True
                else:
                    committed = False
                if not committed:
                    await StarTools.create_event(abm=new_message, platform=platform, is_wake=True)
                break
            except Exception:
                if mid:
                    await self.discard_synthetic_id(mid)
                if attempt >= MERGED_EVENT_MAX_ATTEMPTS:
                    raise
                logger.warning(
                    "[%s] merged event submission failed; retrying (%d/%d)",
                    DISPLAY_NAME,
                    attempt,
                    MERGED_EVENT_MAX_ATTEMPTS,
                )
                await asyncio.sleep(MERGED_EVENT_RETRY_DELAY_SECONDS * attempt)

    async def prune_expired(self, now: float) -> None:
        async with self._merge_lock:
            expired_ids = {
                mid for mid, ts in self._skip_merge_timestamps.items() if now - ts > SYNTHETIC_ID_TTL_SECONDS
            }
            self._skip_merge_ids.difference_update(expired_ids)
            for mid in expired_ids:
                self._skip_merge_timestamps.pop(mid, None)
                self._synthetic_source_ids.pop(mid, None)
                self._synthetic_epochs.pop(mid, None)

    async def terminate(self) -> None:
        async with self._merge_lock:
            tasks = list(self._merge_monitors.values())
            self._merge_monitors.clear()
            self._merges.clear()
            self._skip_merge_ids.clear()
            self._skip_merge_timestamps.clear()
            self._synthetic_source_ids.clear()
            self._synthetic_epochs.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
