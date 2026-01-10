import os
import base64
import logging
import sqlite3
import time
from datetime import datetime, timedelta
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
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

# Optional OpenAI
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False


# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gurenko-bot")


# -------------------- ENV HELPERS --------------------
def env_str(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n, "")
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

def env_int(*names: str, default: int = 0) -> int:
    v = env_str(*names, default="")
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        return default

def env_bool(*names: str, default: bool = False) -> bool:
    v = env_str(*names, default="")
    if v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


# -------------------- CONFIG --------------------
# Telegram token (accept multiple keys to avoid Render confusion)
TELEGRAM_TOKEN = env_str("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_API_TOKEN", default="")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN (or TELEGRAM_BOT_TOKEN) is not set")

# Webhook base url (Render)
WEBHOOK_BASE = env_str("WEBHOOK_BASE", "WEBHOOK_URL", "RENDER_EXTERNAL_URL", default="").rstrip("/")
WEBHOOK_PATH = "/webhook"

# Gates
REQUIRED_CHANNEL = env_str("REQUIRED_CHANNEL", "TG_CHANNEL", default="@gurenko_kristina_ai")
CHANNEL_INVITE_URL = env_str("CHANNEL_INVITE_URL", default="https://t.me/gurenko_kristina_ai")

INSTAGRAM_URL = env_str("INSTAGRAM_URL", default="https://www.instagram.com/gurenko_kristina/")

STRICT_CHANNEL_CHECK = env_bool("STRICT_CHANNEL_CHECK", default=True)
AUTO_IG_VERIFY = env_bool("AUTO_IG_VERIFY", default=True)

# Admin
ADMIN_USER_ID = env_int("ADMIN_USER_ID", default=0)

# Limits (free/vip)
FREE_DAILY_LIMIT = env_int("FREE_DAILY_LIMIT", "GEN_FREE_DAILY", "DAILY_LIMIT", default=1)
VIP_DAILY_LIMIT = env_int("VIP_DAILY_LIMIT", default=30)
VIP_DAYS = env_int("VIP_DAYS", default=30)

# VIP price in Telegram Stars
VIP_PRICE_STARS = env_int("VIP_PRICE_STARS", default=299)  # 299 stars

# Models
OPENAI_API_KEY = env_str("OPENAI_API_KEY", default="")
OPENAI_MODEL = env_str("OPENAI_MODEL", default="gpt-4o-mini")
OPENAI_IMAGE_MODEL = env_str("OPENAI_IMAGE_MODEL", "IMAGE_MODEL", default="gpt-image-1")
OPENAI_VIDEO_MODEL = env_str("OPENAI_VIDEO_MODEL", default="sora-2")

# DB
DB_PATH = env_str("DB_PATH", default="bot.db")


# -------------------- FASTAPI --------------------
app = FastAPI()
tg_app: Application | None = None
BOT_USERNAME: str | None = None


# -------------------- DB --------------------
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
            ig_pending INTEGER DEFAULT 0,

            vip_until TEXT,

            used_day TEXT,
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
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            payload TEXT,
            stars INTEGER,
            status TEXT,
            created_at TEXT
        )
        """)
        conn.commit()

def now_utc() -> datetime:
    return datetime.utcnow()

def today_key_utc() -> str:
    # daily reset in UTC (stable on servers)
    return now_utc().date().isoformat()

def ensure_user(u):
    with db() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (u.id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, created_at, used_day) VALUES (?, ?, ?, ?, ?)",
                (u.id, u.username or "", u.first_name or "", now_utc().isoformat(), today_key_utc()),
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
        # reward: +1 bonus credit per invite
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

def vip_until_text(row) -> str:
    if not row or not row["vip_until"]:
        return "нет"
    return str(row["vip_until"]).replace("T", " ")

def reset_daily_if_needed(row):
    if not row:
        return
    td = today_key_utc()
    if row["used_day"] != td:
        with db() as conn:
            conn.execute("UPDATE users SET used_day=?, used_count=0 WHERE user_id=?", (td, row["user_id"]))
            conn.commit()

def can_use_generation(row) -> tuple[bool, str]:
    reset_daily_if_needed(row)
    row = get_user(row["user_id"])
    vip = is_vip(row)

    bonus = int(row["bonus_credits"] or 0)
    if bonus > 0:
        return True, f"🎁 Бонус-генерации: {bonus} (они тратятся первыми)."

    limit = VIP_DAILY_LIMIT if vip else FREE_DAILY_LIMIT
    used = int(row["used_count"] or 0)
    if used >= limit:
        if vip:
            return False, f"Лимит на сегодня: {used}/{limit} (VIP)."
        return False, f"Лимит на сегодня: {used}/{limit}. Завтра снова будет доступно."
    return True, f"Осталось сегодня: {limit - used}."

def consume_generation(row):
    reset_daily_if_needed(row)
    row = get_user(row["user_id"])
    bonus = int(row["bonus_credits"] or 0)
    with db() as conn:
        if bonus > 0:
            conn.execute("UPDATE users SET bonus_credits = bonus_credits - 1 WHERE user_id=?", (row["user_id"],))
        else:
            conn.execute(
                "UPDATE users SET used_count = used_count + 1, used_day=? WHERE user_id=?",
                (today_key_utc(), row["user_id"])
            )
        conn.commit()


# -------------------- UI --------------------
def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🖼 Фото"), KeyboardButton("🎬 Видео"), KeyboardButton("🤖 AI-помощник")],
            [KeyboardButton("🎁 Промт дня"), KeyboardButton("📆 Челлендж 30"), KeyboardButton("🎁 Пригласить")],
            [KeyboardButton("⭐ VIP (Stars)"), KeyboardButton("✅ IG"), KeyboardButton("ℹ️ Профиль")],
        ],
        resize_keyboard=True
    )

def fmt_header(title: str) -> str:
    return f"✨ <b>{title}</b>\n"

def share_keyboard(user_id: int):
    bot_un = BOT_USERNAME or "your_bot_username"
    deep = f"https://t.me/{bot_un}?start=ref_{user_id}"
    share_url = (
        "https://t.me/share/url?"
        f"url={quote(deep)}&text={quote('Забирай бот с промтами + генерацией фото/видео 👇')}"
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Поделиться (готовый шаблон)", url=share_url)],
        [InlineKeyboardButton("🔗 Открыть мою ссылку", url=deep)],
    ])

def channel_gate_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подписаться на канал", url=CHANNEL_INVITE_URL)],
        [InlineKeyboardButton("🔁 Я подписался — проверить", callback_data="check_channel")]
    ])

def instagram_gate_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Открыть Instagram", url=INSTAGRAM_URL)],
        [InlineKeyboardButton("✅ Я подписался — отправить @ник + скрин", callback_data="ig_request")]
    ])

def vip_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ Купить VIP за {VIP_PRICE_STARS} Stars", callback_data="vip_pay")],
    ])

async def safe_edit(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    except BadRequest as e:
        # If "message is not modified" or other edit problems — just send a new message
        if "Message is not modified" in str(e):
            await query.message.reply_text(
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        else:
            await query.message.reply_text(
                "⚠️ Не смог обновить сообщение, но я рядом. Попробуй ещё раз.",
                reply_markup=main_menu()
            )


# -------------------- GATES --------------------
async def is_subscribed_to_channel(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        # ptb 21.8 status is a string like "member"/"administrator"/"creator"/"restricted"/"left"/"kicked"
        status = getattr(member, "status", None)
        if status in ("member", "administrator", "creator", "restricted"):
            return True
        return False
    except Exception as e:
        log.warning("channel check failed: %s", e)
        return False if STRICT_CHANNEL_CHECK else True

def ig_status_text(row) -> str:
    if int(row["ig_verified"] or 0) == 1:
        return "подтверждён ✅"
    if int(row["ig_pending"] or 0) == 1:
        return "на проверке ⏳"
    return "не подтверждён ❌"

async def require_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    u = update.effective_user
    ensure_user(u)

    ok = await is_subscribed_to_channel(context.bot, u.id)
    if not ok:
        text = (
            fmt_header("Доступ закрыт")
            + "🔒 Чтобы пользоваться ботом, нужно быть подписанным на мой Telegram-канал.\n\n"
            "1) Нажми «Подписаться на канал»\n"
            "2) Затем «Я подписался — проверить»"
        )
        await update.effective_message.reply_text(
            text, reply_markup=channel_gate_keyboard(), parse_mode=ParseMode.HTML
        )
        return False

    row = get_user(u.id)
    if int(row["ig_verified"] or 0) != 1:
        text = (
            fmt_header("Ещё 1 шаг — Instagram")
            + "🔒 Подтверждение подписки на Instagram.\n\n"
            "Instagram не даёт надёжной авто-проверки подписки через бота.\n"
            "Поэтому ты отправляешь @ник + скрин.\n\n"
            "Нажми кнопку ниже 👇"
        )
        await update.effective_message.reply_text(
            text, reply_markup=instagram_gate_keyboard(), parse_mode=ParseMode.HTML
        )
        return False

    return True


# -------------------- OPENAI --------------------
def get_openai_client():
    if not (OPENAI_AVAILABLE and OPENAI_API_KEY):
        return None
    return OpenAI(api_key=OPENAI_API_KEY)

def openai_generate_image(prompt: str) -> tuple[bytes | None, str | None]:
    client = get_openai_client()
    if not client:
        return None, "OpenAI не настроен: нет OPENAI_API_KEY."

    try:
        res = client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024"
        )
        data0 = res.data[0]
        b64 = getattr(data0, "b64_json", None) or (data0.get("b64_json") if isinstance(data0, dict) else None)
        if not b64:
            return None, "Не пришли данные изображения (b64_json пуст)."
        return base64.b64decode(b64), None
    except Exception as e:
        return None, (
            "❌ Не удалось сгенерировать фото.\n\n"
            f"Ошибка: {e}\n\n"
            "Проверь:\n"
            "• ключ должен начинаться с sk- (не proj_)\n"
            "• в проекте включены Images/доступ к модели\n"
            "• включён billing/лимиты\n"
        )

def openai_assistant_reply(user_text: str) -> tuple[str | None, str | None]:
    client = get_openai_client()
    if not client:
        return None, "OpenAI не настроен: нет OPENAI_API_KEY."
    try:
        r = client.responses.create(
            model=OPENAI_MODEL,
            input=user_text,
        )
        # Try to extract text
        out = ""
        if hasattr(r, "output_text") and r.output_text:
            out = r.output_text
        else:
            # fallback: try common structure
            out = str(r)
        return out.strip()[:3500], None
    except Exception as e:
        return None, f"AI-помощник сейчас недоступен: {e}"

def openai_generate_video_stub(prompt: str) -> tuple[None, str]:
    return None, (
        "🎬 Видео (Sora) в этом деплое включим следующим шагом.\n\n"
        "Почему: у видео отдельный API/доступ и часто нужен отдельный enable в проекте.\n"
        "Я оставила кнопку и UX, чтобы всё было готово.\n\n"
        "Пока могу:\n"
        "• сделать супер-промт под Sora/Meta AI\n"
        "• собрать сценарий из кадров\n"
    )


# -------------------- COMMANDS --------------------
async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.effective_message.reply_text(
        f"🆔 Твой Telegram numeric id: <code>{u.id}</code>",
        parse_mode=ParseMode.HTML
    )

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

    # First: channel gate прямо на старте (как ты просила)
    ok = await is_subscribed_to_channel(context.bot, u.id)
    if not ok:
        text = (
            fmt_header("Привет! Я бот Кристины 🤍")
            + "Сначала — быстрая проверка подписки на канал.\n\n"
            "Нажми кнопку ниже 👇"
        )
        await update.effective_message.reply_text(
            text, reply_markup=channel_gate_keyboard(), parse_mode=ParseMode.HTML
        )
        return

    row = get_user(u.id)
    if int(row["ig_verified"] or 0) != 1:
        text = (
            fmt_header("Почти готово 🤍")
            + "Теперь подтвердим Instagram.\n\n"
            "Нажми кнопку ниже и отправь @ник + скрин 👇"
        )
        await update.effective_message.reply_text(
            text, reply_markup=instagram_gate_keyboard(), parse_mode=ParseMode.HTML
        )
        return

    # Full welcome
    row = get_user(u.id)
    ok2, left_msg = can_use_generation(row)
    text = (
        fmt_header("Добро пожаловать 🤍")
        + "Я умею:\n"
        "• 🖼 Генерировать фото\n"
        "• 🤖 AI-помощник (вопросы/идеи/промты)\n"
        "• 🎁 Промт дня + 📆 30-дневный челлендж\n"
        "• 🎁 Рефералка: приглашай друзей → получай бонус-генерации\n"
        "• ⭐ VIP за Telegram Stars\n\n"
        f"Статус: VIP {'✅' if is_vip(row) else '❌'} | IG: {ig_status_text(row)}\n"
        f"{left_msg}\n\n"
        "Выбирай кнопку 👇"
    )
    await update.effective_message.reply_text(
        text,
        reply_markup=main_menu(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    row = get_user(update.effective_user.id)
    ok, msg = can_use_generation(row)
    text = (
        fmt_header("Профиль")
        + f"• VIP: {'активен ✅' if is_vip(row) else 'нет ❌'}\n"
        + f"• VIP до: <b>{vip_until_text(row)}</b>\n"
        + f"• Instagram: <b>{ig_status_text(row)}</b>\n"
        + f"• Приглашено друзей: <b>{int(row['ref_count'] or 0)}</b>\n"
        + f"• Бонус-генерации: <b>{int(row['bonus_credits'] or 0)}</b>\n"
        + f"• Дневной лимит: free <b>{FREE_DAILY_LIMIT}</b> / VIP <b>{VIP_DAILY_LIMIT}</b>\n\n"
        + f"{msg}"
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu(), parse_mode=ParseMode.HTML)

async def vip_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    row = get_user(update.effective_user.id)
    if is_vip(row):
        text = (
            fmt_header("VIP активен ✅")
            + f"VIP до: <b>{vip_until_text(row)}</b>\n"
            + f"Лимит: <b>{VIP_DAILY_LIMIT}/день</b>\n\n"
            "Спасибо 🤍"
        )
        await update.effective_message.reply_text(text, reply_markup=main_menu(), parse_mode=ParseMode.HTML)
        return

    text = (
        fmt_header("VIP / Подписка ⭐")
        + "VIP даёт:\n"
        f"• до <b>{VIP_DAILY_LIMIT}</b> генераций/день\n"
        f"• срок <b>{VIP_DAYS} дней</b>\n\n"
        f"Цена: <b>{VIP_PRICE_STARS} Stars</b>\n\n"
        "Нажми кнопку оплаты 👇"
    )
    await update.effective_message.reply_text(
        text,
        reply_markup=vip_keyboard(),
        parse_mode=ParseMode.HTML
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    text = (
        fmt_header("Как пользоваться")
        + "1) 🖼 Фото → напиши описание → получишь картинку.\n"
        "2) 🤖 AI-помощник → задай вопрос/попроси промт/сценарий.\n"
        "3) 🎁 Пригласить → делись ссылкой → получаешь бонус-генерации.\n"
        "4) ⭐ VIP → оплатить Stars → всё открывается автоматически.\n\n"
        "Команды:\n"
        "• /start — старт\n"
        "• /myid — твой numeric id\n"
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu(), parse_mode=ParseMode.HTML)


# -------------------- ADMIN --------------------
async def ig_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /ig_ok <user_id>")
        return
    uid = int(context.args[0])
    with db() as conn:
        conn.execute("UPDATE users SET ig_verified=1, ig_pending=0 WHERE user_id=?", (uid,))
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
        conn.execute("UPDATE users SET ig_pending=0 WHERE user_id=?", (uid,))
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
    days = int(context.args[1]) if len(context.args) > 1 else VIP_DAYS
    until = now_utc() + timedelta(days=days)
    with db() as conn:
        conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until.isoformat(), uid))
        conn.commit()
    await update.message.reply_text(f"⭐️ VIP выдан для {uid} до {until.isoformat()}")
    try:
        await context.bot.send_message(uid, f"⭐️ VIP активирован до {until.isoformat()} 🎉", reply_markup=main_menu())
    except Exception:
        pass


# -------------------- MODES --------------------
async def set_mode_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    context.user_data["mode"] = "image"
    await update.effective_message.reply_text(
        fmt_header("Фото-генерация")
        + "Напиши описание (чем подробнее — тем лучше).\n\n"
        "<i>Пример:</i> ультра-реалистичный зимний fashion-портрет, мягкий свет, 8K, натуральная кожа…",
        parse_mode=ParseMode.HTML
    )

async def set_mode_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    context.user_data["mode"] = "video"
    await update.effective_message.reply_text(
        fmt_header("Видео-генерация")
        + "Напиши идею/сцену — я:\n"
        "• либо сгенерирую (если видео-API включено)\n"
        "• либо соберу идеальный промт + сценарий\n",
        parse_mode=ParseMode.HTML
    )

async def set_mode_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    context.user_data["mode"] = "ai"
    await update.effective_message.reply_text(
        fmt_header("AI-помощник 🤖")
        + "Пиши запрос: идеи Reels, промты Sora/HeyGen/Meta, сценарий, текст поста, оффер, хештеги.\n\n"
        "Я отвечу как твой личный продюсер/маркетолог/промт-инженер 💅",
        parse_mode=ParseMode.HTML
    )


# -------------------- TEXT/PHOTO HANDLERS --------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    u = update.effective_user
    ensure_user(u)

    # Menu buttons
    if txt == "🖼 Фото":
        return await set_mode_image(update, context)
    if txt == "🎬 Видео":
        return await set_mode_video(update, context)
    if txt == "🤖 AI-помощник":
        return await set_mode_ai(update, context)
    if txt == "ℹ️ Профиль":
        return await profile_cmd(update, context)
    if txt == "⭐ VIP (Stars)":
        return await vip_menu(update, context)
    if txt == "ℹ️ Помощь":
        return await help_cmd(update, context)
    if txt == "🎁 Пригласить":
        if not await require_access(update, context):
            return
        await update.effective_message.reply_text(
            fmt_header("Приглашай друзей — получай бонусы 🎁")
            + "За каждого друга по твоей ссылке ты получаешь +1 бонус-генерацию.\n\n"
            "Жми кнопку «Поделиться» — текст уже готов ✅",
            reply_markup=share_keyboard(u.id),
            parse_mode=ParseMode.HTML
        )
        return
    if txt == "✅ IG":
        await update.effective_message.reply_text(
            fmt_header("Instagram подтверждение")
            + "Нажми кнопку ниже и отправь @ник + скрин.\n",
            reply_markup=instagram_gate_keyboard(),
            parse_mode=ParseMode.HTML
        )
        return

    if txt == "🎁 Промт дня":
        if not await require_access(update, context):
            return
        prompts = [
            "Ультра-реалистичный fashion-портрет, морозные ресницы, 85mm, мягкий свет, 8K, детальная кожа.",
            "Кинематографичный зимний кадр, лёгкий снег, объёмный свет, реалистичная ткань, 4K.",
            "Editorial-фото, минимализм, чистый фон, натуральная кожа, high-end retouch.",
            "Reels-стиль: динамичный ракурс, лёгкий motion blur, реализм, естественные цвета, 4K.",
        ]
        idx = int(time.time() // 86400) % len(prompts)
        await update.effective_message.reply_text(
            fmt_header("Промт дня 🎁") + prompts[idx],
            reply_markup=main_menu(),
            parse_mode=ParseMode.HTML
        )
        return

    if txt == "📆 Челлендж 30":
        if not await require_access(update, context):
            return
        tasks = [
            "День 1: 3 варианта одного портрета (разный свет).",
            "День 2: Один кадр в 3 ракурсах (close/mid/full).",
            "День 3: Кожа: поры/текстура/без пластика.",
            "День 4: Снег/частицы: реалистичный snowfall и bokeh.",
            "День 5: Outfit-замена без изменения лица.",
        ]
        day_idx = int(time.time() // 86400) % len(tasks)
        await update.effective_message.reply_text(
            fmt_header("Челлендж 📆") + tasks[day_idx] + "\n\nХочешь — добавлю все 30 дней + прогресс ✅",
            reply_markup=main_menu(),
            parse_mode=ParseMode.HTML
        )
        return

    # If user is currently providing IG info after pressing button
    if context.user_data.get("await_ig_info"):
        handle = txt.strip()
        if handle.startswith("@"):
            handle = handle[1:]
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
                (u.id, handle, "handle received", now_utc().isoformat())
            )
            conn.execute("UPDATE users SET ig_pending=1 WHERE user_id=?", (u.id,))
            conn.commit()

        if AUTO_IG_VERIFY:
            with db() as conn:
                conn.execute("UPDATE users SET ig_verified=1, ig_pending=0 WHERE user_id=?", (u.id,))
                conn.execute("DELETE FROM ig_requests WHERE user_id=?", (u.id,))
                conn.commit()
            context.user_data["await_ig_info"] = False
            await update.effective_message.reply_text(
                fmt_header("Готово ✅")
                + "Instagram подтвержден (auto). Доступ открыт 🎉\n\nВыбирай кнопку 👇",
                reply_markup=main_menu(),
                parse_mode=ParseMode.HTML
            )
            return

        context.user_data["await_ig_info"] = False
        await update.effective_message.reply_text(
            fmt_header("Заявка принята ✅")
            + "Я проверю и открою доступ.\n\n"
            "Если нужно быстрее — напиши мне: «проверь IG в боте».",
            reply_markup=main_menu(),
            parse_mode=ParseMode.HTML
        )
        if ADMIN_USER_ID:
            try:
                await context.bot.send_message(
                    ADMIN_USER_ID,
                    f"IG-заявка: user_id={u.id}, username=@{u.username}, ig=@{handle}\n"
                    f"Подтверди: /ig_ok {u.id}  |  Отклонить: /ig_no {u.id}"
                )
            except Exception:
                pass
        return

    # Normal flow needs access
    if not await require_access(update, context):
        return

    mode = context.user_data.get("mode")

    if mode in ("image", "video", "ai") and txt:
        row = get_user(u.id)

        # AI helper does NOT consume generations
        if mode == "ai":
            await update.effective_message.reply_text("🤖 Думаю…")
            ans, err = openai_assistant_reply(txt)
            if err:
                await update.effective_message.reply_text(err, reply_markup=main_menu())
            else:
                await update.effective_message.reply_text(ans, reply_markup=main_menu())
            return

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
            _, err = openai_generate_video_stub(txt)
            await update.effective_message.reply_text(err, reply_markup=main_menu())

        context.user_data["mode"] = None
        return

    await update.effective_message.reply_text(
        "Выбирай кнопку в меню 👇",
        reply_markup=main_menu()
    )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)

    # If waiting for IG proof
    if context.user_data.get("await_ig_proof"):
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
                (u.id, "", "photo proof received", now_utc().isoformat())
            )
            conn.execute("UPDATE users SET ig_pending=1 WHERE user_id=?", (u.id,))
            conn.commit()

        context.user_data["await_ig_proof"] = False

        if AUTO_IG_VERIFY:
            with db() as conn:
                conn.execute("UPDATE users SET ig_verified=1, ig_pending=0 WHERE user_id=?", (u.id,))
                conn.execute("DELETE FROM ig_requests WHERE user_id=?", (u.id,))
                conn.commit()
            await update.effective_message.reply_text(
                fmt_header("Готово ✅")
                + "Instagram подтвержден (auto). Доступ открыт 🎉",
                reply_markup=main_menu(),
                parse_mode=ParseMode.HTML
            )
            return

        await update.effective_message.reply_text(
            fmt_header("Принято ✅")
            + "Я подтвержу и открою доступ.\n\n"
            "Если нужно быстрее — напиши мне: «проверь IG в боте».",
            reply_markup=main_menu(),
            parse_mode=ParseMode.HTML
        )
        if ADMIN_USER_ID:
            try:
                await context.bot.send_message(
                    ADMIN_USER_ID,
                    f"IG-заявка (скрин): user_id={u.id}, username=@{u.username}\n"
                    f"Подтверди: /ig_ok {u.id}  |  Отклонить: /ig_no {u.id}"
                )
            except Exception:
                pass
        return

    if not await require_access(update, context):
        return

    await update.effective_message.reply_text(
        "Фото получил ✅\n"
        "Если хочешь — добавлю режим: «загрузить фото → сделать промт/оживление под Sora/Meta/HeyGen».",
        reply_markup=main_menu()
    )


# -------------------- CALLBACK BUTTONS --------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    u = update.effective_user
    ensure_user(u)

    if query.data == "check_channel":
        ok = await is_subscribed_to_channel(context.bot, u.id)
        if ok:
            await safe_edit(
                query,
                fmt_header("Канал подтверждён ✅") + "Теперь подтвердим Instagram 👇",
                reply_markup=instagram_gate_keyboard()
            )
        else:
            await safe_edit(
                query,
                fmt_header("Подписка не найдена 😔")
                + "Подпишись на канал и нажми «проверить» ещё раз.",
                reply_markup=channel_gate_keyboard()
            )
        return

    if query.data == "ig_request":
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
                (u.id, "", "requested via button", now_utc().isoformat())
            )
            conn.execute("UPDATE users SET ig_pending=1 WHERE user_id=?", (u.id,))
            conn.commit()

        await safe_edit(
            query,
            fmt_header("Подтверждение Instagram ✅")
            + "Отправь одним сообщением:\n"
            "1) твой <b>@ник</b> в Instagram\n"
            "2) потом можешь отправить <b>скрин</b> (если есть)\n\n"
            "После этого доступ откроется.",
            reply_markup=None
        )
        context.user_data["await_ig_info"] = True
        context.user_data["await_ig_proof"] = True
        return

    if query.data == "vip_pay":
        # send invoice in Stars (XTR)
        # provider_token is empty string for Stars in many libs; Bot API uses XTR currency.
        payload = f"vip:{u.id}:{int(time.time())}"
        prices = [LabeledPrice(label=f"VIP {VIP_DAYS} дней", amount=VIP_PRICE_STARS)]
        try:
            await context.bot.send_invoice(
                chat_id=u.id,
                title="VIP подписка ⭐",
                description=f"VIP на {VIP_DAYS} дней: до {VIP_DAILY_LIMIT}/день + приоритет.",
                payload=payload,
                provider_token="",  # Stars
                currency="XTR",
                prices=prices,
                start_parameter="vip",
            )
            with db() as conn:
                conn.execute(
                    "INSERT INTO payments (user_id, payload, stars, status, created_at) VALUES (?, ?, ?, ?, ?)",
                    (u.id, payload, VIP_PRICE_STARS, "invoice_sent", now_utc().isoformat())
                )
                conn.commit()
        except Exception as e:
            await query.message.reply_text(f"Не смог отправить счёт: {e}")
        return


# -------------------- PAYMENTS --------------------
async def on_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    try:
        await q.answer(ok=True)
    except Exception:
        pass

async def on_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)

    sp = update.effective_message.successful_payment
    payload = sp.invoice_payload if sp else ""
    stars = (sp.total_amount if sp else 0)

    with db() as conn:
        conn.execute(
            "INSERT INTO payments (user_id, payload, stars, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (u.id, payload, stars, "paid", now_utc().isoformat())
        )
        conn.commit()

    if payload.startswith("vip:"):
        until = now_utc() + timedelta(days=VIP_DAYS)
        with db() as conn:
            conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until.isoformat(), u.id))
            conn.commit()
        await update.effective_message.reply_text(
            fmt_header("Оплата прошла ✅")
            + f"VIP активирован до <b>{until.isoformat()}</b>\n\n"
            "Спасибо 🤍",
            reply_markup=main_menu(),
            parse_mode=ParseMode.HTML
        )
        return

    await update.effective_message.reply_text(
        "Оплата прошла ✅",
        reply_markup=main_menu()
    )


# -------------------- ERRORS --------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Что-то пошло не так, но я уже чиню. Нажми /start",
            )
    except Exception:
        pass


# -------------------- FASTAPI ROUTES --------------------
@app.get("/", response_class=PlainTextResponse)
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

    # commands
    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler("help", help_cmd))
    tg_app.add_handler(CommandHandler("myid", myid_cmd))

    # admin
    tg_app.add_handler(CommandHandler("ig_ok", ig_ok))
    tg_app.add_handler(CommandHandler("ig_no", ig_no))
    tg_app.add_handler(CommandHandler("vip_add", vip_add))

    # payments
    tg_app.add_handler(PreCheckoutQueryHandler(on_precheckout))
    tg_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_successful_payment))

    # callbacks, photos, texts
    tg_app.add_handler(CallbackQueryHandler(on_button))
    tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    tg_app.add_error_handler(on_error)

    await tg_app.initialize()
    await tg_app.start()

    me = await tg_app.bot.get_me()
    BOT_USERNAME = me.username
    log.info("Bot username: %s", BOT_USERNAME)

    if WEBHOOK_BASE:
        url = WEBHOOK_BASE + WEBHOOK_PATH
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
