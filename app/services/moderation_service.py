"""Scoped moderation views intentionally omit routing, reporter and reply-author metadata."""

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert

from app.core.permissions import AccessDenied, InputError
from app.core.security import Purpose
from app.database.identity_repository import IdentityRepository
from app.services.conversation_service import ConversationService

REASONS = {
    "insult": "توهین",
    "threat": "تهدید",
    "inappropriate": "محتوای نامناسب",
    "spam": "اسپم",
    "other": "سایر",
}


class ModerationService:
    def __init__(self, feedback):
        self.s = feedback
        self.t = feedback.tables

    @staticmethod
    def page(page):
        if type(page) is not int or not 0 <= page <= 10000:
            raise InputError("صفحه نامعتبر است.")

    async def reports(self, actor_id, status="open", page=0):
        await self.s.identity.require(actor_id, self.s.scope.value, "manage_reports")
        self.page(page)
        if status not in {"open", "accepted", "dismissed"}:
            raise InputError("وضعیت نامعتبر است.")
        r = self.t["reports"]
        async with self.s.database.transaction() as conn:
            return [
                dict(row)
                for row in (
                    await conn.execute(
                        select(r.c.id, r.c.reason, r.c.status)
                        .where(r.c.status == status)
                        .order_by(r.c.created_at, r.c.id)
                        .offset(page * 10)
                        .limit(11)
                    )
                ).mappings()
            ]

    async def report_detail(self, actor_id, report_id):
        await self.s.identity.require(actor_id, self.s.scope.value, "manage_reports")
        r, m = self.t["reports"], self.t["messages"]
        async with self.s.database.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(
                            r.c.id,
                            r.c.reason,
                            r.c.status,
                            r.c.resolution,
                            m.c.id.label("mid"),
                            m.c.body_cipher,
                        )
                        .join(m, m.c.id == r.c.message_id)
                        .where(r.c.id == report_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise InputError("گزارش یافت نشد.")
            return {
                "id": row["id"],
                "reason": row["reason"],
                "status": row["status"],
                "resolution": row["resolution"],
                "body": self.s.unseal(row["body_cipher"], row["mid"], "body", Purpose.CONTENT),
            }

    async def feedback_list(self, actor_id, page=0):
        await self.s.identity.require(actor_id, self.s.scope.value, "read_feedback")
        self.page(page)
        c = self.t["conversations"]
        async with self.s.database.transaction() as conn:
            rows = (
                await conn.execute(
                    select(c.c.id, c.c.kind, c.c.subject_cipher, c.c.status)
                    .order_by(c.c.created_at.desc(), c.c.id.desc())
                    .offset(page * 10)
                    .limit(11)
                )
            ).mappings()
            return [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "subject": self.s.unseal(
                        row["subject_cipher"], row["id"], "subject", Purpose.CONTENT
                    )["name"],
                }
                for row in rows
            ]

    async def initial_feedback(self, actor_id, cid):
        await self.s.identity.require(actor_id, self.s.scope.value, "read_feedback")
        c, m = self.t["conversations"], self.t["messages"]
        async with self.s.database.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(
                            c.c.id,
                            c.c.kind,
                            c.c.status,
                            c.c.subject_cipher,
                            m.c.id.label("mid"),
                            m.c.body_cipher,
                        )
                        .join(m, m.c.conversation_id == c.c.id)
                        .where(c.c.id == cid, m.c.message_type == "initial")
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise InputError("بازخورد یافت نشد.")
            return {
                "id": row["id"],
                "kind": row["kind"],
                "status": row["status"],
                "subject": self.s.unseal(row["subject_cipher"], cid, "subject", Purpose.CONTENT)[
                    "name"
                ],
                "body": self.s.unseal(row["body_cipher"], row["mid"], "body", Purpose.CONTENT),
            }

    async def _report(self, conn, actor, cid, mid, reason, manager=False):
        if reason not in REASONS:
            raise InputError("دلیل گزارش نامعتبر است.")
        _, role = await ConversationService(self.s)._access(conn, actor, cid, manager)
        m = self.t["messages"]
        row = (
            (
                await conn.execute(
                    select(m.c.direction_cipher).where(m.c.id == mid, m.c.conversation_id == cid)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise AccessDenied()
        direction = self.s.unseal(row["direction_cipher"], mid, "direction")
        if isinstance(direction, dict):
            own = direction.get("user") == actor.user_id
        else:
            own = direction == role
            # An external manager might also be the original author of this conversation.
            if manager and direction == "sender":
                a = self.t["participant_access"]
                own = bool(
                    await conn.scalar(
                        select(a.c.id).where(
                            a.c.conversation_id == cid,
                            a.c.access_tag == self.s.cipher.tag("participant", cid, actor.user_id),
                        )
                    )
                )
        if own:
            raise InputError("گزارش پیام خودتان ممکن نیست.")
        r = self.t["reports"]
        result = await conn.execute(
            insert(r)
            .values(
                message_id=mid,
                reason=reason,
                reporter_tag=self.s.cipher.tag("reporter", mid, actor.user_id),
            )
            .on_conflict_do_nothing(index_elements=["message_id", "reporter_tag"])
        )
        if result.rowcount != 1:
            raise InputError("این پیام را قبلاً گزارش کرده‌اید.")

    async def _close(self, conn, cid):
        c, m, o = self.t["conversations"], self.t["messages"], self.t["outbox"]
        exists = await conn.scalar(select(c.c.id).where(c.c.id == cid))
        if exists is None:
            raise AccessDenied()
        await conn.execute(
            update(c)
            .where(c.c.id == cid, c.c.status != "closed")
            .values(status="closed", version=c.c.version + 1, updated_at=int(self.s.clock()))
        )
        await conn.execute(
            update(o)
            .where(
                o.c.message_id.in_(select(m.c.id).where(m.c.conversation_id == cid)),
                o.c.status == "pending",
            )
            .values(status="suspended", error_code="conversation_closed", lease_until=None)
        )

    async def execute(self, actor_id, token):
        """Recheck permissions and consume confirmation in the same transaction as the decision."""
        s = self.s
        async with s.identity.database.transaction(write=True) as ic:
            actor = await IdentityRepository(ic).authenticate(actor_id)
            async with s.database.transaction(write=True) as conn:
                _, state = await s._read_token(conn, actor, token)
                action, data = state["action"], state["payload"]
                if action == "mod_report_save":
                    await self._report(conn, actor, **data)
                elif action == "mod_close_save":
                    await ConversationService(s)._access(
                        conn, actor, data["cid"], data.get("manager", False)
                    )
                    if data.get("manager"):
                        actor.require(s.scope.value, "manage_reports")
                    await self._close(conn, data["cid"])
                elif action == "mod_admin_close_save":
                    actor.require(s.scope.value, "read_feedback")
                    actor.require(s.scope.value, "manage_reports")
                    await self._close(conn, data["cid"])
                elif action == "mod_resolve_save":
                    actor.require(s.scope.value, "manage_reports")
                    if type(data["accept"]) is not bool:
                        raise InputError("تصمیم نامعتبر است.")
                    r, m = self.t["reports"], self.t["messages"]
                    row = await conn.execute(
                        select(r.c.id, m.c.conversation_id)
                        .join(m, m.c.id == r.c.message_id)
                        .where(r.c.id == data["id"], r.c.status == "open")
                    )
                    row = row.mappings().one_or_none()
                    if row is None:
                        raise InputError("این گزارش قبلاً بررسی شده یا وجود ندارد.")
                    if data["accept"]:
                        await self._close(conn, row["conversation_id"])
                    await conn.execute(
                        update(r)
                        .where(r.c.id == row["id"])
                        .values(
                            status="accepted" if data["accept"] else "dismissed",
                            resolution="conversation_closed" if data["accept"] else "no_action",
                        )
                    )
                else:
                    raise InputError("تأیید نامعتبر است.")
                sessions = self.t["interaction_sessions"]
                await conn.execute(
                    delete(sessions).where(sessions.c.owner_tag == s.owner(actor.user_id))
                )
                return action
