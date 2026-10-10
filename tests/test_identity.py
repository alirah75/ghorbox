import asyncio

import pytest
from sqlalchemy import func, select

from app.core.permissions import MANAGE_ADMINS, AccessDenied, InputError, Role
from app.database.storage import Database
from app.services.identity_service import IdentityService


async def test_bootstrap_is_one_time_and_environment_cannot_regrant(identity_service):
    service = identity_service
    target = await service.add_user(111, 222, "مدیر دوم")
    await service.set_admin(111, 222, Role.FULL, can_manage_admins=True)
    root = await service.authenticate(111)
    await service.revoke_admin(222, root.user_id)
    assert await service.bootstrap((111, 333)) is False
    assert (await service.authenticate(111)).admin_id is None
    with pytest.raises(AccessDenied):
        await service.authenticate(333)
    assert target


async def test_main_admin_always_has_full_hierarchy_permissions(identity_service):
    service = identity_service
    await service.add_user(111, 222, "مدیر اصلی دوم")
    # The legacy flag may not downgrade a main administrator.
    await service.set_admin(111, 222, Role.MAIN, can_manage_admins=False)
    principal = await service.authenticate(222)
    assert principal.allows(*MANAGE_ADMINS)
    assert principal.allows("internal", "manage_users")
    await service.add_user(222, 333, "عضو عادی")
    await service.set_admin(222, 333, Role.INTERNAL)
    assert (await service.authenticate(333)).allows("internal", "read_feedback")


async def test_internal_admin_cannot_promote_admins(identity_service):
    service = identity_service
    manager_id = await service.add_user(111, 222, "مدیر داخلی")
    await service.set_admin(111, 222, Role.INTERNAL)
    await service.create_internal_group(111, "گروه مدیر داخلی", manager_id)
    await service.add_user(222, 333, "عضو عادی")
    with pytest.raises(AccessDenied):
        await service.set_admin(222, 333, Role.INTERNAL)


async def test_membership_disable_reenable_and_start(identity_service):
    service = identity_service
    with pytest.raises(AccessDenied):
        await service.authenticate(222)
    target = await service.add_user(111, 222, "علی رضایی")
    principal = await service.authenticate(222, started=True)
    assert principal.display_name == "علی رضایی"
    assert principal.admin_id is None
    assert (await service.get_user(111, target))["started_at"] is not None
    await service.update_user(111, target, active=False)
    with pytest.raises(AccessDenied):
        await service.authenticate(222)
    await service.update_user(111, target, active=True, name="علی جدید")
    assert (await service.authenticate(222)).display_name == "علی جدید"


@pytest.mark.parametrize(
    "role,scope,allowed",
    [
        (Role.INTERNAL, "internal", True),
        (Role.INTERNAL, "external", False),
        (Role.EXTERNAL, "internal", False),
        (Role.EXTERNAL, "external", True),
        (Role.FULL, "internal", True),
        (Role.FULL, "external", True),
    ],
)
@pytest.mark.parametrize("permission", ["read_feedback", "view_statistics"])
async def test_scope_matrix(identity_service, role, scope, allowed, permission):
    await identity_service.add_user(111, 222, "مدیر آزمایش")
    await identity_service.set_admin(111, 222, role)
    if allowed:
        await identity_service.require(222, scope, permission)
    else:
        with pytest.raises(AccessDenied):
            await identity_service.require(222, scope, permission)


async def test_external_admin_cannot_access_identity_queries(identity_service):
    service = identity_service
    target = await service.add_user(111, 222, "مدیر خارجی")
    await service.set_admin(111, 222, Role.EXTERNAL)
    for operation in [
        service.list_users(222),
        service.get_user(222, target),
        service.list_admins(222),
        service.add_user(222, 333, "نفوذ"),
        service.update_user(222, target, active=False),
    ]:
        with pytest.raises(AccessDenied):
            await operation


async def test_ordinary_user_cannot_mutate_or_list(identity_service):
    service = identity_service
    target = await service.add_user(111, 222, "کاربر عادی")
    for operation in [
        service.list_users(222),
        service.set_admin(222, 222, Role.FULL),
        service.revoke_admin(222, target),
    ]:
        with pytest.raises(AccessDenied):
            await operation


@pytest.mark.parametrize("change", ["disable", "revoke", "demote"])
async def test_last_manager_protected_and_transaction_rolled_back(identity_service, change):
    service = identity_service
    root = await service.authenticate(111)
    with pytest.raises(InputError):
        if change == "disable":
            await service.update_user(111, root.user_id, active=False)
        elif change == "revoke":
            await service.revoke_admin(111, root.user_id)
        else:
            await service.set_admin(111, 111, Role.INTERNAL)
    assert (await service.authenticate(111)).allows("system", "manage_admins")


async def test_user_manager_cannot_disable_admin_or_escalate(identity_service):
    service = identity_service
    await service.add_user(111, 222, "مدیر داخلی")
    await service.set_admin(111, 222, Role.INTERNAL)
    root = await service.authenticate(111)
    with pytest.raises(AccessDenied):
        await service.update_user(222, root.user_id, active=False)
    with pytest.raises(AccessDenied):
        await service.set_admin(222, 222, Role.FULL, can_manage_admins=True)


async def test_scope_limited_role_cannot_receive_global_grant(identity_service):
    await identity_service.add_user(111, 222, "مدیر خارجی")
    with pytest.raises(InputError):
        await identity_service.set_admin(111, 222, Role.EXTERNAL, can_manage_admins=True)


async def test_revocation_immediate_even_after_prior_authentication(identity_service):
    service = identity_service
    user_id = await service.add_user(111, 222, "مدیر داخلی")
    await service.set_admin(111, 222, Role.INTERNAL)
    await service.authenticate(222)
    await service.revoke_admin(111, user_id)
    with pytest.raises(AccessDenied):
        await service.add_user(222, 333, "دسترسی قدیمی")


async def test_search_pagination_duplicate_and_invalid_inputs(identity_service):
    service = identity_service
    for number in range(12):
        await service.add_user(111, 1000 + number, f"کاربر {number:02}")
    assert len(await service.list_users(111, page=0, search="کاربر")) == 11
    assert len(await service.list_users(111, page=1, search="کاربر")) == 2
    assert await service.list_users(111, search="%") == []
    found = await service.list_users(111, search="1003")
    assert len(found) == 1 and found[0]["telegram_id"] == 1003
    with pytest.raises(InputError):
        await service.add_user(111, 1000, "تکراری")
    with pytest.raises(InputError):
        await service.add_user(111, True, "نام معتبر")
    with pytest.raises(InputError):
        await service.add_user(111, 5000, "نام\nنام")


async def test_concurrent_last_manager_changes_keep_one_active(identity_service):
    service = identity_service
    await service.add_user(111, 222, "مدیر دوم")
    await service.set_admin(111, 222, Role.FULL, can_manage_admins=True)
    # Separate pools model simultaneous writers, not merely one pooled connection.
    path = service.database.engine.url.database
    from pathlib import Path
    from urllib.parse import unquote, urlparse

    parsed = unquote(urlparse(path).path)
    import os

    if os.name == "nt":
        parsed = parsed.lstrip("/")
    second_db = Database(Path(parsed), "identity")
    second = IdentityService(second_db)
    root, other = await service.authenticate(111), await service.authenticate(222)
    try:
        results = await asyncio.gather(
            service.revoke_admin(111, root.user_id),
            second.revoke_admin(222, other.user_id),
            return_exceptions=True,
        )
        assert sum(isinstance(result, InputError) for result in results) == 1
        remaining = [
            item
            for item in await service.list_admins(
                111 if (await service.authenticate(111)).admin_id else 222
            )
            if item["is_active"]
        ]
        assert len(remaining) == 1
    finally:
        await second_db.close()


async def test_audit_has_no_message_payload_and_failed_changes_not_committed(identity_service):
    service = identity_service
    target = await service.add_user(111, 222, "نام محرمانه")
    audit = service.tables["admin_audit"]
    async with service.database.transaction() as conn:
        rows = (await conn.execute(select(audit))).mappings().all()
        assert len(rows) == 2
        assert rows[-1]["target_ref"] == target
        assert "نام محرمانه" not in str(rows)
        assert (
            await conn.execute(select(func.count()).select_from(service.tables["users"]))
        ).scalar() == 2
