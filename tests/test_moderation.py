import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.permissions import AccessDenied, InputError, Role
from app.core.security import Purpose
from app.services.conversation_service import ConversationService
from app.services.moderation_service import ModerationService
from app.workers.delivery import DeliveryWorker
from tests.test_admin_flows import ChatHarness
from tests.test_bootstrap import callback_update
from tests.test_conversations import internal_conversation, reply
from tests.test_external import compose as external_compose
from tests.test_external import external as external
from tests.test_feedback import BODY, count


async def action(s, actor, name, data):
    token = await s.issue(actor, name, data)
    return await ModerationService(s).execute(actor, token)


async def reported(s):
    cid = await internal_conversation(s)
    view = await ConversationService(s).thread(222, cid)
    data = {"cid": cid, "mid": view["message_id"], "reason": "insult"}
    await action(s, 222, "mod_report_save", data)
    rid = (await ModerationService(s).reports(111))[0]["id"]
    return cid, rid, data


async def test_report_ownership_self_and_cross_conversation(feedback_service):
    s = feedback_service
    cid, _, data = await reported(s)
    with pytest.raises(InputError):
        await action(s, 333, "mod_report_save", data)
    with pytest.raises(AccessDenied):
        await action(s, 111, "mod_report_save", data)
    with pytest.raises(InputError):
        await action(s, 222, "mod_report_save", data)
    other = await internal_conversation(s)
    with pytest.raises(AccessDenied):
        await action(s, 222, "mod_report_save", {**data, "cid": other})
    assert await count(s, "reports") == 1
    assert (await ConversationService(s).thread(222, cid))["can_reply"]


async def test_accept_closes_and_suspends_pending_without_deleting(feedback_service):
    s = feedback_service
    cid, rid, _ = await reported(s)
    pending_reply = await reply(s, 333, cid)
    await action(s, 111, "mod_resolve_save", {"id": rid, "accept": True})
    assert (await ModerationService(s).report_detail(111, rid))["status"] == "accepted"
    assert not (await ConversationService(s).thread(222, cid))["can_reply"]
    with pytest.raises(InputError):
        await ConversationService(s).submit_reply(333, pending_reply)
    assert await count(s, "messages") == 1
    async with s.database.transaction() as conn:
        assert await conn.scalar(select(s.tables["outbox"].c.status)) == "suspended"
    bot = AsyncMock()
    assert not await DeliveryWorker(s, bot).run_once()
    bot.send_message.assert_not_called()


async def test_dismiss_leaves_conversation_open_and_no_duplicate(feedback_service):
    s = feedback_service
    cid, rid, data = await reported(s)
    await action(s, 111, "mod_resolve_save", {"id": rid, "accept": False})
    assert (await ConversationService(s).thread(222, cid))["can_reply"]
    assert (await ModerationService(s).reports(111, "dismissed"))[0]["id"] == rid
    with pytest.raises(InputError):
        await action(s, 222, "mod_report_save", data)
    with pytest.raises(InputError):
        await action(s, 111, "mod_resolve_save", {"id": rid, "accept": True})


async def test_review_concurrency_and_rollback(feedback_service, monkeypatch):
    s = feedback_service
    cid, rid, _ = await reported(s)
    moderation = ModerationService(s)
    original = moderation._close

    async def failure(conn, cid):
        await original(conn, cid)
        raise RuntimeError("Simulated failure after closure")

    monkeypatch.setattr(moderation, "_close", failure)
    token = await s.issue(111, "mod_resolve_save", {"id": rid, "accept": True})
    with pytest.raises(RuntimeError):
        await moderation.execute(111, token)
    assert (await moderation.report_detail(111, rid))["status"] == "open"
    assert (await ConversationService(s).thread(222, cid))["can_reply"]
    monkeypatch.setattr(moderation, "_close", original)
    await s.identity.set_admin(111, 222, Role.INTERNAL)
    second = await s.issue(222, "mod_resolve_save", {"id": rid, "accept": False})
    results = await asyncio.gather(
        moderation.execute(111, token), moderation.execute(222, second), return_exceptions=True
    )
    assert sum(isinstance(r, str) for r in results) == 1
    assert sum(isinstance(r, InputError) for r in results) == 1


async def test_report_duplicate_click_and_tag_unlinkability(feedback_service):
    s = feedback_service
    cid = await internal_conversation(s)
    view = await ConversationService(s).thread(222, cid)
    token = await s.issue(
        222, "mod_report_save", {"cid": cid, "mid": view["message_id"], "reason": "spam"}
    )
    m = ModerationService(s)
    results = await asyncio.gather(
        m.execute(222, token), m.execute(222, token), return_exceptions=True
    )
    assert sum(isinstance(r, str) for r in results) == 1
    cid2 = await internal_conversation(s)
    view2 = await ConversationService(s).thread(222, cid2)
    await action(
        s, 222, "mod_report_save", {"cid": cid2, "mid": view2["message_id"], "reason": "spam"}
    )
    async with s.database.transaction() as conn:
        tags = (await conn.execute(select(s.tables["reports"].c.reporter_tag))).scalars().all()
    assert len(tags) == len(set(tags)) == 2


async def test_scope_authorization_before_admin_queries(external, feedback_service, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Unauthorized domain query")

    monkeypatch.setattr(feedback_service.database, "transaction", blocked)
    moderation = ModerationService(feedback_service)
    for call in [
        moderation.reports(222),
        moderation.report_detail(222, "x"),
        moderation.feedback_list(222),
        moderation.initial_feedback(222, "x"),
    ]:
        with pytest.raises(AccessDenied):
            await call
    await external.identity.set_admin(111, 333, Role.INTERNAL)
    monkeypatch.setattr(external.database, "transaction", blocked)
    with pytest.raises(AccessDenied):
        await ModerationService(external).reports(333)


async def test_admin_views_never_decrypt_routing_and_hide_reply_linkage(
    feedback_service, monkeypatch
):
    s = feedback_service
    cid = await internal_conversation(s)
    cs = ConversationService(s)
    await cs.submit_reply(222, await reply(s, 222, cid))
    mid = (await cs.thread(333, cid, 1))["message_id"]
    await action(s, 333, "mod_report_save", {"cid": cid, "mid": mid, "reason": "other"})
    original = s.unseal

    def content_only(value, record, field, purpose=Purpose.ROUTING):
        assert purpose == Purpose.CONTENT
        return original(value, record, field, purpose)

    monkeypatch.setattr(s, "unseal", content_only)
    moderation = ModerationService(s)
    rid = (await moderation.reports(111))[0]["id"]
    detail = await moderation.report_detail(111, rid)
    assert set(detail) == {"id", "reason", "status", "resolution", "body"}
    assert "همکار گیرنده" not in str(detail)
    assert cid not in str(detail) and mid not in str(detail)
    first = await moderation.initial_feedback(111, cid)
    assert first["body"] == BODY
    assert set(first) == {"id", "kind", "status", "subject", "body"}
    assert "همکار فرستنده" not in str(first)
    assert len(await moderation.feedback_list(111)) == 1


async def test_participant_closure_and_third_party_denial(feedback_service):
    s = feedback_service
    cid = await internal_conversation(s)
    with pytest.raises(AccessDenied):
        await action(s, 111, "mod_close_save", {"cid": cid})
    await action(s, 222, "mod_close_save", {"cid": cid})
    assert not (await ConversationService(s).thread(333, cid))["can_reply"]
    assert await count(s, "messages") == 1


async def test_revoked_admin_cannot_use_old_confirmation(external):
    s = external
    _, token = await external_compose(s)
    await s.submit(333, token)
    cid = (await ConversationService(s).history(333))[0]["id"]
    close = await s.issue(222, "mod_admin_close_save", {"cid": cid})
    await s.identity.revoke_admin(111, (await s.identity.authenticate(222)).user_id)
    with pytest.raises(AccessDenied):
        await ModerationService(s).execute(222, close)
    assert (await ConversationService(s).thread(333, cid))["can_reply"]


async def test_external_member_reports_manager_response(external):
    s = external
    _, token = await external_compose(s)
    await s.submit(333, token)
    cs = ConversationService(s)
    cid = (await cs.history(333))[0]["id"]
    await cs.submit_reply(222, await reply(s, 222, cid, True))
    mid = (await cs.thread(333, cid, 1))["message_id"]
    await action(s, 333, "mod_report_save", {"cid": cid, "mid": mid, "reason": "threat"})
    moderation = ModerationService(s)
    rid = (await moderation.reports(111))[0]["id"]
    await action(s, 111, "mod_resolve_save", {"id": rid, "accept": True})
    assert not (await cs.thread(333, cid))["can_reply"]


async def test_report_and_review_ui_survives_restart(feedback_service):
    s = feedback_service
    await internal_conversation(s)
    chat = ChatHarness(s.identity, s)
    await chat.text("/start", 222)
    await chat.click("گفتگوهای داخلی", 222)
    await chat.click("دریافتی", 222)
    await chat.click("فرستنده ناشناس", 222)
    await chat.click("گزارش پیام", 222)
    await chat.click("توهین", 222)
    token = chat.button("ثبت گزارش")
    chat = ChatHarness(s.identity, s)
    await chat.feed(callback_update(user_id=222, data=token))
    assert "گزارش ثبت شد" in chat.latest[-1].text
    await chat.text("/admin", 111)
    await chat.click("نظارت و گزارش‌های داخلی", 111)
    await chat.click("بررسی گزارش", 111)
    await chat.click("گزارش 1", 111)
    assert BODY in chat.latest[-1].text
    assert "همکار فرستنده" not in chat.latest[-1].text
    await chat.click("پذیرش و بستن", 111)
    await chat.click("تأیید تصمیم", 111)
    assert "نتیجه بررسی ثبت شد" in chat.latest[-1].text


async def test_admin_initial_inbox_and_close_ui(feedback_service):
    s = feedback_service
    cid = await internal_conversation(s)
    chat = ChatHarness(s.identity, s)
    await chat.text("/admin")
    await chat.click("نظارت و گزارش‌های داخلی")
    await chat.click("بازخوردهای اولیه")
    await chat.click("همکار گیرنده")
    assert BODY in chat.latest[-1].text
    await chat.click("بستن گفتگو")
    await chat.click("✅ بستن گفتگو")
    assert not (await ConversationService(s).thread(222, cid))["can_reply"]
