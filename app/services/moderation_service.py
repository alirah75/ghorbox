"""Scoped initial-feedback views."""

from sqlalchemy import select

from app.core.permissions import MANAGE_ADMINS, AccessDenied, InputError
from app.core.security import Purpose, Scope
from app.database.identity_repository import IdentityRepository


class ModerationService:
    def __init__(self, feedback):
        self.s = feedback
        self.t = feedback.tables

    @staticmethod
    def page(page):
        if type(page) is not int or not 0 <= page <= 10000:
            raise InputError("صفحه نامعتبر است.")

    async def _subject_groups(self, actor_id):
        s = self.s
        async with s.identity.database.transaction() as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            actor.require(s.scope.value, "read_feedback")
            if s.scope != Scope.INTERNAL:
                return True, {}
            memberships = s.identity.tables["internal_group_memberships"]
            groups = s.identity.tables["internal_groups"]
            query = (
                select(memberships.c.user_id, groups.c.name)
                .join(groups, groups.c.id == memberships.c.group_id)
                .where(groups.c.is_active == 1)
            )
            unrestricted = actor.allows(*MANAGE_ADMINS)
            if not unrestricted:
                query = query.where(groups.c.manager_admin_id == actor.admin_id)
            return unrestricted, dict((await conn.execute(query)).all())

    async def feedback_list(self, actor_id, page=0):
        self.page(page)
        unrestricted, groups = await self._subject_groups(actor_id)
        c = self.t["conversations"]
        async with self.s.database.transaction() as conn:
            rows = (
                await conn.execute(
                    select(c.c.id, c.c.kind, c.c.subject_cipher, c.c.status).order_by(
                        c.c.created_at.desc(), c.c.id.desc()
                    )
                )
            ).mappings()
            result = []
            for row in rows:
                subject = self.s.unseal(
                    row["subject_cipher"], row["id"], "subject", Purpose.CONTENT
                )
                if not unrestricted and subject.get("id") not in groups:
                    continue
                result.append(
                    {
                        "id": row["id"],
                        "kind": row["kind"],
                        "status": row["status"],
                        "subject": subject["name"],
                        "group": groups.get(subject.get("id")),
                    }
                )
                if len(result) >= page * 10 + 11:
                    break
            return result[page * 10 : page * 10 + 11]

    async def initial_feedback(self, actor_id, cid):
        unrestricted, groups = await self._subject_groups(actor_id)
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
            subject = self.s.unseal(row["subject_cipher"], cid, "subject", Purpose.CONTENT)
            if not unrestricted and subject.get("id") not in groups:
                raise AccessDenied()
            return {
                "id": row["id"],
                "kind": row["kind"],
                "status": row["status"],
                "subject": subject["name"],
                "group": groups.get(subject.get("id")),
                "body": self.s.unseal(row["body_cipher"], row["mid"], "body", Purpose.CONTENT),
            }
