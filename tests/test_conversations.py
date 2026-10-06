import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.core.permissions import AccessDenied, InputError
from app.services.conversation_service import ConversationService
from app.workers.delivery import DeliveryWorker
from tests.test_admin_flows import ChatHarness
from tests.test_bootstrap import callback_update
from tests.test_external import compose as external_compose
from tests.test_external import external as external
from tests.test_feedback import compose, count

REPLY = "از پیشنهاد شما سپاسگزارم؛ آن را بررسی می‌کنیم."


async def internal_conversation(service):
    receipt = await service.submit(333, await compose(service))
    _, message_id = await service.peek(333, receipt)
    # Histories break same-second timestamp ties by UUID, not insertion order.
    async with service.database.transaction() as conn:
        messages = service.tables["messages"]
        return await conn.scalar(
            select(messages.c.conversation_id).where(messages.c.id == message_id)
        )


async def reply(service, actor, cid, manager=False):
    cs = ConversationService(service)
    await cs.begin_reply(actor, cid, manager)
    _, token = await cs.preview_reply(actor, REPLY)
    return token


async def test_bidirectional_reply_delivery_and_private_history(feedback_service):
    s = feedback_service
    cs = ConversationService(s)
    cid = await internal_conversation(s)
    bot = AsyncMock()
    worker = DeliveryWorker(s, bot)
    await worker.run_once()
    assert (await cs.history(222, "received"))[0]["subject"] == "فرستنده ناشناس"
    assert await cs.history(111) == []
    for actor in (111,):  # Full administrator is not a participant.
        with pytest.raises(AccessDenied):
            await cs.thread(actor, cid)
        with pytest.raises(AccessDenied):
            await cs.begin_reply(actor, cid)
        with pytest.raises(AccessDenied):
            await cs.thread(actor, cid, manager=True)
    result = await cs.submit_reply(222, await reply(s, 222, cid))
    assert result["queued"]
    await worker.run_once()
    params = bot.send_message.call_args.kwargs
    assert params["chat_id"] == 333
    assert REPLY in params["text"]
    assert "همکار گیرنده" not in params["text"]
    assert "reply_parameters" not in params
    assert params["reply_markup"].inline_keyboard[0][0].callback_data.startswith("f:")
    view = await cs.thread(333, cid, 1)
    assert view["speaker"] == "گیرنده بازخورد"
    assert "sender" not in view and "route" not in view
    assert (await cs.thread(222, cid, 1))["delivery"] == "sent"
    await cs.submit_reply(333, await reply(s, 333, cid))
    await worker.run_once()
    assert bot.send_message.call_args.kwargs["chat_id"] == 222
    assert (await cs.thread(333, cid, 2))["speaker"] == "شما"
    assert await count(s, "weekly_contributors") == 1
    assert (await cs.profile(333))["sent_suggestion"] == 1
    assert (await cs.profile(222))["received_suggestion"] == 1
    assert (await cs.profile(222))["sent_replies"] == 1
    assert (await cs.profile(333))["received_replies"] == 1


async def test_reply_double_click_and_rollback(feedback_service, monkeypatch):
    s = feedback_service
    cs = ConversationService(s)
    cid = await internal_conversation(s)
    token = await reply(s, 222, cid)
    results = await asyncio.gather(
        cs.submit_reply(222, token), cs.submit_reply(222, token), return_exceptions=True
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, InputError) for r in results) == 1
    assert await count(s, "messages") == 2
    token = await reply(s, 333, cid)
    original = s.seal

    def fail(value, record, field, *args):
        if field == "destination":
            raise RuntimeError("Simulated failure after message insert")
        return original(value, record, field, *args)

    monkeypatch.setattr(s, "seal", fail)
    with pytest.raises(RuntimeError):
        await cs.submit_reply(333, token)
    assert await count(s, "messages") == 2
    assert (await s.peek(333, token))[0] == "reply_confirm"


@pytest.mark.parametrize("status", ["closed", "suspended", "expired"])
async def test_closed_conversations_remain_readable_but_not_writable(feedback_service, status):
    s = feedback_service
    cs = ConversationService(s)
    cid = await internal_conversation(s)
    token = await reply(s, 222, cid)
    async with s.database.transaction(write=True) as conn:
        c = s.tables["conversations"]
        await conn.execute(update(c).where(c.c.id == cid).values(status=status))
    assert not (await cs.thread(222, cid))["can_reply"]
    with pytest.raises(InputError):
        await cs.submit_reply(222, token)
    with pytest.raises(InputError):
        await cs.begin_reply(333, cid)


async def test_reply_expiry_membership_and_actor_binding(feedback_service):
    s = feedback_service
    cs = ConversationService(s)
    cid = await internal_conversation(s)
    token = await reply(s, 222, cid)
    with pytest.raises(InputError):
        await cs.submit_reply(333, token)
    sender = await s.identity.authenticate(333)
    await s.identity.update_user(111, sender.user_id, active=False)
    with pytest.raises(InputError):
        await cs.submit_reply(222, token)
    with pytest.raises(AccessDenied):
        await cs.thread(333, cid)
    await s.identity.update_user(111, sender.user_id, active=True)
    now = s.clock()
    s.clock = lambda: now + 601
    with pytest.raises(InputError):
        await cs.submit_reply(222, token)
    s.clock = lambda: now + 91 * 86400
    with pytest.raises(InputError):
        await cs.begin_reply(222, cid)


async def test_external_manager_reply_and_user_followup(external):
    s = external
    cs = ConversationService(s)
    _, token = await external_compose(s)
    await s.submit(333, token)
    cid = (await cs.history(333))[0]["id"]
    with pytest.raises(AccessDenied):
        await cs.thread(222, cid)
    assert (await cs.thread(222, cid, manager=True))["body"]
    await cs.submit_reply(222, await reply(s, 222, cid, True))
    bot = AsyncMock()
    worker = DeliveryWorker(s, bot)
    await worker.run_once()
    assert bot.send_message.call_args.kwargs["chat_id"] == 333
    assert "مدیریت بخش خارجی (واسطه)" in bot.send_message.call_args.kwargs["text"]
    assert "همکار گیرنده" not in bot.send_message.call_args.kwargs["text"]
    view = await cs.thread(333, cid, 1)
    assert view["speaker"] == "مدیریت بخش خارجی (واسطه)"
    assert not (await cs.submit_reply(333, await reply(s, 333, cid)))["queued"]
    assert not await worker.run_once()
    assert (await cs.thread(222, cid, 2, manager=True))["body"] == REPLY
    assert (await cs.profile(222))["sent_suggestion"] == 0
    assert await count(s, "weekly_contributors") == 1


async def test_revocation_after_preview_and_before_delivery(external):
    s = external
    cs = ConversationService(s)
    _, token = await external_compose(s)
    await s.submit(333, token)
    cid = (await cs.history(333))[0]["id"]
    await cs.submit_reply(222, await reply(s, 222, cid, True))
    pending = await reply(s, 222, cid, True)
    admin = await s.identity.authenticate(222)
    await s.identity.revoke_admin(111, admin.user_id)
    with pytest.raises(AccessDenied):
        await cs.submit_reply(222, pending)
    with pytest.raises(AccessDenied):
        await cs.thread(222, cid, manager=True)
    bot = AsyncMock()
    await DeliveryWorker(s, bot).run_once()
    bot.send_message.assert_not_called()
    async with s.database.transaction() as conn:
        assert await conn.scalar(select(s.tables["outbox"].c.status)) == "suspended"


async def test_ui_history_reply_restart_and_profile(feedback_service):
    s = feedback_service
    await internal_conversation(s)
    chat = ChatHarness(s.identity, s)
    await chat.text("/start", 222)
    await chat.click("گفتگوهای داخلی", 222)
    await chat.click("دریافتی", 222)
    await chat.click("فرستنده ناشناس", 222)
    await chat.click("پاسخ به گفتگو", 222)
    await chat.text(REPLY, 222)
    token = chat.button("ارسال پاسخ")
    chat = ChatHarness(s.identity, s)
    await chat.feed(callback_update(user_id=222, data=token))
    assert REPLY in chat.latest[-1].text
    await chat.text("/start", 333)
    await chat.click("گفتگوهای داخلی", 333)
    await chat.click("ارسالی", 333)
    await chat.click("همکار گیرنده", 333)
    await chat.click("پیام بعدی", 333)
    assert REPLY in chat.latest[-1].text
    await chat.text("/start", 333)
    await chat.click("پروفایل", 333)
    assert "پیشنهاد ارسالی: 1" in chat.latest[-1].text


async def test_external_ui_manager_response_and_sender_history(external, feedback_service):
    s = external
    _, token = await external_compose(s)
    await s.submit(333, token)
    chat = ChatHarness(s.identity, feedback_service, s)
    await chat.text("/admin", 222)
    await chat.click("مدیریت بخش خارجی", 222)
    await chat.click("صندوق", 222)
    await chat.click("فرد خارجی", 222)
    assert "تأمین‌کننده" in chat.latest[-1].text
    await chat.click("پاسخ به گفتگو", 222)
    await chat.text(REPLY, 222)
    assert "واسطه" in chat.latest[-1].text
    await chat.click("ارسال پاسخ", 222)
    await chat.text("/start", 333)
    await chat.click("گفتگوهای خارجی", 333)
    await chat.click("ارسالی", 333)
    await chat.click("فرد خارجی", 333)
    await chat.click("پیام بعدی", 333)
    assert "مدیریت بخش خارجی (واسطه)" in chat.latest[-1].text
    assert REPLY in chat.latest[-1].text


async def test_history_pagination_and_scope_isolation(external, feedback_service):
    for _ in range(12):
        await internal_conversation(feedback_service)
    cs = ConversationService(feedback_service)
    first = await cs.history(333)
    second = await cs.history(333, page=1)
    assert len(first) == 11 and len(second) == 2
    assert not ({r["id"] for r in first[:10]} & {r["id"] for r in second})
    assert await ConversationService(external).history(333) == []
    with pytest.raises(AccessDenied):
        await ConversationService(external).thread(333, first[0]["id"])
    assert await cs.history(111, "received") == []


async def test_pending_reply_suspended_when_peer_disabled(feedback_service):
    s = feedback_service
    cid = await internal_conversation(s)
    bot = AsyncMock()
    worker = DeliveryWorker(s, bot)
    await worker.run_once()
    await ConversationService(s).submit_reply(222, await reply(s, 222, cid))
    peer = await s.identity.authenticate(333)
    await s.identity.update_user(111, peer.user_id, active=False)
    bot.reset_mock()
    await worker.run_once()
    bot.send_message.assert_not_called()
    assert (await ConversationService(s).thread(222, cid, 1))["delivery"] == "suspended"
