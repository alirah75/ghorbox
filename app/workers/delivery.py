"""A single serial sender. Ambiguous network outcomes never trigger blind resends."""

import asyncio

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import Event, log_event
from app.core.permissions import AccessDenied
from app.core.security import Purpose, Scope
from app.database.identity_repository import IdentityRepository
from app.services.feedback_service import KINDS


class DeliveryWorker:
    def __init__(self, feedback, bot):
        self.feedback, self.bot = feedback, bot
        self.pause_until = 0

    async def claim(self):
        service = self.feedback
        table = service.tables["outbox"]
        now = int(service.clock())
        async with service.database.transaction(write=True) as conn:
            await service._clean(conn)
            # A crash after Telegram accepted a send is indistinguishable from a lost response.
            await conn.execute(
                update(table)
                .where(table.c.status == "sending", table.c.lease_until <= now)
                .values(status="uncertain", error_code="expired_lease", lease_until=None)
            )
            row = (
                (
                    await conn.execute(
                        select(table)
                        .where(
                            table.c.status == "pending",
                            table.c.next_attempt_at <= now,
                        )
                        .order_by(table.c.next_attempt_at, table.c.id)
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            await conn.execute(
                update(table)
                .where(table.c.id == row["id"])
                .values(status="sending", attempts=row["attempts"] + 1, lease_until=now + 120)
            )
            return {**row, "attempts": row["attempts"] + 1}

    async def outcome(self, job, status, error=None, delay=0):
        service = self.feedback
        table = service.tables["outbox"]
        async with service.database.transaction(write=True) as conn:
            await conn.execute(
                update(table)
                .where(table.c.id == job["id"], table.c.status == "sending")
                .values(
                    status=status,
                    error_code=error,
                    lease_until=None,
                    next_attempt_at=int(service.clock()) + delay,
                )
            )

            if job["weekly_report_id"] is not None:
                from app.services.statistics_service import StatisticsService

                await StatisticsService(service).reconcile(conn)

    async def prepare(self, job):
        service = self.feedback
        if job["weekly_report_id"] is not None:
            from app.services.statistics_service import StatisticsService

            prepared = await StatisticsService(service).prepare_delivery(job)
            if prepared is None:
                return None
            telegram_id, text, user_id = prepared
            token = await service.issue(
                telegram_id, "stats_view", {"id": job["weekly_report_id"], "page": 0}
            )
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📊 مشاهده گزارش هفتگی", callback_data=token)]
                ]
            )
            return telegram_id, text, user_id, markup
        messages, conversations = (service.tables[key] for key in ("messages", "conversations"))
        async with service.database.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(
                            messages.c.id,
                            messages.c.body_cipher,
                            messages.c.direction_cipher,
                            messages.c.sequence,
                            messages.c.conversation_id,
                            conversations.c.route_cipher,
                            conversations.c.kind,
                            conversations.c.status,
                            conversations.c.expires_at,
                        )
                        .join(conversations, messages.c.conversation_id == conversations.c.id)
                        .where(messages.c.id == job["message_id"])
                    )
                )
                .mappings()
                .one()
            )
        if row["status"] != "open" or row["expires_at"] <= int(service.clock()):
            return None
        route = service.unseal(row["route_cipher"], row["conversation_id"], "route")
        recipient_id = service.unseal(job["destination_cipher"], job["id"], "destination")
        direction = service.unseal(row["direction_cipher"], row["id"], "direction")
        role = direction["role"] if isinstance(direction, dict) else direction
        if service.scope == Scope.INTERNAL:
            if role not in {"sender", "recipient"}:
                raise ValueError("Invalid direction")
            expected = route["recipient"] if role == "sender" else route["sender"]
            required = {route["sender"], route["recipient"]}
        else:
            if not isinstance(direction, dict) or role != "manager":
                raise ValueError("Invalid external direction")
            expected = route["sender"]
            required = {route["sender"], direction["user"]}
            async with service.database.transaction() as conn:
                people = service.tables["external_people"]
                if not await conn.scalar(
                    select(people.c.id).where(
                        people.c.id == route["external_person"], people.c.is_active == 1
                    )
                ):
                    return None
        if recipient_id != expected:
            raise ValueError("Invalid destination binding")
        body = service.unseal(row["body_cipher"], row["id"], "body", Purpose.CONTENT)
        users = service.identity.tables["users"]
        async with service.identity.database.transaction() as conn:
            rows = (
                (
                    await conn.execute(
                        select(users).where(users.c.id.in_(required), users.c.is_active == 1)
                    )
                )
                .mappings()
                .all()
            )
            if len(rows) != len(required):
                return None
            target = next(item for item in rows if item["id"] == recipient_id)
            if target["started_at"] is None or target["delivery_state"] == "blocked":
                return None
            if service.scope == Scope.EXTERNAL:
                author = next(item for item in rows if item["id"] == direction["user"])
                try:
                    principal = await IdentityRepository(conn).authenticate(author["telegram_id"])
                    principal.require("external", "read_feedback")
                except AccessDenied:
                    return None
        heading = (
            "📩 پیام ناشناس جدید"
            if row["sequence"] == 1
            else (
                "↩️ پاسخ مدیریت بخش خارجی (واسطه)"
                if service.scope == Scope.EXTERNAL
                else "↩️ پاسخ در گفتگوی ناشناس"
            )
        )
        text = f"{heading}\n\nنوع: {KINDS[row['kind']]}\n\n{body}"
        action = "chat_open" if service.scope == Scope.INTERNAL else "thread"
        payload = (
            {"cid": row["conversation_id"]}
            if service.scope == Scope.INTERNAL
            else {"cid": row["conversation_id"], "page": row["sequence"] - 1}
        )
        token = await service.issue(
            target["telegram_id"],
            action,
            payload,
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=(
                            "💬 ورود به گفتگوی ناشناس"
                            if service.scope == Scope.INTERNAL
                            else "↩️ مشاهده گفتگو و پاسخ"
                        ),
                        callback_data=token,
                    )
                ]
            ]
        )
        return target["telegram_id"], text, recipient_id, markup

    async def run_once(self):
        if self.feedback.clock() < self.pause_until:
            return False
        job = await self.claim()
        if job is None:
            return False
        try:
            prepared = await self.prepare(job)
        except SQLAlchemyError:
            await self.outcome(
                job, "pending", "storage_unavailable", min(300, 2 ** min(job["attempts"], 8))
            )
            return True
        except Exception:
            await self.outcome(job, "failed", "invalid_payload")
            return True
        if prepared is None:
            await self.outcome(job, "suspended", "unavailable")
            return True
        target_id, text, recipient_id, markup = prepared
        try:
            await self.bot.send_message(
                chat_id=target_id,
                text=text,
                reply_markup=markup,
                parse_mode=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                protect_content=True,
                request_timeout=30,
            )
        except TelegramRetryAfter as error:
            delay = max(int(error.retry_after), min(300, 2 ** min(job["attempts"], 8))) + 1
            self.pause_until = self.feedback.clock() + delay
            await self.outcome(job, "pending", "rate_limited", delay)
        except TelegramForbiddenError:
            await self.outcome(job, "failed", "unavailable")
            users = self.feedback.identity.tables["users"]
            async with self.feedback.identity.database.transaction(write=True) as conn:
                await conn.execute(
                    update(users).where(users.c.id == recipient_id).values(delivery_state="blocked")
                )
        except (TelegramNetworkError, TelegramServerError):
            await self.outcome(job, "uncertain", "ambiguous_network")
        except TelegramBadRequest:
            await self.outcome(job, "failed", "invalid_request")
        except TelegramAPIError:
            await self.outcome(job, "failed", "api_error")
        except asyncio.CancelledError:
            await self.outcome(job, "uncertain", "interrupted")
            raise
        except Exception:
            await self.outcome(job, "uncertain", "unknown_result")
        else:
            await self.outcome(job, "sent")
        return True

    async def run(self):
        while True:
            try:
                worked = await self.run_once()
            except Exception:
                log_event(Event.DELIVERY_FAILED)
                worked = False
            await asyncio.sleep(1.1 if worked else 2)


async def run_deliveries(*workers):
    while True:
        worked = False
        pause = max(worker.pause_until for worker in workers)
        for worker in workers:
            worker.pause_until = pause
            try:
                worked = await worker.run_once() or worked
            except Exception:
                log_event(Event.DELIVERY_FAILED)
            pause = max(pause, worker.pause_until)
        await asyncio.sleep(1.1 if worked else 2)
