import os
import base64
import sqlite3
import logging
import time
import asyncio
from datetime import datetime, timedelta, date
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    PreCheckoutQueryHandler,
    filters,
)

# OpenAI
try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False


# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gurenko-bot")

# -------------------- ENV (with aliases) --------------------
def env(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

TELEGRAM_TOKEN = env("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN/TELEGRAM_BOT_TOKEN is not set")

WEBHOOK_BASE = env("WEBHOOK_URL", "WEBHOOK_BASE", "RENDER_EXTERNAL_URL", default="").strip()
WEBHOOK_PATH = "/webhook"

REQUIRED_CHANNEL = env("REQUIRED_CHANNEL", "TG_CHANNEL", default="@gurenko_kristina_ai").strip()
CHANNEL_INVITE_URL = env("CHANNEL_INVITE_URL", default="https://t.me/gurenko_kristina_ai").strip()

INSTAGRAM_URL = env("INSTAGRAM_URL", default="https://www.instagram.com/gurenko_kristina/").strip()

ADMIN_USER_ID = int(env("ADMIN_USER_ID", default="0") or "0")

# Gates / Behavior
STRICT_CHANNEL_CHECK = env("STRICT_CHANNEL_CHECK", default="1") in ("1", "true", "True", "yes", "YES")
AUTO_IG_VERIFY = env("AUTO_IG_VERIFY", default="1") in ("1", "true", "True", "yes", "YES")

# Limits
FREE_DAILY_LIMIT = int(env("GEN_FREE_DAILY", "FREE_DAILY_LIMIT", "DAILY_LIMIT", default="1"))
VIP_DAILY_LIMIT = int(env("VIP_DAILY_LIMIT", default="30"))
VIP_DAYS = int(env("VIP_DAYS", "VIP_DURATION_DAYS", default="30"))

# Stars VIP
VIP_PRICE_STARS = int(env("VIP_PRICE_STARS", default="299"))  # stars count

# Models
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
OPENAI_MODEL = env("OPENAI_MODEL", default="gpt-4o-mini")
OPENAI_IMAGE_MODEL = env("OPENAI_IMAGE_MODEL", "IMAGE_MODEL", default="gpt-image-1")
OPENAI_VIDEO_MODEL = env("OPENAI_VIDEO_MODEL", default="sora-2")  # stub in this code

DB_PATH = env("DB_PATH", default="bot.db")

SYSTEM_PROMPT = (
    "Ты — ИИ помощник Кристины (создательница AI-контента). "
    "Отвечай коротко, по делу, дружелюбно. "
    "Если запрос про генерацию — помоги промптом, хуками, сценариями, настройками."
)

# -------------------- APP --------------------
app = FastAPI()
tg_app: Application | None = None
BOT_USERNAME: str | None = None

# -------------------- DB --------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def now_utc():
    return datetime.utcnow()

def today_str():
    return date.today().isoformat()

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,

            referred_by INTEGER,
            ref_count INTEGER DEFAULT 0,

            ig_verified INTEGER DEFAULT 0,
            ig_handle TEXT,

            vip_until TEXT,

            used_date TEXT,
            used_count INTEGER DEFAULT 0,

            bonus_credits INTEGER DEFAULT 0,

            created_at TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS ig_requests (
            user_id INTEGER PRIMARY KEY,
            ig_handle TEXT,
            note TEXT,
            created_at TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            created_at TEXT
        )
        """)
        conn.commit()

def ensure_user(u):
    with db() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (u.id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, created_at, used_date, used_count) VALUES (?, ?, ?, ?, ?, 0)",
                (u.id, u.username or "", u.first_name or "", now_utc().isoformat(), today_str()),
            )
        else:
            conn.execute(
                "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                (u.username or "", u.first_name or "", u.id),
            )
        conn.commit()

def get_user(user_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def set_ig_verified(user_id: int, handle: str | None = None):
    with db() as conn:
        if handle is None:
            conn.execute("UPDATE users SET ig_verified=1 WHERE user_id=?", (user_id,))
        else:
            conn.execute("UPDATE users SET ig_verified=1, ig_handle=? WHERE user_id=?", (handle, user_id))
        conn.execute("DELETE FROM ig_requests WHERE user_id=?", (user_id,))
        conn.commit()

def set_ig_request(user_id: int, handle: str, note: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
            (user_id, handle, note, now_utc().isoformat())
        )
        conn.execute("UPDATE users SET ig_handle=? WHERE user_id=?", (handle, user_id))
        conn.commit()

def set_referred(user_id: int, inviter_id: int):
    if inviter_id == user_id:
        return
    with db() as conn:
        me = conn.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not me:
            return
        if me["referred_by"]:
            return
        conn.execute("UPDATE users SET referred_by=? WHERE user_id=?", (inviter_id, user_id))
        conn.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (inviter_id,))
        conn.execute("UPDATE users SET bonus_credits = bonus_credits + 1 WHERE user_id=?", (inviter_id,))
        conn.commit()

def is_vip(row) -> bool:
    if not row or not row["vip_until"]:
        return False
    try:
        return datetime.fromisoformat(row["vip_until"]) > now_utc()
    except Exception:
        return False

def vip_until_text(row):
    if not row or not row["vip_until"]:
        return "нет"
    return row["vip_until"].replace("T", " ")

def reset_daily_if_needed(user_id: int):
    row = get_user(user_id)
    if not row:
        return
    td = today_str()
    if row["used_date"] != td:
        with db() as conn:
            conn.execute("UPDATE users SET used_date=?, used_count=0 WHERE user_id=?", (td, user_id))
            conn.commit()

def can_use_generation(user_id: int) -> tuple[bool, str]:
    reset_daily_if_needed(user_id)
    row = get_user(user_id)
    vip = is_vip(row)

    bonus = int(row["bonus_credits"] or 0)
    if bonus > 0:
        return True, f"🎁 Бонус-генерации: {bonus}."

    limit = VIP_DAILY_LIMIT if vip else FREE_DAILY_LIMIT
    used = int(row["used_count"] or 0)
    if used >= limit:
        return False, f"Лимит на сегодня исчерпан: {used}/{limit}."
    return True, f"Осталось на сегодня: {limit - used}."

def consume_generation(user_id: int):
    reset_daily_if_needed(user_id)
    row = get_user(user_id)
    if not row:
        return
    bonus = int(row["bonus_credits"] or 0)
    with db() as conn:
        if bonus > 0:
            conn.execute("UPDATE users SET bonus_credits = bonus_credits - 1 WHERE user_id=?", (user_id,))
        else:
            conn.execute("UPDATE users SET used_count = used_count + 1, used_date=? WHERE user_id=?",
                         (today_str(), user_id))
        conn.commit()

def chat_add(user_id: int, role: str, content: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, now_utc().isoformat())
        )
        # keep last 12 messages
        conn.execute("""
            DELETE FROM chat_messages
            WHERE id NOT IN (
                SELECT id FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 12
            ) AND user_id=?
        """, (user_id, user_id))
        conn.commit()

def chat_get(user_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT role, content FROM chat_messages WHERE user_id=? ORDER BY id ASC",
            (user_id,)
        ).fetchall()

# -------------------- UI --------------------
def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🤖 ИИ помощник"), KeyboardButton("🖼 Сгенерировать фото")],
            [KeyboardButton("🎬 Сгенерировать видео"), KeyboardButton("🎁 Промт дня")],
            [KeyboardButton("📆 Челлендж 30 дней"), KeyboardButton("🎁 Пригласить друга")],
            [KeyboardButton("⭐️ VIP за Stars"), KeyboardButton("✅ Instagram доступ")],
            [KeyboardButton("ℹ️ Помощь")],
        ],
        resize_keyboard=True
    )

def share_keyboard(user_id: int):
    bot_un = BOT_USERNAME or "your_bot_username"
    deep = f"https://t.me/{bot_un}?start=ref_{user_id}"
    share_url = f"https://t.me/share/url?url={quote(deep)}&text={quote('Смотри, бот с промтами и генерацией 👇')}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Поделиться ботом", url=share_url)],
        [InlineKeyboardButton("🔗 Открыть ссылку приглашения", url=deep)],
    ])

def channel_gate_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подписаться на канал", url=CHANNEL_INVITE_URL)],
        [InlineKeyboardButton("🔁 Я подписался — проверить", callback_data="check_channel")]
    ])

def instagram_gate_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Открыть Instagram", url=INSTAGRAM_URL)],
        [InlineKeyboardButton("✅ Я подписался — продолжить", callback_data="ig_request")]
    ])

# -------------------- GATES --------------------
async def is_subscribed_to_channel(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        if member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        ):
            return True
        if member.status == ChatMemberStatus.RESTRICTED:
            return True
        return False
    except Exception as e:
        log.warning("channel check failed: %s", e)
        return (not STRICT_CHANNEL_CHECK)

async def require_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    u = update.effective_user
    ensure_user(u)

    # 1) TG channel
    ok = await is_subscribed_to_channel(context.bot, u.id)
    if not ok:
        await update.effective_message.reply_text(
            "🔒 Чтобы пользоваться ботом, подпишись на мой Telegram-канал.\n"
            "Нажми «Подписаться», затем «Я подписался — проверить».",
            reply_markup=channel_gate_keyboard()
        )
        return False

    # 2) IG gate
    row = get_user(u.id)
    if int(row["ig_verified"] or 0) != 1:
        await update.effective_message.reply_text(
            "🔒 Ещё шаг: подписка на Instagram.\n\n"
            "Нажми кнопку ниже, потом пришли свой @ник (и при желании скрин).",
            reply_markup=instagram_gate_keyboard()
        )
        return False

    return True

# -------------------- OPENAI --------------------
def get_client() -> AsyncOpenAI | None:
    if not (OPENAI_AVAILABLE and OPENAI_API_KEY):
        return None
    return AsyncOpenAI(api_key=OPENAI_API_KEY)

async def openai_assistant(user_id: int, user_text: str) -> tuple[str | None, str | None]:
    client = get_client()
    if not client:
        return None, "OpenAI API не настроен (нет OPENAI_API_KEY)."

    try:
        history = chat_get(user_id)
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        for r in history:
            msgs.append({"role": r["role"], "content": r["content"]})
        msgs.append({"role": "user", "content": user_text})

        res = await client.responses.create(
            model=OPENAI_MODEL,
            input=msgs,
        )
        text = getattr(res, "output_text", None)
        if not text:
            text = "Я ответил, но текст не извлёкся. Скажи: «повтори ответ»."
        return text, None
    except Exception as e:
        return None, f"Ошибка ИИ помощника: {e}"

async def openai_generate_image(prompt: str) -> tuple[bytes | None, str | None]:
    client = get_client()
    if not client:
        return None, "OpenAI API не настроен (нет OPENAI_API_KEY)."

    try:
        res = await client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024",
        )
        data0 = res.data[0]
        b64 = getattr(data0, "b64_json", None) or (data0.get("b64_json") if isinstance(data0, dict) else None)
        if not b64:
            return None, "Не пришли данные изображения (b64_json пуст)."
        img = base64.b64decode(b64)
        return img, None
    except Exception as e:
        return None, f"Не удалось сгенерировать фото: {e}"

async def openai_generate_video_stub(prompt: str) -> tuple[None, str]:
    return None, (
        "🎬 Видео (Sora) в этом проекте сейчас как заглушка.\n\n"
        "Чтобы реально генерировать видео через API, нужно:\n"
        "1) чтобы у OpenAI API проекта был доступ к Sora/Video endpoint;\n"
        "2) включён billing;\n"
        "3) реализовать конкретный endpoint под твою модель.\n\n"
        "Если хочешь — скажи, и я дам рабочую реализацию под доступный тебе Video API."
    )

# -------------------- HANDLERS --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME
    u = update.effective_user
    ensure_user(u)

    # referral
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                inviter = int(arg.replace("ref_", "").strip())
                set_referred(u.id, inviter)
            except Exception:
                pass

    if BOT_USERNAME is None:
        me = await context.bot.get_me()
        BOT_USERNAME = me.username

    # строгий гейт прямо на /start (как ты просила)
    ok = await require_access(update, context)
    if not ok:
        return

    row = get_user(u.id)
    ok2, msg = can_use_generation(u.id)
    text = (
        "Привет, Кристина на связи 🤍\n"
        "Я — твой бот с промтами, генерацией и ИИ помощником.\n\n"
        "Что умею:\n"
        "• 🤖 ИИ помощник (идеи, сценарии, хуки, промты)\n"
        "• 🖼 Генерация фото (через API)\n"
        "• 🎬 Генерация видео (пока заглушка/подключим)\n"
        "• 🎁 Рефералка: приглашай друзей → бонус-генерации\n"
        "• ⭐️ VIP за Telegram Stars\n\n"
        f"VIP: {'активен ✅' if is_vip(row) else 'нет ❌'} | до: {vip_until_text(row)}\n"
        f"{msg}\n\n"
        "Выбирай в меню 👇"
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu())

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.effective_message.reply_text(f"Твой user_id: `{u.id}`", parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    row = get_user(update.effective_user.id)
    ok, msg = can_use_generation(update.effective_user.id)
    await update.effective_message.reply_text(
        "ℹ️ Помощь\n\n"
        "🤖 ИИ помощник — просто напиши вопрос.\n"
        "🖼 Фото — нажми кнопку и пришли описание.\n"
        "🎬 Видео — нажми кнопку и пришли описание.\n"
        "🎁 Пригласить друга — получишь ссылку, за каждого друга +1 бонус.\n"
        "⭐️ VIP — оплатишь Stars, лимиты вырастут.\n\n"
        f"VIP: {'✅' if is_vip(row) else '❌'} до {vip_until_text(row)}\n"
        f"{msg}",
        reply_markup=main_menu()
    )

async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    if not await require_access(update, context):
        return
    context.user_data["mode"] = mode
    if mode == "assistant":
        await update.effective_message.reply_text("🤖 Пиши запрос — отвечу как помощник.", reply_markup=main_menu())
    elif mode == "image":
        await update.effective_message.reply_text("🖼 Напиши описание для фото.", reply_markup=main_menu())
    elif mode == "video":
        await update.effective_message.reply_text("🎬 Напиши описание для видео (пока заглушка/подключим).", reply_markup=main_menu())

async def vip_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return

    payload = f"vip_{update.effective_user.id}_{int(time.time())}"
    prices = [LabeledPrice(label=f"VIP на {VIP_DAYS} дней", amount=VIP_PRICE_STARS)]

    # Stars: currency XTR, provider_token empty string
    await context.bot.send_invoice(
        chat_id=update.effective_user.id,
        title="VIP доступ",
        description=f"VIP на {VIP_DAYS} дней: до {VIP_DAILY_LIMIT} генераций/день + приоритет.",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=prices,
    )
    await update.effective_message.reply_text("⭐️ Счёт выставлен. Оплати Stars — VIP включится автоматически ✅")

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    await q.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    until = now_utc() + timedelta(days=VIP_DAYS)
    with db() as conn:
        conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until.isoformat(), u.id))
        conn.commit()
    await update.effective_message.reply_text(
        f"✅ Оплата получена! VIP активирован до {until.isoformat()}",
        reply_markup=main_menu()
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = update.effective_user
    ensure_user(u)

    if query.data == "check_channel":
        ok = await is_subscribed_to_channel(context.bot, u.id)
        if ok:
            row = get_user(u.id)
            if int(row["ig_verified"] or 0) == 1:
                await query.edit_message_text("✅ Канал подтверждён! Доступ открыт 🎉")
                await context.bot.send_message(u.id, "Меню доступно 👇", reply_markup=main_menu())
            else:
                await query.edit_message_text(
                    "✅ Канал подтверждён! Теперь Instagram 👇",
                    reply_markup=instagram_gate_keyboard()
                )
        else:
            await query.edit_message_text(
                "Пока не вижу подписку 😔\n\nПодпишись и нажми «проверить» ещё раз.",
                reply_markup=channel_gate_keyboard()
            )
        return

    if query.data == "ig_request":
        # просим прислать @ник
        context.user_data["await_ig_info"] = True
        await query.edit_message_text(
            "Отправь одним сообщением свой Instagram @ник.\n"
            "Можешь добавить скрин подписки (по желанию)."
        )
        return

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)

    if context.user_data.get("await_ig_info"):
        # фото как доказательство
        set_ig_request(u.id, handle=get_user(u.id)["ig_handle"] or "", note="photo proof")
        if AUTO_IG_VERIFY:
            set_ig_verified(u.id, handle=(get_user(u.id)["ig_handle"] or ""))
            context.user_data["await_ig_info"] = False
            await update.effective_message.reply_text("✅ Instagram подтверждён (авто). Доступ открыт 🎉", reply_markup=main_menu())
        else:
            context.user_data["await_ig_info"] = False
            await update.effective_message.reply_text("✅ Принято! Жду подтверждения админом.", reply_markup=main_menu())
            if ADMIN_USER_ID:
                await context.bot.send_message(ADMIN_USER_ID, f"IG запрос (фото): user_id={u.id} @{u.username}\n/ig_ok {u.id}  /ig_no {u.id}")
        return

    # обычное фото (не IG)
    if not await require_access(update, context):
        return
    await update.effective_message.reply_text("Фото получил ✅ Сейчас генерация у нас по тексту.", reply_markup=main_menu())

async def ig_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Формат: /ig_ok <user_id>")
        return
    uid = int(context.args[0])
    set_ig_verified(uid)
    await update.effective_message.reply_text(f"✅ IG подтвержден для {uid}")
    try:
        await context.bot.send_message(uid, "✅ Instagram подтвержден! Доступ открыт 🎉", reply_markup=main_menu())
    except Exception:
        pass

async def ig_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Формат: /ig_no <user_id>")
        return
    uid = int(context.args[0])
    with db() as conn:
        conn.execute("DELETE FROM ig_requests WHERE user_id=?", (uid,))
        conn.commit()
    await update.effective_message.reply_text(f"❌ IG отклонен для {uid}")
    try:
        await context.bot.send_message(uid, "❌ Не получилось подтвердить Instagram. Нажми «Instagram доступ» и пришли @ник снова.")
    except Exception:
        pass

async def vip_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    if len(context.args) < 1:
        await update.effective_message.reply_text("Формат: /vip_add <user_id> [days]")
        return
    uid = int(context.args[0])
    days = int(context.args[1]) if len(context.args) > 1 else VIP_DAYS
    until = now_utc() + timedelta(days=days)
    with db() as conn:
        conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until.isoformat(), uid))
        conn.commit()
    await update.effective_message.reply_text(f"⭐️ VIP выдан для {uid} до {until.isoformat()}")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    u = update.effective_user
    ensure_user(u)

    # IG handle capture
    if context.user_data.get("await_ig_info") and txt:
        handle = txt.strip()
        if handle.startswith("@"):
            handle = handle[1:]
        # store request
        set_ig_request(u.id, handle=handle, note="handle provided")
        if AUTO_IG_VERIFY:
            set_ig_verified(u.id, handle=handle)
            context.user_data["await_ig_info"] = False
            await update.effective_message.reply_text("✅ Instagram подтверждён (авто). Доступ открыт 🎉", reply_markup=main_menu())
        else:
            context.user_data["await_ig_info"] = False
            await update.effective_message.reply_text("✅ Принято! Жду подтверждения админом.", reply_markup=main_menu())
            if ADMIN_USER_ID:
                await context.bot.send_message(ADMIN_USER_ID, f"IG запрос: user_id={u.id}, ig=@{handle}\n/ig_ok {u.id}  /ig_no {u.id}")
        return

    # Menu buttons
    if txt == "🤖 ИИ помощник":
        await set_mode(update, context, "assistant")
        return
    if txt == "🖼 Сгенерировать фото":
        await set_mode(update, context, "image")
        return
    if txt == "🎬 Сгенерировать видео":
        await set_mode(update, context, "video")
        return
    if txt == "🎁 Пригласить друга":
        if not await require_access(update, context):
            return
        await update.effective_message.reply_text("Вот твоя ссылка:", reply_markup=share_keyboard(u.id))
        return
    if txt == "⭐️ VIP за Stars":
        await vip_invoice(update, context)
        return
    if txt == "✅ Instagram доступ":
        await update.effective_message.reply_text(
            "Подпишись на Instagram и нажми кнопку «✅ Я подписался — продолжить».",
            reply_markup=instagram_gate_keyboard()
        )
        return
    if txt == "🎁 Промт дня":
        if not await require_access(update, context):
            return
        prompts = [
            "Ультра-реалистичный fashion-портрет, морозные ресницы, 85mm, мягкий свет, 8K, кожа детальная.",
            "Кинематографичный зимний кадр, лёгкий снег, объёмный свет, реалистичная ткань, 4K.",
            "Editorial-фото, натуральная кожа, поры, high-end retouch без пластика.",
            "Reels-стиль: динамичный ракурс, лёгкий motion blur, реализм, естественные цвета, 4K.",
        ]
        idx = int(time.time() // 86400) % len(prompts)
        await update.effective_message.reply_text(f"🎁 Промт дня:\n\n{prompts[idx]}", reply_markup=main_menu())
        return
    if txt == "📆 Челлендж 30 дней":
        if not await require_access(update, context):
            return
        tasks = [
            "День 1: 3 варианта одного портрета (разный свет).",
            "День 2: 3 ракурса (close/mid/full).",
            "День 3: кожа: поры/текстура/без пластика.",
            "День 4: снег/частицы: реалистичный snowfall + bokeh.",
            "День 5: outfit-замена без изменения лица.",
        ]
        day_idx = int(time.time() // 86400) % len(tasks)
        await update.effective_message.reply_text(f"📆 Челлендж:\n\n{tasks[day_idx]}", reply_markup=main_menu())
        return
    if txt == "ℹ️ Помощь":
        await help_cmd(update, context)
        return

    # Require access for everything else
    if not await require_access(update, context):
        return

    mode = context.user_data.get("mode", "assistant")  # по умолчанию — ИИ помощник

    # IMAGE/VIDEO need limits
    if mode in ("image", "video"):
        ok, msg = can_use_generation(u.id)
        if not ok:
            await update.effective_message.reply_text("⛔️ " + msg, reply_markup=main_menu())
            context.user_data["mode"] = "assistant"
            return

    if mode == "image":
        await update.effective_message.reply_text("⏳ Генерирую фото…")
        img, err = await openai_generate_image(txt)
        if err:
            await update.effective_message.reply_text(err, reply_markup=main_menu())
        else:
            consume_generation(u.id)
            await update.effective_message.reply_photo(photo=img, caption="Готово ✅", reply_markup=main_menu())
        context.user_data["mode"] = "assistant"
        return

    if mode == "video":
        await update.effective_message.reply_text("⏳ Готовлю видео…")
        _, err = await openai_generate_video_stub(txt)
        await update.effective_message.reply_text(err, reply_markup=main_menu())
        context.user_data["mode"] = "assistant"
        return

    # ASSISTANT
    chat_add(u.id, "user", txt)
    reply, err = await openai_assistant(u.id, txt)
    if err:
        await update.effective_message.reply_text(err, reply_markup=main_menu())
    else:
        chat_add(u.id, "assistant", reply)
        await update.effective_message.reply_text(reply, reply_markup=main_menu())

# -------------------- FASTAPI ROUTES --------------------
@app.api_route("/", methods=["GET", "HEAD"], response_class=PlainTextResponse)
async def root():
    return "OK"

@app.post(WEBHOOK_PATH)
async def webhook(req: Request):
    if tg_app is None:
        return {"ok": False, "error": "bot not ready"}
    data = await req.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

# -------------------- STARTUP/SHUTDOWN --------------------
@app.on_event("startup")
async def on_startup():
    global tg_app, BOT_USERNAME

    init_db()

    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler("help", help_cmd))
    tg_app.add_handler(CommandHandler("myid", myid_cmd))

    # admin
    tg_app.add_handler(CommandHandler("ig_ok", ig_ok))
    tg_app.add_handler(CommandHandler("ig_no", ig_no))
    tg_app.add_handler(CommandHandler("vip_add", vip_add))

    # callbacks
    tg_app.add_handler(CallbackQueryHandler(on_button))

    # payments (Stars)
    tg_app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    tg_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # content
    tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await tg_app.initialize()
    await tg_app.start()

    me = await tg_app.bot.get_me()
    BOT_USERNAME = me.username
    log.info("Bot username: %s", BOT_USERNAME)

    if WEBHOOK_BASE:
        url = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH
        await tg_app.bot.set_webhook(url)
        log.info("Webhook set: %s", url)
    else:
        log.warning("WEBHOOK_BASE/WEBHOOK_URL/RENDER_EXTERNAL_URL not set. Webhook NOT configured.")

@app.on_event("shutdown")
async def on_shutdown():
    global tg_app
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
