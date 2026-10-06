"""External directory and anonymous inbox, with authorization before domain queries."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete, select, update

from app.core.permissions import InputError
from app.core.security import Purpose, Scope
from app.database.identity_repository import IdentityRepository
from app.services.feedback_service import KINDS, FeedbackService, validate_body
from app.services.identity_service import valid_name


class ExternalService(FeedbackService):
    scope = Scope.EXTERNAL
    prefix = "e:"

    async def people(self, actor_id, page=0, search="", *, management=False):
        actor = await self.identity.authenticate(actor_id)
        if management:
            actor.require("external", "manage_external_people")
        if type(page) is not int or not 0 <= page <= 10000 or len(search) > 80:
            raise InputError("جستجو نامعتبر است.")
        table = self.tables["external_people"]
        query = select(table)
        if not management:
            query = query.where(table.c.is_active == 1)
        if search:
            query = query.where(table.c.display_name.contains(search, autoescape=True))
        async with self.database.transaction() as conn:
            return [
                dict(row)
                for row in (
                    await conn.execute(
                        query.order_by(table.c.display_name, table.c.id).offset(page * 10).limit(11)
                    )
                ).mappings()
            ]

    async def _person(self, conn, person_id):
        table = self.tables["external_people"]
        person = (
            (
                await conn.execute(
                    select(table).where(table.c.id == person_id, table.c.is_active == 1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if person is None:
            raise InputError("این فرد دیگر در فهرست فعال نیست.")
        return person

    async def form(self, actor_id, action, payload=None):
        actor = await self.identity.authenticate(actor_id)
        if action in {"add_person", "edit_person"}:
            actor.require("external", "manage_external_people")
        if action not in {"add_person", "edit_person", "propose", "search"}:
            raise InputError("عملیات نامعتبر است.")
        async with self.database.transaction(write=True) as conn:
            await self._clean(conn)
            await self._session(conn, actor.user_id, action, payload, draft=True)

    async def save_person(self, actor_id, name, department=None):
        async with self.identity.database.transaction(write=True) as ic:
            actor = await IdentityRepository(ic).authenticate(actor_id)
            actor.require("external", "manage_external_people")
            name = valid_name(name)
            department = valid_name(department) if department else None
            async with self.database.transaction(write=True) as conn:
                return await self._insert_person(conn, name, department)

    async def _insert_person(self, conn, name, department):
        table = self.tables["external_people"]
        duplicate = await conn.scalar(
            select(table.c.id).where(
                table.c.display_name == name,
                table.c.department.is_(None)
                if department is None
                else table.c.department == department,
            )
        )
        if duplicate:
            raise InputError("این نام و بخش قبلاً ثبت شده؛ وضعیت همان فرد را بررسی کنید.")
        person_id = uuid4().hex
        await conn.execute(
            table.insert().values(id=person_id, display_name=name, department=department)
        )
        return person_id

    async def set_active(self, actor_id, person_id, active):
        async with self.identity.database.transaction(write=True) as ic:
            actor = await IdentityRepository(ic).authenticate(actor_id)
            actor.require("external", "manage_external_people")
            if type(active) is not bool:
                raise InputError("وضعیت نامعتبر است.")
            async with self.database.transaction(write=True) as conn:
                table = self.tables["external_people"]
                result = await conn.execute(
                    update(table).where(table.c.id == person_id).values(is_active=int(active))
                )
                if result.rowcount != 1:
                    raise InputError("فرد یافت نشد.")

    async def edit_person(self, actor_id, person_id, name, department=None):
        async with self.identity.database.transaction(write=True) as ic:
            actor = await IdentityRepository(ic).authenticate(actor_id)
            actor.require("external", "manage_external_people")
            name = valid_name(name)
            department = valid_name(department) if department else None
            async with self.database.transaction(write=True) as conn:
                table = self.tables["external_people"]
                duplicate = await conn.scalar(
                    select(table.c.id).where(
                        table.c.id != person_id,
                        table.c.display_name == name,
                        table.c.department.is_(None)
                        if department is None
                        else table.c.department == department,
                    )
                )
                if duplicate:
                    raise InputError("این نام و بخش قبلاً ثبت شده است.")
                result = await conn.execute(
                    update(table)
                    .where(table.c.id == person_id)
                    .values(display_name=name, department=department)
                )
                if result.rowcount != 1:
                    raise InputError("فرد یافت نشد.")

    async def propose(self, actor_id, name, department=None):
        await self.identity.authenticate(actor_id)
        name = valid_name(name)
        department = valid_name(department) if department else None
        async with self.database.transaction(write=True) as conn:
            table = self.tables["external_person_proposals"]
            duplicate = await conn.scalar(
                select(table.c.id).where(
                    table.c.name == name,
                    table.c.status == "pending",
                    table.c.department.is_(None)
                    if department is None
                    else table.c.department == department,
                )
            )
            if duplicate:
                raise InputError("این پیشنهاد قبلاً در انتظار بررسی است.")
            await conn.execute(table.insert().values(name=name, department=department))

    async def proposals(self, actor_id, page=0):
        await self.identity.require(actor_id, "external", "manage_external_people")
        if type(page) is not int or not 0 <= page <= 10000:
            raise InputError("صفحه نامعتبر است.")
        table = self.tables["external_person_proposals"]
        async with self.database.transaction() as conn:
            return [
                dict(row)
                for row in (
                    await conn.execute(
                        select(table.c.id, table.c.name, table.c.department)
                        .where(table.c.status == "pending")
                        .order_by(table.c.created_at, table.c.id)
                        .offset(page * 10)
                        .limit(11)
                    )
                ).mappings()
            ]

    async def review(self, actor_id, proposal_id, accept):
        async with self.identity.database.transaction(write=True) as ic:
            actor = await IdentityRepository(ic).authenticate(actor_id)
            actor.require("external", "manage_external_people")
            if type(accept) is not bool:
                raise InputError("تصمیم نامعتبر است.")
            async with self.database.transaction(write=True) as conn:
                table = self.tables["external_person_proposals"]
                row = (
                    (
                        await conn.execute(
                            select(table).where(
                                table.c.id == proposal_id, table.c.status == "pending"
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise InputError("این پیشنهاد قبلاً بررسی شده است.")
                if accept:
                    await self._insert_person(conn, row["name"], row["department"])
                await conn.execute(
                    update(table)
                    .where(table.c.id == proposal_id)
                    .values(status="accepted" if accept else "dismissed")
                )

    async def begin(self, actor_id, recipient, kind):
        actor = await self.identity.authenticate(actor_id)
        if kind not in KINDS:
            raise InputError("نوع بازخورد نامعتبر است.")
        async with self.database.transaction(write=True) as conn:
            target = await self._person(conn, recipient)
            await self._clean(conn)
            await self._session(
                conn,
                actor.user_id,
                "compose",
                {"recipient": recipient, "recipient_name": target["display_name"], "kind": kind},
                draft=True,
            )

    async def submit(self, actor_id, token):
        async with self.identity.database.transaction(write=True) as ic:
            actor = await IdentityRepository(ic).authenticate(actor_id)
            async with self.database.transaction(write=True) as conn:
                _, state = await self._read_token(conn, actor, token)
                if state["action"] != "confirm":
                    raise InputError("دکمه تأیید معتبر نیست.")
                data = state["payload"]
                target = await self._person(conn, data["recipient"])
                body = validate_body(data["body"])
                if data["kind"] not in KINDS:
                    raise InputError("نوع بازخورد نامعتبر است.")
                cid, mid, access = uuid4().hex, uuid4().hex, uuid4().hex
                now = int(self.clock())
                await conn.execute(
                    self.tables["conversations"]
                    .insert()
                    .values(
                        id=cid,
                        kind=data["kind"],
                        expires_at=now + 90 * 86400,
                        subject_cipher=self.seal(
                            {
                                "id": target["id"],
                                "name": target["display_name"],
                                "department": target["department"],
                            },
                            cid,
                            "subject",
                            Purpose.CONTENT,
                        ),
                        route_cipher=self.seal(
                            {"sender": actor.user_id, "external_person": target["id"]}, cid, "route"
                        ),
                    )
                )
                await conn.execute(
                    self.tables["participant_access"]
                    .insert()
                    .values(
                        id=access,
                        conversation_id=cid,
                        access_tag=self.cipher.tag("participant", cid, actor.user_id),
                        permissions_cipher=self.seal({"role": "sender"}, access, "permissions"),
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
                await self._record_contributor(conn, actor.user_id, now)
                sessions = self.tables["interaction_sessions"]
                await conn.execute(
                    delete(sessions).where(sessions.c.owner_tag == self.owner(actor.user_id))
                )
                return mid

    async def inbox(self, actor_id, page=0):
        await self.identity.require(actor_id, "external", "read_feedback")
        if type(page) is not int or not 0 <= page <= 10000:
            raise InputError("صفحه نامعتبر است.")
        messages, conversations = self.tables["messages"], self.tables["conversations"]
        async with self.database.transaction() as conn:
            rows = (
                await conn.execute(
                    select(
                        messages.c.id,
                        conversations.c.id.label("cid"),
                        conversations.c.kind,
                        conversations.c.subject_cipher,
                    )
                    .join(conversations, messages.c.conversation_id == conversations.c.id)
                    .where(messages.c.message_type == "initial")
                    .order_by(messages.c.created_at.desc(), messages.c.id.desc())
                    .offset(page * 10)
                    .limit(11)
                )
            ).mappings()
            return [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "subject": self.unseal(
                        r["subject_cipher"], r["cid"], "subject", Purpose.CONTENT
                    )["name"],
                }
                for r in rows
            ]

    async def read_feedback(self, actor_id, message_id):
        await self.identity.require(actor_id, "external", "read_feedback")
        messages, conversations = self.tables["messages"], self.tables["conversations"]
        async with self.database.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(
                            messages.c.id,
                            messages.c.body_cipher,
                            conversations.c.id.label("cid"),
                            conversations.c.kind,
                            conversations.c.subject_cipher,
                        )
                        .join(conversations, messages.c.conversation_id == conversations.c.id)
                        .where(messages.c.id == message_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise InputError("پیام یافت نشد.")
            return {
                "conversation_id": row["cid"],
                "subject": self.unseal(
                    row["subject_cipher"], row["cid"], "subject", Purpose.CONTENT
                ),
                "kind": row["kind"],
                "body": self.unseal(row["body_cipher"], row["id"], "body", Purpose.CONTENT),
            }
