"""Inbound moderation: blocklist, spam guard, meme fallback, typing notice, poke."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import random
import tempfile
import time
import unicodedata
from typing import Any, Callable

from astrbot.api import logger
from astrbot.api.message_components import Image
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from PIL import Image as PILImage
except Exception:  # pragma: no cover
    PILImage = None  # type: ignore[assignment]

if __package__ and "." in __package__:
    from ..infra.constants import (
        DEFAULT_FORCE_WAKE_PREFIX,
        DISPLAY_NAME,
        IMAGE_SUFFIXES,
        RunState,
    )
    from ..infra.runtime import BoundedTTLCache, extract_components, group_scope_allows
    from ..qq_official import is_qq_official
else:
    from infra.constants import (
        DEFAULT_FORCE_WAKE_PREFIX,
        DISPLAY_NAME,
        IMAGE_SUFFIXES,
        RunState,
    )
    from infra.runtime import BoundedTTLCache, extract_components, group_scope_allows
    from qq_official import is_qq_official


class MemeService:
    """Meme tag lookup, static-to-GIF conversion, and one-shot delivery."""

    def __init__(
        self,
        plugin: Any,
        send_order: Any,
        qq_send_fn: Callable[[Any, Any], Any],
    ) -> None:
        self.plugin = plugin
        self.send_order = send_order
        self._qq_send = qq_send_fn
        self._meme_cfg_cache: tuple[Path, float, bool] | None = None
        self._meme_cfg_absent_ts: float = 0.0

    def reset_cache(self) -> None:
        self._meme_cfg_cache = None
        self._meme_cfg_absent_ts = 0.0

    async def send_meme(self, event: Any) -> None:
        if not self.plugin.meme_tags:
            return
        meme_file = await asyncio.to_thread(self.select_meme_file)
        if not meme_file:
            logger.warning(
                "[%s] no meme found in meme_manager categories: %s",
                DISPLAY_NAME,
                ", ".join(self.plugin.meme_tags) if self.plugin.meme_tags else "(empty)",
            )
            return
        send_file, should_cleanup = await asyncio.to_thread(self.prepare_meme_file, meme_file)
        chain = MessageChain([Image.fromFileSystem(str(send_file))])
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            async with self.send_order.one_shot(umo, event=event):
                if is_qq_official(event):
                    delivered = await self._qq_send(event, chain)
                    if delivered is False:
                        logger.warning("[%s] QQ官方表情发送失败（平台未确认）：session=%s", DISPLAY_NAME, umo)
                else:
                    await event.send(chain)
        except Exception as exc:
            logger.warning("[%s] failed to send meme: %s", DISPLAY_NAME, exc)
        finally:
            if should_cleanup:
                await asyncio.to_thread(self.cleanup_temp_file, send_file)

    def select_meme_file(self) -> Path | None:
        if not self.plugin.meme_tags:
            return None
        tags = list(self.plugin.meme_tags)
        random.shuffle(tags)
        for root in self.meme_roots():
            if not root.is_dir():
                continue
            for tag in tags:
                category_dir = root / tag
                if not category_dir.is_dir():
                    continue
                files = [
                    path
                    for path in category_dir.iterdir()
                    if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
                ]
                if files:
                    return random.choice(files)
        return None

    @staticmethod
    def meme_roots() -> list[Path]:
        roots: list[Path] = []
        try:
            roots.append(StarTools.get_data_dir("meme_manager") / "memes")
        except Exception as exc:
            logger.debug("[%s] cannot resolve meme_manager data dir: %s", DISPLAY_NAME, exc)
            try:
                roots.append(Path(get_astrbot_data_path()) / "plugin_data" / "meme_manager" / "memes")
            except Exception:  # noqa: BLE001
                pass
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root)
            if key not in seen:
                seen.add(key)
                unique.append(root)
        return unique

    def prepare_meme_file(self, meme_file: Path) -> tuple[Path, bool]:
        if not self._meme_manager_convert_static_to_gif_enabled():
            return meme_file, False
        converted = self._convert_static_to_gif(meme_file)
        return converted, converted != meme_file

    def _meme_manager_convert_static_to_gif_enabled(self) -> bool:
        cached = self._meme_cfg_cache
        if cached is not None:
            cached_path, cached_mtime, cached_value = cached
            try:
                for config_path in self._meme_manager_config_paths():
                    if config_path.is_file() and config_path.samefile(cached_path):
                        if config_path.stat().st_mtime == cached_mtime:
                            return cached_value
                        break
            except OSError:
                pass
        elif time.monotonic() - self._meme_cfg_absent_ts < 60.0:
            return False

        found = False
        for config_path in self._meme_manager_config_paths():
            if not config_path.is_file():
                continue
            found = True
            try:
                data = json.loads(config_path.read_text(encoding="utf-8-sig"))
                value = bool(data.get("convert_static_to_gif", False))
                try:
                    self._meme_cfg_cache = (config_path, config_path.stat().st_mtime, value)
                except OSError:
                    pass
                return value
            except Exception as exc:
                logger.debug("[%s] failed to read meme_manager config %s: %s", DISPLAY_NAME, config_path, exc)
                continue
        if not found:
            self._meme_cfg_absent_ts = time.monotonic()
        return False

    @staticmethod
    def _meme_manager_config_paths() -> list[Path]:
        paths: list[Path] = []
        try:
            paths.append(Path(get_astrbot_data_path()) / "config" / "meme_manager_config.json")
        except Exception as exc:
            logger.debug("[%s] cannot resolve AstrBot data path: %s", DISPLAY_NAME, exc)
        unique: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            key = str(path)
            if key not in seen:
                seen.add(key)
                unique.append(path)
        return unique

    @staticmethod
    def _convert_static_to_gif(image_path: Path) -> Path:
        if image_path.suffix.lower() == ".gif":
            return image_path
        if PILImage is None:
            logger.warning("[%s] PIL is unavailable, send original meme", DISPLAY_NAME)
            return image_path
        temp_path: Path | None = None
        try:
            with PILImage.open(image_path) as img:
                if img.format == "GIF" and getattr(img, "is_animated", False):
                    return image_path
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    rgba = img.convert("RGBA")
                    bg = PILImage.new("RGB", rgba.size, (255, 255, 255))
                    bg.paste(rgba, mask=rgba.split()[3])
                    rgb = bg
                elif img.mode != "RGB":
                    rgb = img.convert("RGB")
                else:
                    rgb = img.copy()
            fd, temp_name = tempfile.mkstemp(prefix="nuoxiaomiao_meme_", suffix=".gif")
            import os

            os.close(fd)
            temp_path = Path(temp_name)
            rgb.save(temp_path, format="GIF", save_all=False, optimize=True)
            return temp_path
        except Exception as exc:
            logger.warning("[%s] failed to convert static meme to gif (%s): %s", DISPLAY_NAME, image_path, exc)
            if temp_path is not None:
                MemeService.cleanup_temp_file(temp_path)
            return image_path

    @staticmethod
    def cleanup_temp_file(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except Exception as exc:
            logger.debug("[%s] failed to remove temp meme file %s: %s", DISPLAY_NAME, path, exc)


class InboundGuard:
    """Spam guard, typing notice tracking, poke response, and trigger/command helpers."""

    def __init__(
        self,
        plugin: Any,
        meme_service: MemeService,
        *,
        display_group_name_fn: Callable[[Any], str],
        qq_quote_is_bot_fn: Callable[[Any], bool],
    ) -> None:
        self.plugin = plugin
        self.meme = meme_service
        self._display_group_name = display_group_name_fn
        self._qq_quote_is_bot = qq_quote_is_bot_fn
        self._lock = asyncio.Lock()
        self._states: dict[str, RunState] = {}
        self._last_meme_at: dict[tuple[str, str], float] = {}
        self._poke_cooldown_ts = BoundedTTLCache(max_size=1024, ttl_seconds=60.0)
        self._poke_lock = asyncio.Lock()
        self._typing_until: dict[str, float] = {}

    def clear(self) -> None:
        self._states.clear()
        self._last_meme_at.clear()
        self._poke_cooldown_ts.clear()
        self._typing_until.clear()

    @staticmethod
    def is_typing_notice(event: Any) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return (
            isinstance(raw, dict)
            and raw.get("post_type") == "notice"
            and raw.get("notice_type") == "notify"
            and raw.get("sub_type") == "input_status"
        )

    @staticmethod
    def is_poke_event(event: Any) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return (
            isinstance(raw, dict)
            and raw.get("post_type") == "notice"
            and raw.get("notice_type") == "notify"
            and raw.get("sub_type") == "poke"
        )

    @staticmethod
    def is_self_poked(event: Any) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return False
        try:
            target_id = str(raw.get("target_id", "") or "")
            self_id = str(event.get_self_id() or "")
            return bool(target_id) and target_id == self_id
        except Exception:
            return False

    @staticmethod
    def close_event_span(event: Any, note: str) -> None:
        try:
            trace = getattr(event, "trace", None)
            record = getattr(trace, "record", None)
            if callable(record):
                record("astr_agent_complete", stats={}, resp=note)
        except Exception:
            pass

    @staticmethod
    def block_default_llm(event: Any) -> None:
        # ProcessStage: not event.call_llm 才走默认 LLM → True 表示禁止
        event.should_call_llm(True)

    def event_group_allowed(self, event: Any, whitelist: set[str], blacklist: set[str]) -> bool:
        try:
            group_id = str(event.get_group_id() or "").strip()
            if not group_id:
                return True
            group_name = self._display_group_name(event)
            return group_scope_allows(
                group_id,
                group_name,
                whitelist,
                blacklist,
                str(event.get_platform_id() or ""),
            )
        except Exception:
            return True

    def guard_scope_applies(self, event: Any) -> bool:
        p = self.plugin
        try:
            if event.is_private_chat():
                return p.include_private
            if event.get_group_id():
                if not p.include_group:
                    return False
                return self.event_group_allowed(event, p.guard_group_whitelist, p.guard_group_blacklist)
        except Exception:
            return False
        return False

    def should_check_user(self, sender_id: str) -> bool:
        p = self.plugin
        if sender_id in p.user_whitelist:
            return False
        if p.user_blacklist:
            return sender_id in p.user_blacklist
        return True

    def is_dialogue_trigger(self, event: Any) -> bool:
        try:
            if event.is_private_chat():
                return True
        except Exception:
            return False
        if event.get_group_id():
            if bool(event.is_at_or_wake_command):
                return True
            if (
                is_qq_official(event)
                and bool(getattr(self.plugin, "qq_official_enabled", True))
                and bool(getattr(self.plugin, "qq_official_quote_trigger", True))
                and self._qq_quote_is_bot(event)
            ):
                return True
        return False

    def wake_prefixes(self, event: Any) -> list[str]:
        config: Any = None
        try:
            config = self.plugin.context.get_config(umo=event.unified_msg_origin)
        except TypeError:
            try:
                config = self.plugin.context.get_config()
            except Exception:
                config = None
        except Exception:
            config = None
        raw = config.get("wake_prefix", []) if hasattr(config, "get") else []
        if isinstance(raw, str):
            raw = [raw]
        prefixes = [str(prefix) for prefix in (raw or []) if str(prefix)]
        return sorted(prefixes, key=len, reverse=True)

    def effective_wake_prefix(self, event: Any) -> str:
        active_prefixes = self.wake_prefixes(event)
        if active_prefixes:
            return active_prefixes[0]
        return DEFAULT_FORCE_WAKE_PREFIX

    @staticmethod
    def _is_symbol_prefix(prefix: str) -> bool:
        return bool(prefix) and all(
            unicodedata.category(ch)[0] in {"P", "S"} for ch in prefix
        )

    def is_command_event(self, event: Any) -> bool:
        get_extra = getattr(event, "get_extra", None)
        if callable(get_extra):
            try:
                handlers = get_extra("activated_handlers", None)
            except Exception:
                handlers = None
            if handlers is not None:
                for handler in handlers:
                    for event_filter in getattr(handler, "event_filters", []) or []:
                        if type(event_filter).__name__ in {"CommandFilter", "CommandGroupFilter"}:
                            return True
                return False
        raw_text = "".join(
            str(getattr(component, "text", "") or "")
            for component in extract_components(event)
            if type(component).__name__ == "Plain"
        ).strip()
        if not raw_text:
            return False
        if raw_text.startswith("/") and bool(raw_text[1:].strip()):
            return True
        return any(
            self._is_symbol_prefix(prefix)
            and raw_text.startswith(prefix)
            and bool(raw_text[len(prefix) :].strip())
            for prefix in self.wake_prefixes(event)
        )

    async def record_and_decide(self, session_key: str, sender_id: str, now: float) -> tuple[bool, bool]:
        p = self.plugin
        async with self._lock:
            state = self._states.get(session_key)
            if state is None or state.sender_id != sender_id or now - state.last_ts > p.window_seconds:
                state = RunState(sender_id=sender_id, count=1, last_ts=now)
            else:
                state.count += 1
                state.last_ts = now
            should_block = state.count >= p.trigger_rounds
            if should_block:
                state.count = p.trigger_rounds
            self._states[session_key] = state
            if not should_block:
                return False, False
            cooldown_key = (session_key, sender_id)
            last_meme_ts = self._last_meme_at.get(cooldown_key, 0.0)
            should_send_meme = p.meme_cooldown_seconds <= 0 or now - last_meme_ts >= p.meme_cooldown_seconds
            if should_send_meme:
                self._last_meme_at[cooldown_key] = now
            return True, should_send_meme

    async def apply_guard_block(
        self, event: Any, session_key: str, sender_id: str, should_send_meme: bool
    ) -> None:
        event.set_extra("nuoxiaomiao_blocked", True)
        self.block_default_llm(event)
        if should_send_meme:
            await self.meme.send_meme(event)
        event.stop_event()
        self.close_event_span(event, "［连刷拦截］本轮对话被拦截丢弃")
        logger.info(
            "[%s] dropped dialogue: session=%s sender=%s count>=%d",
            DISPLAY_NAME,
            session_key,
            sender_id,
            self.plugin.trigger_rounds,
        )

    async def handle_typing_notice(self, event: Any, merger: Any) -> None:
        p = self.plugin
        try:
            if not p.enabled or not p.debounce_typing_enabled:
                return
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if not isinstance(raw, dict) or raw.get("group_id"):
                return
            sender_id = str(event.get_sender_id() or "").strip()
            if not sender_id:
                return
            key = f"{event.unified_msg_origin}:{sender_id}"
            try:
                event_type = int(raw.get("event_type", 1))
            except (TypeError, ValueError):
                event_type = 1
            if event_type == 2:
                self._typing_until.pop(key, None)
                if await merger.has_active_merge(key):
                    logger.info("[%s] 对方停止输入，立即放行: key=%s", DISPLAY_NAME, key)
                    await merger.release_merge(key, reason="typing_stopped")
                return
            now = time.monotonic()
            was_typing = self._typing_until.get(key, 0.0) > now
            self._typing_until[key] = now + p.debounce_typing_hold_seconds
            if not was_typing:
                logger.info("[%s] 检测到对方正在输入，先等你说完: key=%s", DISPLAY_NAME, key)
        except Exception as exc:
            logger.debug("[%s] 处理输入状态失败: %s", DISPLAY_NAME, exc)

    def is_typing(self, key: str, now: float) -> bool:
        return self._typing_until.get(key, 0.0) > now

    async def handle_poke_event(self, event: Any) -> Any:
        p = self.plugin
        if not self.is_self_poked(event) or not p.enabled:
            return None
        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id and sender_id in p.blocked_users:
            event.stop_event()
            self.close_event_span(event, "［屏蔽名单］戳一戳被丢弃")
            logger.info("[%s] blocked user poke dropped: session=%s sender=%s", DISPLAY_NAME, str(event.unified_msg_origin), sender_id)
            return None
        if not p.poke_reply_enabled:
            return None
        if not self.event_group_allowed(event, p.poke_group_whitelist, p.poke_group_blacklist):
            return None
        group_id = event.get_group_id()
        if group_id:
            cooldown_key = f"{event.get_platform_id()}:{group_id}"
            now = time.monotonic()
            async with self._poke_lock:
                last = float(self._poke_cooldown_ts.get(cooldown_key, 0.0, now=now) or 0.0)
                if last and now - last < p.poke_reply_cooldown_seconds:
                    event.stop_event()
                    logger.debug("[%s] poke reply cooldown dropped: group=%s", DISPLAY_NAME, group_id)
                    return None
                self._poke_cooldown_ts.set(cooldown_key, now, now=now)
        username = await self._get_poke_username(event)
        prompt = self._format_poke_prompt(username, sender_id)
        conversation = await self._get_or_create_conversation(event)
        logger.info("[%s] poke reply triggered: session=%s sender=%s", DISPLAY_NAME, str(event.unified_msg_origin), sender_id)
        return event.request_llm(prompt=prompt, conversation=conversation)

    async def _get_poke_username(self, event: Any) -> str:
        group_id = event.get_group_id()
        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            return str(event.get_sender_name() or "").strip() or sender_id
        bot = getattr(event, "bot", None)
        info: dict[str, Any] = {}
        if bot is not None and group_id and str(group_id).isdigit() and str(sender_id).isdigit():
            try:
                info = await bot.get_group_member_info(group_id=int(group_id), user_id=int(sender_id)) or {}
            except Exception:
                info = {}
        if not info and bot is not None and str(sender_id).isdigit():
            try:
                info = await bot.get_stranger_info(user_id=int(sender_id)) or {}
            except Exception:
                info = {}
        name = info.get("card") or info.get("nickname") or info.get("nick") or event.get_sender_name()
        return str(name or sender_id)

    def _format_poke_prompt(self, username: str, user_id: str) -> str:
        template = self.plugin.poke_reply_prompt or "{username} 戳了你一下。"
        try:
            return template.format(username=username or user_id, user_id=user_id)
        except Exception:
            return f"{template}\n{username or user_id} 戳了你一下。"

    async def _get_or_create_conversation(self, event: Any) -> Any:
        umo = event.unified_msg_origin
        conv_mgr = getattr(self.plugin.context, "conversation_manager", None)
        if conv_mgr is None:
            return None
        try:
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                cid = await conv_mgr.new_conversation(umo, event.get_platform_id())
            return await conv_mgr.get_conversation(umo, cid)
        except Exception as exc:
            logger.warning("[%s] 获取或创建对话失败: %s", DISPLAY_NAME, exc)
            return None

    async def prune_expired(self, now: float) -> None:
        p = self.plugin
        async with self._lock:
            self._states = {
                k: st for k, st in self._states.items() if now - st.last_ts <= p.window_seconds
            }
            if p.meme_cooldown_seconds <= 0:
                self._last_meme_at.clear()
            else:
                self._last_meme_at = {
                    k: ts for k, ts in self._last_meme_at.items() if now - ts <= p.meme_cooldown_seconds
                }
        self._typing_until = {k: dl for k, dl in self._typing_until.items() if dl > now}
        self._poke_cooldown_ts.prune(now=now)
