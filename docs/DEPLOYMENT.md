# استقرار نسخه 1.0.0

یکی از دو روش Docker Compose یا systemd را انتخاب کنید. برای هر توکن Telegram
فقط یک نمونه ربات اجرا شود؛ SQLite باید روی دیسک محلی باشد، نه پوشه شبکه‌ای.
این نسخه با Python 3.13 و وابستگی‌های uv.lock ساخته می‌شود. هیچ پورت ورودی
لازم نیست؛ دسترسی خروجی HTTPS به Telegram و DNS لازم است. حالت اجرا long polling است.

در ۲۰۲۶/۱۰/۰۶ تصویر ghorbox:1.0.0 روی Docker Desktop ساخته شد و آزمون آفلاین
نصب، دیتابیس، backup و restore با شبکه قطع و کاربر غیر root موفق بود. هر دو
تنظیم Compose نیز اعتبارسنجی شدند. اتصال واقعی Telegram در کانتینر و اجرای
systemd هنوز به تست میزبان مقصد نیاز دارند. توکن و کلیدها را در مخزن عمومی قرار ندهید.

## گزینه اول: Docker Compose روی Linux

Docker Engine و Compose v2 باید نصب باشند. دستورها از ریشه پروژه اجرا می‌شوند.
پروفایل زیر برای Docker معمولی روی Linux با حساب کانتینر 10001 است؛ نگاشت UID
در rootless Docker یا مجوز پوشه در Docker Desktop باید جداگانه بررسی شود.
از نسخه Linux کانتینر استفاده کنید.

### آماده‌سازی نصب تازه

این بخش فقط برای نصب بدون داده و کلید است. برای انتقال نصب موجود، بخش انتقال
را بخوانید؛ هیچ کلیدی را دوباره نسازید.

```bash
sudo install -d -m 0700 -o 10001 -g 10001 deploy/runtime
sudo install -d -m 0700 -o 10001 -g 10001 deploy/runtime/data deploy/runtime/keys deploy/runtime/backups deploy/runtime/recovery
sudo install -m 0600 -o 10001 -g 10001 .env.example deploy/runtime/bot.env
sudo nano deploy/runtime/bot.env
docker compose build --pull
docker compose config --quiet
```

در فایل bot.env توکن و آرایه شناسه مدیر اولیه را محلی وارد کنید. RETENTION_ENABLED
را برای شروع false نگه دارید. مسیرهای DATA_DIR و SECRETS_DIR داخل کانتینر از
Dockerfile می‌آیند؛ مسیر Windows در bot.env ننویسید. فایل تنظیمات مستقیم و فقط
خواندنی نصب می‌شود؛ توکن در Dockerfile یا پارامتر build نیست.

```bash
docker compose -f compose.yaml -f deploy/compose.setup.yaml run --rm bot python -m app.cli keys-init
docker compose run --rm bot python -m app.cli db-upgrade
docker compose run --rm bot python -m app.cli db-check
docker compose run --rm bot python -m app.cli admin-bootstrap
docker compose up -d
docker compose ps
docker compose logs --tail=30 bot
```

فایل compose.setup.yaml فقط هنگام ساخت اولیه کلید، پوشه کلید را نوشتنی می‌کند؛
با این فایل ربات را بالا نیاورید. اجرای معمول ریشه فقط‌خواندنی، کاربر غیر root،
کلید فقط‌خواندنی، بدون capability و بدون انتشار پورت دارد. data، backups و recovery
پایدارند و پس از حذف کانتینر باقی می‌مانند. مقادیر حافظه/تعداد پردازش در Compose
نقطه شروع عملیاتی‌اند و باید با بار واقعی بررسی شوند.

### آزمون تصویر بدون Telegram

این آزمون مستقل از تنظیمات و داده واقعی است و تمام فایل‌ها را در /tmp می‌سازد:

```bash
docker run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges --network=none --tmpfs /tmp:rw,noexec,nosuid,size=128m,mode=1777 --mount type=bind,source="$PWD/deploy/smoke.py",target=/smoke.py,readonly ghorbox:1.0.0 python /smoke.py
```

خروجی مورد انتظار `Offline installed-package checks passed` است. این کار نصب
پکیج و همراه‌بودن Migrationها، ساخت و بررسی دیتابیس، پشتیبان و بازیابی را می‌سنجد.
اتصال Telegram و دریافت واقعی SIGTERM باید جداگانه با ربات آزمایشی بررسی شوند.

### توقف، پشتیبان و بازیابی آزمایشی

هر بار نام تازه برای backup و مقصد restore انتخاب کنید. پس از توقف، ابتدا وضعیت
سرویس را بررسی کنید. بازگرداندن داده به پوشه فعال یا حذف داده خودکار انجام نمی‌شود.

```bash
docker compose stop bot
docker compose run --rm bot python -m app.cli backup --output /backups/before-update.afb
docker compose run --rm bot python -m app.cli backup-check --source /backups/before-update.afb
docker compose run --rm bot python -m app.cli restore --source /backups/before-update.afb --target /recovery/drill-01
docker compose run --rm bot python -m app.cli maintenance
docker compose up -d
```

برای بازیابی واقعی، در حالی که سرویس متوقف است، source نصب‌شده روی
`/var/lib/feedback` را در compose.yaml به `./deploy/runtime/recovery/drill-01`
تغییر دهید. پوشه keys همان کلید اصلی بماند. db-check بگیرید و سپس یک نمونه را
با `docker compose up -d --force-recreate` اجرا کنید. پوشه داده قبلی را تا تأیید
بازیابی نگه دارید. جزئیات صف و مرز آمار در [مرحله ۹](TEST_STAGE_09.md) آمده است.

## گزینه دوم: systemd روی Linux

این روش به Linux دارای systemd، Python 3.13 و uv نیاز دارد. برای نمونه سرور
Debian دارای Python 3.13 مناسب است. Docker را هم‌زمان برای همین ربات فعال نکنید.
دستورهای نصب تازه از ریشه پروژه اجرا می‌شوند؛ حساب feedback باید مختص همین سرویس باشد.

```bash
sudo useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin feedback
sudo install -d -m 0755 /opt/feedback
sudo cp -R app pyproject.toml uv.lock /opt/feedback/
sudo "$(command -v uv)" sync --project /opt/feedback --locked --no-dev --no-editable --python /usr/bin/python3.13 --no-python-downloads
sudo install -d -m 0755 -o root -g root /etc/feedback
sudo install -d -m 0700 -o feedback -g feedback /etc/feedback/keys /var/lib/feedback /var/backups/feedback /var/lib/feedback-recovery
sudo install -m 0600 -o feedback -g feedback .env.example /etc/feedback/.env
sudo nano /etc/feedback/.env
sudo install -m 0644 deploy/feedback-bot.service /etc/systemd/system/feedback-bot.service
sudo systemd-analyze verify /etc/systemd/system/feedback-bot.service
```

اگر حساب feedback وجود دارد، آن را دوباره نسازید. مسیر /opt/feedback باید متعلق
به root و برای feedback خواندنی/قابل اجرا باشد. سرویس تنها در /var/lib/feedback
و فضای موقت خصوصی می‌نویسد. فایل .env از WorkingDirectory خوانده می‌شود؛ مقادیر
DATA_DIR و SECRETS_DIR واحد systemd بر مقادیر فایل مقدم‌اند. سرویس خودش کلید
یا دیتابیس تازه نمی‌سازد و Migration خودکار در شروع ندارد.

برای دستورهای مدیریتی، در همان نشست shell تابع زیر را تعریف کنید:

```bash
cd /etc/feedback
feedback_cli() {
  sudo -u feedback env DATA_DIR=/var/lib/feedback SECRETS_DIR=/etc/feedback/keys /opt/feedback/.venv/bin/python -m app.cli "$@"
}
feedback_cli check-config
feedback_cli keys-init
feedback_cli db-upgrade
feedback_cli db-check
feedback_cli admin-bootstrap
sudo systemctl daemon-reload
sudo systemctl enable --now feedback-bot
sudo systemctl status feedback-bot --no-pager
sudo journalctl -u feedback-bot -n 30 --no-pager
```

تابع فوق فقط در نشست فعلی تعریف می‌شود و باید از /etc/feedback اجرا شود. فرمان‌های
keys-init، db-upgrade و admin-bootstrap این بلوک برای راه‌اندازی تازه هستند.
برای نصب موجود db-check کافی است، مگر نسخه جدید صریحاً Migration بخواهد.

### نگهداری آفلاین systemd

```bash
sudo systemctl stop feedback-bot
feedback_cli backup --output /var/backups/feedback/before-update.afb
feedback_cli backup-check --source /var/backups/feedback/before-update.afb
feedback_cli restore --source /var/backups/feedback/before-update.afb --target /var/lib/feedback-recovery/drill-01
feedback_cli maintenance
sudo systemctl start feedback-bot
```

برای بازیابی واقعی، پس از توقف، در واحد systemd هم DATA_DIR و هم ReadWritePaths
را به `/var/lib/feedback-recovery/drill-01` تغییر دهید. SECRETS_DIR ثابت بماند.
برای db-check آفلاین هم DATA_DIR همین مسیر باشد. daemon-reload سپس start اجرا شود.
داده قبلی حذف نشود؛ از اجرای دو پوشه با یک توکن جلوگیری کنید.

## انتقال نصب موجود از Windows

۱. ربات مبدا را متوقف کنید. پشتیبان رمزگذاری‌شده بگیرید و backup-check انجام دهید.
۲. بسته .afb را با مسیر محافظت‌شده به سرور منتقل کنید؛ فایل keyring.json اصلی را
جدا از backup و فقط برای مسئول مجاز منتقل کنید. توکن در فایل تنظیمات سرور باشد.
۳. برای Docker مالکیت کلید 10001:10001 و برای systemd مالکیت feedback:feedback،
پوشه کلید 0700 و فایل کلید 0600 باشد. کلید تازه نسازید. پوشه مقصد بازیابی موجود نباشد.
۴. restore را در پوشه جدید اجرا و مسیر سرویس را مطابق بخش بازیابی واقعی تغییر دهید.
۵. db-check و آزمون دو حساب را انجام دهید؛ مبدا تا زمان اجرای مقصد خاموش بماند.
صف قدیمی عمداً خودکار دوباره ارسال نمی‌شود. ارسال‌های انجام‌شده بعد از زمان
backup قابل تشخیص قطعی نیستند. گزارش‌های گذشته پس از restore دوباره تولید نمی‌شوند.

## ارتقا و بازگشت

پیش از ارتقا نسخه کد/تصویر قبلی، backup بررسی‌شده و نسخه جداگانه کلید را نگه دارید.
سرویس را متوقف و نسخه جدید را نصب کنید. وابستگی‌ها باید از uv.lock نصب شوند؛
نسخه 1.0.0 همچنان Schema 0001 دارد. db-check سپس شروع و تست دو حساب انجام شود.
برای Docker تصویر قبلی را پیش از build جدید با برچسب جدا حفظ کنید. برچسب پایه
Python وصله‌های جدید دریافت می‌کند؛ برای تکرار دقیق تصویر، digest تصویر ساخته‌شده
را در فرایند انتشار خود ثبت کنید. این مخزن هنوز digest آزموده‌شده سرور ندارد.

اگر فقط کد مشکل دارد و Schema سازگار است، به نسخه کد/تصویر قبلی برگردید؛ داده
زنده را با backup قدیمی جایگزین نکنید. اگر بازیابی داده لازم شد، مسیر جدید و
روش مرحله ۹ را به کار ببرید؛ داده بعد از زمان backup از دست می‌رود.

## پایش و خطایابی

- رویداد application_starting یعنی بررسی اولیه و getMe موفق شده‌اند؛ به‌تنهایی
  تضمین سلامت ادامه polling نیست. /status و یک ارسال آزمایشی را بررسی کنید.
- startup_failed: ابتدا db-check، مجوز فایل‌ها، کلید اصلی و اتصال شبکه بررسی شوند.
- delivery_worker_failed یا statistics_scheduler_failed: فضای دیسک، صحت داده و
  مجوزها بررسی شوند؛ فایل‌های داده را دستی ویرایش نکنید.
- uncertain یعنی تحویل نامشخص است؛ ارسال مجدد خودکار انجام نمی‌شود.
- از دست‌رفتن توکن: از BotFather تعویض و فایل تنظیمات را اصلاح و سرویس را restart کنید.
  از دست‌رفتن کلید: کلید اصلی را از نسخه مستقل بازیابی کنید؛ کلید جدید راه‌حل نیست.
- کمبود فضا: سرویس را متوقف کنید، فضا را بدون حذف دیتابیس/کلید آزاد کنید و db-check
  بگیرید. ظرفیت داده، backup و فضای موقت را پایش کنید؛ backup به فضای چند نسخه نیاز دارد.
- محدودیت شروع systemd بعد از خطاهای مکرر: پس از رفع علت، systemctl reset-failed
  feedback-bot سپس start اجرا شود. Log عمداً متن پیام و جزئیات خام استثنا ندارد.

پشتیبان‌گیری زمان‌بندی‌شده و پایش بیرونی در این تحویل خودکار فعال نشده‌اند؛
بهره‌بردار باید زمان‌بندی، نگهداری نسخه‌ها و آزمون دوره‌ای restore را تعیین کند.
نگهداری داده ساعتی با RETENTION_ENABLED=true مستقل از backup است و پیش‌فرض خاموش است.

مبنای تنظیمات: [راهنمای رسمی uv و Docker](https://docs.astral.sh/uv/guides/integration/docker/)،
[Build context و dockerignore](https://docs.docker.com/build/concepts/context/)،
[مشخصات Compose](https://compose-spec.github.io/compose-spec/spec.html)،
[تعریف رسمی systemd.service](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml).
