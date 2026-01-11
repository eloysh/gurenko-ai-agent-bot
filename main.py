import os
import re
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import FastAPI, Request
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from db import (
    init_db,
    upsert_user,
    get_user,
    is_vip as db_is_vip,
    set_vip,
    add_credits,
    add_prompt,
    list_prompts,
    count_prompts,
    get_prompt,
    toggle_favorite,
    list_favorites,
    add_referral,
    has_referral,
    toggle_notify,
    list_users_for_broadcast,
)

# OpenAI optional (чтобы бот не падал без ключа)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

UTC = timezone.utc
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gurenko-bot")

# -------------------- ENV --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL_TEXT = os.getenv("OPENAI_MODEL_TEXT", "gpt-4o-mini").strip()

WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()  # например: @gurenko_kristina_ai или -100123...
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "").strip() or (
    f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}" if REQUIRED_CHANNEL else ""
)

INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "https://instagram.com/").strip()
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))

DB_PATH = os.getenv("DB_PATH", "/var/data/bot.db").strip()

VIP_PRICE_STARS = int(os.getenv("VIP_PRICE_STARS", "299"))  # Stars
VIP_DAYS = int(os.getenv("VIP_DAYS", "30"))
VIP_BONUS_CREDITS = int(os.getenv("VIP_BONUS_CREDITS", "30"))

REF_BONUS_REFERRER = int(os.getenv("REF_BONUS_REFERRER", "15"))
REF_BONUS_NEW = int(os.getenv("REF_BONUS_NEW", "10"))

AUTO_IMPORT_FROM_CHANNEL = os.getenv("AUTO_IMPORT_FROM_CHANNEL", "true").lower() == "true"
AUTO_BROADCAST_NEW_PROMPTS = os.getenv("AUTO_BROADCAST_NEW_PROMPTS", "false").lower() == "true"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty. Set it in Render environment variables.")

client = None
if OpenAI and OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)

CATEGORIES = [
    ("sora", "🎬 Sora"),
    ("heygen", "🗣️ HeyGen"),
    ("meta", "🧠 Meta AI"),
    ("reels", "🚀 Reels Hooks"),
]

SYSTEM_PROMPT = (
    "Ты — помощник Кристины. Пиши по-русски, очень практично.\n"
    "Задача: выдавать готовые промпты и пошаговые инструкции для Sora/HeyGen/Meta AI/Reels.\n"
    "Формат:\n"
    "1) Короткий контекст (1-2 строки)\n"
    "2) PROMPT (в одном блоке)\n"
    "3) SETTINGS (если уместно: длительность/кадры/камера/свет/стиль)\n"
    "4) 3 варианта хуков/CTA для Reels\n"
    "Не выдумывай доступ к Instagram API. Не проси лишнего."
)

# -------------------- Helpers --------------------
def _main_menu(is_vip: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📌 База промптов", callback_data="menu:library")],
        [
            InlineKeyboardButton("🎬 Sora", callback_data="cat:sora"),
            InlineKeyboardButton("🗣️ HeyGen", callback_data="cat:heygen"),
        ],
        [
            InlineKeyboardButton("🧠 Meta AI", callback_data="cat:meta"),
            InlineKeyboardButton("🚀 Reels hooks", callback_data="cat:reels"),
        ],
        [
            InlineKeyboardButton("⭐ VIP" + (" ✅" if is_vip else ""), callback_data="menu:vip"),
            InlineKeyboardButton("🎁 Реферал", callback_data="menu:ref"),
        ],
        [
            InlineKeyboardButton("📸 Instagram", url=INSTAGRAM_URL),
            InlineKeyboardButton("🔔 Уведомления", callback_data="menu:notify"),
        ],
        [InlineKeyboardButton("🆘 Помощь", callback_data="menu:help")],
    ]
    return InlineKeyboardMarkup(rows)


def _subscribe_kb() -> InlineKeyboardMarkup:
    rows = []
    if REQUIRED_CHANNEL_URL:
        rows.append([InlineKeyboardButton("✅ Подписаться на канал", url=REQUIRED_CHANNEL_URL)])
    rows.append([InlineKeyboardButton("🔄 Я подписался — проверить", callback_data="check_sub")])
    if INSTAGRAM_URL:
        rows.append([InlineKeyboardButton("📸 Мой Instagram", url=INSTAGRAM_URL)])
    return InlineKeyboardMarkup(rows)


async def _is_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True  # если канал не задан — не блокируем
    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
            ChatMemberStatus.CREATOR,
        )
    except Exception as e:
        log.warning("get_chat_member failed: %s", e)
        return False


async def ensure_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True = можно продолжать, False = показали экран подписки и стоп."""
    user = update.effective_user
    if not user:
        return False

    ok = await _is_subscribed(update, context, user.id)
    if ok:
        return True

    text = (
        "🔒 Доступ к промптам открывается после подписки на мой Telegram-канал.\n\n"
        "1) Нажми «Подписаться»\n"
        "2) Вернись и нажми «Я подписался — проверить»"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=_subscribe_kb())
    else:
        await update.effective_chat.send_message(text, reply_markup=_subscribe_kb())
    return False


def _parse_ref(start_text: str) -> Optional[int]:
    # /start ref_123456
    m = re.search(r"ref_(\d+)", start_text or "")
    return int(m.group(1)) if m else None


def _category_menu(cat_key: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✨ Сгенерировать промпт", callback_data=f"gen:{cat_key}")],
        [InlineKeyboardButton("📚 Показать базу", callback_data=f"list:{cat_key}:0")],
        [InlineKeyboardButton("⭐ Избранное", callback_data="fav:0")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
    ]
    return InlineKeyboardMarkup(rows)


def _short(text: str, n: int = 350) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


async def _openai_generate(category: str, topic: str) -> str:
    # Если ключа нет / неверный — вернём шаблон, чтобы бот НЕ ПАДАЛ
    if not client:
        return _fallback_prompt(category, topic, reason="(OpenAI отключён: нет ключа)")

    user_input = (
        f"Категория: {category}\n"
        f"Запрос пользователя: {topic}\n\n"
        "Сделай результат максимально прикладным и копируемым."
    )
    try:
        resp = client.responses.create(
            model=OPENAI_MODEL_TEXT,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_input},
            ],
        )
        # в новых SDK есть удобное поле output_text
        out = getattr(resp, "output_text", None)
        if out:
            return out.strip()
        # fallback: попробуем собрать руками
        try:
            parts = []
            for item in resp.output:
                for c in item.content:
                    if c.type == "output_text":
                        parts.append(c.text)
            return ("\n".join(parts)).strip() or _fallback_prompt(category, topic, reason="(пустой ответ)")
        except Exception:
            return _fallback_prompt(category, topic, reason="(не смог распарсить ответ)")
    except Exception as e:
        log.warning("OpenAI call failed: %s", e)
        return _fallback_prompt(category, topic, reason="(ошибка OpenAI — проверь ключ/доступ)")


def _fallback_prompt(category: str, topic: str, reason: str = "") -> str:
    return (
        f"⚠️ Автогенерация временно недоступна {reason}\n\n"
        f"PROMPT:\n"
        f"Сделай {category.upper()}-контент по теме: «{topic}».\n"
        f"Стиль: ультра-реализм, кино-свет, чистая кожа с текстурой, 8K, натуральные эмоции.\n"
        f"Камера: 35mm, shallow depth of field, мягкий боковой свет.\n"
        f"Требования: не менять лицо/возраст, без искажения пропорций.\n\n"
        f"SETTINGS:\n"
        f"- вертикаль 9:16\n- 5–7 секунд (если видео)\n- лёгкое движение камеры (dolly-in)\n\n"
        f"HOOKS:\n"
        f"1) «Хочешь так же? Напиши слово PROMPT»\n"
        f"2) «Сохрани, чтобы повторить за 1 минуту»\n"
        f"3) «Ссылка на гайд — в Telegram»"
    )


# -------------------- Bot Handlers --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    upsert_user(DB_PATH, user.id, user.username, user.first_name)

    # referral
    ref = _parse_ref(update.message.text if update.message else "")
    if ref and ref != user.id and not has_referral(DB_PATH, user.id):
        add_referral(DB_PATH, referrer_id=ref, referred_id=user.id)
        add_credits(DB_PATH, ref, REF_BONUS_REFERRER)
        add_credits(DB_PATH, user.id, REF_BONUS_NEW)

    # gate
    if not await ensure_access(update, context):
        return

    vip = db_is_vip(DB_PATH, user.id)
    u = get_user(DB_PATH, user.id) or {}
    credits = int(u.get("credits") or 0)

    text = (
        "Привет! Я бот Кристины 👋\n"
        "Здесь: Sora / HeyGen / Meta AI / Reels hooks + база промптов.\n\n"
        f"⭐ VIP: {'да' if vip else 'нет'} | 💎 credits: {credits}"
    )
    await update.effective_chat.send_message(text, reply_markup=_main_menu(vip))


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    await update.effective_chat.send_message(f"Твой Telegram user_id: `{user.id}`", parse_mode="Markdown")


async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    # gate for ALL callbacks except check_sub
    if q.data != "check_sub":
        ok = await ensure_access(update, context)
        if not ok:
            return

    user = update.effective_user
    if not user:
        return

    upsert_user(DB_PATH, user.id, user.username, user.first_name)
    vip = db_is_vip(DB_PATH, user.id)

    data = q.data or ""

    if data == "check_sub":
        ok = await _is_subscribed(update, context, user.id)
        if not ok:
            await q.edit_message_text(
                "Пока не вижу подписку 😕\nПроверь, что ты подписался и попробуй ещё раз.",
                reply_markup=_subscribe_kb(),
            )
            return
        await q.edit_message_text("✅ Подписка подтверждена! Добро пожаловать 🎉")
        await q.message.reply_text("Выбирай раздел:", reply_markup=_main_menu(vip))
        return

    if data == "menu:home":
        await q.edit_message_text("Меню:", reply_markup=_main_menu(vip))
        return

    if data == "menu:library":
        await q.edit_message_text(
            "📌 База промптов — выбери категорию:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(name, callback_data=f"list:{key}:0")] for key, name in CATEGORIES]
                + [[InlineKeyboardButton("⭐ Избранное", callback_data="fav:0")],
                   [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")]]
            ),
        )
        return

    if data.startswith("cat:"):
        cat = data.split(":", 1)[1]
        title = dict(CATEGORIES).get(cat, cat)
        await q.edit_message_text(f"{title}\nВыбери действие:", reply_markup=_category_menu(cat))
        return

    if data.startswith("gen:"):
        cat = data.split(":", 1)[1]
        context.user_data["awaiting_topic"] = True
        context.user_data["gen_category"] = cat
        await q.edit_message_text(
            "Напиши тему/идею (например: «зимняя fashion-съёмка со снежными ресницами»).\n"
            "Я сделаю копируемый PROMPT + настройки + хуки.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_gen")]]),
        )
        return

    if data == "cancel_gen":
        context.user_data.pop("awaiting_topic", None)
        context.user_data.pop("gen_category", None)
        await q.edit_message_text("Ок, отменено.", reply_markup=_main_menu(vip))
        return

    if data.startswith("list:"):
        _, cat, offset_s = data.split(":")
        offset = int(offset_s)
        total = count_prompts(DB_PATH, cat)
        items = list_prompts(DB_PATH, cat, offset=offset, limit=5)

        if not items:
            await q.edit_message_text(
                "Пока пусто. Я могу сгенерировать промпт по твоей теме 👇",
                reply_markup=_category_menu(cat),
            )
            return

        lines = [f"📚 {dict(CATEGORIES).get(cat, cat)} — всего: {total}\n"]
        for p in items:
            lines.append(f"#{p['id']} — *{p['title']}*\n{_short(p['body'])}\n")

        nav = []
        if offset > 0:
            nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"list:{cat}:{max(0, offset-5)}"))
        if offset + 5 < total:
            nav.append(InlineKeyboardButton("➡️ Далее", callback_data=f"list:{cat}:{offset+5}"))

        rows = []
        for p in items:
            rows.append([InlineKeyboardButton(f"⭐ В избранное #{p['id']}", callback_data=f"fav_toggle:{p['id']}")])
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("⬅️ Меню", callback_data="menu:home")])

        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("fav_toggle:"):
        pid = int(data.split(":", 1)[1])
        state = toggle_favorite(DB_PATH, user.id, pid)
        await q.answer("Добавлено ⭐" if state else "Убрано ❌", show_alert=False)
        return

    if data.startswith("fav:"):
        offset = int(data.split(":", 1)[1])
        items = list_favorites(DB_PATH, user.id, offset=offset, limit=5)

        if not items:
            await q.edit_message_text("⭐ Избранное пусто.", reply_markup=_main_menu(vip))
            return

        lines = ["⭐ Избранное:\n"]
        for p in items:
            lines.append(f"#{p['id']} — *{p['title']}*\n{_short(p['body'])}\n")

        rows = []
        if offset > 0:
            rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"fav:{max(0, offset-5)}")])
        if len(items) == 5:
            rows.append([InlineKeyboardButton("➡️ Далее", callback_data=f"fav:{offset+5}")])
        rows.append([InlineKeyboardButton("⬅️ Меню", callback_data="menu:home")])

        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "menu:vip":
        u = get_user(DB_PATH, user.id) or {}
        vip_until = u.get("vip_until") or "-"
        text = (
            "⭐ VIP доступ:\n"
            "— больше промптов, приоритет, бонус-кредиты\n\n"
            f"Твой VIP до: {vip_until}\n\n"
            f"Купить VIP на {VIP_DAYS} дней: {VIP_PRICE_STARS} ⭐"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Купить VIP за {VIP_PRICE_STARS}⭐", callback_data="buy_vip")],
            [InlineKeyboardButton("⬅️ Меню", callback_data="menu:home")],
        ])
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data == "buy_vip":
        # Telegram Stars invoice (currency XTR). Provider token обычно пустой для Stars.
        prices = [LabeledPrice(label=f"VIP {VIP_DAYS} дней", amount=VIP_PRICE_STARS)]
        await context.bot.send_invoice(
            chat_id=user.id,
            title="VIP доступ",
            description=f"VIP на {VIP_DAYS} дней + бонус {VIP_BONUS_CREDITS} credits",
            payload=f"vip_{VIP_DAYS}d",
            provider_token="",  # Stars
            currency="XTR",
            prices=prices,
        )
        await q.edit_message_text("Счёт отправила в личку ✅", reply_markup=_main_menu(vip))
        return

    if data == "menu:ref":
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=ref_{user.id}"
        text = (
            "🎁 Реферальная ссылка:\n"
            f"{link}\n\n"
            f"За друга: тебе +{REF_BONUS_REFERRER} credits, другу +{REF_BONUS_NEW} credits."
        )
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="menu:home")]]))
        return

    if data == "menu:notify":
        enabled = toggle_notify(DB_PATH, user.id)
        await q.edit_message_text(
            f"🔔 Уведомления о новых промптах: {'ВКЛ ✅' if enabled else 'ВЫКЛ ❌'}",
            reply_markup=_main_menu(vip),
        )
        return

    if data == "menu:help":
        await q.edit_message_text(
            "🆘 Помощь:\n"
            "— Нужен промпт → выбери раздел и нажми «Сгенерировать промпт»\n"
            "— Не пускает → проверь подписку на канал\n"
            "— /id покажет твой user_id\n",
            reply_markup=_main_menu(vip),
        )
        return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_access(update, context):
        return

    user = update.effective_user
    if not user or not update.message or not update.message.text:
        return

    if context.user_data.get("awaiting_topic"):
        cat = context.user_data.get("gen_category", "sora")
        topic = update.message.text.strip()

        context.user_data["awaiting_topic"] = False
        context.user_data.pop("gen_category", None)

        result = await _openai_generate(cat, topic)
        await update.message.reply_text(result)
        await update.message.reply_text("Меню:", reply_markup=_main_menu(db_is_vip(DB_PATH, user.id)))


# -------------------- Payments --------------------
async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    if not q:
        return
    await q.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user = update.effective_user
    if not msg or not user:
        return

    # VIP activation
    vip_until = (datetime.now(UTC) + timedelta(days=VIP_DAYS)).isoformat()
    set_vip(DB_PATH, user.id, vip_until)
    add_credits(DB_PATH, user.id, VIP_BONUS_CREDITS)

    await msg.reply_text(
        f"✅ Оплата получена!\nVIP активирован до: {vip_until}\n+{VIP_BONUS_CREDITS} credits"
    )
    await msg.reply_text("Меню:", reply_markup=_main_menu(True))


# -------------------- Auto import from channel --------------------
def _guess_category(text: str) -> str:
    t = (text or "").lower()
    if "#heygen" in t or "heygen" in t:
        return "heygen"
    if "#meta" in t or "meta ai" in t:
        return "meta"
    if "#reels" in t or "reels" in t or "хуки" in t:
        return "reels"
    return "sora"


def _extract_title_body(text: str) -> Tuple[str, str]:
    t = (text or "").strip()
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    title = lines[0][:120] if lines else "Prompt"
    body = t
    return title, body


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not AUTO_IMPORT_FROM_CHANNEL:
        return
    msg = update.effective_message
    if not msg or not msg.text:
        return
    # импортируем только из того же канала, что и REQUIRED_CHANNEL (если задан)
    if REQUIRED_CHANNEL and str(msg.chat_id) != str(REQUIRED_CHANNEL) and msg.chat.username != REQUIRED_CHANNEL.lstrip("@"):
        return

    cat = _guess_category(msg.text)
    title, body = _extract_title_body(msg.text)
    pid = add_prompt(DB_PATH, cat, title, body, source=f"channel:{msg.chat_id}:{msg.message_id}")
    log.info("Imported prompt #%s from channel", pid)

    if AUTO_BROADCAST_NEW_PROMPTS:
        users = list_users_for_broadcast(DB_PATH)
        for uid in users[:5000]:
            try:
                await context.bot.send_message(uid, f"🆕 Новый промпт ({dict(CATEGORIES).get(cat, cat)}): *{title}*\n\n{_short(body, 800)}", parse_mode="Markdown")
            except Exception:
                pass


# -------------------- FastAPI Webhook --------------------
app = FastAPI()
telegram_app: Application


@app.get("/")
async def root():
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.update_queue.put(update)
    return {"ok": True}


@app.on_event("startup")
async def on_startup():
    global telegram_app
    init_db(DB_PATH)

    telegram_app = Application.builder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("id", cmd_id))

    telegram_app.add_handler(CallbackQueryHandler(cb_router))
    telegram_app.add_handler(PreCheckoutQueryHandler(precheckout))
    telegram_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # channel import
    telegram_app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, on_channel_post))

    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await telegram_app.initialize()
    await telegram_app.start()

    if WEBHOOK_BASE_URL:
        webhook_url = f"{WEBHOOK_BASE_URL}/webhook"
        await telegram_app.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        log.info("Webhook set to %s", webhook_url)
    else:
        log.warning("WEBHOOK_BASE_URL is empty — webhook not set.")


@app.on_event("shutdown")
async def on_shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()
