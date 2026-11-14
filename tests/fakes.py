"""Consolidated test fakes, factories, and async/DB helpers."""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from typing import Any

import tests  # noqa: F401 - triggers tests.bootstrap.ensure_test_env() before astrbot import

from astrbot.core.message.components import At, Image, Plain, Reply
from main import NuoxiaomiaoPlugin
from infra.constants import ActivityEntry


class FakeEvent:
    def __init__(
        self,
        sender_id="10001",
        session="aiocqhttp:GroupMessage:20001",
        group_id="20001",
        is_private=False,
        is_trigger=True,
        pipeline_wake=None,
        message_str="",
        message_id="",
        components=None,
        activated_handlers=None,
        sender_name="测试用户",
        self_id="bot",
        group_name=None,
        is_admin=False,
    ):
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._self_id = self_id
        self.unified_msg_origin = session
        self._group_id = group_id
        self._is_private = is_private
        self._is_admin = is_admin
        self.is_at_or_wake_command = is_trigger
        self._is_wake = is_trigger if pipeline_wake is None else pipeline_wake
        self.message_str = message_str
        self.extras = {}
        if activated_handlers is not None:
            self.extras["activated_handlers"] = activated_handlers
        self.stopped = False
        self.call_llm = False
        self.sent = []
        self._result = None
        self.bot = None
        self.session_id = session
        self.message_obj = type(
            "MO",
            (),
            {
                "message_id": message_id or f"mid-{id(self)}",
                "session_id": session,
                "type": type("T", (), {"value": "FriendMessage" if is_private else "GroupMessage"})(),
                "sender": type("S", (), {"user_id": sender_id, "nickname": sender_name})(),
                "group": None if group_name is None else type("G", (), {"group_id": group_id, "group_name": group_name})(),
                "message": list(components or []),
                "timestamp": 0,
            },
        )()

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._self_id

    def get_sender_name(self):
        return self._sender_name

    def get_platform_id(self):
        return "aiocqhttp"

    def get_platform_name(self):
        return "aiocqhttp"

    def is_private_chat(self):
        return self._is_private

    def is_admin(self):
        return self._is_admin

    def is_wake_up(self):
        return self._is_wake

    def get_messages(self):
        return self.message_obj.message

    def get_message_outline(self):
        parts = []
        for component in self.message_obj.message:
            if isinstance(component, Plain):
                parts.append(component.text)
            elif isinstance(component, Image):
                parts.append("[图片]")
            elif isinstance(component, Reply):
                parts.append(
                    f"[引用 {component.sender_nickname or component.sender_id}: "
                    f"{component.message_str or '内容不可用'}]"
                )
            elif isinstance(component, At):
                parts.append(f"@{component.qq}")
            else:
                parts.append(f"[{type(component).__name__}]")
        return " ".join(parts)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self.extras
        return self.extras.get(key, default)

    def should_call_llm(self, value):
        self.call_llm = value

    def get_result(self):
        return self._result

    def set_result(self, result):
        self._result = result

    def clear_result(self):
        self._result = None

    def plain_result(self, text):
        from astrbot.core.message.message_event_result import MessageEventResult

        return MessageEventResult().message(text)

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped

    async def send(self, message):
        self.sent.append(message)


class FakePokeEvent:
    def __init__(self, raw, sender_id="10001", self_id="bot", group_id="20001"):
        self.message_obj = type("PokeMO", (), {"raw_message": raw, "message": []})()
        self._sender_id = sender_id
        self._self_id = self_id
        self._group_id = group_id
        self.unified_msg_origin = "aiocqhttp:GroupMessage:20001" if group_id else f"aiocqhttp:FriendMessage:{sender_id}"
        self.message_str = ""
        self.stopped = False
        self.call_llm = False
        self.extras = {}
        self.bot = None

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_group_id(self):
        return self._group_id

    def get_platform_id(self):
        return "aiocqhttp"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_sender_name(self):
        return "戳戳怪"

    def is_private_chat(self):
        return not bool(self._group_id)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self.extras
        return self.extras.get(key, default)

    def stop_event(self):
        self.stopped = True

    def request_llm(self, prompt="", conversation=None):
        return SimpleNamespace(prompt=prompt, conversation=conversation)


class FakeProvider:
    def __init__(self, text="来找我喵～", raise_error=False):
        self.text = text
        self.raise_error = raise_error
        self.calls = []

    async def text_chat(self, prompt=None, system_prompt=None, contexts=None, **kwargs):
        if self.raise_error:
            raise RuntimeError("LLM 挂了")
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        return SimpleNamespace(completion_text=self.text, result_chain=None)


class FakePersonaManager:
    def __init__(self, prompt=""):
        self.prompt = prompt

    async def get_default_persona_v3(self, umo=None):
        return SimpleNamespace(system_prompt=self.prompt)


class FakeProviderManager:
    def __init__(self, provider):
        self.provider = provider
        self.fetched_ids = []

    def get_using_provider(self, provider_type, umo=None):
        return self.provider

    async def get_provider_by_id(self, provider_id):
        self.fetched_ids.append(str(provider_id))
        return self.provider


def fake_provider_manager(provider):
    return type(
        "PM",
        (),
        {
            "get_provider_by_id": None,
            "get_using_provider": lambda *a, **k: provider,
            "get_using_provider_async": None,
        },
    )()


class FakeContext:
    def __init__(self, wake_prefixes=None, admins=None, provider=None, persona_prompt=""):
        self.wake_prefixes = list(wake_prefixes or [])
        self.admins = list(admins or [])
        self.provider = provider if provider is not None else FakeProvider()
        self.persona_prompt = persona_prompt
        self.sent = []
        self.provider_manager = FakeProviderManager(self.provider)
        self.persona_manager = FakePersonaManager(persona_prompt)
        meta = SimpleNamespace(id="aiocqhttp", name="aiocqhttp", description="test")
        inst = SimpleNamespace(meta=meta)
        self.platform_manager = SimpleNamespace(platform_insts=[inst])

    def get_config(self, umo=None):
        return {"wake_prefix": self.wake_prefixes, "admins_id": self.admins}

    def register_web_api(self, *args, **kwargs):
        pass

    def get_llm_tool_manager(self):
        return type("FTM", (), {"func_list": []})()

    def get_registered_star(self, name):
        return None

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


class FakeConversationManager:
    def __init__(self):
        self.sessions = {}
        self.conversations = {}

    async def get_curr_conversation_id(self, umo):
        return self.sessions.get(umo)

    async def new_conversation(
        self, umo, platform_id=None, content=None, title=None, persona_id=None
    ):
        cid = f"conv-{len(self.conversations) + 1}"
        self.sessions[umo] = cid
        self.conversations[cid] = []
        return cid

    async def get_conversation(self, umo, cid, create_if_not_exists=False):
        if cid not in self.conversations:
            return None
        return SimpleNamespace(cid=cid, history=json.dumps(self.conversations[cid]))

    async def update_conversation(
        self,
        umo,
        cid=None,
        history=None,
        title=None,
        persona_id=None,
        token_usage=None,
    ):
        if cid is None:
            cid = self.sessions.get(umo)
        if cid is None or cid not in self.conversations:
            return
        self.conversations[cid] = list(history or [])


class ProviderRequestStub:
    def __init__(self, prompt="", system_prompt=""):
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.contexts = []
        self.extra_user_content_parts = []
        self.func_tool = None
        self.conversation = None


_TEST_RUNTIME_DIR = tempfile.TemporaryDirectory(prefix="nxm_test_runtime_")
_TEST_DB_COUNTER = itertools.count()


def make_plugin(context=None, **cfg) -> NuoxiaomiaoPlugin:
    idx = next(_TEST_DB_COUNTER)
    data_dir = Path(_TEST_RUNTIME_DIR.name) / f"plugin_data_{idx}"
    data_dir.mkdir(parents=True, exist_ok=True)
    base: dict[str, Any] = {
        "activity_db_path": str(Path(_TEST_RUNTIME_DIR.name) / f"activity_{idx}.db"),
        "trigger_rounds": 3,
        "meme_tags": [],
        "meme_cooldown_seconds": 0,
        "merge_enabled": True,
        "merge_idle_seconds": 4,
        "merge_max_total_seconds": 12,
        "force_wake_prefix": "小喵",
    }
    if "merge_localize_images_dir" not in cfg:
        base["merge_localize_images_dir"] = str(data_dir / "merged_images")
    base.update(cfg)
    plugin = NuoxiaomiaoPlugin(context or FakeContext(), base)
    plugin.plugin_data_dir = lambda d=data_dir: (d.mkdir(parents=True, exist_ok=True) or d)
    return plugin


def make_activity_plugin(context=None, **cfg) -> NuoxiaomiaoPlugin:
    idx = next(_TEST_DB_COUNTER)
    cfg.setdefault("activity_db_path", str(Path(_TEST_RUNTIME_DIR.name) / f"act_{idx}.db"))
    return make_plugin(context, **cfg)


def make_proactive_plugin(context=None, **cfg) -> NuoxiaomiaoPlugin:
    idx = next(_TEST_DB_COUNTER)
    cfg.setdefault("activity_db_path", str(Path(_TEST_RUNTIME_DIR.name) / f"pro_{idx}.db"))
    cfg.setdefault("proactive_cooldown_minutes", 0)
    cfg.setdefault("proactive_quiet_hours_start", "08:00")
    cfg.setdefault("proactive_quiet_hours_end", "08:00")
    return make_plugin(context, **cfg)


def make_user_default_plugin(**cfg) -> NuoxiaomiaoPlugin:
    idx = next(_TEST_DB_COUNTER)
    plugin = make_plugin(**cfg)
    plugin.user_default_config_path = str(Path(_TEST_RUNTIME_DIR.name) / f"udef_{idx}" / "user_default_config.json")
    return plugin


def fetch_activity_rows(plugin: NuoxiaomiaoPlugin, sender_id: str | None = None):
    conn = plugin.db.get_connection()
    sql = "SELECT kind, conversation_id, conversation_name, sender_id, sender_name, text FROM activity_messages"
    if sender_id:
        return conn.execute(sql + " WHERE sender_id = ?", (sender_id,)).fetchall()
    return conn.execute(sql).fetchall()


def record_activity(
    plugin: NuoxiaomiaoPlugin,
    text: str,
    sender: str = "9",
    name: str = "某人",
    mid: str = "",
    mentioned: bool = False,
    kind: str = "group",
    conversation_id: str = "20001",
    conversation_name: str = "20001",
    platform_id: str = "aiocqhttp",
    ts: float | None = None,
) -> int:
    return plugin.activity.record_activity(
        ActivityEntry(
            ts=time.time() if ts is None else ts,
            sender_id=sender,
            sender_name=name,
            conversation_id=conversation_id,
            conversation_name=conversation_name,
            kind=kind,
            text=text,
            platform_id=platform_id,
            message_id=mid or f"mid-{time.time_ns()}",
            mentioned=mentioned,
        )
    )


_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def image_file(directory: str | Path, name: str = "a.png", body: bytes | None = None) -> str:
    p = Path(directory) / name
    p.write_bytes(_PNG_1X1 if body is None else body)
    return str(p)


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


async def await_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def gather_bg(plugin: NuoxiaomiaoPlugin) -> None:
    tasks = list(plugin._resident_bg_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
