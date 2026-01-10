import os
import re
import io
import time
import base64
import json
import asyncio
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("gurenko-bot")

# ----------------------------
# ENV
# ----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # e.g. https://xxx.onrender.com/webhook
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()  # e.g. @gurenko_kristina_ai or -100xxxxxxxxxx

OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1").strip()
OPENAI_VIDEO_MODEL = os.getenv("OPENAI_VIDEO_MODEL", "sora-2").strip()
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4.1-mini").strip()  # для "промт по фото"

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or "0")  # твой телеграм id (для /grantvip и /diag)

TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "10") or "10")  # Владивосток/Приморье +10
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))

# Лимиты
FREE_DAILY_GENERATIONS = 1          # 1 в день бесплатно (фото ИЛИ видео)
VIP_DAILY_GENERATIONS = 50          # для VIP (можешь менять)
VIP_DURATION_DAYS = 30              # VIP на 30 дней

DB_PATH = os.getenv("DB_PATH", "bot.db")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL is not set")
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY is not set -> генерации работать не будут")

# ----------------------------
# DB
# ----------------------------
_db_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

def db_init():
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            referred_by INTEGER,
            created_at TEXT,
            vip_until TEXT,
            gen_credits INTEGER DEFAULT 0
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            referrer_id INTEGER,
            invited_id INTEGER,
            created_at TEXT,
            UNIQUE(referrer_id, invited_id)
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS usage_daily (
            user_id INTEGER,
            day TEXT,
            used INTEGER,
            PRIMARY KEY(user_id, day)
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS saved_prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            prompt TEXT,
            created_at TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS challenge (
            user_id INTEGER PRIMARY KEY,
            day INTEGER DEFAULT 1,
            started_at TEXT,
            updated_at TEXT
        )
        """)
        _conn.commit()

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)

def today_key() -> str:
    return now_local().strftime("%Y-%m-%d")

def upsert_user(u: Update):
    user = u.effective_user
    if not user:
        return
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("""
        INSERT INTO users(user_id, username, first_name, created_at)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
        """, (user.id, user.username or "", user.first_name or "", now_local().isoformat()))
        _conn.commit()

def get_user(user_id: int):
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return cur.fetchone()

def set_referred_by(user_id: int, referrer_id: int) -> bool:
    """Set referred_by only if user has none. Return True if set now."""
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return False
        if row["referred_by"]:
            return False
        cur.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer_id, user_id))
        _conn.commit()
        return True

def add_referral(referrer_id: int, invited_id: int) -> bool:
    """Insert referral relation once. Return True if inserted."""
    with _db_lock:
        cur = _conn.cursor()
        try:
            cur.execute(
                "INSERT INTO referrals(referrer_id, invited_id, created_at) VALUES(?,?,?)",
                (referrer_id, invited_id, now_local().isoformat())
            )
            _conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def count_referrals(referrer_id: int) -> int:
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (referrer_id,))
        return int(cur.fetchone()["c"])

def add_gen_credits(user_id: int, amount: int):
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("UPDATE users SET gen_credits = COALESCE(gen_credits,0) + ? WHERE user_id=?",
                    (amount, user_id))
        _conn.commit()

def set_vip_until(user_id: int, until_dt: datetime):
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("UPDATE users SET vip_until=? WHERE user_id=?",
                    (until_dt.isoformat(), user_id))
        _conn.commit()

def is_vip(user_id: int) -> bool:
    row = get_user(user_id)
    if not row:
        return False
    vip_until = row["vip_until"]
    if not vip_until:
        return False
    try:
        dt = datetime.fromisoformat(vip_until)
        return dt > now_local()
    except Exception:
        return False

def daily_used(user_id: int) -> int:
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("SELECT used FROM usage_daily WHERE user_id=? AND day=?",
                    (user_id, today_key()))
        row = cur.fetchone()
        return int(row["used"]) if row else 0

def set_daily_used(user_id: int, used: int):
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("""
        INSERT INTO usage_daily(user_id, day, used)
        VALUES(?,?,?)
        ON CONFLICT(user_id, day) DO UPDATE SET used=excluded.used
        """, (user_id, today_key(), used))
        _conn.commit()

def consume_generation(user_id: int) -> tuple[bool, str]:
    """
    True if allowed and consumed. Logic:
    - VIP: daily quota VIP_DAILY_GENERATIONS
    - Free: daily quota FREE_DAILY_GENERATIONS
    - If daily exceeded, try gen_credits (ref bonus).
    """
    row = get_user(user_id)
    if not row:
        return False, "Пользователь не найден в базе."

    used = daily_used(user_id)
    quota = VIP_DAILY_GENERATIONS if is_vip(user_id) else FREE_DAILY_GENERATIONS

    if used < quota:
        set_daily_used(user_id, used + 1)
        return True, f"✅ Лимит: {used+1}/{quota} за сегодня."

    credits = int(row["gen_credits"] or 0)
    if credits > 0:
        # consume credit
        with _db_lock:
            cur = _conn.cursor()
            cur.execute("UPDATE users SET gen_credits = gen_credits - 1 WHERE user_id=?", (user_id,))
            _conn.commit()
        return True, f"✅ Использован бонусный кредит. Осталось: {credits-1}."

    return False, f"⛔️ Лимит на сегодня исчерпан ({quota}/{quota}).\n\n💎 Хочешь больше — VIP на 30 дней."

def save_prompt(user_id: int, title: str, prompt: str):
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("""
        INSERT INTO saved_prompts(user_id, title, prompt, created_at)
        VALUES(?,?,?,?)
        """, (user_id, title[:80], prompt, now_local().isoformat()))
        _conn.commit()

def list_prompts(user_id: int, limit: int = 10):
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("""
        SELECT id, title, created_at FROM saved_prompts
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
        """, (user_id, limit))
        return cur.fetchall()

def get_prompt(user_id: int, prompt_id: int):
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("""
        SELECT * FROM saved_prompts
        WHERE user_id=? AND id=?
        """, (user_id, prompt_id))
        return cur.fetchone()

def challenge_get(user_id: int):
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("SELECT * FROM challenge WHERE user_id=?", (user_id,))
        return cur.fetchone()

def challenge_start(user_id: int):
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("""
        INSERT INTO challenge(user_id, day, started_at, updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            day=1,
            started_at=excluded.started_at,
            updated_at=excluded.updated_at
        """, (user_id, 1, today_key(), today_key()))
        _conn.commit()

def challenge_advance(user_id: int):
    with _db_lock:
        cur = _conn.cursor()
        cur.execute("""
        INSERT INTO challenge(user_id, day, started_at, updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            day=challenge.day + 1,
            updated_at=excluded.updated_at
        """, (user_id, 1, today_key(), today_key()))
        _conn.commit()

# ----------------------------
# Content: Prompt of the day & Challenge 30 days
# ----------------------------
PROMPT_OF_DAY = [
    # Можно расширять сколько хочешь
    "Ультра-реалистичный портрет, fashion-editorial, зимний свет, микротекстура кожи, натуральные поры, без пластика.",
    "Кинематографичный кадр: тёплый интерьер, мягкий боке, естественный шум плёнки, живые тени, настоящая кожа.",
    "Улица ночь: неоновые отражения на мокром асфальте, контрастный свет, без кукольного лица, реализм.",
    "Глянцевый beauty-close-up: ресницы со снежинками, морозная дымка, высокая детализация, без искажений.",
    "Портрет на 85mm: натуральный цвет кожи, без сглаживания, мягкий rim light, editorial-стиль.",
]

CHALLENGE_30 = [
    {"title": "Реалистичная кожа", "task": "Сделай фото, где кожа выглядит как в жизни: поры, лёгкий пушок, микротекстура.", "hint": "Добавь: micro skin texture, realistic pores, no doll skin."},
    {"title": "Естественный свет", "task": "Сделай фото с мягким дневным светом из окна + правильные тени.", "hint": "Добавь: soft window light, natural shadows."},
    {"title": "Кино-кадр", "task": "Кинематографичный портрет: глубина резкости, лёгкое зерно, драматичный свет.", "hint": "Добавь: cinematic grading, subtle film grain."},
    {"title": "Ночь/неон", "task": "Улица ночью + неоновые отражения, реализм лица без пластика.", "hint": "Добавь: wet asphalt reflections, neon glow."},
    {"title": "Снег и детали", "task": "Зимний кадр со снегом на волосах/одежде, без “игрушечной” фактуры.", "hint": "Добавь: snow particles, realistic fabric weave."},
    {"title": "Видео 4 секунды", "task": "Сгенерируй видео 4 сек: лёгкое движение камеры (пан/тилт), естественная мимика.", "hint": "Добавь: subtle camera movement, natural facial motion."},
    {"title": "Говорящая голова (сцена)", "task": "Видео: персонаж говорит 1–2 фразы, движение губ естественное.", "hint": "Добавь: realistic lip motion, calm breathing."},
    {"title": "Рекламный кадр", "task": "Сделай картинку как бренд-реклама: чистый фон, премиум-свет, аккуратный стиль.", "hint": "Добавь: studio softbox lighting, premium look."},
    {"title": "Детали ткани", "task": "Фото с акцентом на ткань: шуба/куртка/шарф, видны волокна.", "hint": "Добавь: detailed fabric texture, visible fibers."},
    {"title": "Сторителлинг кадра", "task": "Сделай кадр, где есть история: взгляд, действие, эмоция.", "hint": "Добавь: candid moment, authentic emotion."},
    # 11–30 (можешь менять под свой стиль)
    {"title": "Крупный план (beauty)", "task": "Супер-крупный план лица: глаза/ресницы/кожа — реализм.", "hint": "85mm, macro detail, no over-smoothing."},
    {"title": "Портрет + контровой свет", "task": "Контровой свет по волосам, мягкие тени на лице.", "hint": "rim light, soft shadows."},
    {"title": "Снегопад в движении", "task": "Видео: снежинки летят, камера чуть двигается.", "hint": "falling snow particles, gentle pan."},
    {"title": "Тёплый интерьер", "task": "Фото в тёплом свете: лампы, уют, естественные цвета.", "hint": "warm tungsten lighting, cozy mood."},
    {"title": "Глянец/журнал", "task": "Журнальная подача: поза, свет, чистый фон.", "hint": "editorial pose, glossy magazine."},
    {"title": "Сцена “до/после”", "task": "Сделай 2 варианта промта: обычный и PRO (с негативом и настройками).", "hint": "Сравни результат."},
    {"title": "Хук для Reels", "task": "Придумай хук 1–2 секунды под свой стиль + текст на экране.", "hint": "коротко и резко."},
    {"title": "Разбор ролика", "task": "Возьми свой ролик и выпиши 3 улучшения: хук/монтаж/CTA.", "hint": "Пиши конкретно."},
    {"title": "Серия 3 кадров", "task": "Сделай 3 фото в одном стиле (цвет, свет, камера).", "hint": "consistency, same lens."},
    {"title": "Вариативность 3 промта", "task": "Один сюжет — 3 варианта промта (свет/камера/стиль).", "hint": "вариации."},
    {"title": "Стрит-фото", "task": "Фото как случайный снимок на улице, но красиво.", "hint": "candid street photo."},
    {"title": "Тени на лице", "task": "Фото с интересными тенями (жалюзи/ветки/окно).", "hint": "patterned shadows."},
    {"title": "Свет от витрины", "task": "Ночной кадр: свет от витрины/фонаря, реализм.", "hint": "shop window light."},
    {"title": "Мини-сцена 4 сек", "task": "Видео: шаг/поворот головы/улыбка — естественно.", "hint": "subtle motion."},
    {"title": "Боке и глубина", "task": "Портрет с красивым боке и резкостью по глазам.", "hint": "shallow depth of field."},
    {"title": "Ретушь без пластика", "task": "Улучши лицо, но оставь кожу живой.", "hint": "no plastic skin."},
    {"title": "Стиль “как у Кристины”", "task": "Собери шаблон: хук → промт → настройки → подпись → 5 тегов.", "hint": "в одном сообщении."},
    {"title": "Шеринг", "task": "Сделай такой результат, чтобы хотелось отправить другу (вау-идея).", "hint": "концепт > техника."},
    {"title": "Финал", "task": "Сделай лучший ролик недели: коротко, сильно, с CTA.", "hint": "закрепи результат."},
]

# ----------------------------
# OpenAI client
# ----------------------------
oa_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

async def run_in_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)

def openai_generate_image(prompt: str) -> bytes:
    """
    Returns image bytes (PNG/JPG depending).
    Uses Images API, which returns base64 for GPT image models. :contentReference[oaicite:3]{index=3}
    """
    if not oa_client:
        raise RuntimeError("OPENAI_API_KEY not set")
    res = oa_client.images.generate(
        model=OPENAI_IMAGE_MODEL,
        prompt=prompt,
        size="1024x1024",
    )
    b64 = res.data[0].b64_json
    return base64.b64decode(b64)

def openai_create_video(prompt: str, seconds: str = "4", size: str = "720x1280") -> bytes:
    """
    Create video job and download content.
    API: videos.create / videos.retrieve / videos.download_content :contentReference[oaicite:4]{index=4}
    """
    if not oa_client:
        raise RuntimeError("OPENAI_API_KEY not set")

    job = oa_client.videos.create(
        model=OPENAI_VIDEO_MODEL,
        prompt=prompt,
        seconds=seconds,   # "4" | "8" | "12" (по докам) :contentReference[oaicite:5]{index=5}
        size=size,         # "720x1280" | "1280x720" | ...
    )

    # poll until done
    video_id = job.id
    for _ in range(60):  # ~до 60 попыток
        time.sleep(2)
        st = oa_client.videos.retrieve(video_id)
        if st.status in ("succeeded", "failed", "cancelled"):
            job = st
            break

    if job.status != "succeeded":
        err = getattr(job, "error", None)
        raise RuntimeError(f"Video job failed: status={job.status} error={err}")

    response = oa_client.videos.download_content(video_id=video_id)
    content = response.read()
    return content

def openai_prompt_from_photo(image_bytes: bytes, goal_text: str) -> str:
    """
    Создаёт пакет: prompt + negative + settings.
    Делается через Responses (vision).
    """
    if not oa_client:
        raise RuntimeError("OPENAI_API_KEY not set")

    data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("utf-8")

    system = (
        "Ты — эксперт по промтам для Sora/Meta/HeyGen. "
        "Сделай ответ строго структурированно:\n"
        "1) PROMPT (для фото)\n"
        "2) PROMPT (для видео)\n"
        "3) NEGATIVE PROMPT\n"
        "4) 3 настройки (качество/свет/камера)\n"
        "Пиши по-русски, коротко, но мощно."
    )

    user = (
        f"На фото человек/сцена. Цель пользователя: {goal_text}\n"
        f"Сохрани реализм, не меняй личность. Укажи анти-кукла и анти-искажения."
    )

    resp = oa_client.responses.create(
        model=OPENAI_TEXT_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": system}]},
            {"role": "user", "content": [
                {"type": "input_text", "text": user},
                {"type": "input_image", "image_url": data_url},
            ]},
        ],
    )
    return resp.output_text

# ----------------------------
# Telegram UI
# ----------------------------
def main_menu_kb(bot_username: str, user_id: int) -> InlineKeyboardMarkup:
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    share_url = "https://t.me/share/url?url=" + quote(ref_link) + "&text=" + quote("Забирай бота с промтами и генерацией 👇")
    kb = [
        [InlineKeyboardButton("🧠 Сделай промт по фото", callback_data="p_photo")],
        [InlineKeyboardButton("🖼️ Сгенерировать ФОТО (Sora)", callback_data="gen_img")],
        [InlineKeyboardButton("🎬 Сгенерировать ВИДЕО (Sora)", callback_data="gen_vid")],
        [InlineKeyboardButton("🎁 Промт дня", callback_data="pod")],
        [InlineKeyboardButton("🏆 Челлендж 30 дней", callback_data="ch_menu")],
        [InlineKeyboardButton("📌 Мои промты", callback_data="my_prompts")],
        [InlineKeyboardButton("👥 Пригласить друга (бонусы)", callback_data="ref")],
        [InlineKeyboardButton("📤 Поделиться ботом", url=share_url)],
        [InlineKeyboardButton("💎 VIP на 30 дней", callback_data="vip")],
    ]
    return InlineKeyboardMarkup(kb)

def subscribe_kb() -> InlineKeyboardMarkup:
    if REQUIRED_CHANNEL.startswith("@"):
        url = f"https://t.me/{REQUIRED_CHANNEL[1:]}"
    else:
        url = "https://t.me/"  # fallback
    kb = [
        [InlineKeyboardButton("✅ Подписаться на канал", url=url)],
        [InlineKeyboardButton("🔄 Проверить подписку", callback_data="check_sub")],
    ]
    return InlineKeyboardMarkup(kb)

async def is_channel_member(bot, user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True  # если канал не задан — не блокируем
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception as e:
        logger.warning(f"get_chat_member failed: {e}")
        return False

async def gate_or_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if allowed, else show subscribe message and return False."""
    uid = update.effective_user.id
    ok = await is_channel_member(context.bot, uid)
    if ok:
        return True

    text = (
        "🔒 Доступ к генерации и премиум-функциям — только для подписчиков канала.\n\n"
        "Подпишись и нажми «Проверить подписку» ✅"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=subscribe_kb())
    else:
        await update.message.reply_text(text, reply_markup=subscribe_kb())
    return False

async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str | None = None):
    me = await context.bot.get_me()
    bot_username = me.username
    uid = update.effective_user.id
    row = get_user(uid)
    vip_flag = "💎 VIP активен" if is_vip(uid) else "🆓 Free"
    credits = int(row["gen_credits"] or 0) if row else 0

    header = text or "Выбери действие 👇"
    status = f"\n\nСтатус: {vip_flag}\nСегодня использовано: {daily_used(uid)}/{VIP_DAILY_GENERATIONS if is_vip(uid) else FREE_DAILY_GENERATIONS}\nБонус-кредиты: {credits}"
    await update.effective_message.reply_text(header + status, reply_markup=main_menu_kb(bot_username, uid))

# ----------------------------
# Handlers
# ----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)

    uid = update.effective_user.id
    args = context.args or []
    if args:
        m = re.match(r"ref_(\d+)", args[0])
        if m:
            referrer_id = int(m.group(1))
            if referrer_id != uid:
                # set referred_by once
                if set_referred_by(uid, referrer_id):
                    inserted = add_referral(referrer_id, uid)
                    if inserted:
                        # rewards:
                        # 1 invite -> +5 generation credits
                        # 3 invites -> VIP 3 days
                        c = count_referrals(referrer_id)
                        if c == 1:
                            add_gen_credits(referrer_id, 5)
                        if c == 3:
                            set_vip_until(referrer_id, now_local() + timedelta(days=3))

    await send_menu(update, context, text="Привет! Я бот Кристины 🤍\nПромты + генерация Фото/Видео + челленджи.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — меню\n"
        "/balance — статус/лимиты\n"
        "/diag — диагностика (для админа)\n"
    )

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    row = get_user(uid)
    if not row:
        await update.message.reply_text("Нет данных. Нажми /start")
        return
    vip = is_vip(uid)
    vip_until = row["vip_until"] or "-"
    used = daily_used(uid)
    quota = VIP_DAILY_GENERATIONS if vip else FREE_DAILY_GENERATIONS
    credits = int(row["gen_credits"] or 0)
    await update.message.reply_text(
        f"Статус: {'VIP 💎' if vip else 'Free 🆓'}\n"
        f"VIP до: {vip_until}\n"
        f"Сегодня: {used}/{quota}\n"
        f"Бонус-кредиты: {credits}\n"
    )

async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if ADMIN_USER_ID and uid != ADMIN_USER_ID:
        await update.message.reply_text("⛔️ Нет доступа.")
        return

    msg = []
    msg.append(f"OPENAI_IMAGE_MODEL={OPENAI_IMAGE_MODEL}")
    msg.append(f"OPENAI_VIDEO_MODEL={OPENAI_VIDEO_MODEL}")
    msg.append(f"OPENAI_TEXT_MODEL={OPENAI_TEXT_MODEL}")
    msg.append(f"API key set: {'YES' if bool(OPENAI_API_KEY) else 'NO'}")

    # Try list models (best-effort)
    if oa_client:
        try:
            models = oa_client.models.list()
            names = [m.id for m in models.data]
            msg.append(f"Models visible: {len(names)}")
            msg.append(f"Has image model? {'YES' if OPENAI_IMAGE_MODEL in names else 'NO/UNKNOWN'}")
            msg.append(f"Has video model? {'YES' if OPENAI_VIDEO_MODEL in names else 'NO/UNKNOWN'}")
        except Exception as e:
            msg.append(f"models.list failed: {repr(e)}")

    await update.message.reply_text("\n".join(msg))

async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    uid = update.effective_user.id
    upsert_user(update)

    data = q.data

    if data == "check_sub":
        ok = await is_channel_member(context.bot, uid)
        if ok:
            await q.message.reply_text("✅ Подписка подтверждена! Открываю меню.")
            await send_menu(update, context)
        else:
            await q.message.reply_text("⛔️ Я всё ещё не вижу подписку. Проверь, что подписалась на канал и попробуй снова.")
        return

    # Gate most actions
    if data in ("gen_img", "gen_vid", "p_photo", "my_prompts", "ch_menu", "ref", "vip"):
        allowed = await gate_or_menu(update, context)
        if not allowed:
            return

    if data == "pod":
        # Prompt of day доступен всем (можешь тоже загейтить)
        i = int(now_local().strftime("%j")) % len(PROMPT_OF_DAY)
        p = PROMPT_OF_DAY[i]
        text = (
            "🎁 *Промт дня*\n\n"
            f"`{p}`\n\n"
            "Негатив:\n"
            "`doll face, plastic skin, over-smoothing, deformed hands, extra fingers, bad anatomy, blur`\n\n"
            "Настройки:\n"
            "• Качество: max / 4K\n• Свет: soft + natural shadows\n• Камера: 85mm, shallow depth of field"
        )
        await q.message.reply_text(text, parse_mode="Markdown")
        return

    if data == "gen_img":
        ok, info = consume_generation(uid)
        if not ok:
            await q.message.reply_text(info)
            return
        context.user_data["awaiting"] = "img_prompt"
        await q.message.reply_text(
            "🖼️ Напиши промт для ФОТО.\n\n"
            "Пример: *ультра-реалистичный зимний fashion-editorial портрет, 85mm, micro skin texture…*",
            parse_mode="Markdown"
        )
        await q.message.reply_text(info)
        return

    if data == "gen_vid":
        ok, info = consume_generation(uid)
        if not ok:
            await q.message.reply_text(info)
            return
        context.user_data["awaiting"] = "vid_prompt"
        await q.message.reply_text(
            "🎬 Напиши промт для ВИДЕО.\n\n"
            "Я сделаю клип 4 секунды (вертикальный 720×1280).",
            parse_mode="Markdown"
        )
        await q.message.reply_text(info)
        return

    if data == "p_photo":
        context.user_data["awaiting"] = "photo_for_prompt"
        await q.message.reply_text(
            "🧠 Пришли фото (как документ или обычным фото).\n"
            "После фото я спрошу: *что хочешь получить?* и соберу промт + негатив + настройки."
        )
        return

    if data == "ref":
        me = await context.bot.get_me()
        bot_username = me.username
        ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"
        c = count_referrals(uid)
        await q.message.reply_text(
            "👥 *Реферальная программа*\n\n"
            f"Твоя ссылка:\n{ref_link}\n\n"
            "Награды:\n"
            "• 1 приглашённый → +5 генераций (кредиты)\n"
            "• 3 приглашённых → VIP на 3 дня\n\n"
            f"Приглашено: {c}",
            parse_mode="Markdown"
        )
        return

    if data == "vip":
        await q.message.reply_text(
            "💎 *VIP на 30 дней*\n\n"
            "Что даёт:\n"
            f"• до {VIP_DAILY_GENERATIONS} генераций/день\n"
            "• PRO-шаблоны промтов\n"
            "• быстрые разборы\n\n"
            "Оплата Stars/магазин можно подключить отдельно.\n"
            "Пока что VIP выдаётся вручную админом командой /grantvip (если хочешь — добавлю оплату позже).",
            parse_mode="Markdown"
        )
        return

    if data == "my_prompts":
        rows = list_prompts(uid, limit=10)
        if not rows:
            await q.message.reply_text("📌 У тебя пока нет сохранённых промтов.\nПосле выдачи промта нажимай «Сохранить».")
            return
        lines = ["📌 *Мои промты* (последние 10):\n"]
        for r in rows:
            lines.append(f"• #{r['id']}: {r['title']} ({r['created_at'][:10]})")
        lines.append("\nЧтобы открыть: отправь команду `#ID` (например `#12`).")
        await q.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if data == "ch_menu":
        st = challenge_get(uid)
        if not st:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Старт", callback_data="ch_start")]])
            await q.message.reply_text("🏆 Челлендж 30 дней.\nНажми «Старт» и каждый день делай задание.", reply_markup=kb)
        else:
            day = int(st["day"])
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Готово → следующий день", callback_data="ch_done")],
                [InlineKeyboardButton("📄 Показать задание", callback_data="ch_show")],
            ])
            await q.message.reply_text(f"🏆 Ты в челлендже. Текущий день: {day}/30", reply_markup=kb)
        return

    if data == "ch_start":
        challenge_start(uid)
        await send_challenge_day(update, context, 1)
        return

    if data == "ch_show":
        st = challenge_get(uid)
        day = int(st["day"]) if st else 1
        await send_challenge_day(update, context, day)
        return

    if data == "ch_done":
        st = challenge_get(uid)
        day = int(st["day"]) if st else 1
        if day >= 30:
            await q.message.reply_text("🎉 Ты прошла челлендж 30/30! Хочешь — сделаю “следующий сезон” челленджа.")
            return
        challenge_advance(uid)
        await send_challenge_day(update, context, day + 1)
        return

async def send_challenge_day(update: Update, context: ContextTypes.DEFAULT_TYPE, day: int):
    day = max(1, min(30, day))
    item = CHALLENGE_30[day - 1]
    text = (
        f"🏆 *День {day}/30 — {item['title']}*\n\n"
        f"{item['task']}\n\n"
        f"Подсказка: `{item['hint']}`"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Готово → следующий день", callback_data="ch_done")]])
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

async def cmd_grantvip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if ADMIN_USER_ID and uid != ADMIN_USER_ID:
        await update.message.reply_text("⛔️ Нет доступа.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /grantvip <user_id> <days>")
        return
    target = int(context.args[0])
    days = int(context.args[1])
    set_vip_until(target, now_local() + timedelta(days=days))
    await update.message.reply_text(f"✅ VIP выдан пользователю {target} на {days} дней.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    upsert_user(update)

    # open saved prompt by #ID
    m = re.match(r"#(\d+)", (update.message.text or "").strip())
    if m:
        if not await gate_or_menu(update, context):
            return
        pid = int(m.group(1))
        row = get_prompt(uid, pid)
        if not row:
            await update.message.reply_text("Не найдено.")
            return
        await update.message.reply_text(f"📌 *{row['title']}*\n\n{row['prompt']}", parse_mode="Markdown")
        return

    awaiting = context.user_data.get("awaiting")

    if awaiting == "img_prompt":
        prompt = update.message.text.strip()
        context.user_data["awaiting"] = None
        await update.message.reply_text("⏳ Генерирую фото…")
        try:
            img_bytes = await run_in_thread(openai_generate_image, prompt)
            await update.message.reply_photo(photo=img_bytes, caption="🖼️ Готово!")
            # offer save
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Сохранить в мои промты", callback_data="save_last")],
            ])
            context.user_data["last_prompt"] = prompt
            await update.message.reply_text("Хочешь сохранить этот промт?", reply_markup=kb)
        except Exception as e:
            logger.exception("image gen failed")
            await update.message.reply_text(f"⛔️ Не удалось сгенерировать фото.\n\nОшибка: `{repr(e)}`", parse_mode="Markdown")
        return

    if awaiting == "vid_prompt":
        prompt = update.message.text.strip()
        context.user_data["awaiting"] = None
        await update.message.reply_text("⏳ Генерирую видео (4 сек)…")
        try:
            vid_bytes = await run_in_thread(openai_create_video, prompt, "4", "720x1280")
            # Telegram expects file-like object for video
            bio = io.BytesIO(vid_bytes)
            bio.name = "video.mp4"
            await update.message.reply_video(video=bio, caption="🎬 Готово!")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Сохранить промт", callback_data="save_last")],
            ])
            context.user_data["last_prompt"] = prompt
            await update.message.reply_text("Хочешь сохранить этот промт?", reply_markup=kb)
        except Exception as e:
            logger.exception("video gen failed")
            await update.message.reply_text(
                "⛔️ Не удалось сгенерировать видео.\n\n"
                "Если текст ошибки про доступ/модель — значит у API-аккаунта нет доступа к Sora-видео.\n"
                f"Ошибка: `{repr(e)}`",
                parse_mode="Markdown"
            )
        return

    # default: show menu
    await send_menu(update, context)

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    upsert_user(update)

    awaiting = context.user_data.get("awaiting")
    if awaiting != "photo_for_prompt":
        await update.message.reply_text("Фото получил(а). Если хочешь промт по фото — нажми «Сделай промт по фото» в меню.")
        return

    if not await gate_or_menu(update, context):
        return

    # download best resolution photo
    photos = update.message.photo or []
    if not photos:
        await update.message.reply_text("Не вижу фото. Пришли обычным фото (не сжатым — лучше как документ).")
        return
    file_id = photos[-1].file_id
    f = await context.bot.get_file(file_id)
    b = await f.download_as_bytearray()

    context.user_data["photo_bytes"] = bytes(b)
    context.user_data["awaiting"] = "photo_goal"

    await update.message.reply_text(
        "✅ Фото принято.\n\nТеперь одним сообщением напиши:\n"
        "1) что хочешь получить (Фото / Видео / HeyGen)\n"
        "2) стиль (зима/глянец/кино/ночь)\n"
        "3) важные детали (одежда/фон/эмоция)\n\n"
        "Пример: *Видео, зима, я в красной шапке, мягкий кино-свет, реализм 1:1*",
        parse_mode="Markdown"
    )

async def on_photo_goal_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    upsert_user(update)

    awaiting = context.user_data.get("awaiting")
    if awaiting != "photo_goal":
        return

    if not await gate_or_menu(update, context):
        return

    goal = update.message.text.strip()
    img_bytes = context.user_data.get("photo_bytes")
    context.user_data["awaiting"] = None

    await update.message.reply_text("⏳ Анализирую фото и собираю промт-пакет…")
    try:
        pack = await run_in_thread(openai_prompt_from_photo, img_bytes, goal)
        context.user_data["last_prompt"] = pack
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📌 Сохранить в мои промты", callback_data="save_last")]])
        await update.message.reply_text(pack, reply_markup=kb)
    except Exception as e:
        logger.exception("prompt-by-photo failed")
        await update.message.reply_text(
            "⛔️ Не удалось сделать промт по фото.\n\n"
            f"Ошибка: `{repr(e)}`",
            parse_mode="Markdown"
        )

async def cb_save_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    uid = update.effective_user.id
    if not await gate_or_menu(update, context):
        return

    p = context.user_data.get("last_prompt")
    if not p:
        await q.message.reply_text("Нет последнего промта для сохранения.")
        return

    title = "Промт " + now_local().strftime("%d.%m %H:%M")
    save_prompt(uid, title, p)
    await q.message.reply_text("✅ Сохранено в «Мои промты».")

# ----------------------------
# App / Webhook
# ----------------------------
db_init()

app = FastAPI()
tg_app: Application | None = None

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "ok"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("webhook error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

async def on_startup():
    global tg_app
    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_help))
    tg_app.add_handler(CommandHandler("balance", cmd_balance))
    tg_app.add_handler(CommandHandler("diag", cmd_diag))
    tg_app.add_handler(CommandHandler("grantvip", cmd_grantvip))

    tg_app.add_handler(CallbackQueryHandler(cb_router))
    tg_app.add_handler(CallbackQueryHandler(cb_save_last, pattern=r"^save_last$"))

    tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_photo_goal_text))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await tg_app.initialize()
    await tg_app.start()

    me = await tg_app.bot.get_me()
    logger.info(f"Bot username: {me.username}")

    await tg_app.bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook set: {WEBHOOK_URL}")

async def on_shutdown():
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()

@app.on_event("startup")
async def _startup():
    await on_startup()

@app.on_event("shutdown")
async def _shutdown():
    await on_shutdown()
