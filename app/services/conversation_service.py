"""Participant-owned history and atomic replies; no sender identity in returned views."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from app.core.permissions import AccessDenied, InputError
from app.core.security import Purpose, Scope
from app.database.identity_repository import IdentityRepository
from app.services.feedback_service import validate_body


class ConversationService:
    def __init__(self, feedback):
        self.feedback = feedback
        self.tables = feedback.tables

    def _manager(self, actor, manager):
        if type(manager) is not bool:
            raise AccessDenied()
        if manager:
            if self.feedback.scope != Scope.EXTERNAL:
                raise AccessDenied()
            actor.require("external", "read_feedback")

    async def _access(self, conn, actor, cid, manager=False, writable=False):
        self._manager(actor, manager)
        s = self.feedback
        role = "manager"
        if not manager:
            access = self.tables["participant_access"]
            row = (
                (
                    await conn.execute(
                        select(access).where(
                            access.c.conversation_id == cid,
                            access.c.access_tag == s.cipher.tag("participant", cid, actor.user_id),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AccessDenied()
            role = s.unseal(row["permissions_cipher"], row["id"], "permissions")["role"]
            if role not in {"sender", "recipient"}:
                raise AccessDenied()
        table = self.tables["conversations"]
        conversation = (
            (await conn.execute(select(table).where(table.c.id == cid))).mappings().one_or_none()
        )
        if conversation is None:
            raise AccessDenied()
        if writable and (
            conversation["status"] != "open" or conversation["expires_at"] <= int(s.clock())
        ):
            raise InputError("این گفتگو بسته یا منقضی شده و پاسخ‌گویی ممکن نیست.")
        return conversation, role

    async def _owned(self, conn, actor):
        # Version 0001 deliberately uses conversation-specific tags, not a linkable user index.
        s = self.feedback
        c, a = self.tables["conversations"], self.tables["participant_access"]
        rows = (
            await conn.execute(
                select(c, a.c.id.label("access_id"), a.c.access_tag, a.c.permissions_cipher)
                .join(a, a.c.conversation_id == c.c.id)
                .order_by(c.c.updated_at.desc(), c.c.id.desc())
            )
        ).mappings()
        for row in rows:
            if row["access_tag"] == s.cipher.tag("participant", row["id"], actor.user_id):
                role = s.unseal(row["permissions_cipher"], row["access_id"], "permissions")["role"]
                yield row, role

    async def history(self, actor_id, folder="sent", page=0):
        if folder not in {"sent", "received"} or type(page) is not int or not 0 <= page <= 10000:
            raise InputError("صفحه نامعتبر است.")
        s = self.feedback
        actor = await s.identity.authenticate(actor_id)
        result = []
        async with s.database.transaction() as conn:
            async for row, role in self._owned(conn, actor):
                if role != ("sender" if folder == "sent" else "recipient"):
                    continue
                subject = s.unseal(row["subject_cipher"], row["id"], "subject", Purpose.CONTENT)
                result.append(
                    {
                        "id": row["id"],
                        "kind": row["kind"],
                        "subject": subject["name"] if role == "sender" else "فرستنده ناشناس",
                    }
                )
                if len(result) >= page * 10 + 11:
                    break
        return result[page * 10 : page * 10 + 11]

    async def profile(self, actor_id):
        s = self.feedback
        actor = await s.identity.authenticate(actor_id)
        counts = {
            "sent_criticism": 0,
            "sent_suggestion": 0,
            "received_criticism": 0,
            "received_suggestion": 0,
            "sent_replies": 0,
            "received_replies": 0,
        }
        async with s.database.transaction() as conn:
            async for row, role in self._owned(conn, actor):
                prefix = "sent" if role == "sender" else "received"
                counts[f"{prefix}_{row['kind']}"] += 1
                m = self.tables["messages"]
                replies = (
                    await conn.execute(
                        select(m.c.id, m.c.direction_cipher).where(
                            m.c.conversation_id == row["id"], m.c.message_type == "reply"
                        )
                    )
                ).mappings()
                for message in replies:
                    direction = s.unseal(message["direction_cipher"], message["id"], "direction")
                    direction = direction["role"] if isinstance(direction, dict) else direction
                    counts["sent_replies" if direction == role else "received_replies"] += 1
        return counts

    async def thread(self, actor_id, cid, page=0, manager=False):
        if type(page) is not int or not 0 <= page <= 100000:
            raise InputError("صفحه نامعتبر است.")
        s = self.feedback
        actor = await s.identity.authenticate(actor_id)
        self._manager(actor, manager)
        async with s.database.transaction() as conn:
            row, role = await self._access(conn, actor, cid, manager)
            m = self.tables["messages"]
            message = (
                (
                    await conn.execute(
                        select(m)
                        .where(m.c.conversation_id == cid)
                        .order_by(m.c.sequence)
                        .offset(page)
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if message is None:
                raise InputError("پیام یافت نشد.")
            total = await conn.scalar(
                select(func.count()).select_from(m).where(m.c.conversation_id == cid)
            )
            direction = s.unseal(message["direction_cipher"], message["id"], "direction")
            direction = direction["role"] if isinstance(direction, dict) else direction
            label = {
                "sender": "نویسنده ناشناس",
                "recipient": "گیرنده بازخورد",
                "manager": "مدیریت بخش خارجی (واسطه)",
            }[direction]
            if direction == role and role != "manager":
                label = "شما"
            subject = s.unseal(row["subject_cipher"], cid, "subject", Purpose.CONTENT)
            outbox = self.tables["outbox"]
            status = await conn.scalar(
                select(outbox.c.status).where(outbox.c.message_id == message["id"])
            )
            return {
                "id": cid,
                "message_id": message["id"],
                "can_report": direction != role,
                "kind": row["kind"],
                "subject": subject["name"],
                "department": subject.get("department"),
                "body": s.unseal(message["body_cipher"], message["id"], "body", Purpose.CONTENT),
                "speaker": label,
                "total": total,
                "page": page,
                "delivery": status if direction == role else None,
                "can_reply": row["status"] == "open" and row["expires_at"] > int(s.clock()),
            }

    async def begin_reply(self, actor_id, cid, manager=False):
        s = self.feedback
        actor = await s.identity.authenticate(actor_id)
        self._manager(actor, manager)
        async with s.database.transaction(write=True) as conn:
            await self._access(conn, actor, cid, manager, writable=True)
            await s._clean(conn)
            await s._session(
                conn, actor.user_id, "reply", {"cid": cid, "manager": manager}, draft=True
            )

    async def preview_reply(self, actor_id, body):
        s = self.feedback
        actor = await s.identity.authenticate(actor_id)
        body = validate_body(body)
        sessions = self.tables["interaction_sessions"]
        async with s.database.transaction(write=True) as conn:
            row = (
                (
                    await conn.execute(
                        select(sessions).where(
                            sessions.c.token_hash == s.cipher.tag("draft", actor.user_id),
                            sessions.c.expires_at > int(s.clock()),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise InputError("پیش‌نویس منقضی شده است.")
            state = s.unseal(row["state_cipher"], row["id"], "session")
            if state["action"] != "reply":
                raise InputError("ابتدا پاسخ به گفتگو را انتخاب کنید.")
            data = {**state["payload"], "body": body}
            await self._access(conn, actor, data["cid"], data["manager"], writable=True)
            await conn.execute(
                delete(sessions).where(sessions.c.owner_tag == s.owner(actor.user_id))
            )
            return data, await s._session(conn, actor.user_id, "reply_confirm", data)

    async def submit_reply(self, actor_id, token):
        s = self.feedback
        async with s.identity.database.transaction(write=True) as ic:
            actor = await IdentityRepository(ic).authenticate(actor_id)
            async with s.database.transaction(write=True) as conn:
                _, state = await s._read_token(conn, actor, token)
                if state["action"] != "reply_confirm":
                    raise InputError("تأیید پاسخ معتبر نیست.")
                data = state["payload"]
                cid = data["cid"]
                conversation, role = await self._access(
                    conn, actor, cid, data["manager"], writable=True
                )
                route = s.unseal(conversation["route_cipher"], cid, "route")
                destination = None
                if s.scope == Scope.INTERNAL:
                    destination = route["recipient"] if role == "sender" else route["sender"]
                    await s._target(ic, actor, destination)
                else:
                    await s._person(conn, route["external_person"])
                    if role == "manager":
                        users = s.identity.tables["users"]
                        ready = await ic.scalar(
                            select(users.c.id).where(
                                users.c.id == route["sender"],
                                users.c.is_active == 1,
                                users.c.started_at.is_not(None),
                                users.c.delivery_state != "blocked",
                            )
                        )
                        if not ready:
                            raise InputError("طرف گفتگو فعلاً آماده دریافت پاسخ نیست.")
                        destination = route["sender"]
                body = validate_body(data["body"])
                m = self.tables["messages"]
                sequence = 1 + await conn.scalar(
                    select(func.max(m.c.sequence)).where(m.c.conversation_id == cid)
                )
                mid, now = uuid4().hex, int(s.clock())
                direction = (
                    {"role": "manager", "user": actor.user_id} if role == "manager" else role
                )
                await conn.execute(
                    m.insert().values(
                        id=mid,
                        conversation_id=cid,
                        sequence=sequence,
                        body_cipher=s.seal(body, mid, "body", Purpose.CONTENT),
                        direction_cipher=s.seal(direction, mid, "direction"),
                        message_type="reply",
                        accepted_day=datetime.fromtimestamp(now, s.zone).date().isoformat(),
                    )
                )
                if destination is not None:
                    job = uuid4().hex
                    await conn.execute(
                        self.tables["outbox"]
                        .insert()
                        .values(
                            id=job,
                            message_id=mid,
                            destination_cipher=s.seal(destination, job, "destination"),
                            dedupe_key=mid,
                            next_attempt_at=now,
                        )
                    )
                c = self.tables["conversations"]
                await conn.execute(
                    update(c)
                    .where(c.c.id == cid)
                    .values(updated_at=now, expires_at=now + 90 * 86400, version=c.c.version + 1)
                )
                sessions = self.tables["interaction_sessions"]
                await conn.execute(
                    delete(sessions).where(sessions.c.owner_tag == s.owner(actor.user_id))
                )
                return {
                    "cid": cid,
                    "page": sequence - 1,
                    "manager": data["manager"],
                    "queued": destination is not None,
                }
