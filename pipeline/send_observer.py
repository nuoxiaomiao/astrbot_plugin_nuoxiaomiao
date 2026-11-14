"""Per-turn send/streaming observer, QQ Official outbound adapter, and send ordering."""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain

if __package__ and "." in __package__:
    from ..infra.constants import (
        DISPLAY_NAME,
        QQ_PASSIVE_SENDS_EXTRA,
        QQ_SCENE_EXTRA,
        SEND_OBSERVER_EXTRA,
        SYNTHETIC_EVENT_EXTRA,
        UNTRACKED_SEND_TYPES,
    )
    from ..infra.runtime import extract_components, message_id_of
    from .. import outputpro_split
    from ..qq_official import (
        QQ_OFFICIAL_PASSIVE_REPLY_LIMIT,
        apply_content_mode,
        clean_platform_text,
        is_qq_official,
        is_supported_raw_message,
        normalize_inbound_components,
        platform_capability_note,
        platform_name_of_inst,
        prepare_outbound_chains,
        scene_of_event,
    )
    from ..send_order import OBSERVER_WAIT_SECONDS, SendOrderGate
else:
    from infra.constants import (
        DISPLAY_NAME,
        QQ_PASSIVE_SENDS_EXTRA,
        QQ_SCENE_EXTRA,
        SEND_OBSERVER_EXTRA,
        SYNTHETIC_EVENT_EXTRA,
        UNTRACKED_SEND_TYPES,
    )
    from infra.runtime import extract_components, message_id_of
    import outputpro_split
    from qq_official import (
        QQ_OFFICIAL_PASSIVE_REPLY_LIMIT,
        apply_content_mode,
        clean_platform_text,
        is_qq_official,
        is_supported_raw_message,
        normalize_inbound_components,
        platform_capability_note,
        platform_name_of_inst,
        prepare_outbound_chains,
        scene_of_event,
    )
    from send_order import OBSERVER_WAIT_SECONDS, SendOrderGate

_MIN_QUOTE_MATCH_LEN = 4


class SendObserver:
    """Coordinate QQ Official inbound/outbound normalization and whole-turn SendOrderGate."""

    def __init__(
        self,
        plugin: Any,
        send_order: SendOrderGate,
        *,
        record_bot_reply_fn: Callable[[Any, str], None],
        recent_bot_texts_fn: Callable[[Any], list[str]],
        platform_inst_by_id_fn: Callable[[str], Any | None],
        admin_ids_fn: Callable[[], set[str]],
    ) -> None:
        self.plugin = plugin
        self.send_order = send_order
        self._record_bot_reply = record_bot_reply_fn
        self._recent_bot_texts = recent_bot_texts_fn
        self._platform_inst_by_id = platform_inst_by_id_fn
        self._admin_ids = admin_ids_fn
        self._qq_state_logged = False

    def maybe_log_qq_official_state(self, event: Any) -> None:
        if self._qq_state_logged:
            return
        self._qq_state_logged = True
        p = self.plugin
        if not getattr(p, "qq_official_enabled", True):
            logger.info(
                "[%s] QQ官方平台适配已关闭（qq_official_enabled=False），按原平台行为运行",
                DISPLAY_NAME,
            )
            return
        try:
            platform_id = str(event.get_platform_id() or "")
            inst = self._platform_inst_by_id(platform_id)
            scene = scene_of_event(event)
            admins = self._admin_ids()
            qq_admins = set(getattr(p, "qq_official_admin_openids", set()) or set())
            openid_admins = {item for item in admins if item and not str(item).isdigit()}
            logger.info(
                "[%s] QQ官方平台适配生效：平台=%s(%s) scene=%s 内容模式=%s 群主动=%s 清洗@标记=%s 文本上限=%s 转发展开=%s 管理员=%d",
                DISPLAY_NAME,
                platform_id,
                platform_name_of_inst(inst) if inst is not None else "unknown",
                scene,
                getattr(p, "qq_official_content_mode", True),
                getattr(p, "qq_official_group_proactive", False),
                getattr(p, "qq_official_strip_at_markup", True),
                getattr(p, "qq_official_text_chars", 2000),
                getattr(p, "qq_official_flatten_forward", True),
                len(admins),
            )
            if inst is None:
                logger.warning(
                    "[%s] QQ官方平台实例未找到（platform_id=%s），适配参数无法完全生效",
                    DISPLAY_NAME,
                    platform_id,
                )
            sender_id = str(event.get_sender_id() or "")
            if not qq_admins and not openid_admins:
                logger.warning(
                    "[%s] QQ官方尚未配置管理员 openid：sender=%s；请在插件配置 qq_official_admin_openids 或 AstrBot admins_id 中填写 openid，否则速览工具、主动私聊、自醒会被拒绝（数字 QQ 号不会匹配 openid）。",
                    DISPLAY_NAME,
                    sender_id or "?",
                )
            logger.info("[%s] QQ官方能力边界：%s", DISPLAY_NAME, platform_capability_note())
            logger.info(
                "[%s] QQ官方标识示例：群=%s:GroupMessage:<群openid> 私聊=%s:FriendMessage:<用户openid>",
                DISPLAY_NAME,
                platform_id,
                platform_id,
            )
        except Exception as exc:
            logger.debug("[%s] QQ官方平台状态日志失败: %s", DISPLAY_NAME, exc)

    def qq_official_quote_is_bot(self, event: Any) -> bool:
        try:
            replies = [comp for comp in extract_components(event) if type(comp).__name__ == "Reply"]
        except Exception:
            return False
        if not replies:
            return False
        self_id = str(event.get_self_id() or "")
        recent_bot_texts = self._recent_bot_texts(event)
        for reply in replies:
            sender_id = str(getattr(reply, "sender_id", "") or "")
            if sender_id and self_id and sender_id == self_id:
                return True
            quoted = str(getattr(reply, "message_str", "") or "").strip()
            if not quoted:
                quoted = "".join(
                    str(getattr(comp, "text", "") or "")
                    for comp in (getattr(reply, "chain", None) or [])
                    if type(comp).__name__ == "Plain"
                ).strip()
            if not quoted or not recent_bot_texts:
                continue
            quoted_norm = re.sub(r"\s+", "", quoted)
            for bot_text in recent_bot_texts:
                if quoted_norm == bot_text:
                    return True
                if (
                    len(quoted_norm) >= _MIN_QUOTE_MATCH_LEN
                    and len(bot_text) >= _MIN_QUOTE_MATCH_LEN
                    and (quoted_norm.startswith(bot_text) or bot_text.startswith(quoted_norm))
                ):
                    return True
                if (
                    len(quoted_norm) >= 20
                    and len(bot_text) >= 20
                    and (quoted_norm[:60] in bot_text or bot_text[:60] in quoted_norm)
                ):
                    return True
        return False

    async def prepare_qq_official_event(self, event: Any) -> None:
        p = self.plugin
        if not p.enabled or not is_qq_official(event) or not getattr(p, "qq_official_enabled", True):
            return
        outputpro_split.enable_qq_official()
        scene = scene_of_event(event)
        event.set_extra(QQ_SCENE_EXTRA, scene)
        strip_markup = bool(getattr(p, "qq_official_strip_at_markup", True))
        try:
            components = extract_components(event)
            # 保留是否曾出现官方群 @ 标记，避免清理占位 At 后丢失 mentioned 状态。
            if any(type(c).__name__ == "At" and str(getattr(c, "qq", "") or "") == "qq_official" for c in components):
                if getattr(event, "is_at_or_wake_command", False):
                    event.set_extra("nuoxiaomiao_qq_was_at", True)
            cleaned, dropped = normalize_inbound_components(components, event=event, strip_markup=strip_markup)
            if dropped or cleaned != components:
                message_obj = getattr(event, "message_obj", None)
                if message_obj is not None:
                    try:
                        message_obj.message = cleaned
                    except Exception:
                        pass
                if dropped:
                    logger.debug(
                        "[%s] QQ官方入站清理：scene=%s 丢弃占位组件=%d",
                        DISPLAY_NAME,
                        scene,
                        dropped,
                    )
            if strip_markup:
                raw_text = str(getattr(event, "message_str", "") or "")
                cleaned_text = clean_platform_text(event, raw_text, True)
                if cleaned_text != raw_text:
                    event.message_str = cleaned_text
                    message_obj = getattr(event, "message_obj", None)
                    if message_obj is not None:
                        try:
                            message_obj.message_str = cleaned_text
                        except Exception:
                            pass
        except Exception as exc:
            logger.debug("[%s] QQ官方入站归一化失败: %s", DISPLAY_NAME, exc)

        if scene == "channel":
            event.is_at_or_wake_command = True
            event.is_wake = True
        elif (
            scene == "group"
            and bool(getattr(p, "qq_official_quote_trigger", True))
            and not getattr(event, "is_at_or_wake_command", False)
            and self.qq_official_quote_is_bot(event)
        ):
            event.is_at_or_wake_command = True
            event.is_wake = True
            logger.info(
                "[%s] QQ官方引用消息触发对话：session=%s sender=%s",
                DISPLAY_NAME,
                str(getattr(event, "unified_msg_origin", "") or ""),
                str(event.get_sender_id() or ""),
            )

        self.ensure_send_observer(event)
        self.maybe_log_qq_official_state(event)

    @staticmethod
    def derive_chain(source: Any, components: list[Any]) -> Any:
        derive = getattr(source, "derive", None)
        if callable(derive):
            try:
                return derive(list(components))
            except Exception:
                pass
        return MessageChain(list(components))

    def prepare_qq_official_outbound(self, event: Any, message: Any) -> list[Any] | None:
        p = self.plugin
        if not is_qq_official(event) or not getattr(p, "qq_official_enabled", True):
            return None
        chains, info = prepare_outbound_chains(
            message,
            event=event,
            content_mode=bool(getattr(p, "qq_official_content_mode", True)),
            flatten_forward=bool(getattr(p, "qq_official_flatten_forward", True)),
            inbound_message_id=message_id_of(event),
            strip_markup=bool(getattr(p, "qq_official_strip_at_markup", True)),
            text_limit=int(getattr(p, "qq_official_text_chars", 2000) or 0),
            derive_fn=self.derive_chain,
        )
        if info.get("dropped") or info.get("flattened"):
            logger.debug(
                "[%s] QQ官方出站清洗: dropped=%s flattened=%s types=%s",
                DISPLAY_NAME,
                info.get("dropped"),
                info.get("flattened"),
                info.get("dropped_types"),
            )
        return chains

    @staticmethod
    def note_qq_passive_sends(event: Any, count: int) -> None:
        if count <= 0:
            return
        try:
            total = int(event.get_extra(QQ_PASSIVE_SENDS_EXTRA) or 0) + max(0, int(count))
            event.set_extra(QQ_PASSIVE_SENDS_EXTRA, total)
            if total > QQ_OFFICIAL_PASSIVE_REPLY_LIMIT and not event.get_extra("nuoxiaomiao_qq_passive_warned"):
                event.set_extra("nuoxiaomiao_qq_passive_warned", True)
                logger.warning(
                    "[%s] QQ官方同一 msg_id 已发 %d 条被动回复（保守上限 %d），超出后适配器会改走主动消息并消耗主动配额；请减少单轮分段条数。",
                    DISPLAY_NAME,
                    total,
                    QQ_OFFICIAL_PASSIVE_REPLY_LIMIT,
                )
        except Exception:
            pass

    @staticmethod
    def sent_text_from_chain(chain: list[Any]) -> str:
        from astrbot.core.message.components import Node, Nodes, Plain

        parts: list[str] = []
        for comp in chain:
            if isinstance(comp, Plain) and comp.text:
                parts.append(comp.text)
            elif isinstance(comp, Node):
                text = SendObserver.sent_text_from_chain(comp.content)
                if text:
                    parts.append(text)
            elif isinstance(comp, Nodes):
                text = SendObserver.sent_text_from_chain(comp.nodes)
                if text:
                    parts.append(text)
        return "\n".join(parts)

    async def qq_official_send(self, event: Any, message: Any) -> bool:
        p = self.plugin
        if is_qq_official(event) and bool(getattr(p, "qq_official_enabled", True)):
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if not is_supported_raw_message(raw):
                logger.warning(
                    "[%s] QQ官方消息源不可发送（raw_message=%s），已阻止静默丢弃: session=%s",
                    DISPLAY_NAME,
                    type(raw).__name__,
                    str(getattr(event, "unified_msg_origin", "") or ""),
                )
                return False
            chain = list(getattr(message, "chain", None) or [])
            if not chain:
                return False
        try:
            result = await event.send(message)
        except Exception as exc:
            logger.warning("[%s] QQ官方发送失败: %s", DISPLAY_NAME, exc)
            return False
        return result is not False

    def ensure_send_observer(self, event: Any) -> dict[str, Any] | None:
        p = self.plugin
        if not p.enabled or bool(event.get_extra("nuoxiaomiao_self_wake")):
            return None
        if is_qq_official(event) and getattr(p, "qq_official_enabled", True):
            outputpro_split.enable_qq_official()
        observed = event.get_extra(SEND_OBSERVER_EXTRA)
        if isinstance(observed, dict):
            return observed
        original_send = getattr(event, "send", None)
        if not callable(original_send):
            return None
        original_stream = getattr(event, "send_streaming", None)
        observed = {
            "attempted": 0,
            "succeeded": 0,
            "active": True,
            "round_lease": None,
            "round_lease_attempted": False,
            "round_lease_timed_out": False,
            "llm_text": "",
            "recorded_text": False,
            "original": original_send,
            "original_streaming": original_stream,
        }

        async def _acquire_for_send(umo: str, trackable: bool) -> bool:
            """Return True if caller should still acquire a one-shot hold()."""
            if not observed["active"] or not trackable:
                return False
            if not observed.get("round_lease_attempted"):
                observed["round_lease_attempted"] = True
                lease, timed_out = await self.send_order.acquire_round_ex(
                    umo, event=event, timeout=OBSERVER_WAIT_SECONDS
                )
                observed["round_lease"] = lease
                observed["round_lease_timed_out"] = timed_out
            if observed.get("round_lease") is not None:
                return False
            if observed.get("round_lease_timed_out"):
                return False
            return True

        async def send_observed(message: Any, *args: Any, **kwargs: Any) -> Any:
            chain = list(getattr(message, "chain", None) or [])
            trackable = bool(chain) and str(getattr(message, "type", "") or "").lower() not in UNTRACKED_SEND_TYPES
            if observed["active"] and trackable:
                observed["attempted"] += 1
                if event.get_extra(SYNTHETIC_EVENT_EXTRA):
                    event.set_extra("_outputpro_pending_reply", None)
                    from astrbot.core.message.components import Reply

                    synthetic_id = message_id_of(event)
                    safe_chain = [
                        comp
                        for comp in chain
                        if not (isinstance(comp, Reply) and str(comp.id) == synthetic_id)
                    ]
                    if len(safe_chain) != len(chain):
                        message = self.derive_chain(message, safe_chain)
                        chain = safe_chain

            umo = str(getattr(event, "unified_msg_origin", "") or "")
            need_hold = await _acquire_for_send(umo, trackable)
            if need_hold:
                async with self.send_order.hold(umo, event=event, timeout=OBSERVER_WAIT_SECONDS):
                    return await self.dispatch_send(
                        event, message, chain, trackable, observed, original_send, args, kwargs
                    )
            return await self.dispatch_send(
                event, message, chain, trackable, observed, original_send, args, kwargs
            )

        async def send_streaming_observed(generator: Any, *args: Any, **kwargs: Any) -> Any:
            if not callable(original_stream):
                return None
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            need_hold = await _acquire_for_send(umo, True)
            try:
                if need_hold:
                    async with self.send_order.hold(umo, event=event, timeout=OBSERVER_WAIT_SECONDS):
                        res = original_stream(generator, *args, **kwargs)
                        if inspect.isawaitable(res):
                            return await res
                        return res
                res = original_stream(generator, *args, **kwargs)
                if inspect.isawaitable(res):
                    return await res
                return res
            finally:
                self.finish_send_observer(event)

        observed["wrapper"] = send_observed
        event.send = send_observed
        if callable(original_stream):
            observed["streaming_wrapper"] = send_streaming_observed
            event.send_streaming = send_streaming_observed
        event.set_extra(SEND_OBSERVER_EXTRA, observed)
        return observed

    async def dispatch_send(
        self,
        event: Any,
        message: Any,
        chain: list[Any],
        trackable: bool,
        observed: dict[str, Any],
        original_send: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if is_qq_official(event) and getattr(self.plugin, "qq_official_enabled", True):
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if not is_supported_raw_message(raw):
                logger.warning(
                    "[%s] QQ官方消息源不可发送（raw_message=%s），已阻止静默丢弃: session=%s",
                    DISPLAY_NAME,
                    type(raw).__name__,
                    str(getattr(event, "unified_msg_origin", "") or ""),
                )
                return False
        prepared = self.prepare_qq_official_outbound(event, message)
        if prepared is not None:
            if not prepared:
                return False
            sent_texts: list[str] = []
            sent_count = 0
            try:
                for item in prepared:
                    delivered = await original_send(item, *args, **kwargs)
                    if delivered is False:
                        return False
                    sent_count += 1
                    item_chain = list(getattr(item, "chain", None) or [])
                    sent_texts.append(self.sent_text_from_chain(item_chain))
            finally:
                if sent_count > 0:
                    self.note_qq_passive_sends(event, sent_count)
                if observed["active"] and trackable and sent_count > 0:
                    observed["succeeded"] += 1
                    text = "\n".join(part for part in sent_texts if part)
                    if text:
                        observed["recorded_text"] = True
                        self._record_bot_reply(event, text)
            return True

        delivered = await original_send(message, *args, **kwargs)
        if observed["active"] and trackable and chain and delivered is not False:
            observed["succeeded"] += 1
            text = self.sent_text_from_chain(chain)
            if text:
                observed["recorded_text"] = True
                self._record_bot_reply(event, text)
        return delivered

    def finish_send_observer(self, event: Any) -> None:
        observed = event.get_extra(SEND_OBSERVER_EXTRA)
        if isinstance(observed, dict):
            if (
                observed.get("succeeded", 0) > 0
                and not observed.get("recorded_text")
                and observed.get("llm_text")
            ):
                observed["recorded_text"] = True
                self._record_bot_reply(event, observed["llm_text"])
                observed["llm_text"] = ""
            lease = observed.pop("round_lease", None)
            if lease is not None:
                self.send_order.release_round(lease)
            observed["active"] = False
            if getattr(event, "send", None) is observed.get("wrapper"):
                event.send = observed["original"]
            if callable(observed.get("original_streaming")) and getattr(event, "send_streaming", None) is observed.get("streaming_wrapper"):
                event.send_streaming = observed["original_streaming"]

    def force_content_mode(self, event: Any) -> None:
        p = self.plugin
        try:
            if not p.enabled or not getattr(p, "qq_official_enabled", True):
                return
            if not getattr(p, "qq_official_content_mode", True):
                return
            if not is_qq_official(event):
                return
            result = event.get_result()
            if result is None:
                return
            apply_content_mode(result, True)
        except Exception as exc:
            logger.debug("[%s] QQ官方纯文本模式标记失败: %s", DISPLAY_NAME, exc)
