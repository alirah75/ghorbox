import asyncio

import pytest
from sqlalchemy import func, select

from app.core.permissions import AccessDenied, InputError, Role
from app.services.feedback_service import FeedbackService, validate_body

BODY = "بهتر است جلسات هفتگی منظم برگزار شوند."


async def compose(service, actor=333):
    recipient = await service.identity.authenticate(222)
    await service.begin(actor, recipient.user_id, "suggestion")
    _, token = await service.preview(actor, BODY)
    return token


async def count(service, name):
    async with service.database.transaction() as conn:
        return (await conn.execute(select(func.count()).select_from(service.tables[name]))).scalar()


@pytest.mark.parametrize(
    "body", ["", "    ", "سلام", "\u200c" * 6, "متن\u202eمخرب", "x" * 3001, "😀" * 1501]
)
def test_invalid_text(body):
    with pytest.raises(InputError):
        validate_body(body)


def test_valid_persian_and_utf16_boundary():
    assert validate_body("  می‌توان\nبهتر نوشت  ") == "می‌توان\nبهتر نوشت"
    assert validate_body("😀" * 1500) == "😀" * 1500


async def test_colleagues_exclude_self_and_disabled(feedback_service):
    service = feedback_service
    self_id = (await service.identity.authenticate(333)).user_id
    rows = await service.colleagues(333)
    assert all(item["id"] != self_id for item in rows)
    assert all(set(item) == {"id", "display_name"} for item in rows)
    recipient = await service.identity.authenticate(222)
    await service.identity.update_user(111, recipient.user_id, active=False)
    assert recipient.user_id not in [item["id"] for item in await service.colleagues(333)]


async def test_colleagues_and_direct_target_are_limited_to_own_group(feedback_service):
    service = feedback_service
    manager_id = await service.identity.add_user(111, 888, "مدیر گروه دوم")
    await service.identity.set_admin(111, 888, Role.INTERNAL)
    group_id = await service.identity.create_internal_group(111, "گروه دوم", manager_id)
    outsider_id = await service.identity.add_user(888, 777, "عضو گروه دوم")
    await service.identity.authenticate(777, started=True)

    assert outsider_id not in [item["id"] for item in await service.colleagues(333)]
    with pytest.raises(AccessDenied):
        await service.begin(333, outsider_id, "suggestion")
    assert (await service.identity.list_internal_group_members(888, group_id))[0][
        "id"
    ] == outsider_id


async def test_self_and_unstarted_recipient_rejected(feedback_service):
    service = feedback_service
    user = await service.identity.authenticate(333)
    with pytest.raises(InputError):
        await service.begin(333, user.user_id, "suggestion")
    new = await service.identity.add_user(111, 444, "عضو تازه")
    with pytest.raises(InputError):
        await service.begin(333, new, "suggestion")


async def test_disabled_sender_cannot_submit(feedback_service):
    token = await compose(feedback_service)
    user = await feedback_service.identity.authenticate(333)
    await feedback_service.identity.update_user(111, user.user_id, active=False)
    with pytest.raises(AccessDenied):
        await feedback_service.submit(333, token)
    assert await count(feedback_service, "messages") == 0


async def test_recipient_rechecked_at_confirmation(feedback_service):
    token = await compose(feedback_service)
    user = await feedback_service.identity.authenticate(222)
    await feedback_service.identity.update_user(111, user.user_id, active=False)
    with pytest.raises(InputError):
        await feedback_service.submit(333, token)
    assert await count(feedback_service, "outbox") == 0


async def test_atomic_submission_and_concurrent_double_click(feedback_service):
    token = await compose(feedback_service)
    results = await asyncio.gather(
        feedback_service.submit(333, token),
        feedback_service.submit(333, token),
        return_exceptions=True,
    )
    assert sum(isinstance(item, InputError) for item in results) == 1
    for table in ("messages", "conversations", "outbox", "weekly_contributors"):
        assert await count(feedback_service, table) == 1
    assert await count(feedback_service, "participant_access") == 2


async def test_direct_submission_is_atomic_and_activates_anonymous_chat(feedback_service):
    service = feedback_service
    recipient = await service.identity.authenticate(222)
    await service.begin(333, recipient.user_id, "suggestion")
    results = await asyncio.gather(
        service.submit_body(333, BODY),
        service.submit_body(333, BODY),
        return_exceptions=True,
    )
    assert sum(isinstance(item, str) for item in results) == 1
    assert sum(isinstance(item, InputError) for item in results) == 1
    assert await count(service, "messages") == 1
    assert (await service.draft(333))["action"] == "chat_active"


async def test_token_ownership_expiry_and_cancel(feedback_service):
    service = feedback_service
    token = await compose(service)
    with pytest.raises(InputError):
        await service.submit(222, token)
    await service.cancel(333)
    with pytest.raises(InputError):
        await service.submit(333, token)
    token = await compose(service)
    now = service.clock()
    service.clock = lambda: now + 601
    with pytest.raises(InputError):
        await service.submit(333, token)


async def test_draft_and_confirmation_survive_restart(feedback_service):
    service = feedback_service
    recipient = await service.identity.authenticate(222)
    await service.begin(333, recipient.user_id, "criticism")
    restarted = FeedbackService(service.identity, service.database, service.cipher)
    assert (await restarted.draft(333))["action"] == "compose"
    _, token = await restarted.preview(333, BODY)
    again = FeedbackService(service.identity, service.database, service.cipher)
    receipt = await again.submit(333, token)
    assert (await again.delivery_status(333, receipt))[0] == "pending"


async def test_failure_after_outbox_insert_rolls_everything_back(feedback_service, monkeypatch):
    token = await compose(feedback_service)
    original = feedback_service._session

    async def fail(*args, **kwargs):
        raise RuntimeError("simulated commit-stage failure")

    monkeypatch.setattr(feedback_service, "_session", fail)
    with pytest.raises(RuntimeError):
        await feedback_service.submit(333, token)
    for table in (
        "messages",
        "conversations",
        "outbox",
        "participant_access",
        "weekly_contributors",
    ):
        assert await count(feedback_service, table) == 0
    monkeypatch.setattr(feedback_service, "_session", original)
    await feedback_service.submit(333, token)


async def test_no_plaintext_content_or_sender_identity_in_domain_files(feedback_service):
    service = feedback_service
    token = await compose(service)
    for path in service.database.path.parent.glob("internal.db*"):
        assert BODY.encode() not in path.read_bytes()
    await service.submit(333, token)
    for path in service.database.path.parent.glob("internal.db*"):
        data = path.read_bytes()
        assert BODY.encode() not in data
        assert "همکار فرستنده".encode() not in data
    assert (await service.draft(333))["action"] == "chat_active"
