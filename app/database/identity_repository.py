"""Identity queries only. Never opens internal/external feedback databases."""

from sqlalchemy import or_, select

from app.core.permissions import AccessDenied, Principal
from app.database.schema_v1 import schema


class IdentityRepository:
    def __init__(self, connection):
        self.connection = connection
        self.tables = schema("identity").tables

    async def authenticate(self, telegram_id: int) -> Principal:
        users, admins, grants = (self.tables[name] for name in ("users", "admins", "admin_grants"))
        user = (
            (
                await self.connection.execute(
                    select(users).where(users.c.telegram_id == telegram_id, users.c.is_active == 1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if user is None:
            raise AccessDenied()
        admin_id = (
            await self.connection.execute(
                select(admins.c.id).where(admins.c.user_id == user["id"], admins.c.is_active == 1)
            )
        ).scalar_one_or_none()
        permissions = frozenset()
        if admin_id:
            permissions = frozenset(
                (
                    await self.connection.execute(
                        select(grants.c.scope, grants.c.permission).where(
                            grants.c.admin_id == admin_id
                        )
                    )
                ).all()
            )
        return Principal(user["id"], telegram_id, user["display_name"], admin_id, permissions)

    async def user_by_id(self, actor: Principal, user_id: str):
        actor.require("internal", "manage_users")
        users = self.tables["users"]
        result = (
            (await self.connection.execute(select(users).where(users.c.id == user_id)))
            .mappings()
            .one_or_none()
        )
        if result is None:
            raise AccessDenied()
        return dict(result)

    async def list_users(self, actor: Principal, page: int, search: str):
        actor.require("internal", "manage_users")
        users = self.tables["users"]
        query = select(users)
        if search:
            matches = users.c.display_name.contains(search, autoescape=True)
            if search.isdecimal() and 0 < int(search) < 2**52:
                matches = or_(matches, users.c.telegram_id == int(search))
            query = query.where(matches)
        query = query.order_by(users.c.display_name, users.c.id).offset(page * 10).limit(11)
        return [dict(row) for row in (await self.connection.execute(query)).mappings()]
