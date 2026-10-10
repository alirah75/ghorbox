import pytest

from app.core.permissions import AccessDenied, Role
from app.core.security import Purpose
from app.services.conversation_service import ConversationService
from app.services.moderation_service import ModerationService
from tests.test_admin_flows import ChatHarness
from tests.test_bootstrap import callback_update
from tests.test_conversations import internal_conversation
from tests.test_external import external as external
from tests.test_feedback import BODY, count


async def second_group_conversation(service):
    identity = service.identity
    manager_id = await identity.add_user(111, 888, "مدیر گروه دوم")
    await identity.set_admin(111, 888, Role.INTERNAL)
    group_id = await identity.create_internal_group(111, "گروه دوم", manager_id)
    recipient_id = await identity.add_user(888, 777, "گیرنده گروه دوم")
    await identity.add_user(888, 666, "فرستنده گروه دوم")
    await identity.authenticate(777, started=True)
    await identity.authenticate(666, started=True)
    await service.begin(666, recipient_id, "criticism")
    await service.submit_body(666, "این بازخورد فقط باید برای مدیر گروه دوم دیده شود.")
    cid = (await ConversationService(service).history(666))[0]["id"]
    return cid, group_id


@pytest.mark.parametrize(
    ("actor", "action"),
    [
        (222, "mod_report_save"),
        (222, "mod_close_save"),
        (111, "mod_admin_close_save"),
    ],
)
async def test_removed_report_and_close_callbacks_are_rejected(feedback_service, actor, action):
    service = feedback_service
    cid = await internal_conversation(service)
    view = await ConversationService(service).thread(222, cid)
    token = await service.issue(
        actor,
        action,
        {"cid": cid, "mid": view["message_id"], "reason": "insult"},
    )
    chat = ChatHarness(service.identity, service)
    await chat.feed(callback_update(user_id=actor, data=token))
    assert chat.latest[-1].text == "دسترسی فعال نیست."
    assert (await ConversationService(service).thread(222, cid))["can_reply"]
    assert await count(service, "reports") == 0


async def test_scope_authorization_before_admin_queries(external, feedback_service, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Unauthorized domain query")

    monkeypatch.setattr(feedback_service.database, "transaction", blocked)
    moderation = ModerationService(feedback_service)
    for call in [moderation.feedback_list(222), moderation.initial_feedback(222, "x")]:
        with pytest.raises(AccessDenied):
            await call
    await external.identity.set_admin(111, 333, Role.INTERNAL)
    monkeypatch.setattr(external.database, "transaction", blocked)
    with pytest.raises(AccessDenied):
        await ModerationService(external).feedback_list(333)


async def test_admin_views_never_decrypt_routing(feedback_service, monkeypatch):
    service = feedback_service
    cid = await internal_conversation(service)
    original = service.unseal

    def content_only(value, record, field, purpose=Purpose.ROUTING):
        assert purpose == Purpose.CONTENT
        return original(value, record, field, purpose)

    monkeypatch.setattr(service, "unseal", content_only)
    moderation = ModerationService(service)
    first = await moderation.initial_feedback(111, cid)
    assert first["body"] == BODY
    assert set(first) == {"id", "kind", "status", "subject", "group", "body"}
    assert first["group"] == "گروه آزمایش"
    assert "همکار فرستنده" not in str(first)
    assert len(await moderation.feedback_list(111)) == 1


async def test_internal_manager_reads_only_current_group_feedback(feedback_service):
    service = feedback_service
    first_cid = await internal_conversation(service)
    second_cid, second_group = await second_group_conversation(service)
    moderation = ModerationService(service)

    first_manager = await moderation.feedback_list(999)
    second_manager = await moderation.feedback_list(888)
    main = await moderation.feedback_list(111)
    assert [(item["id"], item["group"]) for item in first_manager] == [(first_cid, "گروه آزمایش")]
    assert [(item["id"], item["group"]) for item in second_manager] == [(second_cid, "گروه دوم")]
    assert {item["id"] for item in main} == {first_cid, second_cid}
    with pytest.raises(AccessDenied):
        await moderation.initial_feedback(999, second_cid)
    with pytest.raises(AccessDenied):
        await moderation.initial_feedback(888, first_cid)

    first_recipient = (await service.identity.authenticate(222)).user_id
    await service.identity.assign_internal_group_member(111, second_group, first_recipient)
    assert await moderation.feedback_list(999) == []
    assert {item["id"] for item in await moderation.feedback_list(888)} == {
        first_cid,
        second_cid,
    }


async def test_report_and_close_controls_are_absent_from_user_and_admin_ui(
    feedback_service,
):
    service = feedback_service
    await internal_conversation(service)
    chat = ChatHarness(service.identity, service)
    await chat.text("/start", 222)
    await chat.click("گفتگوهای ناشناس", 222)
    await chat.click("تاریخچه", 222)
    await chat.click("آغازشده", 222)
    await chat.click("فرستنده ناشناس", 222)
    for label in ("گزارش پیام", "بستن گفتگو"):
        with pytest.raises(AssertionError, match="Missing button"):
            chat.button(label)

    await chat.text("/admin", 111)
    await chat.click("بازخوردهای اولیه داخلی", 111)
    assert "گزارش" not in chat.latest[-1].text
    with pytest.raises(AssertionError, match="Missing button"):
        chat.button("بررسی گزارش")
    await chat.click("بازخوردهای اولیه", 111)
    await chat.click("همکار گیرنده", 111)
    assert BODY in chat.latest[-1].text
    with pytest.raises(AssertionError, match="Missing button"):
        chat.button("بستن گفتگو")


async def test_internal_manager_ui_is_labeled_for_own_group(feedback_service):
    service = feedback_service
    await internal_conversation(service)
    await second_group_conversation(service)
    chat = ChatHarness(service.identity, service)
    await chat.text("/admin", 999)
    await chat.click("بازخوردهای گروه من", 999)
    assert "گروه شما" in chat.latest[-1].text
    await chat.click("بازخوردهای اولیه", 999)
    assert chat.button("همکار گیرنده")
    with pytest.raises(AssertionError, match="Missing button"):
        chat.button("گیرنده گروه دوم")
