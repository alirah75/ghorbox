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
                await self._replace_grants(conn, admin_id, role_grants(Role.MAIN))
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
        return (await self.add_users(actor_id, [(telegram_id, name)]))[0]

    async def add_users(self, actor_id: int, selected_users):
        if not isinstance(selected_users, (list, tuple)) or not 1 <= len(selected_users) <= 10:
            raise InputError("بین ۱ تا ۱۰ کاربر را انتخاب کنید.")
        normalized = []
        seen = set()
        for item in selected_users:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise InputError("اطلاعات کاربران انتخاب‌شده معتبر نیست.")
            telegram_id, name = valid_id(item[0]), valid_name(item[1])
            if telegram_id in seen:
                raise InputError("یک کاربر بیش از یک‌بار انتخاب شده است.")
            seen.add(telegram_id)
            normalized.append((telegram_id, name))
        async with self.database.transaction(write=True) as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            actor.require("internal", "manage_users")
            group_id = None
            if not actor.allows(*MANAGE_ADMINS):
                groups = self.tables["internal_groups"]
                group_id = (
                    await conn.execute(
                        select(groups.c.id).where(
                            groups.c.manager_admin_id == actor.admin_id, groups.c.is_active == 1
                        )
                    )
                ).scalar_one_or_none()
                if group_id is None:
                    raise InputError("ابتدا مدیر اصلی باید گروه داخلی شما را ایجاد کند.")
            users = self.tables["users"]
            existing = (
                await conn.execute(
                    select(users.c.telegram_id).where(
                        users.c.telegram_id.in_([item[0] for item in normalized])
                    )
                )
            ).first()
            if existing:
                raise InputError("این شناسه قبلاً ثبت شده است؛ از فهرست کاربران استفاده کنید.")
            user_ids = []
            for telegram_id, name in normalized:
                user_id = uuid4().hex
                user_ids.append(user_id)
                await conn.execute(
                    users.insert().values(id=user_id, telegram_id=telegram_id, display_name=name)
                )
                if group_id is not None:
                    await conn.execute(
                        self.tables["internal_group_memberships"]
                        .insert()
                        .values(
                            id=uuid4().hex,
                            group_id=group_id,
                            user_id=user_id,
                            assigned_by_admin_id=actor.admin_id,
                        )
                    )
                await self._audit(conn, actor.admin_id, "add_user", user_id)
            return user_ids

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
                if not active:
                    await self._require_group_manager_reassigned(conn, user_id)
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
        self, actor_id: int, telegram_id: int, role: Role, *, can_manage_admins=None
    ):
        async with self.database.transaction(write=True) as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            actor.require(*MANAGE_ADMINS)
            valid_id(telegram_id)
            if can_manage_admins is not None and type(can_manage_admins) is not bool:
                raise InputError("مجوز نامعتبر است.")
            try:
                permissions = role_grants(Role(role))
            except ValueError:
                raise InputError("نقش مدیریتی معتبر نیست.") from None
            # Main administrators always manage administrators; scoped roles never do.
            if can_manage_admins and Role(role) != Role.MAIN:
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
                if Role(role) != Role.INTERNAL:
                    await self._require_group_manager_reassigned(conn, user_id)
                await conn.execute(
                    update(admins).where(admins.c.id == admin_id).values(is_active=1)
                )
            else:
                admin_id = uuid4().hex
                await conn.execute(admins.insert().values(id=admin_id, user_id=user_id))
            # Internal/main administrators are not ordinary group members. Promotion
            # removes prior ordinary membership in the same atomic transaction.
            if Role(role) in {Role.INTERNAL, Role.MAIN}:
                await conn.execute(
                    delete(self.tables["internal_group_memberships"]).where(
                        self.tables["internal_group_memberships"].c.user_id == user_id
                    )
                )
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
            await self._require_group_manager_reassigned(conn, user_id)
            await conn.execute(update(admins).where(admins.c.id == admin_id).values(is_active=0))
            await self._replace_grants(conn, admin_id, set())
            await self._ensure_manager(conn)
            await self._audit(conn, actor.admin_id, "revoke_admin", user_id)

    async def create_internal_group(self, actor_id: int, name: str, manager_user_id: str):
        async with self.database.transaction(write=True) as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            actor.require(*MANAGE_ADMINS)
            name = valid_name(name)
            groups = self.tables["internal_groups"]
            if (await conn.execute(select(groups.c.id).where(groups.c.name == name))).first():
                raise InputError("گروهی با این نام قبلاً ثبت شده است.")
            manager_admin_id = await self._internal_manager_admin_id(conn, manager_user_id)
            if (
                await conn.execute(
                    select(groups.c.id).where(groups.c.manager_admin_id == manager_admin_id)
                )
            ).first():
                raise InputError("این مدیر داخلی قبلاً مسئول یک گروه است.")
            group_id = uuid4().hex
            await conn.execute(
                groups.insert().values(id=group_id, name=name, manager_admin_id=manager_admin_id)
            )
            await self._audit(conn, actor.admin_id, "create_internal_group", group_id)
            return group_id

    async def set_internal_group_manager(self, actor_id: int, group_id: str, manager_user_id: str):
        async with self.database.transaction(write=True) as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            actor.require(*MANAGE_ADMINS)
            groups = self.tables["internal_groups"]
            if not (await conn.execute(select(groups.c.id).where(groups.c.id == group_id))).first():
                raise AccessDenied()
            manager_admin_id = await self._internal_manager_admin_id(conn, manager_user_id)
            occupied = (
                await conn.execute(
                    select(groups.c.id).where(
                        groups.c.manager_admin_id == manager_admin_id, groups.c.id != group_id
                    )
                )
            ).scalar_one_or_none()
            if occupied is not None:
                raise InputError("این مدیر داخلی قبلاً مسئول یک گروه است.")
            await conn.execute(
                update(groups)
                .where(groups.c.id == group_id)
                .values(manager_admin_id=manager_admin_id)
            )
            await self._audit(conn, actor.admin_id, "set_internal_group_manager", group_id)

    async def list_internal_groups(self, actor_id: int):
        async with self.database.transaction() as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            if not actor.admin_id:
                raise AccessDenied()
            groups = self.tables["internal_groups"]
            memberships = self.tables["internal_group_memberships"]
            admins = self.tables["admins"]
            users = self.tables["users"]
            query = (
                select(
                    groups.c.id,
                    groups.c.name,
                    groups.c.is_active,
                    users.c.id.label("manager_user_id"),
                    users.c.display_name.label("manager_name"),
                    func.count(memberships.c.id).label("member_count"),
                )
                .join(admins, admins.c.id == groups.c.manager_admin_id)
                .join(users, users.c.id == admins.c.user_id)
                .outerjoin(memberships, memberships.c.group_id == groups.c.id)
                .group_by(groups.c.id, users.c.id)
                .order_by(groups.c.name)
            )
            if not actor.allows(*MANAGE_ADMINS):
                actor.require("internal", "manage_users")
                query = query.where(groups.c.manager_admin_id == actor.admin_id)
            return [dict(row) for row in (await conn.execute(query)).mappings()]

    async def list_internal_group_members(self, actor_id: int, group_id: str):
        async with self.database.transaction() as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            await self._group_for_actor(conn, actor, group_id)
            memberships = self.tables["internal_group_memberships"]
            users = self.tables["users"]
            query = (
                select(users.c.id, users.c.telegram_id, users.c.display_name, users.c.is_active)
                .join(memberships, memberships.c.user_id == users.c.id)
                .where(memberships.c.group_id == group_id)
                .order_by(users.c.display_name, users.c.id)
            )
            return [dict(row) for row in (await conn.execute(query)).mappings()]

    async def list_internal_group_candidates(self, actor_id: int, group_id: str):
        async with self.database.transaction() as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            await self._group_for_actor(conn, actor, group_id)
            users = self.tables["users"]
            admins = self.tables["admins"]
            memberships = self.tables["internal_group_memberships"]
            groups = self.tables["internal_groups"]
            query = (
                select(
                    users.c.id,
                    users.c.display_name,
                    memberships.c.group_id.label("current_group_id"),
                    groups.c.name.label("current_group_name"),
                )
                .outerjoin(
                    admins,
                    (admins.c.user_id == users.c.id) & (admins.c.is_active == 1),
                )
                .outerjoin(memberships, memberships.c.user_id == users.c.id)
                .outerjoin(groups, groups.c.id == memberships.c.group_id)
                .where(users.c.is_active == 1, admins.c.id.is_(None))
                .order_by(users.c.display_name, users.c.id)
            )
            if actor.allows(*MANAGE_ADMINS):
                query = query.where(
                    (memberships.c.group_id.is_(None)) | (memberships.c.group_id != group_id)
                )
            else:
                query = query.where(memberships.c.group_id.is_(None))
            return [dict(row) for row in (await conn.execute(query)).mappings()]

    async def list_available_internal_managers(
        self, actor_id: int, *, current_group_id: str | None = None
    ):
        async with self.database.transaction() as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            actor.require(*MANAGE_ADMINS)
            users, admins, grants = (
                self.tables[name] for name in ("users", "admins", "admin_grants")
            )
            groups = self.tables["internal_groups"]
            query = (
                select(users.c.id, users.c.display_name, groups.c.id.label("group_id"))
                .join(admins, admins.c.user_id == users.c.id)
                .join(
                    grants,
                    (grants.c.admin_id == admins.c.id)
                    & (grants.c.scope == "internal")
                    & (grants.c.permission == "manage_users"),
                )
                .outerjoin(groups, groups.c.manager_admin_id == admins.c.id)
                .where(users.c.is_active == 1, admins.c.is_active == 1)
            )
            managers = [dict(row) for row in (await conn.execute(query)).mappings()]
            result = []
            for manager in managers:
                permissions = (
                    await conn.execute(
                        select(grants.c.scope, grants.c.permission).where(
                            grants.c.admin_id
                            == select(admins.c.id)
                            .where(admins.c.user_id == manager["id"])
                            .scalar_subquery()
                        )
                    )
                ).all()
                if MANAGE_ADMINS in permissions:
                    continue
                if manager["group_id"] not in {None, current_group_id}:
                    continue
                result.append(manager)
            return result

    async def assign_internal_group_member(self, actor_id: int, group_id: str, user_id: str):
        async with self.database.transaction(write=True) as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            await self._group_for_actor(conn, actor, group_id)
            users = self.tables["users"]
            admins = self.tables["admins"]
            memberships = self.tables["internal_group_memberships"]
            if not (
                await conn.execute(
                    select(users.c.id).where(users.c.id == user_id, users.c.is_active == 1)
                )
            ).first():
                raise AccessDenied()
            if (
                await conn.execute(
                    select(admins.c.id).where(admins.c.user_id == user_id, admins.c.is_active == 1)
                )
            ).first():
                raise InputError("فقط کاربر عادی را می‌توان عضو گروه کرد.")
            current = (
                (
                    await conn.execute(
                        select(memberships.c.id, memberships.c.group_id).where(
                            memberships.c.user_id == user_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current and current["group_id"] == group_id:
                return False
            if current and not actor.allows(*MANAGE_ADMINS):
                raise AccessDenied()
            if current:
                await conn.execute(
                    update(memberships)
                    .where(memberships.c.id == current["id"])
                    .values(group_id=group_id, assigned_by_admin_id=actor.admin_id)
                )
            else:
                await conn.execute(
                    memberships.insert().values(
                        id=uuid4().hex,
                        group_id=group_id,
                        user_id=user_id,
                        assigned_by_admin_id=actor.admin_id,
                    )
                )
            await self._audit(conn, actor.admin_id, "assign_internal_group_member", group_id)
            return True

    async def remove_internal_group_member(self, actor_id: int, group_id: str, user_id: str):
        async with self.database.transaction(write=True) as conn:
            actor = await IdentityRepository(conn).authenticate(actor_id)
            await self._group_for_actor(conn, actor, group_id)
            memberships = self.tables["internal_group_memberships"]
            result = await conn.execute(
                delete(memberships).where(
                    memberships.c.group_id == group_id, memberships.c.user_id == user_id
                )
            )
            if not result.rowcount:
                raise AccessDenied()
            await self._audit(conn, actor.admin_id, "remove_internal_group_member", group_id)

    async def _internal_manager_admin_id(self, conn, manager_user_id: str):
        users, admins, grants = (self.tables[name] for name in ("users", "admins", "admin_grants"))
        admin_id = (
            await conn.execute(
                select(admins.c.id)
                .join(users, users.c.id == admins.c.user_id)
                .join(grants, grants.c.admin_id == admins.c.id)
                .where(
                    users.c.id == manager_user_id,
                    users.c.is_active == 1,
                    admins.c.is_active == 1,
                    grants.c.scope == "internal",
                    grants.c.permission == "manage_users",
                )
            )
        ).scalar_one_or_none()
        if admin_id is None:
            raise InputError("مدیر انتخاب‌شده باید مدیر داخلی فعال باشد.")
        if (
            await conn.execute(
                select(grants.c.id).where(
                    grants.c.admin_id == admin_id,
                    grants.c.scope == MANAGE_ADMINS[0],
                    grants.c.permission == MANAGE_ADMINS[1],
                )
            )
        ).first():
            raise InputError("مدیر اصلی نمی‌تواند مدیر مستقیم گروه داخلی باشد.")
        return admin_id

    async def _group_for_actor(self, conn, actor: Principal, group_id: str):
        groups = self.tables["internal_groups"]
        group = (
            (
                await conn.execute(
                    select(groups).where(groups.c.id == group_id, groups.c.is_active == 1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if group is None or (
            not actor.allows(*MANAGE_ADMINS) and group["manager_admin_id"] != actor.admin_id
        ):
            raise AccessDenied()
        return group

    async def _require_group_manager_reassigned(self, conn, user_id: str):
        admins = self.tables["admins"]
        groups = self.tables["internal_groups"]
        owns_group = (
            await conn.execute(
                select(groups.c.id)
                .join(admins, admins.c.id == groups.c.manager_admin_id)
                .where(admins.c.user_id == user_id, groups.c.is_active == 1)
            )
        ).first()
        if owns_group:
            raise InputError("ابتدا مدیر دیگری برای گروه داخلی این شخص تعیین کنید.")

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
