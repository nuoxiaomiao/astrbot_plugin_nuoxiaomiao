import unittest
from types import SimpleNamespace

from astrbot.core.provider.entities import ProviderRequest

from main import SELF_WAKE_EVENT_EXTRA
from test_plugin_logic import make_plugin, FakeContext, FakeEvent


class StubConversation:
    def __init__(self, cid="cid1", history="[]"):
        self.cid = cid
        self.persona_id = None
        self.history = history
        self.token_usage = 0


class StubConvManager:
    def __init__(self, conversation=None):
        self.conversation = conversation or StubConversation()
        self.created = []

    async def get_curr_conversation_id(self, umo):
        return self.conversation.cid

    async def new_conversation(self, umo, platform_id=None):
        self.created.append(umo)
        return "newcid"

    async def get_conversation(self, umo, cid, create_if_not_exists=False):
        return self.conversation


def context_with_conv(conversation=None):
    ctx = FakeContext(admins=["10001"])
    ctx.conversation_manager = StubConvManager(conversation)
    return ctx


class MasterTest(unittest.IsolatedAsyncioTestCase):
    def make(self, **cfg):
        p = make_plugin(context=FakeContext(admins=["10001"]), **cfg)
        self.addAsyncCleanup(p.terminate)
        return p

    async def test_resolve_empty_when_disabled(self):
        p = self.make(shared_memory_enabled=False)
        self.assertEqual(await p.resident.master.resolve_master_umo(), "")

    async def test_resolve_prefers_explicit_config(self):
        p = self.make(
            shared_memory_enabled=True, master_umo="aiocqhttp:GroupMessage:20001"
        )
        self.assertEqual(
            await p.resident.master.resolve_master_umo(),
            "aiocqhttp:GroupMessage:20001",
        )

    async def test_auto_bind_on_first_admin_private(self):
        p = self.make(shared_memory_enabled=True, master_auto_bind=True)
        event = FakeEvent(sender_id="10001", is_private=True, session="aiocqhttp:FriendMessage:10001")
        await p.resident.master.maybe_auto_bind(event)
        self.assertEqual(
            p.resident.store.meta("master_auto_umo", ""),
            "aiocqhttp:FriendMessage:10001",
        )
        self.assertEqual(
            await p.resident.master.resolve_master_umo(),
            "aiocqhttp:FriendMessage:10001",
        )

    async def test_auto_bind_not_for_group_or_non_admin(self):
        p = self.make(shared_memory_enabled=True, master_auto_bind=True)
        # 群消息不绑定。
        await p.resident.master.maybe_auto_bind(
            FakeEvent(sender_id="10001", session="aiocqhttp:GroupMessage:20001")
        )
        self.assertEqual(p.resident.store.meta("master_auto_umo", ""), "")
        # 非管理员私聊不绑定。
        await p.resident.master.maybe_auto_bind(
            FakeEvent(sender_id="9", is_private=True, session="aiocqhttp:FriendMessage:9")
        )
        self.assertEqual(p.resident.store.meta("master_auto_umo", ""), "")

    async def test_bind_master_rebinds_request(self):
        conv = StubConversation(history='[{"role":"user","content":"之前聊过的"}]')
        p = make_plugin(
            context=context_with_conv(conv),
            shared_memory_enabled=True,
            master_umo="aiocqhttp:GroupMessage:20001",
        )
        self.addAsyncCleanup(p.terminate)
        req = ProviderRequest()
        req.conversation = None
        event = FakeEvent()
        bound = await p.resident.master.bind_master(event, req)
        self.assertTrue(bound)
        self.assertIs(req.conversation, conv)
        self.assertEqual(len(req.contexts), 1)

    async def test_bind_master_skips_self_wake(self):
        conv = StubConversation(history="[]")
        p = make_plugin(
            context=context_with_conv(conv),
            shared_memory_enabled=True,
            master_umo="aiocqhttp:GroupMessage:20001",
        )
        self.addAsyncCleanup(p.terminate)
        req = ProviderRequest()
        req.conversation = None
        event = FakeEvent()
        event.set_extra(SELF_WAKE_EVENT_EXTRA, True)
        bound = await p.resident.master.bind_master(event, req)
        self.assertFalse(bound)
        self.assertIsNone(req.conversation)

    async def test_bind_skips_when_disabled(self):
        p = self.make(shared_memory_enabled=False)
        req = ProviderRequest()
        bound = await p.resident.master.bind_master(FakeEvent(), req)
        self.assertFalse(bound)

    async def test_brain_lock_is_reentrant_and_releases(self):
        p = self.make(shared_memory_enabled=True)
        first = await p.resident.master.acquire_brain()
        self.assertTrue(first)
        second = await p.resident.master.acquire_brain()
        self.assertFalse(second)  # 同一任务复用，不重复排队。
        p.resident.master.release_brain()
        third = await p.resident.master.acquire_brain()
        self.assertTrue(third)
        p.resident.master.release_brain()


if __name__ == "__main__":
    unittest.main()
