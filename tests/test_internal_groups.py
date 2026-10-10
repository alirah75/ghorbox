import pytest

from app.core.permissions import AccessDenied, InputError, Role


async def _internal_manager(service, telegram_id, name):
    user_id = await service.add_user(111, telegram_id, name)
    await service.set_admin(111, telegram_id, Role.INTERNAL)
    return user_id


async def test_main_creates_group_and_both_levels_manage_members(identity_service):
    service = identity_service
    manager_id = await _internal_manager(service, 222, "مدیر گروه یک")
    member_id = await service.add_user(111, 333, "عضو گروه")
    group_id = await service.create_internal_group(111, "گروه محصول", manager_id)

    main_view = await service.list_internal_groups(111)
    manager_view = await service.list_internal_groups(222)
    assert main_view == manager_view
    assert main_view[0]["manager_user_id"] == manager_id
    assert main_view[0]["member_count"] == 0

    assert await service.assign_internal_group_member(222, group_id, member_id) is True
    assert await service.assign_internal_group_member(111, group_id, member_id) is False
    assert [item["id"] for item in await service.list_internal_group_members(222, group_id)] == [
        member_id
    ]
    await service.remove_internal_group_member(111, group_id, member_id)
    assert await service.list_internal_group_members(222, group_id) == []


async def test_group_boundaries_and_main_reassignment(identity_service):
    service = identity_service
    first_manager = await _internal_manager(service, 222, "مدیر اول")
    second_manager = await _internal_manager(service, 333, "مدیر دوم")
    member_id = await service.add_user(111, 444, "عضو قابل انتقال")
    first_group = await service.create_internal_group(111, "گروه اول", first_manager)
    second_group = await service.create_internal_group(111, "گروه دوم", second_manager)
    await service.assign_internal_group_member(222, first_group, member_id)

    with pytest.raises(AccessDenied):
        await service.list_internal_group_members(333, first_group)
    with pytest.raises(AccessDenied):
        await service.assign_internal_group_member(333, second_group, member_id)

    await service.assign_internal_group_member(111, second_group, member_id)
    assert await service.list_internal_group_members(222, first_group) == []
    assert (await service.list_internal_group_members(333, second_group))[0]["id"] == member_id


async def test_internal_manager_sees_only_own_members_and_new_users_are_assigned(identity_service):
    service = identity_service
    first_manager = await _internal_manager(service, 222, "مدیر محدود")
    second_manager = await _internal_manager(service, 333, "مدیر گروه دیگر")
    first_group = await service.create_internal_group(111, "گروه محدود", first_manager)
    second_group = await service.create_internal_group(111, "گروه دیگر", second_manager)
    own_member = await service.add_user(222, 444, "عضو خودکار")
    other_member = await service.add_user(333, 555, "عضو غیرمجاز")

    assert [item["id"] for item in await service.list_users(222)] == [own_member]
    assert (await service.get_user(222, own_member))["display_name"] == "عضو خودکار"
    with pytest.raises(AccessDenied):
        await service.get_user(222, other_member)
    assert (await service.list_internal_group_members(222, first_group))[0]["id"] == own_member
    assert (await service.list_internal_group_members(333, second_group))[0]["id"] == other_member


async def test_group_requires_unique_internal_manager_and_ordinary_members(identity_service):
    service = identity_service
    manager_id = await _internal_manager(service, 222, "مدیر داخلی")
    group_id = await service.create_internal_group(111, "گروه داخلی", manager_id)

    root = await service.authenticate(111)
    with pytest.raises(InputError):
        await service.create_internal_group(111, "گروه مدیر اصلی", root.user_id)
    with pytest.raises(InputError):
        await service.create_internal_group(111, "گروه دوم", manager_id)
    with pytest.raises(InputError):
        await service.assign_internal_group_member(111, group_id, manager_id)


async def test_promoting_an_ordinary_member_removes_group_membership(identity_service):
    service = identity_service
    manager_id = await _internal_manager(service, 222, "مدیر گروه")
    group_id = await service.create_internal_group(111, "گروه ارتقا", manager_id)
    await service.add_user(222, 333, "عضو قابل ارتقا")
    await service.set_admin(111, 333, Role.INTERNAL)
    assert await service.list_internal_group_members(222, group_id) == []
    assert (await service.authenticate(333)).allows("internal", "manage_users")


async def test_group_manager_must_be_reassigned_before_role_or_account_changes(identity_service):
    service = identity_service
    old_manager = await _internal_manager(service, 222, "مدیر قبلی")
    new_manager = await _internal_manager(service, 333, "مدیر جدید")
    group_id = await service.create_internal_group(111, "گروه پایدار", old_manager)

    with pytest.raises(InputError):
        await service.revoke_admin(111, old_manager)
    with pytest.raises(InputError):
        await service.update_user(111, old_manager, active=False)
    with pytest.raises(InputError):
        await service.set_admin(111, 222, Role.MAIN)

    await service.set_internal_group_manager(111, group_id, new_manager)
    await service.revoke_admin(111, old_manager)
    assert (await service.list_internal_groups(333))[0]["manager_user_id"] == new_manager
