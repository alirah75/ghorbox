"""Encrypted internal drafts, actor-bound callbacks and atomic feedback/outbox writes."""

import hashlib
import json
import secrets
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.permissions import MANAGE_ADMINS, AccessDenied, InputError
from app.core.security import Purpose, Scope
from app.database.identity_repository import IdentityRepository

KINDS = {"criticism": "انتقاد", "suggestion": "پیشنهاد"}


def validate_body(text: str) -> str:
    text = text.strip()
    if sum(not c.isspace() and c != "\u200c" for c in text) < 5:
        raise InputError("متن باید حداقل ۵ نویسه معنادار داشته باشد.")
    if any(unicodedata.category(c).startswith("C") and c not in "\n\t\u200c" for c in text):
        raise InputError("متن دارای نویسه کنترلی نامعتبر است.")
    if len(text.encode("utf-16-le")) // 2 > 3000:
        raise InputError("متن بیش از حد طولانی است؛ آن را کوتاه‌تر کنید (حداکثر ۳۰۰۰ واحد متنی).")
    return text


class FeedbackService:
    scope = Scope.INTERNAL
    prefix = "f:"

    def __init__(self, identity, database, cipher, timezone="Asia/Tehran", clock=time.time):
        if database.kind != self.scope.value or cipher.scope != self.scope:
            raise ValueError("Matching scoped storage required")
        self.identity, self.database, self.cipher = identity, database, cipher
        self.tables = database.metadata.tables
        self.zone = ZoneInfo(timezone)
        self.clock = clock

    def seal(self, value, record, field, purpose=Purpose.ROUTING):
        return self.cipher.encrypt(
            json.dumps(value, ensure_ascii=False).encode(),
            record=record,
            field=field,
            purpose=purpose,
        )

    def unseal(self, value, record, field, purpose=Purpose.ROUTING):
        return json.loads(self.cipher.decrypt(value, record=record, field=field, purpose=purpose))

    def owner(self, user_id):
        return self.cipher.tag("session-owner", user_id)

    async def _clean(self, conn):
        sessions = self.tables["interaction_sessions"]
        await conn.execute(delete(sessions).where(sessions.c.expires_at <= int(self.clock())))

    async def _session(self, conn, user_id, action, payload, *, draft=False):
        sessions = self.tables["interaction_sessions"]
        record, token = uuid4().hex, self.prefix + secrets.token_urlsafe(24)
        digest = (
            self.cipher.tag("draft", user_id) if draft else hashlib.sha256(token.encode()).digest()
        )
        if draft:
            await conn.execute(delete(sessions).where(sessions.c.token_hash == digest))
        await conn.execute(
            sessions.insert().values(
                id=record,
                token_hash=digest,
                owner_tag=self.owner(user_id),
                state_cipher=self.seal({"action": action, "payload": payload}, record, "session"),
                expires_at=int(self.clock()) + 600,
            )
        )
        return token

    async def issue(self, actor_id, action, payload=None):
        actor = await self.identity.authenticate(actor_id)
        async with self.database.transaction(write=True) as conn:
            await self._clean(conn)
            return await self._session(conn, actor.user_id, action, payload)

    async def _read_token(self, conn, actor, token):
        sessions = self.tables["interaction_sessions"]
        if not isinstance(token, str) or len(token) > 64:
            raise InputError("دکمه نامعتبر است.")
        row = (
            (
                await conn.execute(
                    select(sessions).where(
                        sessions.c.token_hash == hashlib.sha256(token.encode()).digest(),
                        sessions.c.owner_tag == self.owner(actor.user_id),
                        sessions.c.expires_at > int(self.clock()),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise InputError("این دکمه مصرف شده یا منقضی است؛ از /start شروع کنید.")
        return row, self.unseal(row["state_cipher"], row["id"], "session")

    async def peek(self, actor_id, token):
        actor = await self.identity.authenticate(actor_id)
        async with self.database.transaction() as conn:
            _, state = await self._read_token(conn, actor, token)
            return state["action"], state["payload"]

    async def take(self, actor_id, token):
        actor = await self.identity.authenticate(actor_id)
        async with self.database.transaction(write=True) as conn:
            row, state = await self._read_token(conn, actor, token)
            await conn.execute(
                delete(self.tables["interaction_sessions"]).where(
                    self.tables["interaction_sessions"].c.id == row["id"]
                )
            )
            return state["action"], state["payload"]

    async def cancel(self, actor_id):
        actor = await self.identity.authenticate(actor_id)
        async with self.database.transaction(write=True) as conn:
            await self._clean(conn)
            table = self.tables["interaction_sessions"]
            await conn.execute(delete(table).where(table.c.owner_tag == self.owner(actor.user_id)))

    async def colleagues(self, actor_id, page=0, search=""):
        if type(page) is not int or page < 0 or page > 10000 or len(search) > 80:
            raise InputError("جستجو نامعتبر است.")
        async with self.identity.database.transaction() as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            users = self.identity.tables["users"]
            query = select(users.c.id, users.c.display_name).where(
                users.c.is_active == 1, users.c.id != actor.user_id
            )
            if not actor.allows(*MANAGE_ADMINS):
                group_id = await self._group_id_for_actor(conn, actor)
                if group_id is None:
                    return []
                memberships = self.identity.tables["internal_group_memberships"]
                query = query.join(memberships, memberships.c.user_id == users.c.id).where(
                    memberships.c.group_id == group_id
                )
            if search:
                query = query.where(users.c.display_name.contains(search, autoescape=True))
            return [
                dict(row)
                for row in (
                    await conn.execute(
                        query.order_by(users.c.display_name, users.c.id).offset(page * 10).limit(11)
                    )
                ).mappings()
            ]

    async def _target(self, conn, actor, recipient):
        if recipient == actor.user_id:
            raise InputError("ارسال بازخورد به خودتان ممکن نیست.")
        users = self.identity.tables["users"]
        target = (
            (
                await conn.execute(
                    select(users).where(
                        users.c.id == recipient,
                        users.c.is_active == 1,
                        users.c.started_at.is_not(None),
                        users.c.delivery_state != "blocked",
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if target is None:
            raise InputError(
                "گیرنده فعلاً آماده دریافت نیست؛ باید عضو فعال باشد و ربات را Start کند."
            )
        if not actor.allows(*MANAGE_ADMINS):
            group_id = await self._group_id_for_actor(conn, actor)
            if group_id is None:
                raise AccessDenied()
            memberships = self.identity.tables["internal_group_memberships"]
            permitted = (
                await conn.execute(
                    select(memberships.c.id).where(
                        memberships.c.group_id == group_id,
                        memberships.c.user_id == recipient,
                    )
                )
            ).first()
            if not permitted:
                raise AccessDenied()
        return target

    async def _group_id_for_actor(self, conn, actor):
        groups = self.identity.tables["internal_groups"]
        if actor.admin_id:
            return (
                await conn.execute(
                    select(groups.c.id).where(
                        groups.c.manager_admin_id == actor.admin_id, groups.c.is_active == 1
                    )
                )
            ).scalar_one_or_none()
        memberships = self.identity.tables["internal_group_memberships"]
        return (
            await conn.execute(
                select(memberships.c.group_id)
                .join(groups, groups.c.id == memberships.c.group_id)
                .where(memberships.c.user_id == actor.user_id, groups.c.is_active == 1)
            )
        ).scalar_one_or_none()

    async def begin(self, actor_id, recipient, kind):
        if kind not in KINDS:
            raise InputError("نوع بازخورد نامعتبر است.")
        async with self.identity.database.transaction(write=True) as identity_conn:
            actor = await IdentityRepository(identity_conn).authenticate(actor_id)
            target = await self._target(identity_conn, actor, recipient)
            async with self.database.transaction(write=True) as conn:
                await self._clean(conn)
                await self._session(
                    conn,
                    actor.user_id,
                    "compose",
                    {
                        "recipient": recipient,
                        "recipient_name": target["display_name"],
                        "kind": kind,
                    },
                    draft=True,
                )

    async def begin_search(self, actor_id):
        actor = await self.identity.authenticate(actor_id)
        async with self.database.transaction(write=True) as conn:
            await self._clean(conn)
            await self._session(conn, actor.user_id, "search", {}, draft=True)

    async def draft(self, actor_id):
        actor = await self.identity.authenticate(actor_id)
        table = self.tables["interaction_sessions"]
        async with self.database.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(table).where(
                            table.c.token_hash == self.cipher.tag("draft", actor.user_id),
                            table.c.owner_tag == self.owner(actor.user_id),
                            table.c.expires_at > int(self.clock()),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            return self.unseal(row["state_cipher"], row["id"], "session") if row else None

    async def preview(self, actor_id, body):
        actor = await self.identity.authenticate(actor_id)
        body = validate_body(body)
        async with self.database.transaction(write=True) as conn:
            table = self.tables["interaction_sessions"]
            row = (
                (
                    await conn.execute(
                        select(table).where(
                            table.c.token_hash == self.cipher.tag("draft", actor.user_id),
                            table.c.expires_at > int(self.clock()),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if not row:
                raise InputError("پیش‌نویس منقضی شده است.")
            state = self.unseal(row["state_cipher"], row["id"], "session")
            if state["action"] != "compose":
                raise InputError("ابتدا گیرنده و نوع بازخورد را انتخاب کنید.")
            data = {**state["payload"], "body": body}
            # Replace earlier previews and remove sender-linked drafts at submission.
            await conn.execute(delete(table).where(table.c.owner_tag == self.owner(actor.user_id)))
            token = await self._session(conn, actor.user_id, "confirm", data)
            return data, token

    async def submit(self, actor_id, token):
        # Lock membership changes until the content/outbox commit, without a network call.
        async with self.identity.database.transaction(write=True) as identity_conn:
            actor = await IdentityRepository(identity_conn).authenticate(actor_id)
            async with self.database.transaction(write=True) as conn:
                _, state = await self._read_token(conn, actor, token)
                if state["action"] != "confirm":
                    raise InputError("دکمه تأیید معتبر نیست.")
                return await self._store_feedback(identity_conn, conn, actor, state["payload"])

    async def submit_body(self, actor_id, body):
        """Validate and persist a compose draft immediately, with no confirmation state."""
        async with self.identity.database.transaction(write=True) as identity_conn:
            actor = await IdentityRepository(identity_conn).authenticate(actor_id)
            async with self.database.transaction(write=True) as conn:
                data = await self._compose_payload(conn, actor, body)
                return await self._store_feedback(identity_conn, conn, actor, data)

    async def _compose_payload(self, conn, actor, body):
        body = validate_body(body)
        table = self.tables["interaction_sessions"]
        row = (
            (
                await conn.execute(
                    select(table).where(
                        table.c.token_hash == self.cipher.tag("draft", actor.user_id),
                        table.c.owner_tag == self.owner(actor.user_id),
                        table.c.expires_at > int(self.clock()),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise InputError("پیش‌نویس منقضی شده است؛ دوباره گیرنده را انتخاب کنید.")
        state = self.unseal(row["state_cipher"], row["id"], "session")
        if state["action"] != "compose":
            raise InputError("ابتدا گیرنده و نوع بازخورد را انتخاب کنید.")
        return {**state["payload"], "body": body}

    async def _store_feedback(self, identity_conn, conn, actor, data):
        target = await self._target(identity_conn, actor, data["recipient"])
        body = validate_body(data["body"])
        if data["kind"] not in KINDS:
            raise InputError("نوع بازخورد نامعتبر است.")
        cid, mid, job = uuid4().hex, uuid4().hex, uuid4().hex
        now = int(self.clock())
        await conn.execute(
            self.tables["conversations"]
            .insert()
            .values(
                id=cid,
                kind=data["kind"],
                expires_at=now + 90 * 86400,
                subject_cipher=self.seal(
                    {"id": target["id"], "name": target["display_name"]},
                    cid,
                    "subject",
                    Purpose.CONTENT,
                ),
                route_cipher=self.seal(
                    {"sender": actor.user_id, "recipient": target["id"]}, cid, "route"
                ),
            )
        )
        for user_id, role in [(actor.user_id, "sender"), (target["id"], "recipient")]:
            access_id = uuid4().hex
            await conn.execute(
                self.tables["participant_access"]
                .insert()
                .values(
                    id=access_id,
                    conversation_id=cid,
                    access_tag=self.cipher.tag("participant", cid, user_id),
                    permissions_cipher=self.seal({"role": role}, access_id, "permissions"),
                )
            )
        await conn.execute(
            self.tables["messages"]
            .insert()
            .values(
                id=mid,
                conversation_id=cid,
                sequence=1,
                message_type="initial",
                accepted_day=datetime.fromtimestamp(now, self.zone).date().isoformat(),
                body_cipher=self.seal(body, mid, "body", Purpose.CONTENT),
                direction_cipher=self.seal("sender", mid, "direction"),
            )
        )
        await conn.execute(
            self.tables["outbox"]
            .insert()
            .values(
                id=job,
                message_id=mid,
                destination_cipher=self.seal(target["id"], job, "destination"),
                dedupe_key=mid,
                next_attempt_at=now,
            )
        )
        await self._record_contributor(conn, actor.user_id, now)
        sessions = self.tables["interaction_sessions"]
        await conn.execute(
            delete(sessions).where(sessions.c.owner_tag == self.owner(actor.user_id))
        )
        await self._session(
            conn,
            actor.user_id,
            "chat_active",
            {"cid": cid, "manager": False},
            draft=True,
        )
        return await self._session(conn, actor.user_id, "receipt", mid)

    async def _record_contributor(self, conn, user_id, now):
        date = datetime.fromtimestamp(now, self.zone)
        week = (date - timedelta(days=(date.weekday() - 5) % 7)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_start = int(week.astimezone(UTC).timestamp())
        await conn.execute(
            sqlite_insert(self.tables["weekly_contributors"])
            .values(
                period_start=week_start,
                contributor_tag=self.cipher.tag("weekly", str(week_start), user_id),
            )
            .on_conflict_do_nothing(index_elements=["period_start", "contributor_tag"])
        )

    async def delivery_status(self, actor_id, token):
        actor = await self.identity.authenticate(actor_id)
        async with self.database.transaction(write=True) as conn:
            row, state = await self._read_token(conn, actor, token)
            if state["action"] != "receipt":
                raise AccessDenied()
            table = self.tables["outbox"]
            status = (
                await conn.execute(
                    select(table.c.status).where(table.c.message_id == state["payload"])
                )
            ).scalar_one()
            await conn.execute(
                delete(self.tables["interaction_sessions"]).where(
                    self.tables["interaction_sessions"].c.id == row["id"]
                )
            )
            renewed = await self._session(conn, actor.user_id, "receipt", state["payload"])
            return status, renewed
