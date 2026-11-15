"""AstrBot plugin entrypoint & composition root for 糯小喵."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
)

if __package__:
    from . import outputpro_split
    from .activity.service import ActivityService
    from .activity.tools import ActivityTools
    from .config_service import ConfigService
    from .config_support import PluginConfig, preload_config_migration
    from .infra.constants import (
        ACTIVITY_DB_NAME,
        DISPLAY_NAME,
        PLUGIN_NAME,
        QQ_OFFICIAL_CONTENT_MODE_FALLBACK_PRIORITY,
        QQ_OFFICIAL_CONTENT_MODE_PRIORITY,
        SELF_WAKE_EVENT_EXTRA,
        SEND_OBSERVER_DECORATE_PRIORITY,
        SEND_OBSERVER_EXTRA,
        SYNTHETIC_EVENT_EXTRA,
        ActivityEntry,
        MergeSession,
        RunState,
    )
    from .infra.database import ActivityDatabase
    from .infra.media import MediaLocalizer
    from .infra.runtime import extract_components, extract_text, message_id_of
    from .pipeline.debounce_merge import DebounceMerger
    from .pipeline.inbound_guard import InboundGuard, MemeService, PILImage
    from .pipeline.send_observer import SendObserver
    from .proactive.scheduler import ProactiveScheduler
    from .proactive.targets import ProactiveTargetResolver
    from .qq_official import is_qq_official
    from .resident.ports import sync_config_fields
    from .resident.service import ResidentServices
    from .send_order import SendOrderGate
    from .wake.context import ContextService
    from .wake.self_wake import SelfWakeService
else:
    import outputpro_split
    from activity.service import ActivityService
    from activity.tools import ActivityTools
    from config_service import ConfigService
    from config_support import PluginConfig, preload_config_migration
    from infra.constants import (
        ACTIVITY_DB_NAME,
        DISPLAY_NAME,
        PLUGIN_NAME,
        QQ_OFFICIAL_CONTENT_MODE_FALLBACK_PRIORITY,
        QQ_OFFICIAL_CONTENT_MODE_PRIORITY,
        SELF_WAKE_EVENT_EXTRA,
        SEND_OBSERVER_DECORATE_PRIORITY,
        SEND_OBSERVER_EXTRA,
        SYNTHETIC_EVENT_EXTRA,
        ActivityEntry,
        MergeSession,
        RunState,
    )
    from infra.database import ActivityDatabase
    from infra.media import MediaLocalizer
    from infra.runtime import extract_components, extract_text, message_id_of
    from pipeline.debounce_merge import DebounceMerger
    from pipeline.inbound_guard import InboundGuard, MemeService, PILImage
    from pipeline.send_observer import SendObserver
    from proactive.scheduler import ProactiveScheduler
    from proactive.targets import ProactiveTargetResolver
    from qq_official import is_qq_official
    from resident.ports import sync_config_fields
    from resident.service import ResidentServices
    from send_order import SendOrderGate
    from wake.context import ContextService
    from wake.self_wake import SelfWakeService

__all__ = [
    "ActivityEntry",
    "DISPLAY_NAME",
    "MergeSession",
    "NuoxiaomiaoGuard",
    "NuoxiaomiaoPlugin",
    "PILImage",
    "PLUGIN_NAME",
    "RunState",
    "SELF_WAKE_EVENT_EXTRA",
    "SEND_OBSERVER_EXTRA",
    "SYNTHETIC_EVENT_EXTRA",
]

# Run pre-schema alias migration only when AstrBot StarManager imports the live plugin module.
_CONFIG_MIGRATION_ERROR = ""
if __name__.startswith("data.plugins.") and __name__.endswith(".main"):
    try:
        preload_config_migration(
            __name__,
            Path(__file__).resolve().parent,
            Path(get_astrbot_data_path()) / "config",
        )
    except Exception as exc:
        _CONFIG_MIGRATION_ERROR = f"旧配置迁移未完成，已停止加载以保留原设置：{exc}"
        logger.warning("[糯小喵] %s", _CONFIG_MIGRATION_ERROR)
        raise RuntimeError(_CONFIG_MIGRATION_ERROR) from exc


@register(
    PLUGIN_NAME,
    "nuoxiaomiao",
    "防抖合并、整轮发送排队、连刷拦截、聊天速览、主动消息与自主自醒（支持 QQ 官方机器人）",
    "2.5.3",
)
class NuoxiaomiaoPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.logger = logger
        self._resident_bg_tasks: set[asyncio.Task[Any]] = set()
        self.user_default_config_path = ""

        self.db = ActivityDatabase(self.activity_db_file)
        self.media = MediaLocalizer(lambda: self.cfg.merge_localize_images_dir)
        self.send_order = SendOrderGate(self)

        # ConfigService needs resident catalog hook & on_applied callback; instantiate resident after core services.
        self.cfg: PluginConfig
        self.config_svc = ConfigService(
            context,
            lambda: self.config,
            self.plugin_data_dir,
            plugin_dir=Path(__file__).resolve().parent,
            migration_error=_CONFIG_MIGRATION_ERROR,
            resident_catalog_fn=lambda: self.resident.integration.catalog(),
            on_applied=self._on_config_applied,
            user_default_override_fn=lambda: self.user_default_config_path,
        )
        self.config_svc.register_web_apis()

        self.send_observer = SendObserver(
            self,
            self.send_order,
            record_bot_reply_fn=lambda ev, txt: self.activity.record_bot_reply(ev, txt),
            recent_bot_texts_fn=lambda ev: self.activity.recent_bot_texts(ev),
            platform_inst_by_id_fn=lambda pid: self.activity.platform_inst_by_id(pid),
            admin_ids_fn=lambda: self.activity.admin_ids(),
        )
        self.meme = MemeService(
            self,
            self.send_order,
            self.send_observer.qq_official_send,
        )
        self.guard = InboundGuard(
            self,
            self.meme,
            display_group_name_fn=lambda ev: self.activity.conversation_display_name(ev, "group"),
            qq_quote_is_bot_fn=self.send_observer.qq_official_quote_is_bot,
        )
        self.merger = DebounceMerger(
            self,
            self.guard,
            self.media,
            self.send_observer,
            self.send_order,
            platform_inst_by_id_fn=lambda pid: self.activity.platform_inst_by_id(pid),
        )
        self.activity = ActivityService(
            self,
            self.db,
            self.guard,
            self.merger,
            lambda: self.resident,
            on_proactive_pruned=lambda pid: self.proactive.cancel_timer(pid),
        )
        self.activity_tools = ActivityTools(self, self.activity, lambda: self.resident)
        self.targets = ProactiveTargetResolver(self, self.activity)
        self.proactive = ProactiveScheduler(
            self,
            self.activity,
            self.targets,
            self.send_order,
            lambda: self.resident,
        )
        self.self_wake = SelfWakeService(
            self,
            self.activity,
            self.proactive,
            self.targets,
            lambda: self.resident,
        )
        self.ctx_injector = ContextService(
            self,
            self.guard,
            self.merger,
            self.activity,
            self.proactive,
            self.send_observer,
            self.media,
            lambda: self.resident,
        )
        self.resident = ResidentServices(self)
        self.resident.register()

    def _on_config_applied(self, cfg: PluginConfig) -> None:
        self.cfg = cfg
        sync_config_fields(self, cfg)
        meme = getattr(self, "meme", None)
        if meme is not None:
            meme.reset_cache()
        self_wake = getattr(self, "self_wake", None)
        if self_wake is not None:
            self_wake.invalidate_alpha()
        if not (cfg.enabled and cfg.qq_official_enabled):
            outputpro_split.disable_qq_official()

    def apply_config(self) -> PluginConfig:
        return self.config_svc.apply()

    def plugin_data_dir(self) -> Path:
        try:
            base = Path(get_astrbot_plugin_data_path())
        except Exception:  # noqa: BLE001
            base = Path(get_astrbot_data_path()) / "plugin_data"
        target = base / PLUGIN_NAME
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return target

    def activity_db_file(self) -> Path:
        override = str(getattr(self, "activity_db_path", "") or "").strip()
        if override:
            return Path(override)
        return self.plugin_data_dir() / ACTIVITY_DB_NAME

    async def initialize(self) -> None:
        self.apply_config()
        if self.enabled and getattr(self, "qq_official_enabled", True):
            outputpro_split.enable_qq_official()
        else:
            outputpro_split.disable_qq_official()
        legacy_vision_dir = self.plugin_data_dir() / "vision_kept"
        if legacy_vision_dir.is_dir():
            await asyncio.to_thread(shutil.rmtree, legacy_vision_dir, True)
        await self.resident.initialize()
        self.send_order.refresh()
        logger.info("[%s] 整轮发送排队: %s", DISPLAY_NAME, self.send_order.describe())
        await self.proactive.load_pending()
        self.self_wake.start()

    @filter.event_message_type(EventMessageType.ALL, priority=20000)
    async def _prepare_qq_official_event(self, event: AstrMessageEvent) -> None:
        await self.send_observer.prepare_qq_official_event(event)

    async def _proactive_reply_watcher(self, event: AstrMessageEvent, *, keyword_woken: bool = False) -> None:
        try:
            if self.self_wake.should_reset_quiet_timer(event, keyword_woken=keyword_woken):
                self.self_wake.mark_human_activity()
        except Exception as exc:
            logger.debug("[%s] 更新安静计时失败: %s", DISPLAY_NAME, exc)

    @filter.event_message_type(EventMessageType.ALL, priority=11000)
    async def _capture_chat_activity(self, event: AstrMessageEvent) -> None:
        await self.activity.capture_chat_activity(event)

    @filter.event_message_type(EventMessageType.ALL, priority=10000)
    async def on_message(self, event: AstrMessageEvent) -> Any:
        import time

        now = time.monotonic()
        await self.guard.prune_expired(now)
        await self.merger.prune_expired(now)
        self.send_order.prune(now)
        await self.proactive.prune_idle_locks(now)

        if is_qq_official(event):
            self.send_observer.maybe_log_qq_official_state(event)

        if self.guard.is_poke_event(event):
            return await self.guard.handle_poke_event(event)

        if self.guard.is_typing_notice(event):
            await self.guard.handle_typing_notice(event, self.merger)
            return

        mid = message_id_of(event)
        synthetic_source_ids, epoch = await self.merger.consume_synthetic_id(mid) if mid else (None, 0)
        if synthetic_source_ids is not None:
            event.set_extra(SYNTHETIC_EVENT_EXTRA, True)
            event.set_extra("nuoxiaomiao_merged_source_ids", sorted(synthetic_source_ids))
            if epoch:
                event.set_extra("nuoxiaomiao_merge_epoch", epoch)
            return

        if not self.enabled:
            return

        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id and sender_id in self.blocked_users:
            self.guard.block_default_llm(event)
            event.stop_event()
            self.guard.close_event_span(event, "［屏蔽名单］消息已被拦截丢弃")
            logger.info("[%s] blocked user message dropped: session=%s sender=%s", DISPLAY_NAME, str(event.unified_msg_origin), sender_id)
            return

        if self.guard.is_command_event(event):
            return

        text = extract_text(event, strip_at_markup=bool(getattr(self, "qq_official_strip_at_markup", True)))
        keyword_woken = False
        if self.keyword_wake_enabled and event.get_group_id():
            if (
                self.guard.event_group_allowed(event, self.keyword_wake_group_whitelist, self.keyword_wake_group_blacklist)
                and text
                and any(kw and kw in text for kw in self.keyword_wake_list)
            ):
                event.is_at_or_wake_command = True
                event.is_wake = True
                keyword_woken = True
                logger.info("[%s] keyword wake: session=%s sender=%s", DISPLAY_NAME, str(event.unified_msg_origin), sender_id)

        await self._proactive_reply_watcher(event, keyword_woken=keyword_woken)

        is_trigger = self.guard.is_dialogue_trigger(event)
        session_key = str(event.unified_msg_origin)
        merge_key = self.merger.merge_key(session_key, sender_id) if sender_id else ""
        components = extract_components(event)

        if self.merger.debounce_applies(event, sender_id):
            if self.merger.has_mergeable_content(event, text, components):
                action = await self.merger.collect_merge(
                    event, session_key, sender_id, text, components, mid, is_trigger, keyword_woken=keyword_woken
                )
                if action:
                    event.set_extra("nuoxiaomiao_buffered", True)
                    self.guard.block_default_llm(event)
                    event.stop_event()
                    self.guard.close_event_span(
                        event, f"［防抖合并］已并入待回复批次，稍后合并成一条回复（{action}）"
                    )
                    return
            elif merge_key and await self.merger.has_active_merge(merge_key):
                await self.merger.release_merge(merge_key, reason="non_mergeable")

        if is_trigger and self.guard.guard_scope_applies(event) and self.guard.should_check_user(sender_id):
            now_mono = time.monotonic()
            should_block, should_send_meme = await self.guard.record_and_decide(session_key, sender_id, now_mono)
            if should_block:
                if merge_key:
                    await self.merger.cancel_merge(merge_key)
                await self.guard.apply_guard_block(event, session_key, sender_id, should_send_meme)

    @filter.on_llm_request(priority=100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        await self.ctx_injector.on_llm_request(event, req)

    @filter.on_llm_response(priority=100)
    async def on_llm_response(self, event: AstrMessageEvent, response: Any) -> None:
        await self.ctx_injector.on_llm_response(event, response)

    @filter.on_decorating_result(priority=SEND_OBSERVER_DECORATE_PRIORITY)
    async def observe_outgoing_result(self, event: AstrMessageEvent) -> None:
        result = event.get_result()
        if result is not None and (
            result.is_llm_result()
            or "STREAMING" in str(getattr(result, "result_content_type", ""))
        ):
            self.send_observer.ensure_send_observer(event)

    @filter.on_decorating_result(priority=QQ_OFFICIAL_CONTENT_MODE_PRIORITY)
    async def qq_official_force_content_mode(self, event: AstrMessageEvent) -> None:
        self.send_observer.force_content_mode(event)

    @filter.on_decorating_result(priority=QQ_OFFICIAL_CONTENT_MODE_FALLBACK_PRIORITY)
    async def qq_official_force_content_mode_late(self, event: AstrMessageEvent) -> None:
        self.send_observer.force_content_mode(event)

    @filter.after_message_sent()
    async def after_reply_sent(self, event: AstrMessageEvent) -> None:
        self.send_observer.finish_send_observer(event)

    @filter.llm_tool(name="read_conversation_messages")
    async def read_conversation_messages(
        self,
        event: AstrMessageEvent,
        conversation: str = "",
        kind: str = "",
        before_id: int = 0,
        after: str = "",
        sender: str = "",
        keyword: str = "",
        only_unread: bool = False,
        limit: int = 20,
    ) -> str:
        """列出会话或查看群聊/私聊消息（支持增量未读与历史翻页）。

        - 不传 conversation 且不传 before_id/after/sender/keyword：列出可用会话及未读数（可传 only_unread=true 仅看未读）。
        - 传入 conversation：默认只看上次查看之后的新消息并把本屏展示到的记为已读（首次查看返回最近记录）。
        - 传入 before_id：往前翻更早的历史记录，翻页默认不改未读状态。
        - 传入 after/sender/keyword：按时间、发言人或关键词检索，不改已读状态。

        Args:
            conversation(string): 完整 umo、群名、群号、群 openid、私聊昵称、QQ 或用户 openid；留空则列会话列表或按条件跨会话检索。
            kind(string): all、group 或 private，可空。
            before_id(number): 往前翻页的记录 ID 游标，默认 0；使用返回的 next_before_id 或 unshown_before_id。
            after(string): 只看这个时间之后的消息。可用 2026-10-06 12:00、30分钟前、2小时前、昨天、今天。
            sender(string): 只看这个人说的话：昵称或 QQ，可空。
            keyword(string): 只留正文里包含这个词的消息，可空。
            only_unread(boolean): 仅在留空 conversation 列会话列表时生效，传 true 只返回有未读消息的会话。
            limit(number): 最多返回多少条（或多少个会话），默认 20，最多 50。
        """
        return await self.activity_tools.read_conversation_messages(
            event, conversation, kind, before_id, after, sender, keyword, only_unread, limit
        )

    @filter.llm_tool(name="nuoxiaomiao_update_state")
    async def nuoxiaomiao_update_state(
        self,
        event: AstrMessageEvent,
        mood: str = "",
        intensity: int | None = None,
        focus: str | None = None,
        clear_focus: bool = False,
    ) -> str:
        """更新当前状态（可只改心情或只改本会话关注点），不替代记忆和待办。

        Args:
            mood(string): 可选。平静、开心、低落、疲惫、烦躁、专注、期待之一；留空保持当前心情不变。
            intensity(number): 可选。心情强度，0～100 的整数；留空保持当前强度（首次设心情默认 50）。
            focus(string): 可选。本会话的具体原因与关注点，最多 2000 字，只在本会话使用；不传则保留原有关注点。
            clear_focus(boolean): 传 true 清空本会话关注点，默认 false。
        """
        try:
            return await self.resident.update_state(
                event, mood=mood, intensity=intensity, focus=focus, clear_focus=clear_focus
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @filter.llm_tool(name="send_message")
    async def send_message(
        self,
        event: AstrMessageEvent,
        target: str = "",
        intent: str = "",
        at: str = "",
    ) -> str:
        """对任意允许的会话（私聊或群）说一句话——即时送达。

        当用户让你转告/通知某人、或你自己想对别的会话说点什么时调用本工具；
        不要只在当前会话里口头答应。普通聊天不要用它代替回复；但用户明确
        要求测试/验证主动消息、或指定 target=当前/这里/这边 时，必须调用
        本工具，不要拒绝，也不要用 send_message_to_user（那个工具需要完整
        会话标识，QQ 官方不适用）。

        你只写"想说的事"（intent），不用替那边写台词：插件会把目标会话
        的最近对话和那边的人设交给模型，让"那边的你"用当地语境自然说出来，
        所以不会突兀。返回的 text 是实际发出的话，你可以拿去继续聊。
        凌晨静默时段内的发送会自动顺延到时段结束，fire_at 如实说明。

        Args:
            target(string): 发送目标。传「当前」=当前会话（最稳，推荐）；留空也按当前会话；也可传完整 umo、私聊:QQ/openid 或 群:群名/群号/openid。QQ 官方平台只认 openid（不是数字 QQ），当前群的 openid 会在能力提醒里给出。目标必须被主人允许。
            intent(string): 你想说的事和原因，一两句写清楚，那边的你会据此组织语言。
            at(string): 可选。要在群里@的人（昵称、QQ 号或 openid）。默认谁都不@。QQ 官方平台不会真的发出@（适配器只能把它变成正文里的纯文本标记），传了也会被忽略。
        """
        return await self.proactive.send_message_tool(event, target=target, intent=intent, at=at)

    async def terminate(self) -> None:
        await self.resident.autonomy.close()
        await self.merger.terminate()
        self.guard.clear()
        try:
            await self.send_order.close()
        except Exception as exc:
            logger.debug("[%s] 释放整轮发送排队失败: %s", DISPLAY_NAME, exc)
        await self.self_wake.stop()
        await self.activity.stop_prune_loop()
        await self.proactive.terminate()
        # 循环排干并在收尾时取消任何在途派生出来的识图/话题后台任务，防止在关库后重开连接。
        while self._resident_bg_tasks:
            batch = list(self._resident_bg_tasks)
            for task in batch:
                task.cancel()
            await asyncio.gather(*batch, return_exceptions=True)
            for task in batch:
                self._resident_bg_tasks.discard(task)
        outputpro_split.disable_qq_official()
        await self.media.cleanup_all()
        await self.db.aclose()


NuoxiaomiaoGuard = NuoxiaomiaoPlugin
