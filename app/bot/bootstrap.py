"""Private membership and administrator workflows with fresh DB authorization."""

import secrets

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.bot.navigation import Navigation
from app.core.logging import Event, log_event
from app.core.permissions import MANAGE_ADMINS, MANAGE_USERS, AccessDenied, InputError, Role
from app.services.identity_service import IdentityService, valid_id, valid_name

ACCESS_DENIED = "دسترسی شما به این بخش فعال نیست. با مدیر مجموعه تماس بگیرید."
READY = "✅ ربات بازخورد آماده است. نسخه ۱.۰ — مرحله ۱۰ از ۱۰، آماده آزمون نهایی."
HELP = (
    "این سامانه برای بازخورد ناشناس همکاران آماده می‌شود.\n"
    "هویت نویسنده نمایش داده نخواهد شد؛ متن و شرایط گفتگو می‌تواند هویت را قابل حدس کند.\n"
    "برای ارسال از «همکاران داخل گروه» شروع کنید. برای لغو /cancel و برای منو /start را بفرستید."
)


def create_bootstrap_router(service: IdentityService, feedback=None, external=None) -> Router:
    router = Router(name="identity")
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")
    nav = Navigation()

    def keyboard(actor, rows):
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=label, callback_data=nav.issue(actor, action, payload)
                    )
                    for label, action, payload in row
                ]
                for row in rows
            ]
        )

    async def home(message, actor_id):
        actor = await service.authenticate(actor_id)
        rows = [[("👤 پروفایل من", "profile", None), ("ℹ️ راهنما", "help", None)]]
        if actor.admin_id:
            rows.append([("⚙️ پنل مدیریت", "panel", None)])
        markup = keyboard(actor_id, rows)
        if feedback is not None:
            from app.bot.feedback import feedback_home_button

            markup.inline_keyboard.insert(0, [await feedback_home_button(feedback, actor_id)])
        if external is not None:
            markup.inline_keyboard.insert(
                1,
                [
                    InlineKeyboardButton(
                        text="🌐 افراد خارج از گروه",
                        callback_data=await external.issue(actor_id, "menu"),
                    )
                ],
            )
        if feedback is not None:
            markup.inline_keyboard.append(
                [
                    InlineKeyboardButton(
                        text="💬 گفتگوهای ناشناس",
                        callback_data=await feedback.issue(actor_id, "chat_list", 0),
                    )
                ]
            )
        if external is not None:
            markup.inline_keyboard.append(
                [
                    InlineKeyboardButton(
                        text="📚 گفتگوهای خارجی من",
                        callback_data=await external.issue(actor_id, "history_menu"),
                    )
                ]
            )
        await message.answer(READY, reply_markup=markup)

    async def notice(message, actor_id, text):
        await message.answer(
            text, reply_markup=keyboard(actor_id, [[("🏠 خانه / لغو", "home", None)]])
        )

    async def users_page(message, actor_id, page=0, search=""):
        users = await service.list_users(actor_id, page, search)
        rows = [
            [(("🟢 " if user["is_active"] else "⏸ ") + user["display_name"], "user", user["id"])]
            for user in users[:10]
        ]
        pages = []
        if page:
            pages.append(("قبلی", "users", [page - 1, search]))
        if len(users) > 10:
            pages.append(("بعدی", "users", [page + 1, search]))
        if pages:
            rows.append(pages)
        rows += [
            [("➕ افزودن کاربر", "add_user", None), ("🔎 جستجو", "search", None)],
            [("🏠 خانه", "home", None)],
        ]
        await message.answer(
            "👥 کاربران" if users else "کاربری یافت نشد.", reply_markup=keyboard(actor_id, rows)
        )

    async def groups_page(message, actor_id):
        actor = await service.authenticate(actor_id)
        groups = await service.list_internal_groups(actor_id)
        rows = [
            [
                (
                    f"{item['name']} — {item['member_count']} عضو",
                    "group",
                    [item["id"], 0],
                )
            ]
            for item in groups
        ]
        if actor.allows(*MANAGE_ADMINS):
            rows.append([("➕ ساخت گروه داخلی", "add_group", None)])
        rows.append([("بازگشت به مدیریت", "panel", None)])
        await message.answer(
            "🏢 گروه‌های داخلی" if groups else "هنوز گروه داخلی ثبت نشده است.",
            reply_markup=keyboard(actor_id, rows),
        )

    async def group_page(message, actor_id, group_id, page=0):
        groups = await service.list_internal_groups(actor_id)
        group = next((item for item in groups if item["id"] == group_id), None)
        if group is None:
            raise AccessDenied()
        members = await service.list_internal_group_members(actor_id, group_id)
        rows = [
            [
                (
                    ("🟢 " if item["is_active"] else "⏸ ") + item["display_name"],
                    "group_member",
                    [group_id, item["id"], item["display_name"]],
                )
            ]
            for item in members[page * 10 : page * 10 + 10]
        ]
        pages = []
        if page:
            pages.append(("قبلی", "group", [group_id, page - 1]))
        if len(members) > page * 10 + 10:
            pages.append(("بعدی", "group", [group_id, page + 1]))
        if pages:
            rows.append(pages)
        rows.append([("➕ افزودن عضو موجود", "group_candidates", [group_id, 0])])
        actor = await service.authenticate(actor_id)
        if actor.allows(*MANAGE_ADMINS):
            rows.append([("👤 تغییر مدیر گروه", "group_managers", [group_id, None, 0])])
        rows.append([("بازگشت به گروه‌ها", "groups", None)])
        await message.answer(
            f"🏢 {group['name']}\nمدیر: {group['manager_name']}\n"
            f"تعداد اعضا: {group['member_count']}",
            reply_markup=keyboard(actor_id, rows),
        )

    async def group_candidates_page(message, actor_id, group_id, page=0):
        candidates = await service.list_internal_group_candidates(actor_id, group_id)
        rows = [
            [
                (
                    item["display_name"]
                    + (
                        f" — انتقال از {item['current_group_name']}"
                        if item["current_group_name"]
                        else ""
                    ),
                    "ask_group_assign",
                    [group_id, item["id"], item["display_name"]],
                )
            ]
            for item in candidates[page * 10 : page * 10 + 10]
        ]
        pages = []
        if page:
            pages.append(("قبلی", "group_candidates", [group_id, page - 1]))
        if len(candidates) > page * 10 + 10:
            pages.append(("بعدی", "group_candidates", [group_id, page + 1]))
        if pages:
            rows.append(pages)
        rows.append([("بازگشت", "group", [group_id, 0])])
        await message.answer(
            "کاربر عادی را انتخاب کنید:" if candidates else "کاربر قابل افزودنی وجود ندارد.",
            reply_markup=keyboard(actor_id, rows),
        )

    async def group_managers_page(message, actor_id, group_id, group_name, page=0):
        managers = await service.list_available_internal_managers(
            actor_id, current_group_id=group_id
        )
        rows = [
            [
                (
                    item["display_name"],
                    "ask_group_manager",
                    [group_id, group_name, item["id"], item["display_name"]],
                )
            ]
            for item in managers[page * 10 : page * 10 + 10]
        ]
        pages = []
        if page:
            pages.append(("قبلی", "group_managers", [group_id, group_name, page - 1]))
        if len(managers) > page * 10 + 10:
            pages.append(("بعدی", "group_managers", [group_id, group_name, page + 1]))
        if pages:
            rows.append(pages)
        rows.append(
            [
                (
                    "لغو",
                    "groups" if group_id is None else "group",
                    None if group_id is None else [group_id, 0],
                )
            ]
        )
        await message.answer(
            "مدیر داخلی گروه را انتخاب کنید:" if managers else "مدیر داخلیِ بدون گروه وجود ندارد.",
            reply_markup=keyboard(actor_id, rows),
        )

    async def confirm(message, actor_id, description, action, payload):
        await message.answer(
            description + "\n\nآیا تأیید می‌کنید؟",
            reply_markup=keyboard(
                actor_id, [[("✅ تأیید", action, payload), ("❌ لغو", "home", None)]]
            ),
        )

    async def execute(message, actor_id, action, payload):
        actor = await service.authenticate(actor_id)
        if action == "home":
            nav.cancel(actor_id)
            await home(message, actor_id)
        elif action == "help":
            await notice(message, actor_id, HELP)
        elif action == "profile":
            from app.services.conversation_service import ConversationService

            text = f"👤 پروفایل من\nنام: {actor.display_name}\nوضعیت: فعال"
            for domain, label in [(feedback, "داخلی"), (external, "خارجی")]:
                if domain is not None:
                    counts = await ConversationService(domain).profile(actor_id)
                    text += (
                        f"\n\n{label} — بازخوردهای اولیه شما:"
                        f"\nجمع ارسالی: {counts['sent_criticism'] + counts['sent_suggestion']}"
                        " | جمع دریافتی: "
                        f"{counts['received_criticism'] + counts['received_suggestion']}"
                        f"\n📤 انتقاد ارسالی: {counts['sent_criticism']}"
                        f" | پیشنهاد ارسالی: {counts['sent_suggestion']}"
                        f"\n📥 انتقاد دریافتی: {counts['received_criticism']}"
                        f" | پیشنهاد دریافتی: {counts['received_suggestion']}"
                        f"\nپاسخ‌های شما: {counts['sent_replies']}"
                        f" | پاسخ‌های دریافت‌شده: {counts['received_replies']}"
                    )
            await notice(
                message, actor_id, text + "\nپاسخ‌ها در شمارش بازخورد اولیه محاسبه نمی‌شوند."
            )
        elif action == "panel":
            rows = []
            if actor.allows(*MANAGE_USERS):
                rows.append([("👥 کاربران", "users", [0, ""])])
                rows.append([("🏢 گروه‌های داخلی", "groups", None)])
            if actor.allows(*MANAGE_ADMINS):
                rows.append([("👮 مدیران", "admins", 0)])
            if not actor.admin_id:
                raise AccessDenied()
            scopes = [
                label
                for scope, label in [("internal", "داخلی"), ("external", "خارجی")]
                if actor.allows(scope, "read_feedback")
            ]
            rows.append([("🏠 خانه", "home", None)])
            markup = keyboard(actor_id, rows)
            for domain, label in [(feedback, "داخلی"), (external, "خارجی")]:
                if domain is not None and actor.allows(domain.scope.value, "read_feedback"):
                    inbox_label = "📥 بازخوردهای اولیه " + label
                    if domain is feedback:
                        inbox_label = (
                            "📥 همه بازخوردهای اولیه داخلی"
                            if actor.allows(*MANAGE_ADMINS)
                            else "📥 بازخوردهای گروه من"
                        )
                    markup.inline_keyboard.insert(
                        0,
                        [
                            InlineKeyboardButton(
                                text=inbox_label,
                                callback_data=await domain.issue(actor_id, "mod_menu"),
                            )
                        ],
                    )
            for domain, label in [(feedback, "داخلی"), (external, "خارجی")]:
                if domain is not None and actor.allows(domain.scope.value, "view_statistics"):
                    markup.inline_keyboard.insert(
                        0,
                        [
                            InlineKeyboardButton(
                                text="📊 آمار هفتگی " + label,
                                callback_data=await domain.issue(actor_id, "stats_archive", 0),
                            )
                        ],
                    )
            if external is not None and actor.allows("external", "read_feedback"):
                markup.inline_keyboard.insert(
                    0,
                    [
                        InlineKeyboardButton(
                            text="🌐 مدیریت بخش خارجی",
                            callback_data=await external.issue(actor_id, "menu"),
                        )
                    ],
                )
            await message.answer(
                "⚙️ پنل مدیریت\nحوزه مجاز: "
                + "، ".join(scopes)
                + "\nامکانات تکمیلی مدیریت در مراحل بعد فعال می‌شود.",
                reply_markup=markup,
            )
        elif action == "users":
            await users_page(message, actor_id, *payload)
        elif action == "groups":
            await groups_page(message, actor_id)
        elif action == "group":
            await group_page(message, actor_id, *payload)
        elif action == "add_group":
            actor.require(*MANAGE_ADMINS)
            nav.set_form(actor_id, "group_name")
            await notice(message, actor_id, "نام گروه داخلی را بنویسید. برای لغو /cancel")
        elif action == "group_managers":
            await group_managers_page(message, actor_id, *payload)
        elif action == "ask_group_manager":
            group_id, group_name, manager_user_id, manager_name = payload
            description = (
                f"ساخت گروه «{group_name}» با مدیریت {manager_name}"
                if group_id is None
                else f"تغییر مدیر گروه به {manager_name}"
            )
            await confirm(
                message,
                actor_id,
                description,
                "save_group_create" if group_id is None else "save_group_manager",
                [group_id, group_name, manager_user_id],
            )
        elif action == "save_group_create":
            group_id = await service.create_internal_group(actor_id, payload[1], payload[2])
            await group_page(message, actor_id, group_id)
        elif action == "save_group_manager":
            await service.set_internal_group_manager(actor_id, payload[0], payload[2])
            await group_page(message, actor_id, payload[0])
        elif action == "group_candidates":
            await group_candidates_page(message, actor_id, *payload)
        elif action == "ask_group_assign":
            await confirm(
                message,
                actor_id,
                f"افزودن یا انتقال {payload[2]} به این گروه",
                "save_group_assign",
                payload,
            )
        elif action == "save_group_assign":
            await service.assign_internal_group_member(actor_id, payload[0], payload[1])
            await group_page(message, actor_id, payload[0])
        elif action == "group_member":
            await confirm(
                message,
                actor_id,
                f"حذف {payload[2]} از این گروه؛ حساب کاربر حذف نمی‌شود.",
                "save_group_remove",
                payload,
            )
        elif action == "save_group_remove":
            await service.remove_internal_group_member(actor_id, payload[0], payload[1])
            await group_page(message, actor_id, payload[0])
        elif action == "user":
            user = await service.get_user(actor_id, payload)
            active = bool(user["is_active"])
            await message.answer(
                f"نام: {user['display_name']}\nشناسه تلگرام: {user['telegram_id']}\n"
                f"وضعیت: {'فعال' if active else 'غیرفعال'}",
                reply_markup=keyboard(
                    actor_id,
                    [
                        [
                            (
                                "⏸ غیرفعال / حذف نرم" if active else "▶️ فعال کردن",
                                "ask_active",
                                [payload, not active],
                            )
                        ],
                        [("✏️ اصلاح نام", "rename", payload)],
                        [("بازگشت", "users", [0, ""])],
                    ],
                ),
            )
        elif action == "ask_active":
            user = await service.get_user(actor_id, payload[0])
            await confirm(
                message,
                actor_id,
                f"{'فعال‌سازی' if payload[1] else 'غیرفعال‌سازی'} {user['display_name']}",
                "save_active",
                payload,
            )
        elif action == "save_active":
            await service.update_user(actor_id, payload[0], active=payload[1])
            await notice(message, actor_id, "وضعیت کاربر ذخیره شد.")
        elif action in {"add_user", "add_admin"}:
            actor.require(*(MANAGE_USERS if action == "add_user" else MANAGE_ADMINS))
            multiple = action == "add_user"
            request_id = secrets.randbelow(2**31 - 1) + 1
            nav.set_form(
                actor_id,
                "pick_user" if action == "add_user" else "pick_admin",
                {"request_id": request_id},
            )
            await message.answer(
                (
                    "از دکمه زیر، یک تا ده کاربر را در تلگرام انتخاب کنید."
                    if multiple
                    else "از دکمه زیر، شخص موردنظر را در تلگرام انتخاب کنید."
                )
                + "\nبرای لغو /cancel",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[
                        [
                            KeyboardButton(
                                text=(
                                    "👥 انتخاب کاربران تلگرام"
                                    if multiple
                                    else "👤 انتخاب کاربر تلگرام"
                                ),
                                request_users=KeyboardButtonRequestUsers(
                                    request_id=request_id,
                                    user_is_bot=False,
                                    max_quantity=10 if multiple else 1,
                                    request_name=True,
                                    request_username=False,
                                    request_photo=False,
                                ),
                            )
                        ],
                        [KeyboardButton(text="/cancel")],
                    ],
                    resize_keyboard=True,
                    one_time_keyboard=True,
                ),
            )
        elif action in {"search", "rename"}:
            actor.require(*MANAGE_USERS)
            if action == "rename":
                await service.get_user(actor_id, payload)
            nav.set_form(actor_id, action, payload)
            prompt = (
                "نام یا شناسه را برای جستجو بنویسید." if action == "search" else "نام را بنویسید."
            )
            await notice(message, actor_id, prompt + "\nبرای لغو /cancel")
        elif action == "save_user":
            await service.add_user(actor_id, payload[0], payload[1])
            await notice(message, actor_id, "✅ کاربر ثبت شد. اکنون می‌تواند ربات را Start کند.")
        elif action == "save_users":
            user_ids = await service.add_users(actor_id, payload)
            await notice(
                message,
                actor_id,
                f"✅ {len(user_ids)} کاربر ثبت شدند. اکنون می‌توانند ربات را Start کنند.",
            )
        elif action == "save_name":
            await service.update_user(actor_id, payload[0], name=payload[1])
            await notice(message, actor_id, "نام ذخیره شد.")
        elif action == "admins":
            admins = await service.list_admins(actor_id)
            page = payload
            rows = [
                [
                    (
                        item["display_name"] + (" ✅" if item["is_active"] else " ⏸"),
                        "admin",
                        item["id"],
                    )
                ]
                for item in admins[page * 10 : page * 10 + 10]
            ]
            if page:
                rows.append([("قبلی", "admins", page - 1)])
            if len(admins) > page * 10 + 10:
                rows.append([("بعدی", "admins", page + 1)])
            rows += [[("➕ افزودن / تغییر نقش مدیر", "add_admin", None)], [("خانه", "home", None)]]
            await message.answer("👮 مدیران", reply_markup=keyboard(actor_id, rows))
        elif action in {"admin", "ask_revoke"}:
            admins = await service.list_admins(actor_id)
            item = next((item for item in admins if item["id"] == payload), None)
            if not item:
                raise AccessDenied()
            if action == "ask_revoke":
                await confirm(
                    message,
                    actor_id,
                    f"لغو مدیریت {item['display_name']}؛ عضویت عادی باقی می‌ماند.",
                    "revoke",
                    payload,
                )
                return
            scopes = sorted({s for s, p in item["grants"] if p == "read_feedback"})
            scope_names = ", ".join({"internal": "داخلی", "external": "خارجی"}[s] for s in scopes)
            await message.answer(
                f"نام: {item['display_name']}\nشناسه: {item['telegram_id']}\n"
                f"حوزه: {scope_names}\n"
                f"مدیریت مدیران: {'بله' if MANAGE_ADMINS in item['grants'] else 'خیر'}",
                reply_markup=keyboard(
                    actor_id,
                    [[("❌ لغو مدیریت", "ask_revoke", payload)], [("بازگشت", "admins", 0)]],
                ),
            )
        elif action == "revoke":
            await service.revoke_admin(actor_id, payload)
            await notice(message, actor_id, "دسترسی مدیریتی لغو شد.")
        elif action == "role":
            actor.require(*MANAGE_ADMINS)
            role_name = {"internal": "مدیر داخلی", "full": "مدیر اصلی"}[payload[1]]
            await confirm(
                message,
                actor_id,
                f"تنظیم مدیریت: {payload[3]}\nنقش: {role_name}\n"
                f"مدیریت مدیران: {'بله' if payload[1] == 'full' else 'خیر'}\n"
                "مجوزهای قبلی جایگزین می‌شوند.",
                "save_admin",
                payload,
            )
        elif action == "save_admin":
            await service.set_admin(
                actor_id, payload[0], Role(payload[1]), can_manage_admins=payload[2]
            )
            await notice(message, actor_id, "مجوزهای مدیر ذخیره شدند.")
        else:
            raise InputError("دکمه معتبر نیست؛ از /start استفاده کنید.")

    async def process_text(message, actor_id):
        actor = await service.authenticate(actor_id)
        form = nav.get_form(actor_id)
        if not form:
            await message.answer("برای ارسال بازخورد ابتدا از منوی /start همکار را انتخاب کنید.")
            return
        action, payload = form
        actor.require(*(MANAGE_ADMINS if action in {"pick_admin", "group_name"} else MANAGE_USERS))
        if action in {"pick_user", "pick_admin"}:
            shared = message.users_shared
            if (
                shared is None
                or shared.request_id != payload["request_id"]
                or not shared.users
                or len(shared.users) > (10 if action == "pick_user" else 1)
            ):
                raise InputError("از دکمه انتخاب کاربر تلگرام استفاده کنید؛ برای لغو /cancel")
            selected_users = []
            for selected in shared.users:
                telegram_id = valid_id(selected.user_id)
                name = " ".join(part for part in [selected.first_name, selected.last_name] if part)
                selected_users.append([telegram_id, name])
            nav.forms.pop(actor_id, None)
            await message.answer(
                f"{len(selected_users)} کاربر انتخاب شد.", reply_markup=ReplyKeyboardRemove()
            )
            if action == "pick_admin":
                telegram_id, _ = selected_users[0]
                registered_name = await service.admin_candidate(actor_id, telegram_id)
                rows = [
                    [(label, "role", [telegram_id, role, manager, registered_name])]
                    for label, role, manager in [
                        ("مدیر داخلی", "internal", False),
                        ("مدیر اصلی", "full", True),
                    ]
                ]
                rows.append([("❌ لغو", "home", None)])
                await message.answer("نقش را انتخاب کنید:", reply_markup=keyboard(actor_id, rows))
                return
            if len(selected_users) == 1:
                telegram_id, name = selected_users[0]
                try:
                    name = valid_name(name)
                except InputError:
                    nav.set_form(actor_id, "user_name", telegram_id)
                    await notice(message, actor_id, "نام نمایشی این کاربر را بنویسید.")
                    return
                selected_users[0][1] = name
            else:
                try:
                    selected_users = [
                        [telegram_id, valid_name(name)] for telegram_id, name in selected_users
                    ]
                except InputError:
                    raise InputError(
                        "نام یکی از کاربران دریافت نشد؛ کاربران را دوباره انتخاب کنید."
                    ) from None
            names = "\n".join(f"• {name}" for _, name in selected_users)
            await confirm(
                message,
                actor_id,
                f"افزودن {len(selected_users)} کاربر:\n{names}",
                "save_users",
                selected_users,
            )
            return
        if message.text is None:
            raise InputError("فقط متن بفرستید؛ برای لغو /cancel")
        value = message.text.strip()
        if action == "group_name":
            name = valid_name(value)
            nav.forms.pop(actor_id, None)
            await group_managers_page(message, actor_id, None, name)
        elif action in {"user_name", "rename"}:
            name = valid_name(value)
            nav.forms.pop(actor_id, None)
            description = f"نام: {name}" + (
                f"\nشناسه تلگرام: {payload}" if action == "user_name" else ""
            )
            await confirm(
                message,
                actor_id,
                description,
                "save_user" if action == "user_name" else "save_name",
                [payload, name],
            )
        elif action == "search":
            if len(value) > 80:
                raise InputError("عبارت جستجو حداکثر ۸۰ نویسه است.")
            nav.forms.pop(actor_id, None)
            await users_page(message, actor_id, search=value)

    @router.message(CommandStart())
    @router.message(Command("status", "cancel"))
    async def start(message: Message):
        if message.from_user:
            await service.authenticate(message.from_user.id, started=True)
            if nav.get_form(message.from_user.id) is not None or message.text == "/cancel":
                await message.answer("عملیات لغو شد.", reply_markup=ReplyKeyboardRemove())
            if external is not None:
                await external.cancel(message.from_user.id)
            if feedback is not None:
                await feedback.cancel(message.from_user.id)
            nav.cancel(message.from_user.id)
            await home(message, message.from_user.id)

    @router.message(Command("help"))
    async def help_command(message: Message):
        if message.from_user:
            await service.authenticate(message.from_user.id)
            await message.answer(HELP)

    @router.message(Command("admin"))
    async def admin_command(message: Message):
        if message.from_user:
            if external is not None:
                await external.cancel(message.from_user.id)
            if feedback is not None:
                await feedback.cancel(message.from_user.id)
            nav.cancel(message.from_user.id)
            await execute(message, message.from_user.id, "panel", None)

    @router.callback_query(~F.data.startswith("f:") & ~F.data.startswith("e:"))
    async def callback(query: CallbackQuery):
        try:
            await service.authenticate(query.from_user.id)
            action, payload = nav.consume(query.from_user.id, query.data or "")
        except (AccessDenied, InputError) as error:
            await query.answer(
                ACCESS_DENIED if isinstance(error, AccessDenied) else str(error), show_alert=True
            )
            return
        nav.cancel(query.from_user.id)
        if external is not None:
            await external.cancel(query.from_user.id)
        if feedback is not None:
            await feedback.cancel(query.from_user.id)
        await query.answer()
        if isinstance(query.message, Message):
            await execute(query.message, query.from_user.id, action, payload)

    @router.message()
    async def fallback(message: Message):
        if message.from_user:
            if external is not None and nav.get_form(message.from_user.id) is None:
                from app.bot.external import handle_external_text

                if await handle_external_text(message, external):
                    return
            if feedback is not None and nav.get_form(message.from_user.id) is None:
                from app.bot.feedback import handle_feedback_text

                if await handle_feedback_text(message, feedback):
                    return
            await process_text(message, message.from_user.id)

    @router.errors()
    async def errors(event: ErrorEvent):
        error = event.exception
        if isinstance(error, AccessDenied):
            response = ACCESS_DENIED
        elif isinstance(error, InputError):
            response = str(error)
        else:
            log_event(Event.UPDATE_FAILED)
            response = "خطایی رخ داد؛ دوباره تلاش کنید."
        try:
            message = event.update.message
            if event.update.callback_query:
                message = event.update.callback_query.message
            if isinstance(message, Message) and message.chat.type == "private":
                await message.answer(response)
        except Exception:
            pass
        return True

    if feedback is not None:
        from app.bot.feedback import create_feedback_router

        router.include_router(create_feedback_router(feedback, nav.cancel, external, home))
    if external is not None:
        from app.bot.external import create_external_router

        router.include_router(create_external_router(external, nav.cancel, feedback, home))
    return router
