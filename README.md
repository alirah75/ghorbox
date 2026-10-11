# غر‌باکس | GhorBox 📦

**غر بزن، پیشنهاد بده، بهترش کنیم!**

صندوق بازخورد تیمی در تلگرام برای انتقاد، پیشنهاد و گفتگوی ناشناس، با دو حوزه
مستقل داخلی و خارجی. نام پیشنهادی مخزن: **`ghorbox`**.

[English](README.en.md) · [راهنمای کاربران](docs/USER_GUIDE.md) ·
[نصب روی سرور](docs/DEPLOYMENT.md) · [معماری و امنیت](docs/ARCHITECTURE.md)

## برنامه در ۱۰ خط

۱. فقط اعضایی که مدیر ثبت و فعال کرده است می‌توانند از ربات استفاده کنند.
۲. مدیر با انتخاب‌گر تلگرام تا ۱۰ عضو را یک‌جا اضافه می‌کند؛ شناسه‌ها در پشت صحنه ذخیره می‌شوند.
۳. هر عضو می‌تواند برای همکار خود انتقاد یا پیشنهاد متنی ارسال کند.
۴. متن پس از نوشته‌شدن، بدون پیش‌نمایش یا تأیید دوباره مستقیماً ثبت و ارسال می‌شود.
۵. گیرنده متن را به‌صورت اعلان مستقیم ربات می‌گیرد و مشخصات نویسنده نمایش داده نمی‌شود.
۶. چند گفت‌وگو هم‌زمان باز می‌ماند و دو طرف با انتخاب گفت‌وگوی فعال، ناشناس چت می‌کنند.
۷. گزارش یا بستن دستی وجود ندارد؛ گفت‌وگوهای بدون فعالیت طبق زمان نگهداری منقضی می‌شوند.
۸. بخش خارجی فهرست و مدیر مستقل دارد و پاسخ آن با واسطه مدیریت انجام می‌شود.
۹. مدیر اصلی همه و مدیر داخلی فقط بازخورد اولیه اعضای گروه خودش را بدون نام نویسنده می‌بیند.
۱۰. گزارش هفتگی، پشتیبان رمزگذاری‌شده و نگهداری اختیاری داده‌ها فراهم است.

## امکانات

- رابط فارسی، افزودن گروهی اعضا، ارسال مستقیم و لغو پیش از نوشتن متن؛ پذیرش فقط متن.
- نقش‌های مدیریتی داخلی، خارجی و کامل؛ اختیار مستقل مدیریت مدیران.
- فهرست گفتگوهای باز، تعویض گفتگوی فعال، تاریخچه و آمار شخصی.
- سه SQLite مستقل برای هویت، بازخورد داخلی و بازخورد خارجی.
- رمزگذاری محتوا و مسیریابی، کنترل دسترسی در سرویس و صف تحویل پایدار.
- گزارش هفتگی با پنهان‌سازی گروه‌های کوچک و نمایش تعدادها به‌صورت بازه.
- backup رمزگذاری‌شده، بررسی بسته، restore در پوشه جدید و پیش‌نمایش پاک‌سازی.
- Dockerfile، Docker Compose، واحد systemd و آزمون‌های آفلاین.

## نصب محلی

نیازمندی‌ها: **Python 3.13** و **uv**. برای نصب تازه، از ریشه پروژه:

```powershell
git clone https://github.com/alirah75/ghorbox.git
cd ghorbox
uv sync --locked
uv run --locked python -m app.cli init-env
```

فایل `.env` را محلی ویرایش کنید؛ توکن را از BotFather بگیرید. تنظیمات:

| متغیر | کاربرد / پیش‌فرض |
|---|---|
| `BOT_TOKEN` | توکن خصوصی ربات؛ الزامی |
| `BOOTSTRAP_ADMIN_IDS` | آرایه JSON شناسه‌های عددی مدیر اولیه؛ الزامی |
| `APP_TIMEZONE` | `Asia/Tehran` |
| `DATA_DIR` | `data` |
| `SECRETS_DIR` | `.secrets`؛ مستقل از پوشه داده |
| `RETENTION_ENABLED` | `false`؛ پاک‌سازی خودکار پیش‌فرض خاموش است |

```powershell
uv run --locked python -m app.cli check-config
uv run --locked python -m app.cli keys-init
uv run --locked python -m app.cli db-upgrade
uv run --locked python -m app.cli db-check
uv run --locked python -m app.cli admin-bootstrap
uv run --locked python -m app.main
```

**در نصب موجود، init-env و keys-init را تکرار نکنید.** اگر داده پاک شده ولی کلید
و ساختار دیتابیس باقی است، db-check و سپس اجرای ربات کافی است؛ مدیر اولیه هنگام
شروع ثبت می‌شود. کلید مفقود را با ساخت کلید تازه جایگزین نکنید.
اعضا و گیرنده‌ها باید در گفتگوی خصوصی ربات `/start` زده باشند. انتخاب‌گر حساب
برای افزودن اعضا بعد از راه‌اندازی مدیر اولیه در دسترس است.

## Docker Compose

[Dockerfile](Dockerfile) و [compose.yaml](compose.yaml) در ریشه مخزن هستند.
دستورهای نصب **تازه** روی Linux با Docker Engine و Compose v2:

```bash
sudo install -d -m 0700 -o 10001 -g 10001 deploy/runtime
sudo install -d -m 0700 -o 10001 -g 10001 deploy/runtime/data deploy/runtime/keys deploy/runtime/backups deploy/runtime/recovery
sudo install -m 0600 -o 10001 -g 10001 .env.example deploy/runtime/bot.env
sudo nano deploy/runtime/bot.env
docker compose build --pull
docker compose config --quiet
docker compose -f compose.yaml -f deploy/compose.setup.yaml run --rm bot python -m app.cli keys-init
docker compose run --rm bot python -m app.cli db-upgrade
docker compose run --rm bot python -m app.cli db-check
docker compose run --rm bot python -m app.cli admin-bootstrap
docker compose up -d
```

توکن و شناسه مدیر را در bot.env وارد کنید. تنظیمات یا کلید نصب موجود را بازنویسی
نکنید. `deploy/runtime` روی میزبان باقی می‌ماند و وارد تصویر نمی‌شود. ربات با
کاربر غیر root، کلید فقط‌خواندنی و بدون پورت ورودی اجرا می‌شود. فایل setup فقط
برای ساخت اولیه کلید است. هر توکن تنها **یک نمونه فعال** داشته باشد.
برای Docker Desktop، systemd، انتقال و بازیابی، [راهنمای استقرار](docs/DEPLOYMENT.md) را بخوانید.

## آزمون و نگهداری

ساخت تصویر Docker و آزمون آفلاین نصب، دیتابیس، پشتیبان و بازیابی در
۲۰۲۶/۱۰/۰۶ موفق بوده است؛ اتصال واقعی Telegram در کانتینر باید روی میزبان مقصد بررسی شود.

```powershell
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
```

برای پشتیبان‌گیری ابتدا ربات را متوقف و هر بار نام تازه انتخاب کنید:

```powershell
uv run --locked python -m app.cli backup --output backups/first.afb
uv run --locked python -m app.cli backup-check --source backups/first.afb
uv run --locked python -m app.cli restore --source backups/first.afb --target recovery/first
uv run --locked python -m app.cli maintenance
```

restore فایل موجود را بازنویسی و مسیر فعال را عوض نمی‌کند. maintenance بدون
`--apply` فقط پیش‌نمایش است. کلید اصلی را جدا از backup نگه دارید؛ بدون آن بازیابی
ممکن نیست. سیاست حذف و صف پس از بازیابی در [راهنمای نگهداری](docs/TEST_STAGE_09.md) آمده است.

## حریم خصوصی و محدودیت‌ها

مدیر مجاز می‌تواند ببیند «یک شخص ناشناس برای علی این بازخورد را نوشته است»؛
نام و شناسه نویسنده در پنل نشان داده نمی‌شود. این تضمین شامل دسترسی کامل به
سرور/کلیدها، Telegram یا استنتاج هویت از متن و زمان نیست. نویسنده اولیه، گیرنده
را انتخاب کرده است؛ بنابراین پاسخ گیرنده می‌تواند برای او قابل انتساب باشد.

تحویل دقیقاً یک‌باره Telegram تضمین نمی‌شود؛ نتیجه مبهم خودکار تکرار نمی‌شود.
زمان‌بندی خودکار backup و گردش خودکار کلید ارائه نشده‌اند. پیش از استفاده واقعی،
[آزمون پذیرش](docs/TEST_STAGE_10.md) و بازیابی روی میزبان مقصد را انجام دهید.

## انتشار مخزن

نام پیشنهادی GitHub/GitLab: **`ghorbox`**. `.gitignore` تنظیمات، کلیدها، داده‌ها،
backup، Log و محیط اجرا را کنار می‌گذارد؛ `.dockerignore` فقط ورودی‌های ساخت را
می‌پذیرد. فقط `.env.example` با مقادیر خالی منتشر شود. پیش از commit، خروجی
`git status --short` و `git diff --cached` را بررسی کنید؛ فایل خصوصی را force-add نکنید.
نشانی remote باید متعلق به مخزنی باشد که خودتان ساخته‌اید.

## مستندات

- [راهنمای استفاده](docs/USER_GUIDE.md)
- [استقرار، انتقال و بازیابی](docs/DEPLOYMENT.md)
- [معماری، مجوزها و مدل تهدید](docs/ARCHITECTURE.md)
- [ساختار پایگاه‌های داده](docs/DATABASE_SCHEMA.md)
- [ذخیره‌سازی و کلیدها](docs/STORAGE.md)
- [چک‌لیست پذیرش](docs/TEST_STAGE_10.md)

رابط ربات فارسی است؛ README انگلیسی برای معرفی و راه‌اندازی در دسترس است.
