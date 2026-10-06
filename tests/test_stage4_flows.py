from unittest.mock import AsyncMock

import pytest
from aiogram.types import ReplyKeyboardMarkup

from app.core.permissions import AccessDenied
from app.workers.delivery import DeliveryWorker
from tests.test_admin_flows import ChatHarness
from tests.test_bootstrap import callback_update, message_update
from tests.test_feedback import BODY, count


async def test_contact_picker_rejects_wrong_request_and_rechecks_permissions(identity_service):
    chat = ChatHarness(identity_service)
    await chat.text("/admin")
    await chat.click("کاربران")
    await chat.click("افزودن")
    request = chat.latest[-1].reply_markup
    assert isinstance(request, ReplyKeyboardMarkup)
    picker = request.keyboard[0][0].request_users
    assert picker.user_is_bot is False and picker.max_quantity == 1
    assert not picker.request_username and not picker.request_photo
    await chat.feed(
        message_update(
            text=None,
            users_shared={
                "request_id": picker.request_id + 1,
                "users": [{"user_id": 222, "first_name": "جعلی"}],
            },
        )
    )
    assert "انتخاب کاربر" in chat.latest[-1].text
    with pytest.raises(AccessDenied):
        await identity_service.authenticate(222)
    await chat.feed(
        message_update(
            text=None,
            users_shared={
                "request_id": picker.request_id,
                "users": [{"user_id": 222, "first_name": "نام منتخب"}],
            },
        )
    )
    await chat.click("تأیید")
    assert (await identity_service.authenticate(222)).display_name == "نام منتخب"


async def test_missing_shared_name_prompts_for_name_not_id(identity_service):
    chat = ChatHarness(identity_service)
    await chat.text("/admin")
    await chat.click("کاربران")
    await chat.click("افزودن")
    request_id = chat.latest[-1].reply_markup.keyboard[0][0].request_users.request_id
    await chat.feed(
        message_update(
            text=None, users_shared={"request_id": request_id, "users": [{"user_id": 222}]}
        )
    )
    assert "نام نمایشی" in chat.latest[-1].text
    await chat.text("نام سازمانی")
    await chat.click("تأیید")
    assert (await identity_service.authenticate(222)).display_name == "نام سازمانی"


async def test_complete_internal_feedback_confirmation_and_delivery(feedback_service):
    service = feedback_service
    chat = ChatHarness(service.identity, service)
    await chat.text("/start", user=333)
    await chat.click("داخل گروه", user=333)
    assert all(
        "همکار فرستنده" not in button.text
        for row in chat.latest[-1].reply_markup.inline_keyboard
        for button in row
    )
    await chat.click("همکار گیرنده", user=333)
    await chat.click("پیشنهاد", user=333)
    await chat.text("کم", user=333)
    assert "۵" in chat.latest[-1].text
    await chat.text(BODY, user=333)
    token = chat.button("ارسال")
    assert BODY in chat.latest[-1].text
    assert await count(service, "messages") == 0
    await chat.click("ارسال", user=333)
    receipt = chat.button("وضعیت")
    assert await count(service, "messages") == 1
    await chat.feed(callback_update(user_id=333, data=token))
    assert chat.latest[-1].show_alert
    bot = AsyncMock()
    await DeliveryWorker(service, bot).run_once()
    await chat.feed(callback_update(user_id=333, data=receipt))
    assert "تحویل شد" in chat.latest[-1].text
    assert bot.send_message.call_args.kwargs["chat_id"] == 222


async def test_text_media_rejection_and_cancel(feedback_service):
    chat = ChatHarness(feedback_service.identity, feedback_service)
    await chat.text("/start", user=333)
    await chat.click("داخل گروه", user=333)
    await chat.click("همکار گیرنده", user=333)
    await chat.click("انتقاد", user=333)
    await chat.feed(
        message_update(
            user_id=333,
            text=None,
            photo=[
                {
                    "file_id": "file",
                    "file_unique_id": "unique",
                    "width": 10,
                    "height": 10,
                }
            ],
        )
    )
    assert "فقط متن" in chat.latest[-1].text
    await chat.text(BODY, user=333)
    token = chat.button("ارسال")
    await chat.click("لغو", user=333)
    await chat.feed(callback_update(user_id=333, data=token))
    assert chat.latest[-1].show_alert
    assert await count(feedback_service, "messages") == 0


async def test_confirmation_survives_new_dispatcher(feedback_service):
    chat = ChatHarness(feedback_service.identity, feedback_service)
    await chat.text("/start", user=333)
    await chat.click("داخل گروه", user=333)
    await chat.click("همکار گیرنده", user=333)
    await chat.click("انتقاد", user=333)
    await chat.text(BODY, user=333)
    token = chat.button("ارسال")
    restarted = ChatHarness(feedback_service.identity, feedback_service)
    await restarted.feed(callback_update(user_id=333, data=token))
    assert "ثبت شد" in restarted.latest[-1].text
    assert await count(feedback_service, "messages") == 1
