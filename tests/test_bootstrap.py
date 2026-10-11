from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.bot.bootstrap import ACCESS_DENIED, HELP, READY
from app.main import create_dispatcher, run_bot
from tests.conftest import TEST_TOKEN


def message_update(user_id=111, text="/start", chat_type="private", **kwargs):
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=user_id if chat_type == "private" else -10001, type=chat_type),
            from_user=User(id=user_id, is_bot=False, first_name="PRIVATE NAME"),
            text=text,
            **kwargs,
        ),
    )


def callback_update(user_id=111, data="bootstrap:status", chat_type="private"):
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="opaque-callback",
            from_user=User(id=user_id, is_bot=False, first_name="PRIVATE NAME"),
            chat_instance="test",
            data=data,
            message=message_update(user_id, chat_type=chat_type).message,
        ),
    )


async def dispatch(identity_service, update):
    bot = Bot(TEST_TOKEN)
    bot.session = AsyncMock(return_value=True)
    dispatcher = create_dispatcher(identity_service)
    await dispatcher.feed_update(bot, update)
    return [call.args[1] for call in bot.session.call_args_list]


async def test_authorized_start_shows_stage_and_keyboard(identity_service):
    requests = await dispatch(identity_service, message_update())
    assert len(requests) == 1
    assert isinstance(requests[0], SendMessage)
    assert requests[0].text == READY
    assert requests[0].reply_markup is not None
    assert "PRIVATE NAME" not in requests[0].text


async def test_unknown_user_cannot_access_diagnostics(identity_service):
    requests = await dispatch(identity_service, message_update(user_id=222))
    assert requests[0].text == ACCESS_DENIED
    assert requests[0].reply_markup is None


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_no_response_or_information_in_group(identity_service, chat_type):
    assert await dispatch(identity_service, message_update(chat_type=chat_type)) == []


async def test_help_callback(identity_service):
    bot = Bot(TEST_TOKEN)
    bot.session = AsyncMock(return_value=True)
    dispatcher = create_dispatcher(identity_service)
    await dispatcher.feed_update(bot, message_update())
    start_request = bot.session.call_args.args[1]
    token = start_request.reply_markup.inline_keyboard[0][1].callback_data
    bot.session.reset_mock()
    await dispatcher.feed_update(bot, callback_update(data=token))
    requests = [call.args[1] for call in bot.session.call_args_list]
    assert isinstance(requests[0], AnswerCallbackQuery)
    assert requests[1].text == HELP


async def test_callback_rechecks_identity(identity_service):
    requests = await dispatch(identity_service, callback_update(user_id=222))
    assert len(requests) == 1
    assert requests[0].text == ACCESS_DENIED


async def test_forged_callback_is_rejected(identity_service):
    requests = await dispatch(identity_service, callback_update(data="admin:internal:123"))
    assert len(requests) == 1
    assert requests[0].show_alert is True


async def test_group_callback_is_ignored(identity_service):
    assert await dispatch(identity_service, callback_update(chat_type="supergroup")) == []


async def test_plaintext_is_not_echoed_or_accepted(identity_service):
    requests = await dispatch(identity_service, message_update(text="SECRET FEEDBACK"))
    assert "SECRET FEEDBACK" not in requests[0].text
    assert "منوی /start" in requests[0].text


async def test_media_is_not_accepted(identity_service):
    update = message_update(
        text=None,
        sticker={
            "file_id": "test",
            "file_unique_id": "unique",
            "type": "regular",
            "width": 100,
            "height": 100,
            "is_animated": False,
            "is_video": False,
        },
    )
    requests = await dispatch(identity_service, update)
    assert "منوی /start" in requests[0].text


async def test_notification_failure_is_handled(identity_service):
    bot = Bot(TEST_TOKEN)
    bot.session = AsyncMock(side_effect=RuntimeError("PRIVATE payload"))
    dispatcher = create_dispatcher(identity_service)
    assert await dispatcher.feed_update(bot, message_update()) is True


async def test_polling_closes_session_on_failure(settings, identity_service, monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.validate_storage", AsyncMock())
    settings = settings.model_copy(
        update={"data_dir": tmp_path / "data", "secrets_dir": tmp_path / "keys"}
    )
    bot = AsyncMock()
    dispatcher = AsyncMock()
    dispatcher.start_polling.side_effect = RuntimeError("network failed")
    monkeypatch.setattr("app.main.Bot", lambda **kwargs: bot)
    monkeypatch.setattr("app.main.create_dispatcher", lambda *args: dispatcher)
    with pytest.raises(RuntimeError):
        await run_bot(settings)
    bot.set_my_commands.assert_awaited_once()
    commands = bot.set_my_commands.call_args.args[0]
    assert [command.command for command in commands] == ["start", "cancel"]
    bot.session.close.assert_awaited_once()
    assert dispatcher.start_polling.call_args.kwargs["handle_as_tasks"] is False
