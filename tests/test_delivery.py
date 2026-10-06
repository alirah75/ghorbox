from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from app.workers.delivery import DeliveryWorker
from tests.test_feedback import BODY, compose


async def test_fresh_message_has_no_sender_metadata(feedback_service):
    receipt = await feedback_service.submit(333, await compose(feedback_service))
    bot = AsyncMock()
    worker = DeliveryWorker(feedback_service, bot)
    assert await worker.run_once()
    assert not await worker.run_once()
    params = bot.send_message.call_args.kwargs
    assert params["chat_id"] == 222
    assert BODY in params["text"]
    assert "همکار فرستنده" not in params["text"]
    assert "333" not in params["text"]
    assert params["parse_mode"] is None
    assert params["link_preview_options"].is_disabled
    assert "reply_parameters" not in params
    assert (await feedback_service.delivery_status(333, receipt))[0] == "sent"


@pytest.mark.parametrize(
    "error,status",
    [
        (
            TelegramForbiddenError(method=SendMessage(chat_id=222, text="x"), message="private"),
            "failed",
        ),
        (
            TelegramNetworkError(method=SendMessage(chat_id=222, text="x"), message="private"),
            "uncertain",
        ),
        (
            TelegramRetryAfter(
                method=SendMessage(chat_id=222, text="x"), message="private", retry_after=60
            ),
            "pending",
        ),
    ],
)
async def test_safe_error_outcomes(feedback_service, error, status):
    receipt = await feedback_service.submit(333, await compose(feedback_service))
    bot = AsyncMock()
    bot.send_message.side_effect = error
    worker = DeliveryWorker(feedback_service, bot)
    await worker.run_once()
    assert (await feedback_service.delivery_status(333, receipt))[0] == status
    assert not await worker.run_once()
    assert bot.send_message.call_count == 1


async def test_rate_limit_retry_is_durable(feedback_service):
    receipt = await feedback_service.submit(333, await compose(feedback_service))
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=222, text="x"), message="limit", retry_after=10
    )
    await DeliveryWorker(feedback_service, bot).run_once()
    now = feedback_service.clock()
    feedback_service.clock = lambda: now + 20
    bot.send_message.side_effect = None
    assert await DeliveryWorker(feedback_service, bot).run_once()
    assert (await feedback_service.delivery_status(333, receipt))[0] == "sent"


async def test_expired_claim_is_not_blindly_resent(feedback_service):
    receipt = await feedback_service.submit(333, await compose(feedback_service))
    bot = AsyncMock()
    worker = DeliveryWorker(feedback_service, bot)
    assert await worker.claim()
    now = feedback_service.clock()
    feedback_service.clock = lambda: now + 121
    assert not await DeliveryWorker(feedback_service, bot).run_once()
    bot.send_message.assert_not_called()
    assert (await feedback_service.delivery_status(333, receipt))[0] == "uncertain"


@pytest.mark.parametrize("user", [222, 333])
async def test_disabled_participant_stops_delivery(feedback_service, user):
    receipt = await feedback_service.submit(333, await compose(feedback_service))
    member = await feedback_service.identity.authenticate(user)
    await feedback_service.identity.update_user(111, member.user_id, active=False)
    bot = AsyncMock()
    await DeliveryWorker(feedback_service, bot).run_once()
    bot.send_message.assert_not_called()
    if user == 222:
        assert (await feedback_service.delivery_status(333, receipt))[0] == "suspended"
