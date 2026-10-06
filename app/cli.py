"""Local setup and diagnostics without exposing configuration secrets."""

import argparse
import asyncio
import os
from pathlib import Path

from app.core.config import ConfigurationError, load_settings
from app.core.console import configure_console
from app.core.logging import configure_logging
from app.core.runtime_lock import RuntimeBusy, RuntimeLock
from app.core.security import create_keyring, validate_storage_paths
from app.database.storage import Database, prepare_storage, validate_storage
from app.services.identity_service import IdentityService

ENV_TEMPLATE = """# Local secrets. Never commit or share this file.
BOT_TOKEN=
BOOTSTRAP_ADMIN_IDS=[]
APP_TIMEZONE=Asia/Tehran
DATA_DIR=data
SECRETS_DIR=.secrets
RETENTION_ENABLED=false
"""


def init_env(path: Path = Path(".env")) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(ENV_TEMPLATE)


async def bootstrap_admins(settings):
    await validate_storage(settings)
    database = Database(settings.data_dir / "identity.db", "identity")
    try:
        return await IdentityService(database).bootstrap(settings.bootstrap_admin_ids)
    finally:
        await database.close()


async def maintenance(settings, apply):
    from app.core.security import Keyring, Scope, ScopeCipher
    from app.services.external_service import ExternalService
    from app.services.feedback_service import FeedbackService
    from app.services.maintenance_service import MaintenanceService

    with RuntimeLock(settings.data_dir):
        await validate_storage(settings)
        keyring = Keyring.load(settings.secrets_dir / "keyring.json")
        identity = Database(settings.data_dir / "identity.db", "identity")
        try:
            for scope, cls in [
                (Scope.INTERNAL, FeedbackService),
                (Scope.EXTERNAL, ExternalService),
            ]:
                database = Database(settings.data_dir / f"{scope.value}.db", scope.value)
                try:
                    domain = cls(
                        IdentityService(identity),
                        database,
                        ScopeCipher(keyring, scope),
                        settings.app_timezone,
                    )
                    result = await MaintenanceService(domain).run(apply=apply)
                    label = "داخلی" if scope == Scope.INTERNAL else "خارجی"
                    print(
                        f"{label}: انقضای گفتگو {result['conversations_to_expire']} | "
                        f"حذف گفتگو {result['conversations_to_purge']} | "
                        f"حذف فرم منقضی {result['sessions_to_remove']} | "
                        f"تحویل مبهم {result['ambiguous_deliveries']}"
                    )
                finally:
                    await database.close()
        finally:
            await identity.close()
    print(
        "پاک‌سازی اعمال شد؛ هر حوزه تراکنش مستقل دارد."
        if apply
        else "فقط پیش‌نمایش است؛ هیچ رکوردی حذف نشد. اجرا با --apply"
    )


def main(argv: list[str] | None = None) -> int:
    configure_console()
    configure_logging()
    parser = argparse.ArgumentParser(description="Feedback bot setup and diagnostics")
    parser.add_argument(
        "command",
        choices=[
            "init-env",
            "check-config",
            "keys-init",
            "db-upgrade",
            "db-check",
            "admin-bootstrap",
            "run",
            "backup",
            "backup-check",
            "restore",
            "maintenance",
        ],
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    allowed = {
        "backup": {"output"},
        "backup-check": {"source"},
        "restore": {"source", "target"},
        "maintenance": {"apply"},
    }.get(args.command, set())
    for field in ("output", "source", "target", "apply"):
        if getattr(args, field) and field not in allowed:
            parser.error("گزینه برای این دستور معتبر نیست.")
    for field in allowed - {"apply"}:
        if getattr(args, field) is None:
            parser.error(f"گزینه --{field} لازم است.")
    if args.command in {"backup", "backup-check", "restore", "maintenance"}:
        try:
            settings = load_settings()
            from app.services.backup_service import create_backup, restore_backup

            if args.command == "backup":
                asyncio.run(create_backup(settings, args.output))
                print("نسخه پشتیبان رمزگذاری‌شده ساخته شد؛ کلیدها داخل آن نیستند.")
            elif args.command == "backup-check":
                asyncio.run(restore_backup(settings, args.source))
                print("اصالت، ساختار، دیتابیس‌ها و تطابق کلید نسخه پشتیبان تأیید شد.")
            elif args.command == "restore":
                asyncio.run(restore_backup(settings, args.source, args.target))
                print("بازیابی در پوشه جدید انجام شد؛ DATA_DIR خودکار تغییر نکرد.")
                print("صف قبلی دوباره ارسال نمی‌شود؛ راهنمای مرحله ۹ را پیش از تغییر مسیر بخوانید.")
            else:
                asyncio.run(maintenance(settings, args.apply))
        except RuntimeBusy as error:
            print(str(error))
            return 2
        except Exception:
            print(
                "عملیات ناموفق بود؛ اجرای دیگر ربات، مسیر جدید مقصد، "
                "فضای آزاد و کلید اصلی را بررسی کنید."
            )
            return 2
        return 0
    if args.command == "init-env":
        try:
            init_env()
        except FileExistsError:
            print("فایل .env از قبل وجود دارد و تغییر نکرد.")
            return 2
        except OSError:
            print("ساخت فایل تنظیمات ممکن نشد؛ دسترسی پوشه را بررسی کنید.")
            return 2
        print("فایل .env ساخته شد؛ توکن و شناسه مدیر را فقط در همین فایل وارد کنید.")
        return 0
    if args.command == "check-config":
        try:
            load_settings()
        except ConfigurationError as error:
            print(str(error))
            return 2
        print("تنظیمات معتبر است. اعتبار آنلاین توکن هنوز بررسی نشده است.")
        return 0
    if args.command in {"keys-init", "db-upgrade", "db-check", "admin-bootstrap"}:
        try:
            settings = load_settings()
            if args.command == "keys-init":
                validate_storage_paths(settings.data_dir, settings.secrets_dir)
                # A missing key next to existing data is a recovery problem, not first setup.
                if any(settings.data_dir.glob("*.db")):
                    print("دیتابیس موجود است؛ کلید جدید نسازید. کلید اصلی را بازیابی کنید.")
                    return 2
                create_keyring(settings.secrets_dir / "keyring.json")
                print("کلیدهای مستقل ساخته شدند؛ فایل کلید را جداگانه و امن نگهداری کنید.")
            elif args.command == "db-upgrade":
                with RuntimeLock(settings.data_dir):
                    asyncio.run(prepare_storage(settings))
                print("هر سه دیتابیس در نسخه 0001 آماده‌اند؛ تطابق کلیدها تأیید شد.")
            elif args.command == "admin-bootstrap":
                with RuntimeLock(settings.data_dir):
                    created = asyncio.run(bootstrap_admins(settings))
                print(
                    "مدیر اولیه ثبت شد."
                    if created
                    else "راه‌اندازی اولیه قبلاً انجام شده؛ مجوزها تغییر نکردند."
                )
            else:
                asyncio.run(validate_storage(settings))
                print("identity: OK | internal: OK | external: OK | schema: 0001")
                print("تطابق کلیدها، جداسازی حوزه، صحت دیتابیس، WAL و Foreign Keys تأیید شد.")
        except ConfigurationError as error:
            print(str(error))
            return 2
        except FileExistsError:
            print("فایل کلید از قبل وجود دارد؛ هیچ کلیدی جایگزین نشد.")
            return 2
        except Exception:
            print(
                "بررسی ذخیره‌سازی ناموفق بود؛ مسیرها، کلید اصلی و راهنمای مرحله دوم را بررسی کنید."
            )
            return 2
        return 0
    from app.main import main as run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
