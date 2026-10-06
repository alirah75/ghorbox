import asyncio
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import select, update

from app.core.permissions import AccessDenied
from app.core.security import Purpose
from app.services.statistics_service import StatisticsService, bucket, latest_period
from app.workers.delivery import DeliveryWorker
from tests.test_admin_flows import ChatHarness
from tests.test_conversations import reply
from tests.test_external import external as external
from tests.test_feedback import BODY, count

ZONE = ZoneInfo("Asia/Tehran")


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZONE).timestamp()


async def seed(s, authors=5, kinds=("suggestion",), extra_small=False):
    s.clock = lambda: stamp("2026-09-22T12:00:00")
    target = (await s.identity.authenticate(222)).user_id
    if s.scope.value == "external":
        target = await s.save_person(111, "فرد خارجی آماری")
    for index in range(authors):
        actor = 4000 + index
        await s.identity.add_user(111, actor, f"عضو آماری {index}")
        await s.identity.authenticate(actor, started=True)
        for kind in kinds:
            await s.begin(actor, target, kind)
            _, token = await s.preview(actor, BODY)
            await s.submit(actor, token)
    if extra_small:
        second = (await s.identity.authenticate(111)).user_id
        if s.scope.value == "external":
            second = await s.save_person(111, "فرد کم‌تعداد")
        await s.begin(4000, second, "criticism")
        _, token = await s.preview(4000, BODY)
        await s.submit(4000, token)
    s.clock = lambda: stamp("2026-09-26T09:00:00")
    return target


async def payload(s):
    async with s.database.transaction() as conn:
        w = s.tables["weekly_reports"]
        row = (await conn.execute(select(w).order_by(w.c.period_end.desc()))).mappings().first()
        return s.unseal(row["payload_cipher"], row["id"], "weekly_report", Purpose.CONTENT)


async def finish_ordinary_jobs(s):
    async with s.database.transaction(write=True) as conn:
        o = s.tables["outbox"]
        await conn.execute(update(o).where(o.c.message_id.is_not(None)).values(status="sent"))


def test_saturday_release_boundary_and_buckets():
    before = latest_period(stamp("2026-09-26T08:59:59"), ZONE)
    after = latest_period(stamp("2026-09-26T09:00:00"), ZONE)
    assert before[1] == after[0] == stamp("2026-09-19T00:00:00")
    assert after[1] == stamp("2026-09-26T00:00:00")
    assert latest_period(stamp("2026-10-02T23:59:59"), ZONE) == after
    assert [bucket(n) for n in [0, 1, 4, 5, 9, 10]] == [None, None, None, "5–9", "5–9", "10–14"]


@pytest.mark.parametrize("authors", [0, 1, 4])
async def test_small_cohort_suppresses_all_counts(feedback_service, authors):
    s = feedback_service
    await seed(s, authors)
    await StatisticsService(s).tick()
    data = await payload(s)
    assert data["hidden"] and data["counts"] == {} and data["recipients"] is None
    assert "4000" not in str(data) and "همکار گیرنده" not in str(data)


async def test_cell_and_complementary_suppression(feedback_service):
    s = feedback_service
    await seed(s, extra_small=True)
    await StatisticsService(s).tick()
    data = await payload(s)
    assert not data["hidden"]
    assert data["counts"]["initial"] == "5–9"
    assert data["counts"]["criticism"] is None
    assert data["counts"]["suggestion"] is None
    assert data["recipients"] is None  # Hide the whole dimension, not just the small remainder.


async def test_safe_breakdowns_and_no_sender_identifiers(external, feedback_service):
    for s in (feedback_service, external):
        # Shared identity fixture: use internal authors for external submissions after seeding once.
        if s is feedback_service:
            await seed(s, kinds=("suggestion", "criticism"))
        else:
            s.clock = lambda: stamp("2026-09-22T12:00:00")
            target = await s.save_person(111, "شخص آماری خارجی")
            for actor in range(4000, 4005):
                for kind in ("criticism", "suggestion"):
                    await s.begin(actor, target, kind)
                    _, token = await s.preview(actor, BODY)
                    await s.submit(actor, token)
            s.clock = lambda: stamp("2026-09-26T09:00:00")
        await StatisticsService(s).tick()
        data = await payload(s)
        assert data["counts"]["initial"] == "10–14"
        assert data["counts"]["criticism"] == data["counts"]["suggestion"] == "5–9"
        assert data["recipients"][0]["count"] == "10–14"
        assert "عضو آماری" not in str(data) and BODY not in str(data)
        if s is external:
            assert "active_members" not in data["counts"]
            assert "همکار گیرنده" not in str(data)


async def test_frozen_snapshot_concurrency_and_atomic_fanout(feedback_service, monkeypatch):
    s = feedback_service
    await seed(s)
    statistics = StatisticsService(s)
    original = statistics._queue

    async def fail(*args):
        await original(*args)
        raise RuntimeError("Simulated failure after fanout")

    monkeypatch.setattr(statistics, "_queue", fail)
    with pytest.raises(RuntimeError):
        await statistics.tick()
    assert await count(s, "weekly_reports") == 0
    assert await count(s, "outbox") == 5
    monkeypatch.setattr(statistics, "_queue", original)
    await asyncio.gather(statistics.tick(), StatisticsService(s).tick())
    assert await count(s, "weekly_reports") == 1
    assert await count(s, "outbox") == 6
    before = await payload(s)
    await s.identity.add_user(111, 9999, "عضو جدید")
    await statistics.tick()
    assert await payload(s) == before
    assert await count(s, "outbox") == 6


async def test_downtime_catchup_is_bounded_and_restart_idempotent(feedback_service):
    s = feedback_service
    await seed(s)
    await StatisticsService(s).tick()
    s.clock = lambda: stamp("2026-11-28T09:00:00")
    await StatisticsService(s).tick()
    assert await count(s, "weekly_reports") == 5
    await StatisticsService(s).tick()
    await StatisticsService(s).tick()
    assert await count(s, "weekly_reports") == 10
    await StatisticsService(s).tick()
    assert await count(s, "weekly_reports") == 10


async def test_no_current_week_publication_and_reply_counts_separate(feedback_service):
    s = feedback_service
    await seed(s)
    s.clock = lambda: stamp("2026-09-26T08:00:00")
    await StatisticsService(s).tick()
    assert (await payload(s))["end"] == "2026-09-19"
    from app.services.conversation_service import ConversationService

    cs = ConversationService(s)
    cid = (await cs.history(4000))[0]["id"]
    s.clock = lambda: stamp("2026-09-23T12:00:00")
    for _ in range(5):
        await cs.submit_reply(222, await reply(s, 222, cid))
    s.clock = lambda: stamp("2026-09-26T09:00:00")
    await StatisticsService(s).tick()
    data = await payload(s)
    assert data["counts"]["initial"] == data["counts"]["replies"] == "5–9"
    assert await count(s, "weekly_contributors") == 5


async def test_permission_checks_before_query_and_revocation(
    external, feedback_service, monkeypatch
):
    statistics = StatisticsService(feedback_service)

    def blocked(*args, **kwargs):
        raise AssertionError("Unauthorized domain query")

    monkeypatch.setattr(feedback_service.database, "transaction", blocked)
    with pytest.raises(AccessDenied):
        await statistics.archive(222)
    with pytest.raises(AccessDenied):
        await statistics.view(222, "anything")
    with pytest.raises(AccessDenied):
        await StatisticsService(external).archive(333)


async def test_weekly_delivery_scope_dedupe_and_terminal_state(external):
    s = external
    await seed(s)
    statistics = StatisticsService(s)
    await statistics.tick()
    bot = AsyncMock()
    worker = DeliveryWorker(s, bot)
    assert await worker.run_once()
    assert await worker.run_once()
    assert not await worker.run_once()
    calls = [c.kwargs for c in bot.send_message.call_args_list]
    assert {c["chat_id"] for c in calls} == {111, 222}
    assert all("حوزه خارجی" in c["text"] and "داخلی" not in c["text"] for c in calls)
    assert all(c["protect_content"] and c["parse_mode"] is None for c in calls)
    await statistics.tick()
    assert not await worker.run_once()
    async with s.database.transaction() as conn:
        assert await conn.scalar(select(s.tables["weekly_reports"].c.status)) == "complete"


async def test_revoked_weekly_recipient_is_suspended(external):
    s = external
    await seed(s)
    await StatisticsService(s).tick()
    await s.identity.revoke_admin(111, (await s.identity.authenticate(222)).user_id)
    bot = AsyncMock()
    worker = DeliveryWorker(s, bot)
    await worker.run_once()
    await worker.run_once()
    assert [c.kwargs["chat_id"] for c in bot.send_message.call_args_list] == [111]


@pytest.mark.parametrize(
    "error,status",
    [
        (
            TelegramNetworkError(method=SendMessage(chat_id=111, text="x"), message="failure"),
            "uncertain",
        ),
        (
            TelegramRetryAfter(
                method=SendMessage(chat_id=111, text="x"), message="slow", retry_after=60
            ),
            "pending",
        ),
    ],
)
async def test_weekly_delivery_failure_policy(feedback_service, error, status):
    s = feedback_service
    await seed(s)
    await finish_ordinary_jobs(s)
    await StatisticsService(s).tick()
    bot = AsyncMock()
    bot.send_message.side_effect = error
    worker = DeliveryWorker(s, bot)
    await worker.run_once()
    async with s.database.transaction() as conn:
        o = s.tables["outbox"]
        assert (
            await conn.scalar(select(o.c.status).where(o.c.weekly_report_id.is_not(None))) == status
        )
    assert not await worker.run_once()


async def test_weekly_archive_ui(feedback_service):
    s = feedback_service
    await seed(s)
    await StatisticsService(s).tick()
    chat = ChatHarness(s.identity, s)
    await chat.text("/admin")
    await chat.click("آمار هفتگی داخلی")
    await chat.click("هفته شروع‌شده")
    assert "5–9" in chat.latest[-1].text
    assert "عضو آماری" not in chat.latest[-1].text
    assert chat.latest[-1].protect_content


async def test_crash_after_claim_never_blindly_resends_weekly(feedback_service):
    s = feedback_service
    await seed(s)
    await finish_ordinary_jobs(s)
    await StatisticsService(s).tick()
    bot = AsyncMock()
    assert await DeliveryWorker(s, bot).claim()
    old = s.clock()
    s.clock = lambda: old + 121
    assert not await DeliveryWorker(s, bot).run_once()
    await StatisticsService(s).tick()
    assert not await DeliveryWorker(s, bot).run_once()
    bot.send_message.assert_not_called()
    async with s.database.transaction() as conn:
        o = s.tables["outbox"]
        assert (
            await conn.scalar(select(o.c.status).where(o.c.weekly_report_id.is_not(None)))
            == "uncertain"
        )


async def test_newly_eligible_admin_receives_only_latest_once(feedback_service):
    s = feedback_service
    await seed(s)
    await finish_ordinary_jobs(s)
    stats = StatisticsService(s)
    await stats.tick()
    s.clock = lambda: stamp("2026-10-03T09:00:00")
    await stats.tick()
    from app.core.permissions import Role

    await s.identity.set_admin(111, 222, Role.INTERNAL)
    await stats.tick()
    await stats.tick()
    async with s.database.transaction() as conn:
        o = s.tables["outbox"]
        jobs = (
            (await conn.execute(select(o).where(o.c.weekly_report_id.is_not(None))))
            .mappings()
            .all()
        )
    recipient = (await s.identity.authenticate(222)).user_id
    own = [
        job
        for job in jobs
        if s.unseal(job["destination_cipher"], job["id"], "destination") == recipient
    ]
    assert len(own) == 1
    assert own[0]["weekly_report_id"] == (await stats.archive(222))[0]["id"]


async def test_snapshot_remains_unchanged_after_source_removal(feedback_service):
    from sqlalchemy import delete

    s = feedback_service
    await seed(s)
    stats = StatisticsService(s)
    await stats.tick()
    before = await payload(s)
    async with s.database.transaction(write=True) as conn:
        # Future retention must not rebuild an already released statistical snapshot.
        await conn.execute(delete(s.tables["weekly_contributors"]))
    await stats.tick()
    assert await payload(s) == before


async def test_scheduler_failure_in_one_scope_does_not_stop_other(monkeypatch):
    from app.workers.statistics import run_statistics

    seen = []

    class FakeStatistics:
        def __init__(self, domain):
            self.domain = domain

        async def tick(self):
            seen.append(self.domain)
            if self.domain == "internal":
                raise RuntimeError("Private exception must not be logged")

    async def stop(seconds):
        assert seconds == 60
        raise asyncio.CancelledError

    monkeypatch.setattr("app.workers.statistics.StatisticsService", FakeStatistics)
    monkeypatch.setattr("app.workers.statistics.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await run_statistics("internal", "external")
    assert seen == ["internal", "external"]
