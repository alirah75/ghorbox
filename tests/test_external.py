import asyncio

import pytest
from sqlalchemy import select

from app.core.permissions import AccessDenied, InputError, Role
from app.core.security import Keyring, Scope, ScopeCipher
from app.database.storage import Database
from app.services.external_service import ExternalService
from tests.test_admin_flows import ChatHarness
from tests.test_bootstrap import callback_update
from tests.test_feedback import BODY, count


@pytest.fixture
async def external(feedback_service, tmp_path):
    db = Database(tmp_path / "data" / "external.db", "external")
    service = ExternalService(
        feedback_service.identity,
        db,
        ScopeCipher(Keyring.load(tmp_path / "keys" / "keyring.json"), Scope.EXTERNAL),
    )
    await service.identity.set_admin(111, 222, Role.EXTERNAL)
    try:
        yield service
    finally:
        await db.close()


async def compose(service):
    person = await service.save_person(222, "فرد خارجی", "تأمین‌کننده")
    await service.begin(333, person, "suggestion")
    _, token = await service.preview(333, BODY)
    return person, token


async def test_external_atomic_encrypted_submission(external, feedback_service):
    _, token = await compose(external)
    results = await asyncio.gather(
        external.submit(333, token), external.submit(333, token), return_exceptions=True
    )
    assert sum(isinstance(r, str) for r in results) == 1
    assert sum(isinstance(r, InputError) for r in results) == 1
    assert await count(external, "messages") == 1
    assert await count(external, "participant_access") == 1
    assert await count(external, "weekly_contributors") == 1
    assert await count(external, "outbox") == 0  # No immediate ordinary-feedback alerts.
    assert await count(feedback_service, "messages") == 0
    inbox = await external.inbox(222)
    assert set(inbox[0]) == {"id", "kind", "subject"}
    data = await external.read_feedback(222, inbox[0]["id"])
    assert set(data) == {"subject", "kind", "body", "conversation_id"}
    assert data["body"] == BODY
    async with external.database.transaction() as conn:
        for name in ("messages", "conversations", "participant_access"):
            rows = (await conn.execute(select(external.tables[name]))).mappings().all()
            assert BODY not in str(rows)
            assert (await external.identity.authenticate(333)).user_id not in str(rows)


async def test_authorization_precedes_external_queries(external, monkeypatch):
    await external.identity.set_admin(111, 333, Role.INTERNAL)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unauthorized domain query")

    monkeypatch.setattr(external.database, "transaction", forbidden)
    for call in [
        external.inbox(333),
        external.read_feedback(333, "missing"),
        external.proposals(333),
        external.save_person(333, "فرد تازه"),
        external.review(333, "missing", True),
        external.set_active(333, "missing", False),
        external.people(333, management=True),
    ]:
        with pytest.raises(AccessDenied):
            await call


async def test_external_admin_cannot_manage_internal_users(external):
    with pytest.raises(AccessDenied):
        await external.identity.list_users(222)
    with pytest.raises(AccessDenied):
        await external.identity.list_admins(222)


async def test_deactivated_person_rechecked_at_submit(external):
    person, token = await compose(external)
    await external.set_active(222, person, False)
    with pytest.raises(InputError):
        await external.submit(333, token)
    assert await count(external, "messages") == 0
    assert await external.people(333) == []
    await external.set_active(222, person, True)
    await external.submit(333, token)


async def test_proposals_review_atomic_and_no_proposer_identity(external):
    await external.propose(333, "شخص پیشنهادی", "خرید")
    assert await external.people(333) == []
    rows = await external.proposals(222)
    assert set(rows[0]) == {"id", "name", "department"}
    results = await asyncio.gather(
        external.review(222, rows[0]["id"], True),
        external.review(111, rows[0]["id"], True),
        return_exceptions=True,
    )
    assert sum(isinstance(r, InputError) for r in results) == 1
    assert len(await external.people(333)) == 1
    assert await external.proposals(222) == []
    async with external.database.transaction() as conn:
        row = (
            (await conn.execute(select(external.tables["external_person_proposals"])))
            .mappings()
            .one()
        )
        assert row["notification_route_cipher"] is None


async def test_revoked_admin_old_buttons_and_read_denied(external):
    _, token = await compose(external)
    mid = await external.submit(333, token)
    await external.identity.revoke_admin(111, (await external.identity.authenticate(222)).user_id)
    with pytest.raises(AccessDenied):
        await external.read_feedback(222, mid)
    with pytest.raises(AccessDenied):
        await external.inbox(222)


async def test_owner_binding_expiry_and_scope(external, feedback_service):
    _, token = await compose(external)
    with pytest.raises(InputError):
        await external.submit(222, token)
    with pytest.raises(InputError):
        await feedback_service.peek(333, token)
    now = external.clock()
    external.clock = lambda: now + 601
    with pytest.raises(InputError):
        await external.submit(333, token)


async def test_feedback_rollback(external, monkeypatch):
    _, token = await compose(external)

    async def fail(*args):
        raise RuntimeError("Simulated failure")

    monkeypatch.setattr(external, "_record_contributor", fail)
    with pytest.raises(RuntimeError):
        await external.submit(333, token)
    assert await count(external, "messages") == 0
    assert await count(external, "conversations") == 0
    assert (await external.peek(333, token))[0] == "confirm"


async def test_complete_external_user_and_admin_flow(external, feedback_service):
    chat = ChatHarness(external.identity, feedback_service, external)
    await chat.text("/start", 333)
    await chat.click("افراد خارج", 333)
    await chat.click("فهرست افراد", 333)
    await chat.click("پیشنهاد فرد", 333)
    await chat.text("آقای آزمایش | فروش", 333)
    await chat.click("تأیید", 333)
    await chat.text("/admin", 222)
    await chat.click("مدیریت بخش خارجی", 222)
    await chat.click("پیشنهادهای", 222)
    await chat.click("آقای آزمایش", 222)
    await chat.click("پذیرش", 222)
    await chat.text("/start", 333)
    await chat.click("افراد خارج", 333)
    await chat.click("فهرست افراد", 333)
    await chat.click("آقای آزمایش", 333)
    await chat.click("پیشنهاد", 333)
    await chat.text(BODY, 333)
    token = chat.button("ارسال")
    # Dispatcher restart preserves encrypted confirmation.
    chat = ChatHarness(external.identity, feedback_service, external)
    await chat.feed(callback_update(user_id=333, data=token))
    assert "ثبت شد" in chat.latest[-1].text
    await chat.text("/admin", 222)
    await chat.click("مدیریت بخش خارجی", 222)
    await chat.click("صندوق", 222)
    await chat.click("آقای آزمایش", 222)
    assert BODY in chat.latest[-1].text
    assert "همکار فرستنده" not in chat.latest[-1].text
    assert chat.latest[-1].protect_content


async def test_switching_sections_cancels_previous_draft(external, feedback_service):
    await external.save_person(222, "شخص آزمایش")
    chat = ChatHarness(external.identity, feedback_service, external)
    await chat.text("/start", 333)
    internal_token = chat.button("همکاران داخل")
    await chat.click("افراد خارج", 333)
    await chat.click("فهرست افراد", 333)
    await chat.click("شخص آزمایش", 333)
    await chat.click("پیشنهاد", 333)
    assert await external.draft(333)
    await chat.feed(callback_update(user_id=333, data=internal_token))
    # Switching away invalidated the old home button; it must not disturb a live draft.
    assert await external.draft(333)
    fresh = await feedback_service.issue(333, "list", [0, ""])
    await chat.feed(callback_update(user_id=333, data=fresh))
    assert await external.draft(333) is None
    await chat.text("/cancel", 333)
    assert await feedback_service.draft(333) is None


async def test_management_edit_preserves_historical_subject(external, feedback_service):
    person, token = await compose(external)
    mid = await external.submit(333, token)
    chat = ChatHarness(external.identity, feedback_service, external)
    await chat.text("/start", 222)
    await chat.click("افراد خارج", 222)
    await chat.click("مدیریت افراد", 222)
    await chat.click("فرد خارجی", 222)
    await chat.click("اصلاح نام", 222)
    await chat.text("نام اصلاح شده | بخش جدید", 222)
    await chat.click("تأیید", 222)
    assert (await external.people(333))[0]["display_name"] == "نام اصلاح شده"
    assert (await external.read_feedback(222, mid))["subject"]["name"] == "فرد خارجی"
    await external.set_active(222, person, False)
    assert await external.people(333) == []
    with pytest.raises(InputError):
        await external.save_person(222, "نام اصلاح شده", "بخش جدید")


async def test_search_pagination_and_duplicate_proposal(external):
    for number in range(12):
        await external.save_person(222, f"شخص {number:02}")
    assert len(await external.people(333)) == 11
    assert len(await external.people(333, page=1)) == 2
    assert len(await external.people(333, search="شخص 02")) == 1
    assert await external.people(333, search="%") == []
    await external.propose(333, "شخص پیشنهادی")
    with pytest.raises(InputError):
        await external.propose(333, "شخص پیشنهادی")
    row = (await external.proposals(222))[0]
    await external.review(222, row["id"], False)
    assert await external.people(333, search="پیشنهادی") == []


async def test_disabled_sender_and_media_rejected(external, feedback_service):
    _, token = await compose(external)
    sender = await external.identity.authenticate(333)
    await external.identity.update_user(111, sender.user_id, active=False)
    with pytest.raises(AccessDenied):
        await external.submit(333, token)
    assert await count(external, "messages") == 0
    await external.identity.update_user(111, sender.user_id, active=True)
    chat = ChatHarness(external.identity, feedback_service, external)
    await external.form(333, "propose")
    from tests.test_bootstrap import message_update

    await chat.feed(message_update(user_id=333, text=None))
    assert "فقط متن" in chat.latest[-1].text
