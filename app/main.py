"""Application lifecycle and Telegram polling."""

import asyncio
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from app.bot.bootstrap import create_bootstrap_router
from app.core.config import ConfigurationError, Settings, load_settings
from app.core.console import configure_console
from app.core.logging import Event, configure_logging, log_event
from app.core.runtime_lock import RuntimeLock
from app.core.security import Keyring, Scope, ScopeCipher
from app.database.storage import Database, validate_storage
from app.services.external_service import ExternalService
from app.services.feedback_service import FeedbackService
from app.services.identity_service import IdentityService
from app.workers.delivery import DeliveryWorker, run_deliveries
from app.workers.maintenance import run_maintenance
from app.workers.statistics import run_statistics


def create_dispatcher(service: IdentityService, feedback=None, external=None) -> Dispatcher:
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.include_router(create_bootstrap_router(service, feedback, external))
    return dispatcher


async def run_bot(settings: Settings) -> None:
    with RuntimeLock(settings.data_dir):
        await _run_bot(settings)


async def _run_bot(settings: Settings) -> None:
    # Never create keys or silently migrate production data on startup.
    await validate_storage(settings)
    database = Database(settings.data_dir / "identity.db", "identity")
    service = IdentityService(database)
    bot = None
    internal = None
    external_db = None
    worker_task = None
    scheduler_task = None
    maintenance_task = None
    try:
        await service.bootstrap(settings.bootstrap_admin_ids)
        internal = Database(settings.data_dir / "internal.db", "internal")
        keyring = Keyring.load(settings.secrets_dir / "keyring.json")
        feedback = FeedbackService(
            service, internal, ScopeCipher(keyring, Scope.INTERNAL), settings.app_timezone
        )
        external_db = Database(settings.data_dir / "external.db", "external")
        external = ExternalService(
            service, external_db, ScopeCipher(keyring, Scope.EXTERNAL), settings.app_timezone
        )
        bot = Bot(
            token=settings.bot_token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=None, link_preview_is_disabled=True),
        )
        dispatcher = create_dispatcher(service, feedback, external)
        await bot.get_me()
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="نمایش منوی اصلی"),
                BotCommand(command="cancel", description="لغو عملیات و بازگشت به منوی اصلی"),
            ]
        )
        worker_task = asyncio.create_task(
            run_deliveries(DeliveryWorker(feedback, bot), DeliveryWorker(external, bot))
        )
        scheduler_task = asyncio.create_task(run_statistics(feedback, external))
        if settings.retention_enabled:
            maintenance_task = asyncio.create_task(run_maintenance(feedback, external))
        log_event(Event.STARTING)
        await dispatcher.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            handle_as_tasks=False,
            close_bot_session=False,
        )
    finally:
        try:
            if maintenance_task is not None:
                maintenance_task.cancel()
                with suppress(asyncio.CancelledError):
                    await maintenance_task
            if scheduler_task is not None:
                scheduler_task.cancel()
                with suppress(asyncio.CancelledError):
                    await scheduler_task
            if worker_task is not None:
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task
            if bot is not None:
                await bot.session.close()
        finally:
            if external_db is not None:
                await external_db.close()
            if internal is not None:
                await internal.close()
            await database.close()
        log_event(Event.STOPPED)


def main() -> int:
    configure_console()
    configure_logging()
    try:
        settings = load_settings()
    except ConfigurationError as error:
        print(str(error))
        return 2
    try:
        asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        return 0
    except Exception:
        log_event(Event.STARTUP_FAILED)
        print("اجرای ربات ناموفق بود؛ ابتدا db-check و سپس توکن و اتصال شبکه را بررسی کنید.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
