from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from app.bot.bootstrap import ACCESS_DENIED, READY
from app.core.permissions import AccessDenied, Role
from app.main import create_dispatcher
from tests.conftest import TEST_TOKEN
from tests.test_bootstrap import callback_update, message_update


class ChatHarness:
    def __init__(self, service, feedback=None, external=None):
        self.bot = Bot(TEST_TOKEN)
        self.bot.session = AsyncMock(return_value=True)
        self.dispatcher = create_dispatcher(service, feedback, external)
        self.latest = []

    async def feed(self, update):
        self.bot.session.reset_mock()
        await self.dispatcher.feed_update(self.bot, update)
        self.latest = [call.args[1] for call in self.bot.session.call_args_list]
        return self.latest

    async def text(self, value, user=111):
        return await self.feed(message_update(user_id=user, text=value))

    def button(self, label):
        for request in reversed(self.latest):
            if isinstance(request, SendMessage) and isinstance(
                request.reply_markup, InlineKeyboardMarkup
            ):
                for row in request.reply_markup.inline_keyboard:
                    for button in row:
                        if label in button.text:
                            return button.callback_data
        raise AssertionError(f"Missing button {label}")

    async def click(self, label, user=111):
        return await self.feed(callback_update(user_id=user, data=self.button(label)))

    async def pick(self, number, name, user=111):
        request = next(
            item
            for item in reversed(self.latest)
            if isinstance(getattr(item, "reply_markup", None), ReplyKeyboardMarkup)
        )
        request_id = request.reply_markup.keyboard[0][0].request_users.request_id
        return await self.feed(
            message_update(
                user_id=user,
                text=None,
                users_shared={
                    "request_id": request_id,
                    "users": [{"user_id": number, "first_name": name}],
                },
            )
        )


async def test_complete_add_disable_enable_user_flow(identity_service):
    chat = ChatHarness(identity_service)
    await chat.text("/admin")
    await chat.click("کاربران")
    await chat.click("افزودن کاربر")
    await chat.pick(222, "کاربر آزمایش")
    token = chat.button("تأیید")
    assert "کاربر آزمایش" in chat.latest[-1].text
    with pytest.raises(AccessDenied):
        await identity_service.authenticate(222)
    await chat.click("تأیید")
    await identity_service.authenticate(222)
    await chat.feed(callback_update(data=token))
    assert chat.latest[-1].show_alert
    await chat.text("/admin")
    await chat.click("کاربران")
    await chat.click("کاربر آزمایش")
    await chat.click("غیرفعال")
    await chat.click("تأیید")
    await chat.text("/start", user=222)
    assert chat.latest[-1].text == ACCESS_DENIED
    await chat.text("/admin")
    await chat.click("کاربران")
    await chat.click("کاربر آزمایش")
    await chat.click("فعال کردن")
    await chat.click("تأیید")
    await chat.text("/start", user=222)
    assert chat.latest[-1].text == READY
    assert all(
        "مدیریت" not in button.text
        for row in chat.latest[-1].reply_markup.inline_keyboard
        for button in row
    )


async def test_admin_role_confirmation_and_internal_panel(identity_service):
    await identity_service.add_user(111, 222, "مدیر داخلی")
    chat = ChatHarness(identity_service)
    await chat.text("/admin")
    await chat.click("مدیران")
    await chat.click("افزودن")
    await chat.pick(222, "مدیر داخلی")
    assert chat.button("مدیر اصلی")
    assert all(
        "خارجی" not in button.text
        for request in chat.latest
        if isinstance(getattr(request, "reply_markup", None), InlineKeyboardMarkup)
        for row in request.reply_markup.inline_keyboard
        for button in row
    )
    await chat.click("مدیر داخلی")
    assert (await identity_service.authenticate(222)).admin_id is None
    await chat.click("تأیید")
    await chat.text("/admin", user=222)
    panel = chat.latest[-1]
    assert "داخلی" in panel.text
    assert chat.button("کاربران")
    await chat.text("/admin")
    await chat.click("مدیران")
    await chat.click("مدیر داخلی")
    await chat.click("لغو مدیریت")
    await chat.click("تأیید")
    await chat.text("/admin", user=222)
    assert chat.latest[-1].text == ACCESS_DENIED


async def test_group_management_flow_and_scoped_internal_user_list(identity_service):
    manager_id = await identity_service.add_user(111, 222, "سرپرست محصول")
    await identity_service.set_admin(111, 222, Role.INTERNAL)
    member_id = await identity_service.add_user(111, 333, "عضو محصول")
    await identity_service.add_user(111, 444, "عضو خارج گروه")
    chat = ChatHarness(identity_service)

    await chat.text("/admin")
    await chat.click("گروه‌های داخلی")
    await chat.click("ساخت گروه")
    await chat.text("تیم محصول")
    await chat.click("سرپرست محصول")
    await chat.click("تأیید")
    assert "تیم محصول" in chat.latest[-1].text
    await chat.click("افزودن عضو موجود")
    await chat.click("عضو محصول")
    await chat.click("تأیید")
    assert "1" in chat.latest[-1].text

    await chat.text("/admin", user=222)
    await chat.click("کاربران", user=222)
    assert chat.button("عضو محصول")
    assert all(
        "عضو خارج گروه" not in button.text
        for request in chat.latest
        if isinstance(getattr(request, "reply_markup", None), InlineKeyboardMarkup)
        for row in request.reply_markup.inline_keyboard
        for button in row
    )
    assert (await identity_service.get_user(222, member_id))["display_name"] == "عضو محصول"
    assert manager_id


async def test_stolen_callback_and_revoked_pending_confirmation(identity_service):
    target = await identity_service.add_user(111, 222, "مدیر داخلی")
    await identity_service.set_admin(111, 222, Role.INTERNAL)
    chat = ChatHarness(identity_service)
    await chat.text("/admin", user=222)
    stolen = chat.button("کاربران")
    await chat.feed(callback_update(user_id=111, data=stolen))
    assert chat.latest[-1].show_alert
    await chat.feed(callback_update(user_id=222, data=stolen))
    await chat.click("افزودن", user=222)
    await chat.pick(333, "کاربر آینده", user=222)
    token = chat.button("تأیید")
    await identity_service.revoke_admin(111, target)
    await chat.feed(callback_update(user_id=222, data=token))
    assert chat.latest[-1].text == ACCESS_DENIED
    with pytest.raises(AccessDenied):
        await identity_service.authenticate(333)


async def test_cancel_prevents_confirmation_and_profile_is_own(identity_service):
    await identity_service.add_user(111, 222, "پروفایل شخصی")
    chat = ChatHarness(identity_service)
    await chat.text("/admin")
    await chat.click("کاربران")
    await chat.click("افزودن")
    await chat.pick(333, "کاربر لغوشده")
    token = chat.button("تأیید")
    await chat.text("/cancel")
    await chat.feed(callback_update(data=token))
    assert chat.latest[-1].show_alert
    with pytest.raises(AccessDenied):
        await identity_service.authenticate(333)
    await chat.text("/start", user=222)
    await chat.click("پروفایل", user=222)
    assert "پروفایل شخصی" in chat.latest[-1].text
    assert "مدیر اولیه" not in chat.latest[-1].text
