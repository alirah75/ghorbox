"""Transactional membership and administrator lifecycle with fresh authorization."""

import time
import unicodedata
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from app.core.permissions import (
    MANAGE_ADMINS,
    AccessDenied,
    InputError,
    Principal,
    Role,
    role_grants,
)
from app.database.identity_repository import IdentityRepository


def valid_id(value: int) -> int:
    if type(value) is not int or not 0 < value < 2**52:
        raise InputError("شناسه تلگرام باید عدد صحیح مثبت باشد.")
    return value


def valid_name(value: str) -> str:
    value = value.strip()
    if not 2 <= len(value) <= 80 or any(
        unicodedata.category(char).startswith("C") and char != "\u200c" for char in value
    ):
        raise InputError("نام باید ۲ تا ۸۰ نویسه و بدون نویسه کنترلی باشد.")
    return value


class IdentityService:
    def __init__(self, database):
        if database.kind != "identity":
            raise ValueError("Identity database required")
        self.database = database
        self.tables = database.metadata.tables

    async def bootstrap(self, telegram_ids: tuple[int, ...]) -> bool:
        """Invoked by startup only. Marker and all grants commit in one write transaction."""
        async with self.database.transaction(write=True) as conn:
            marker = self.tables["bootstrap_state"]
            if (await conn.execute(select(marker.c.id))).first():
                return False
            for number, telegram_id in enumerate(telegram_ids, 1):
                valid_id(telegram_id)
                users, admins = (self.tables[name] for name in ("users", "admins"))
                existing = (
                    await conn.execute(select(users.c.id).where(users.c.telegram_id == telegram_id))
                ).scalar_one_or_none()
                if existing:
                    raise InputError("وضعیت راه‌اندازی اولیه نیاز به بررسی مسئول سرور دارد.")
                user_id, admin_id = uuid4().hex, uuid4().hex
                await conn.execute(
                    users.insert().values(
                        id=user_id, telegram_id=telegram_id, display_name=f"مدیر اولیه {number}"
                    )
                )
                await conn.execute(admins.insert().values(id=admin_id, user_id=user_id))
                await self._replace_grants(conn, admin_id, role_grants(Role.FULL) | {MANAGE_ADMINS})
                await self._audit(conn, admin_id, "bootstrap", user_id)
            if not telegram_ids:
                raise InputError("حداقل یک مدیر اولیه لازم است.")
            await conn.execute(marker.insert().values(id="singleton"))
            return True

    async def authenticate(self, telegram_id: int, *, started=False) -> Principal:
        async with self.database.transaction(write=started) as conn:
            principal = await IdentityRepository(conn).authenticate(telegram_id)
            if started:
                users = self.tables["users"]
                await conn.execute(
                    update(users)
                    .where(users.c.id == principal.user_id)
                    .values(
                        started_at=func.coalesce(users.c.started_at, int(time.time())),
                        delivery_state="reachable",
                    )
                )
            return principal

    async def require(self, telegram_id: int, scope: str, permission: str) -> Principal:
        principal = await self.authenticate(telegram_id)
        principal.require(scope, permission)
        return principal

    async def list_users(self, actor_id: int, page=0, search=""):
        if type(page) is not int or page < 0 or page > 10000 or len(search) > 80:
            raise InputError("درخواست جستجو معتبر نیست.")
        async with self.database.transaction() as conn:
            repo = IdentityRepository(conn)
            return await repo.list_users(await repo.authenticate(actor_id), page, search)

    async def get_user(self, actor_id: int, user_id: str):
        async with self.database.transaction() as conn:
            repo = IdentityRepository(conn)
            return await repo.user_by_id(await repo.authenticate(actor_id), user_id)

    async def add_user(self, actor_id: int, telegram_id: int, name: str):
        async with self.database.transaction(write=True) as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            actor.require("internal", "manage_users")
            valid_id(telegram_id)
            name = valid_name(name)
            users = self.tables["users"]
            if (
                await conn.execute(select(users.c.id).where(users.c.telegram_id == telegram_id))
            ).first():
                raise InputError("این شناسه قبلاً ثبت شده است؛ از فهرست کاربران استفاده کنید.")
            user_id = uuid4().hex
            await conn.execute(
                users.insert().values(id=user_id, telegram_id=telegram_id, display_name=name)
            )
            await self._audit(conn, actor.admin_id, "add_user", user_id)
            return user_id

    async def update_user(self, actor_id: int, user_id: str, *, active=None, name=None):
        async with self.database.transaction(write=True) as conn:
            repo = IdentityRepository(conn)
            actor = await repo.authenticate(actor_id)
            await repo.user_by_id(actor, user_id)
            admins, users = (self.tables[key] for key in ("admins", "users"))
            if (await conn.execute(select(admins.c.id).where(admins.c.user_id == user_id))).first():
                actor.require(*MANAGE_ADMINS)
            values = {}
            if active is not None:
                if type(active) is not bool:
                    raise InputError("وضعیت نامعتبر است.")
                values["is_active"] = int(active)
            if name is not None:
                values["display_name"] = valid_name(name)
            if not values:
                raise InputError("تغییری مشخص نشده است.")
            await conn.execute(update(users).where(users.c.id == user_id).values(**values))
            await self._ensure_manager(conn)
            await self._audit(conn, actor.admin_id, "update_user", user_id)

    async def list_admins(self, actor_id: int):
        async with self.database.transaction() as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            actor.require(*MANAGE_ADMINS)
            users, admins, grants = (
                self.tables[name] for name in ("users", "admins", "admin_grants")
            )
            rows = (
                await conn.execute(
                    select(
                        users.c.id,
                        users.c.display_name,
                        users.c.telegram_id,
                        users.c.is_active.label("user_active"),
                        admins.c.id.label("admin_id"),
                        admins.c.is_active,
                    )
                    .join(admins, admins.c.user_id == users.c.id)
                    .order_by(users.c.display_name)
                )
            ).mappings()
            result = []
            for row in rows:
                permissions = (
                    await conn.execute(
                        select(grants.c.scope, grants.c.permission).where(
                            grants.c.admin_id == row["admin_id"]
                        )
                    )
                ).all()
                result.append({**row, "grants": frozenset(permissions)})
            return result

    async def admin_candidate(self, actor_id: int, telegram_id: int):
        async with self.database.transaction() as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            actor.require(*MANAGE_ADMINS)
            users = self.tables["users"]
            name = (
                await conn.execute(
                    select(users.c.display_name).where(
                        users.c.telegram_id == valid_id(telegram_id), users.c.is_active == 1
                    )
                )
            ).scalar_one_or_none()
            if name is None:
                raise InputError("ابتدا این شخص را در بخش کاربران ثبت و فعال کنید.")
            return name

    async def set_admin(
        self, actor_id: int, telegram_id: int, role: Role, *, can_manage_admins=False
    ):
        async with self.database.transaction(write=True) as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            actor.require(*MANAGE_ADMINS)
            valid_id(telegram_id)
            if type(can_manage_admins) is not bool:
                raise InputError("مجوز نامعتبر است.")
            try:
                permissions = role_grants(Role(role))
            except ValueError:
                raise InputError("نقش مدیریتی معتبر نیست.") from None
            # A domain-only admin may never receive global administrator management.
            if can_manage_admins and Role(role) != Role.FULL:
                raise InputError("مدیریت مدیران فقط همراه نقش کامل قابل واگذاری است.")
            users, admins = (self.tables[key] for key in ("users", "admins"))
            user_id = (
                await conn.execute(
                    select(users.c.id).where(
                        users.c.telegram_id == telegram_id, users.c.is_active == 1
                    )
                )
            ).scalar_one_or_none()
            if user_id is None:
                raise InputError("ابتدا کاربر فعال را در بخش کاربران ثبت کنید.")
            admin_id = (
                await conn.execute(select(admins.c.id).where(admins.c.user_id == user_id))
            ).scalar_one_or_none()
            if admin_id:
                await conn.execute(
                    update(admins).where(admins.c.id == admin_id).values(is_active=1)
                )
            else:
                admin_id = uuid4().hex
                await conn.execute(admins.insert().values(id=admin_id, user_id=user_id))
            if can_manage_admins:
                permissions.add(MANAGE_ADMINS)
            await self._replace_grants(conn, admin_id, permissions)
            await self._ensure_manager(conn)
            await self._audit(conn, actor.admin_id, "set_admin", user_id)

    async def revoke_admin(self, actor_id: int, user_id: str):
        async with self.database.transaction(write=True) as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            actor.require(*MANAGE_ADMINS)
            admins = self.tables["admins"]
            admin_id = (
                await conn.execute(select(admins.c.id).where(admins.c.user_id == user_id))
            ).scalar_one_or_none()
            if admin_id is None:
                raise AccessDenied()
            await conn.execute(update(admins).where(admins.c.id == admin_id).values(is_active=0))
            await self._replace_grants(conn, admin_id, set())
            await self._ensure_manager(conn)
            await self._audit(conn, actor.admin_id, "revoke_admin", user_id)

    async def _replace_grants(self, conn, admin_id, permissions):
        grants = self.tables["admin_grants"]
        await conn.execute(delete(grants).where(grants.c.admin_id == admin_id))
        for scope, permission in sorted(permissions):
            await conn.execute(
                grants.insert().values(admin_id=admin_id, scope=scope, permission=permission)
            )

    async def _ensure_manager(self, conn):
        users, admins, grants = (self.tables[name] for name in ("users", "admins", "admin_grants"))
        query = (
            select(admins.c.id)
            .join(users, users.c.id == admins.c.user_id)
            .join(grants, grants.c.admin_id == admins.c.id)
            .where(
                users.c.is_active == 1,
                admins.c.is_active == 1,
                grants.c.scope == "system",
                grants.c.permission == "manage_admins",
            )
        )
        if not (await conn.execute(query)).first():
            raise InputError("آخرین مسئول فعال مدیریت مدیران را نمی‌توان حذف یا غیرفعال کرد.")

    async def _audit(self, conn, admin_id, action, target):
        await conn.execute(
            self.tables["admin_audit"]
            .insert()
            .values(actor_admin_id=admin_id, action=action, target_ref=target, outcome="success")
        )
