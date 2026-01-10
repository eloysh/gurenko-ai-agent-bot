import os
import base64
import time
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta, date
from urllib.parse import quote_plus

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# OpenAI
try:
    from openai import OpenAI, AsyncOpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False


# -------------------- CONFIG --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gurenko-bot")

APP_VERSION = (
    os.getenv("RENDER_GIT_COMMIT")
    or os.getenv("APP_VERSION")
    or "dev"
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

# Render/Webhook
WEBHOOK_BASE = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip()
WEBHOOK_PATH = "/webhook"
USE_POLLING_FALLBACK = os.getenv("USE_POLLING_FALLBACK", "1").strip() == "1"

# Gates
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@gurenko_kristina_ai").strip()
CHANNEL_INVITE_URL = os.getenv("CHANNEL_INVITE_URL", "https://t.me/gurenko_kristina_ai").strip()
STRICT_CHANNEL_CHECK = os.getenv("STRICT_CHANNEL_CHECK", "1").strip() == "1"

INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "https://www.instagram.com/gurenko_kristina/").strip()

# Admin (your Telegram numeric id)
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or "0")

# Limits
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "1"))
VIP_DAILY_LIMIT = int(os.getenv("VIP_DAILY_LIMIT", "30"))
VIP_DURATION_DAYS = int(os.getenv("VIP_DURATION_DAYS", "30"))

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1").strip()
OPENAI_VIDEO_MODEL = os.getenv("OPENAI_VIDEO_MODEL", "sora-2").strip()
VIDEO_DEFAULT_SIZE = os.getenv("VIDEO_DEFAULT_SIZE", "1280x720").strip()
VIDEO_DEFAULT_SECONDS = int(os.getenv("VIDEO_DEFAULT_SECONDS", "8").strip() or "8")

# DB
DB_PATH = os.getenv("DB_PATH", "bot.db")


# -------------------- APP/DB --------------------
app = FastAPI()
tg_app: Application | None = None
BOT_USERNAME: str | None = None

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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
        conn.commit()

def now_utc():
    return datetime.utcnow()

def today_str():
    return date.today().isoformat()

def ensure_user(u):
    with db() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (u.id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, created_at) VALUES (?, ?, ?, ?)",
                (u.id, u.username or "", u.first_name or "", now_utc().isoformat()),
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

def set_referred(user_id: int, inviter_id: int):
    with db() as conn:
        me = conn.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not me:
            return
        if me["referred_by"]:
            return
        if inviter_id == user_id:
            return

        conn.execute("UPDATE users SET referred_by=? WHERE user_id=?", (inviter_id, user_id))
        conn.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (inviter_id,))
        conn.execute("UPDATE users SET bonus_credits = bonus_credits + 1 WHERE user_id=?", (inviter_id,))
        conn.commit()

def is_vip(row) -> bool:
    if not row or not row["vip_until"]:
        return False
    try:
        dt = datetime.fromisoformat(row["vip_until"])
        return dt > now_utc()
    except Exception:
        return False

def vip_until_text(row):
    if not row or not row["vip_until"]:
        return "нет"
    return row["vip_until"].replace("T", " ")

def reset_daily_if_needed(row):
    if not row:
        return
    td = today_str()
    if row["used_date"] != td:
        with db() as conn:
            conn.execute("UPDATE users SET used_date=?, used_count=0 WHERE user_id=?", (td, row["user_id"]))
            conn.commit()

def can_use_generation(row) -> tuple[bool, str]:
    reset_daily_if_needed(row)
    row = get_user(row["user_id"])
    vip = is_vip(row)

    bonus = int(row["bonus_credits"] or 0)
    if bonus > 0:
        return True, f"🎁 У тебя есть бонус: {bonus} доп. генераций."

    limit = VIP_DAILY_LIMIT if vip else FREE_DAILY_LIMIT
    used = int(row["used_count"] or 0)
    if used >= limit:
        if vip:
            return False, f"Лимит на сегодня исчерпан: {used}/{limit} (VIP)."
        return False, f"Лимит на сегодня исчерпан: {used}/{limit}. Завтра будет доступно снова."
    return True, f"Осталось на сегодня: {limit - used}."

def consume_generation(row):
    reset_daily_if_needed(row)
    row = get_user(row["user_id"])
    bonus = int(row["bonus_credits"] or 0)
    with db() as conn:
        if bonus > 0:
            conn.execute("UPDATE users SET bonus_credits = bonus_credits - 1 WHERE user_id=?", (row["user_id"],))
        else:
            conn.execute(
                "UPDATE users SET used_count = used_count + 1, used_date=? WHERE user_id=?",
                (today_str(), row["user_id"]),
            )
        conn.commit()


# -------------------- GATES --------------------
async def is_subscribed_to_channel(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return True
        if member.status == ChatMemberStatus.RESTRICTED:
            return True
        return False
    except Exception as e:
        log.warning("channel check failed: %s", e)
        # Если strict — блокируем, если нет — пропускаем
        return False if STRICT_CHANNEL_CHECK else True

def channel_gate_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подписаться на Telegram-канал", url=CHANNEL_INVITE_URL)],
        [InlineKeyboardButton("🔁 Я подписался — проверить", callback_data="check_channel")]
    ])

def instagram_gate_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Открыть Instagram", url=INSTAGRAM_URL)],
        [InlineKeyboardButton("✅ Я подписался — отправить заявку", callback_data="ig_request")]
    ])

async def require_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    u = update.effective_user
    ensure_user(u)
    ok = await is_subscribed_to_channel(context.bot, u.id)
    if not ok:
        text = (
            "🔒 Чтобы пользоваться ботом, нужно быть подписанным на мой Telegram-канал.\n\n"
            "Нажми «Подписаться», потом «Я подписался — проверить»."
        )
        await update.effective_message.reply_text(text, reply_markup=channel_gate_keyboard())
        return False
    return True

async def require_instagram_verified(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    u = update.effective_user
    ensure_user(u)
    row = get_user(u.id)
    if int(row["ig_verified"] or 0) != 1:
        text = (
            "🔒 Ещё шаг: подтверждение подписки на Instagram.\n\n"
            "Instagram не даёт надёжной авто-проверки подписки через Telegram-бота, "
            "поэтому здесь работает схема: заявка → ручное подтверждение.\n\n"
            "Нажми кнопку ниже 👇"
        )
        await update.effective_message.reply_text(text, reply_markup=instagram_gate_keyboard())
        return False
    return True

async def require_full_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not await require_channel(update, context):
        return False
    if not await require_instagram_verified(update, context):
        return False
    return True


# -------------------- UI --------------------
def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🖼 Сгенерировать фото"), KeyboardButton("🎬 Сгенерировать видео")],
            [KeyboardButton("🎁 Промт дня"), KeyboardButton("📆 Челлендж 30 дней")],
            [KeyboardButton("🎁 Пригласить друга"), KeyboardButton("⭐️ VIP / Подписка")],
            [KeyboardButton("✅ Проверить Instagram"), KeyboardButton("ℹ️ Помощь")],
        ],
        resize_keyboard=True
    )

async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    global BOT_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    me = await context.bot.get_me()
    BOT_USERNAME = me.username
    return BOT_USERNAME or "your_bot_username"

async def share_keyboard(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    bot_un = await get_bot_username(context)
    deep = f"https://t.me/{bot_un}?start=ref_{user_id}"
    share_url = (
        "https://t.me/share/url?"
        f"url={quote_plus(deep)}&text={quote_plus('Смотри, бот с промтами и генерацией фото/видео 👇')}"
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Поделиться ботом", url=share_url)],
        [InlineKeyboardButton("🔗 Открыть ссылку приглашения", url=deep)],
    ])


# -------------------- OPENAI HELPERS --------------------
def get_openai_client():
    if not (OPENAI_AVAILABLE and OPENAI_API_KEY):
        return None
    return OpenAI(api_key=OPENAI_API_KEY)

def get_openai_async_client():
    if not (OPENAI_AVAILABLE and OPENAI_API_KEY):
        return None
    return AsyncOpenAI(api_key=OPENAI_API_KEY)

def openai_generate_image(prompt: str) -> tuple[bytes | None, str | None]:
    client = get_openai_client()
    if not client:
        return None, "OpenAI API не настроен (нет OPENAI_API_KEY)."

    try:
        res = client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024"
        )
        data0 = res.data[0]
        b64 = getattr(data0, "b64_json", None)
        if b64:
            return base64.b64decode(b64), None

        # иногда может прийти url
        url = getattr(data0, "url", None)
        if url:
            # скачиваем картинку
            img = httpx.get(url, timeout=60).content
            return img, None

        return None, "Не пришли данные изображения (нет b64_json/url)."
    except Exception as e:
        return None, f"Не удалось сгенерировать фото: {e}"

async def download_video_mp4(video_id: str) -> bytes:
    # GET /videos/{id}/content
    url = f"https://api.openai.com/v1/videos/{video_id}/content"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return r.content

async def run_video_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, prompt: str):
    """
    Запускает видео-генерацию в фоне и отправляет результат в чат.
    """
    client = get_openai_async_client()
    if not client:
        await context.bot.send_message(chat_id, "OpenAI API не настроен (нет OPENAI_API_KEY).")
        return

    try:
        await context.bot.send_message(
            chat_id,
            "🎬 Запустила генерацию видео… это может занять несколько минут.\n\n"
            "⚠️ Важно: Sora Video API не генерирует реальных людей и отклоняет реф-картинки с лицами. "
            "Если в промте будут реальные люди — запрос может упасть.",
        )

        video = await client.videos.create_and_poll(
            model=OPENAI_VIDEO_MODEL,
            prompt=prompt,
            size=VIDEO_DEFAULT_SIZE,
            seconds=str(VIDEO_DEFAULT_SECONDS),
        )

        if getattr(video, "status", "") != "completed":
            await context.bot.send_message(chat_id, f"❌ Видео не завершилось. Статус: {getattr(video, 'status', 'unknown')}")
            return

        vid = getattr(video, "id", None)
        if not vid:
            await context.bot.send_message(chat_id, "❌ Не нашла id видео в ответе.")
            return

        mp4 = await download_video_mp4(vid)
        await context.bot.send_video(chat_id, video=mp4, caption="Готово ✅")

    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Ошибка генерации видео: {e}")


# -------------------- HANDLERS --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    await get_bot_username(context)

    text = (
        "Привет! Я — AI-помощник Кристины 🤍\n"
        f"Версия: `{APP_VERSION[:7]}`\n\n"
        "Что я умею:\n"
        "• 🖼 Генерировать фото по твоему описанию\n"
        "• 🎬 Генерировать видео (если Sora доступна в твоём OpenAI API)\n"
        "• 🎁 Давать «Промт дня» и задания на 30 дней\n"
        "• 🎁 Рефералка: приглашай друзей → получай бонус-генерации\n\n"
        f"Лимит: бесплатно — {FREE_DAILY_LIMIT} генерация/день. VIP — до {VIP_DAILY_LIMIT}/день.\n\n"
        "Выбирай кнопку в меню 👇"
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu(), parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_channel(update, context):
        return
    row = get_user(update.effective_user.id)
    reset_daily_if_needed(row)
    row = get_user(update.effective_user.id)
    vip = is_vip(row)
    ok, msg = can_use_generation(row)

    text = (
        "ℹ️ Помощь\n\n"
        "🖼 Сгенерировать фото — нажми кнопку и отправь описание.\n"
        "🎬 Сгенерировать видео — нажми кнопку и отправь описание (без реальных людей).\n"
        "🎁 Пригласить друга — получишь ссылку, по ней друзья заходят и тебе капают бонусы.\n"
        "✅ Проверить Instagram — отправляешь заявку, я подтверждаю.\n\n"
        f"VIP: {'активен ✅' if vip else 'нет ❌'}\n"
        f"VIP до: {vip_until_text(row)}\n"
        f"{msg}\n"
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu())

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    u = update.effective_user
    ensure_user(u)

    # 1) Если ждём IG данные — обрабатываем первыми
    if context.user_data.get("await_ig_info"):
        ig = txt.strip()
        # простая нормализация
        if ig.startswith("http"):
            # если прислал ссылку — оставляем как есть
            ig_handle = ig
        else:
            if not ig.startswith("@"):
                ig = "@" + ig.lstrip("@")
            ig_handle = ig

        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
                (u.id, ig_handle, "text proof received", now_utc().isoformat())
            )
            conn.commit()

        context.user_data["await_ig_info"] = False

        await update.effective_message.reply_text(
            "✅ Принято! Заявка на Instagram отправлена.\n\n"
            "Я подтвержу и открою доступ. Если хочешь быстрее — напиши мне: «проверь IG в боте».",
            reply_markup=main_menu()
        )

        if ADMIN_USER_ID:
            try:
                await context.bot.send_message(
                    ADMIN_USER_ID,
                    f"IG-заявка: user_id={u.id}, tg=@{u.username}\n"
                    f"IG: {ig_handle}\n"
                    f"Подтверди: /ig_ok {u.id}  |  Отклонить: /ig_no {u.id}"
                )
            except Exception:
                pass
        return

    # 2) Меню
    if txt == "✅ Проверить Instagram":
        await update.effective_message.reply_text(
            "Подпишись на Instagram и нажми кнопку ниже, чтобы отправить заявку.\n"
            "После кнопки пришли одним сообщением свой @ник (и по желанию скрин).\n\n"
            f"Instagram: {INSTAGRAM_URL}",
            reply_markup=instagram_gate_keyboard(),
        )
        return

    if txt == "🎁 Пригласить друга":
        # Для реферальной ссылки достаточно подписки на Telegram-канал
        if not await require_channel(update, context):
            return
        kb = await share_keyboard(context, u.id)
        await update.effective_message.reply_text(
            "Вот твоя ссылка-приглашение. Нажми «Поделиться», чтобы отправить друзьям:",
            reply_markup=kb,
        )
        return

    if txt == "ℹ️ Помощь":
        await help_cmd(update, context)
        return

    if txt == "⭐️ VIP / Подписка":
        if not await require_channel(update, context):
            return
        row = get_user(u.id)
        text = (
            "⭐️ VIP / Подписка\n\n"
            f"VIP даёт до {VIP_DAILY_LIMIT} генераций в день на {VIP_DURATION_DAYS} дней.\n"
            "Выдача VIP сейчас вручную: я отмечаю VIP в базе.\n\n"
            "Напиши мне в личку: «хочу VIP в боте», и я подключу."
        )
        await update.effective_message.reply_text(text, reply_markup=main_menu())
        return

    if txt == "🎁 Промт дня":
        if not await require_channel(update, context):
            return
        prompts = [
            "Ультра-реалистичный fashion-портрет, морозные ресницы, 85mm, мягкий свет, 8K, детальная кожа.",
            "Кинематографичный зимний кадр, лёгкий снег, объёмный свет, реалистичная ткань, 4K.",
            "Editorial-фото: минимализм, чистый фон, натуральные поры кожи, high-end retouch.",
            "Reels-стиль: динамичный ракурс, лёгкий motion blur, реализм, естественные цвета, 4K.",
        ]
        idx = int(time.time() // 86400) % len(prompts)
        await update.effective_message.reply_text(f"🎁 Промт дня:\n\n{prompts[idx]}", reply_markup=main_menu())
        return

    if txt == "📆 Челлендж 30 дней":
        if not await require_channel(update, context):
            return
        tasks = [
            "День 1: Сделай 3 варианта одного портрета (разный свет).",
            "День 2: Один кадр в 3 ракурсах (close/mid/full).",
            "День 3: Отработай кожу: поры/текстура/без пластика.",
            "День 4: Снег/частицы: реалистичный snowfall и bokeh.",
            "День 5: Outfit-замена без изменения лица.",
        ]
        day_idx = int(time.time() // 86400) % len(tasks)
        await update.effective_message.reply_text(
            f"📆 Челлендж:\n\n{tasks[day_idx]}\n\nХочешь — добавлю все 30 дней и отметки прогресса ✅",
            reply_markup=main_menu()
        )
        return

    # 3) Свободная генерация — только при полном доступе
    mode = context.user_data.get("mode")
    if mode in ("image", "video") and txt:
        if not await require_full_access(update, context):
            return

        row = get_user(u.id)
        ok, msg = can_use_generation(row)
        if not ok:
            await update.effective_message.reply_text("⛔️ " + msg, reply_markup=main_menu())
            context.user_data["mode"] = None
            return

        await update.effective_message.reply_text("⏳ Генерирую…")

        if mode == "image":
            img, err = openai_generate_image(txt)
            if err:
                await update.effective_message.reply_text(err, reply_markup=main_menu())
            else:
                consume_generation(row)
                await update.effective_message.reply_photo(photo=img, caption="Готово ✅", reply_markup=main_menu())

        else:
            # видео — запускаем в фоне
            consume_generation(row)
            context.user_data["mode"] = None
            asyncio.create_task(run_video_job(context, update.effective_chat.id, u.id, txt))
            return

        context.user_data["mode"] = None
        return

    await update.effective_message.reply_text(
        "Выбери кнопку в меню 👇",
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
            await query.edit_message_text("✅ Канал подтверждён! Теперь подтвердим Instagram 👇",
                                          reply_markup=instagram_gate_keyboard())
        else:
            await query.edit_message_text(
                "Пока не вижу подписку 😔\n\n"
                "Подпишись и нажми «проверить» ещё раз.\n\n"
                "⚙️ Если бот не может проверить подписку — добавь бота админом в канал.",
                reply_markup=channel_gate_keyboard()
            )
        return

    if query.data == "ig_request":
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
                (u.id, "", "requested via button", now_utc().isoformat())
            )
            conn.commit()

        await query.edit_message_text(
            "✅ Заявка создана.\n\n"
            "Отправь одним сообщением:\n"
            "1) твой @ник в Instagram\n"
            "2) (по желанию) скрин, где видно подписку\n\n"
            "После подтверждения бот откроется полностью."
        )
        context.user_data["await_ig_info"] = True
        return

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)

    if context.user_data.get("await_ig_info"):
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
                (u.id, "", "photo proof received", now_utc().isoformat())
            )
            conn.commit()

        context.user_data["await_ig_info"] = False

        await update.effective_message.reply_text(
            "✅ Скрин получен! Я подтвержу и открою доступ.\n\n"
            "Если нужно быстрее — напиши мне: «проверь IG в боте».",
            reply_markup=main_menu()
        )

        if ADMIN_USER_ID:
            try:
                await context.bot.send_message(
                    ADMIN_USER_ID,
                    f"IG-заявка (скрин): user_id={u.id}, tg=@{u.username}\n"
                    f"Подтверди: /ig_ok {u.id}  |  Отклонить: /ig_no {u.id}"
                )
            except Exception:
                pass
        return

    await update.effective_message.reply_text("Фото получил ✅", reply_markup=main_menu())


# -------------------- ADMIN COMMANDS --------------------
async def ig_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /ig_ok <user_id>")
        return
    uid = int(context.args[0])
    with db() as conn:
        conn.execute("UPDATE users SET ig_verified=1 WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM ig_requests WHERE user_id=?", (uid,))
        conn.commit()
    await update.message.reply_text(f"✅ IG подтвержден для {uid}")
    try:
        await context.bot.send_message(uid, "✅ Instagram подтвержден! Доступ открыт 🎉", reply_markup=main_menu())
    except Exception:
        pass

async def ig_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /ig_no <user_id>")
        return
    uid = int(context.args[0])
    with db() as conn:
        conn.execute("DELETE FROM ig_requests WHERE user_id=?", (uid,))
        conn.commit()
    await update.message.reply_text(f"❌ IG отклонен для {uid}")
    try:
        await context.bot.send_message(uid, "❌ Не получилось подтвердить Instagram. Пришли заявку ещё раз.")
    except Exception:
        pass

async def vip_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    if len(context.args) < 1:
        await update.message.reply_text("Формат: /vip_add <user_id> [days]")
        return
    uid = int(context.args[0])
    days = int(context.args[1]) if len(context.args) > 1 else VIP_DURATION_DAYS
    until = now_utc() + timedelta(days=days)
    with db() as conn:
        conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until.isoformat(), uid))
        conn.commit()
    await update.message.reply_text(f"⭐️ VIP выдан для {uid} до {until.isoformat()}")
    try:
        await context.bot.send_message(uid, f"⭐️ VIP активирован до {until.isoformat()} 🎉", reply_markup=main_menu())
    except Exception:
        pass

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    text = (
        "🧾 Status\n\n"
        f"version: {APP_VERSION}\n"
        f"webhook_base: {WEBHOOK_BASE or '—'}\n"
        f"strict_channel_check: {STRICT_CHANNEL_CHECK}\n"
        f"openai_available: {OPENAI_AVAILABLE}\n"
        f"openai_key_set: {'yes' if bool(OPENAI_API_KEY) else 'no'}\n"
        f"image_model: {OPENAI_IMAGE_MODEL}\n"
        f"video_model: {OPENAI_VIDEO_MODEL}\n"
        f"video_default: {VIDEO_DEFAULT_SECONDS}s {VIDEO_DEFAULT_SIZE}\n"
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu())


# -------------------- MODE SETTERS --------------------
async def set_mode_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_full_access(update, context):
        return
    context.user_data["mode"] = "image"
    await update.effective_message.reply_text(
        "🖼 Напиши описание для генерации фото.\n\n"
        "Пример: «ультра-реалистичный зимний fashion-портрет, мягкий свет, 8K…»"
    )

async def set_mode_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_full_access(update, context):
        return
    context.user_data["mode"] = "video"
    await update.effective_message.reply_text(
        "🎬 Напиши описание для генерации видео.\n\n"
        "⚠️ В API нельзя генерировать реальных людей и использовать реф-картинки с лицами.\n"
        "Лучше: предметы/текст/анимация/пейзажи/абстракции."
    )


# -------------------- FASTAPI ROUTES --------------------
@app.get("/", response_class=PlainTextResponse)
async def root():
    return f"OK {APP_VERSION}"

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

    # admin
    tg_app.add_handler(CommandHandler("ig_ok", ig_ok))
    tg_app.add_handler(CommandHandler("ig_no", ig_no))
    tg_app.add_handler(CommandHandler("vip_add", vip_add))
    tg_app.add_handler(CommandHandler("status", status_cmd))

    # modes
    tg_app.add_handler(MessageHandler(filters.Regex(r"^🖼 Сгенерировать фото$"), set_mode_image))
    tg_app.add_handler(MessageHandler(filters.Regex(r"^🎬 Сгенерировать видео$"), set_mode_video))

    tg_app.add_handler(CallbackQueryHandler(on_button))
    tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await tg_app.initialize()
    await tg_app.start()

    me = await tg_app.bot.get_me()
    BOT_USERNAME = me.username
    log.info("Bot username: %s", BOT_USERNAME)
    log.info("App version: %s", APP_VERSION)

    # Webhook or polling fallback
    if WEBHOOK_BASE:
        url = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH
        await tg_app.bot.set_webhook(url)
        log.info("Webhook set: %s", url)
    else:
        log.warning("WEBHOOK_URL/RENDER_EXTERNAL_URL not set. Webhook NOT configured.")
        if USE_POLLING_FALLBACK:
            log.warning("Starting polling fallback (delete webhook + start polling)...")
            try:
                await tg_app.bot.delete_webhook(drop_pending_updates=True)
            except Exception:
                pass
            try:
                await tg_app.updater.start_polling(drop_pending_updates=True)
                log.info("Polling started.")
            except Exception as e:
                log.error("Polling failed: %s", e)

@app.on_event("shutdown")
async def on_shutdown():
    global tg_app
    if tg_app:
        try:
            if tg_app.updater and tg_app.updater.running:
                await tg_app.updater.stop()
        except Exception:
            pass
        await tg_app.stop()
        await tg_app.shutdown()
