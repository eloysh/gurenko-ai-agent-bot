import os
import re
import json
import base64
import sqlite3
import asyncio
from io import BytesIO
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Request

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    PreCheckoutQueryHandler,
    filters,
)

# =========================
# ENV / CONFIG
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Public base URL (Render usually provides RENDER_EXTERNAL_URL)
PUBLIC_BASE_URL = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_BASE_URL") or "").strip()
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = (PUBLIC_BASE_URL.rstrip("/") + WEBHOOK_PATH) if PUBLIC_BASE_URL else ""

# Channel gating (growth)
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@gurenko_kristina_ai").strip()

# OpenAI
OPENAI_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini").strip()
IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1").strip()
VIDEO_MODEL = os.getenv("OPENAI_VIDEO_MODEL", "sora-2").strip()  # allowed: sora-2, sora-2-pro

# Limits
FREE_GEN_PER_DAY = int(os.getenv("FREE_GEN_PER_DAY", "1"))  # 1/day total: photo OR video
VIP_GEN_PER_DAY = int(os.getenv("VIP_GEN_PER_DAY", "10"))

FREE_ASK_PER_DAY = int(os.getenv("FREE_ASK_PER_DAY", "20"))
VIP_ASK_PER_DAY = int(os.getenv("VIP_ASK_PER_DAY", "200"))

# VIP Stars shop
VIP_7_STARS = int(os.getenv("VIP_7_STARS", "99"))
VIP_30_STARS = int(os.getenv("VIP_30_STARS", "299"))

# Referral rewards
REF_BONUS_ASK_ON_1 = 5     # +5 AI asks
REF_VIP_DAYS_ON_3 = 3      # VIP 3 days
REF_BONUS_GEN_ON_5 = 3     # +3 generations (extra)

# DB
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY env var")
if not PUBLIC_BASE_URL:
    # Not fatal if you set webhook manually, but on Render лучше указать
    print("WARN: PUBLIC_BASE_URL/RENDER_EXTERNAL_URL is empty. Webhook may not be set automatically.")


# =========================
# DATA: Prompt of day + Challenge 30 days
# =========================

PROMPT_OF_DAY_POOL = [
    # 30+ вариантов, можно расширять
    ("Зимний глянец", "Ультра-реалистичное зимнее fashion-editorial фото, глянец, мягкий снег, кинематографичный свет, детальная кожа, 85mm, shallow DOF. Добавь: ракурс снизу, отражения на льду, чистый фон."),
    ("Кино-кадр", "Кинематографичный кадр как из фильма: теплый контровой свет, лёгкий туман, зерно, естественная кожа без пластика, реалистичные поры, 35mm."),
    ("Ночь/город", "Ночной город, неоновые отражения, мокрый асфальт, резкий фокус на лице, естественная кожа, без «кукольности», 50mm."),
    ("Тёплый интерьер", "Тёплый интерьер, янтарный свет, мягкие тени, текстуры ткани и кожи, реалистичная детализация, editorial."),
    ("Снежные ресницы", "Макро-крупность: снежные кристаллы на ресницах, ультра-детальная кожа, мягкий свет, натуральные оттенки."),
    ("Минимализм", "Белый минималистичный фон, студийный мягкий свет, чистая цветокоррекция, высокая детализация кожи."),
    ("Глянец/обложка", "Обложка журнала: чистая композиция, свет как в студии, контраст, текстуры, идеальная резкость кожи."),
    ("Портрет 8K", "Портрет 8K ultra-real, естественная кожа, без сглаживания, аккуратный HDR, детальные глаза."),
    ("Снежный лес", "Зимний лес, лёгкий снегопад, объемный свет, натуральные цвета, реализм."),
    ("Лёд и отражения", "Ледяная поверхность, реалистичные отражения, трещинки на льду, cinematic."),
]

CHALLENGE_30 = [
    ("День 1 — Реалистичная кожа", "Сделай портрет с акцентом на кожу: поры, текстура, без пластика. Добавь: мягкий свет + один контровой."),
    ("День 2 — Свет и объём", "Повтори портрет, но поменяй свет: боковой + контровой. Посмотри, как меняется объём лица."),
    ("День 3 — Кино-цвет", "Сделай cinematic color grading: лёгкое зерно, мягкий контраст, натуральные тона кожи."),
    ("День 4 — Ракурсы", "Сделай 3 варианта: низкий ракурс / уровень глаз / чуть сверху. Лицо без изменений."),
    ("День 5 — Ночь/неон", "Ночной стиль: неоновые отражения, мокрый асфальт, реализм, без пересвета кожи."),
    ("День 6 — Тёплый интерьер", "Тёплый интерьерный кадр: янтарный свет, текстуры ткани, естественные оттенки."),
    ("День 7 — Глянец", "Fashion-editorial: чистый фон, жестче свет, «глянцевый» результат."),
    ("День 8 — Движение", "Сделай динамику (шаг/поворот головы), заморозь движение быстрым выдержкой."),
    ("День 9 — Макро детали", "Супер-крупно: ресницы/губы/глаза. Важно: натуральные детали, без «куклы»."),
    ("День 10 — Улица день", "Уличный портрет днём: естественный свет, реалистичные тени."),
    ("День 11 — Снегопад", "Снегопад + объемный свет, мягкая глубина резкости."),
    ("День 12 — Лёд", "Ледяной сет: отражения, текстуры, холодная палитра."),
    ("День 13 — Дымка", "Легкий туман/дымка и контровой свет."),
    ("День 14 — 3 варианта одного промта", "Один промт — 3 вариации: разный объектив (35/50/85)."),
    ("День 15 — Поза и руки", "Фокус на красивые руки/позу, естественная анатомия."),
    ("День 16 — Ткань и мех", "Текстуры: мех/шерсть/куртка — максимум детализации."),
    ("День 17 — Контраст", "Более контрастный свет, но кожа натуральная."),
    ("День 18 — Силуэт", "Силуэтный кадр с подсветкой сзади."),
    ("День 19 — Цветовой акцент", "Один яркий акцент (шарф/шапка), остальное спокойно."),
    ("День 20 — Чистый студийный", "Студийный кадр: софтбокс, равномерный свет, clean."),
    ("День 21 — Кино кадр 2", "Кино-кадр: композиция как в фильме, глубина сцены."),
    ("День 22 — Пейзаж+человек", "Человек на фоне красивого пейзажа, реализм."),
    ("День 23 — Блики", "Добавь блики/линзфлер аккуратно, чтобы не убить кожу."),
    ("День 24 — ЧБ", "Чёрно-белый портрет с идеальной тональностью кожи."),
    ("День 25 — Дождь", "Дождь/капли/мокрые волосы, реалистичные детали."),
    ("День 26 — Сторителлинг", "Кадр рассказывает историю: действие, эмоция."),
    ("День 27 — 10-сек видео идея", "Сделай короткий сценарий видео 4–8 сек из одной сцены."),
    ("День 28 — Повтор лучшего", "Повтори самый удачный день, но улучшай 2 детали."),
    ("День 29 — Упаковка Reels", "Сделай подпись + CTA + 5 тегов к результату."),
    ("День 30 — Итог", "Собери «лучшее из 30 дней» + короткое описание своего стиля."),
]


# =========================
# DB helpers
# =========================

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def today_key() -> str:
    return utcnow().strftime("%Y-%m-%d")

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            created_at TEXT,
            last_seen TEXT,
            vip_until TEXT,
            ask_used_date TEXT,
            ask_used_count INTEGER DEFAULT 0,
            gen_used_date TEXT,
            gen_used_count INTEGER DEFAULT 0,
            bonus_ask INTEGER DEFAULT 0,
            bonus_gen INTEGER DEFAULT 0,
            challenge_started TEXT,
            challenge_done_day INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            referrer_id INTEGER,
            referred_id INTEGER UNIQUE,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def ensure_user(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users (user_id, created_at, last_seen) VALUES (?,?,?)",
            (user_id, iso(utcnow()), iso(utcnow())),
        )
    else:
        cur.execute("UPDATE users SET last_seen=? WHERE user_id=?", (iso(utcnow()), user_id))
    conn.commit()
    conn.close()

def get_user(user_id: int) -> sqlite3.Row:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        ensure_user(user_id)
        return get_user(user_id)
    return row

def set_vip(user_id: int, days: int):
    u = get_user(user_id)
    now = utcnow()
    vip_until = parse_dt(u["vip_until"])
    start = vip_until if (vip_until and vip_until > now) else now
    new_until = start + timedelta(days=days)
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET vip_until=? WHERE user_id=?", (iso(new_until), user_id))
    conn.commit()
    conn.close()

def is_vip(user_id: int) -> bool:
    u = get_user(user_id)
    vip_until = parse_dt(u["vip_until"])
    return bool(vip_until and vip_until > utcnow())

def _reset_daily_if_needed(u: sqlite3.Row, col_date: str, col_count: str, user_id: int):
    d = u[col_date]
    if d != today_key():
        conn = db()
        cur = conn.cursor()
        cur.execute(f"UPDATE users SET {col_date}=?, {col_count}=0 WHERE user_id=?", (today_key(), user_id))
        conn.commit()
        conn.close()

def can_consume_generation(user_id: int) -> tuple[bool, str]:
    """
    Returns (ok, message_if_not_ok)
    Free: 1/day total (photo OR video)
    VIP: VIP_GEN_PER_DAY/day
    Can also spend bonus_gen if available.
    """
    u = get_user(user_id)
    _reset_daily_if_needed(u, "gen_used_date", "gen_used_count", user_id)
    u = get_user(user_id)

    limit = VIP_GEN_PER_DAY if is_vip(user_id) else FREE_GEN_PER_DAY
    used = int(u["gen_used_count"] or 0)
    bonus = int(u["bonus_gen"] or 0)

    if used < limit:
        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET gen_used_count=gen_used_count+1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        return True, ""
    if bonus > 0:
        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET bonus_gen=bonus_gen-1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        return True, ""
    return False, f"Лимит генераций на сегодня исчерпан.\n\nБесплатно: {FREE_GEN_PER_DAY}/день.\nVIP: {VIP_GEN_PER_DAY}/день."

def can_consume_ask(user_id: int) -> tuple[bool, str]:
    u = get_user(user_id)
    _reset_daily_if_needed(u, "ask_used_date", "ask_used_count", user_id)
    u = get_user(user_id)

    limit = VIP_ASK_PER_DAY if is_vip(user_id) else FREE_ASK_PER_DAY
    used = int(u["ask_used_count"] or 0)
    bonus = int(u["bonus_ask"] or 0)

    if used < limit:
        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET ask_used_count=ask_used_count+1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        return True, ""
    if bonus > 0:
        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET bonus_ask=bonus_ask-1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        return True, ""
    return False, f"Лимит вопросов на сегодня исчерпан.\n\nБесплатно: {FREE_ASK_PER_DAY}/день.\nVIP: {VIP_ASK_PER_DAY}/день."

def referral_count(referrer_id: int) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (referrer_id,))
    row = cur.fetchone()
    conn.close()
    return int(row["c"] or 0)

def add_referral(referrer_id: int, referred_id: int) -> bool:
    """
    Returns True if inserted (new referral), False if already exists.
    """
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO referrals (referrer_id, referred_id, created_at) VALUES (?,?,?)",
            (referrer_id, referred_id, iso(utcnow())),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def add_bonus_ask(user_id: int, amount: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET bonus_ask=bonus_ask+? WHERE user_id=?", (amount, user_id))
    conn.commit()
    conn.close()

def add_bonus_gen(user_id: int, amount: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET bonus_gen=bonus_gen+? WHERE user_id=?", (amount, user_id))
    conn.commit()
    conn.close()

def set_challenge_start(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET challenge_started=?, challenge_done_day=0 WHERE user_id=?",
        (today_key(), user_id),
    )
    conn.commit()
    conn.close()

def challenge_day(user_id: int) -> int:
    u = get_user(user_id)
    started = u["challenge_started"]
    if not started:
        return 0
    try:
        d0 = datetime.strptime(started, "%Y-%m-%d").date()
    except Exception:
        return 0
    d1 = utcnow().date()
    delta = (d1 - d0).days
    day = min(30, max(1, delta + 1))
    return day

def mark_challenge_done(user_id: int):
    day = challenge_day(user_id)
    if day <= 0:
        return
    u = get_user(user_id)
    done = int(u["challenge_done_day"] or 0)
    if day > done:
        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET challenge_done_day=? WHERE user_id=?", (day, user_id))
        conn.commit()
        conn.close()


# =========================
# OpenAI HTTP helpers
# =========================

def oai_headers() -> dict:
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

async def oai_post_json(path: str, payload: dict) -> tuple[dict | None, str | None]:
    url = f"{OPENAI_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=oai_headers(), json=payload)
            if r.status_code >= 300:
                return None, f"{r.status_code}: {r.text}"
            return r.json(), None
    except Exception as e:
        return None, str(e)

async def oai_get_json(path: str) -> tuple[dict | None, str | None]:
    url = f"{OPENAI_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {OPENAI_API_KEY}"})
            if r.status_code >= 300:
                return None, f"{r.status_code}: {r.text}"
            return r.json(), None
    except Exception as e:
        return None, str(e)

async def oai_get_bytes(path: str) -> tuple[bytes | None, str | None]:
    url = f"{OPENAI_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {OPENAI_API_KEY}"})
            if r.status_code >= 300:
                return None, f"{r.status_code}: {r.text}"
            return r.content, None
    except Exception as e:
        return None, str(e)

async def generate_image(prompt: str, size: str = "1024x1024") -> tuple[bytes | None, str | None]:
    # Correct endpoint: /v1/images/generations  [oai_citation:2‡OpenAI Platform](https://platform.openai.com/docs/api-reference/videos)
    payload = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "size": size,
    }
    j, err = await oai_post_json("/images/generations", payload)
    if err:
        return None, err
    try:
        data0 = j["data"][0]
        if "b64_json" in data0:
            return base64.b64decode(data0["b64_json"]), None
        if "url" in data0:
            # If API returns URL (some configs), fetch it
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.get(data0["url"])
                r.raise_for_status()
                return r.content, None
        return None, f"Unexpected image response: {json.dumps(j)[:500]}"
    except Exception as e:
        return None, f"Parse error: {e}"

async def create_video_job(prompt: str, seconds: int = 4, size: str = "720x1280") -> tuple[str | None, str | None]:
    # Videos endpoint: POST /v1/videos  [oai_citation:3‡OpenAI Platform](https://platform.openai.com/docs/api-reference/videos)
    url = f"{OPENAI_BASE}/videos"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Use multipart-like form (works with/without file ref)
            data = {
                "model": VIDEO_MODEL,
                "prompt": prompt,
                "seconds": str(seconds),
                "size": size,
            }
            r = await client.post(url, headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, data=data)
            if r.status_code >= 300:
                return None, f"{r.status_code}: {r.text}"
            j = r.json()
            return j.get("id"), None
    except Exception as e:
        return None, str(e)

async def wait_video_done(video_id: str, max_wait_sec: int = 120) -> tuple[bool, str | None]:
    t0 = utcnow()
    while (utcnow() - t0).total_seconds() < max_wait_sec:
        j, err = await oai_get_json(f"/videos/{video_id}")
        if err:
            return False, err
        status = j.get("status")
        if status in ("succeeded", "completed"):
            return True, None
        if status in ("failed", "canceled", "cancelled"):
            return False, f"Video status: {status}. {j}"
        await asyncio.sleep(2)
    return False, "Timeout waiting video"

async def download_video(video_id: str) -> tuple[bytes | None, str | None]:
    # GET /v1/videos/{id}/content  [oai_citation:4‡OpenAI Platform](https://platform.openai.com/docs/api-reference/videos)
    return await oai_get_bytes(f"/videos/{video_id}/content")

async def chat_answer(user_text: str) -> tuple[str | None, str | None]:
    # Simple Chat Completions (legacy but stable)
    payload = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": "Ты помощник по нейросетям. Пиши коротко, четко, по делу, с готовыми формулировками."},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.7,
    }
    j, err = await oai_post_json("/chat/completions", payload)
    if err:
        return None, err
    try:
        return j["choices"][0]["message"]["content"], None
    except Exception as e:
        return None, f"Parse error: {e}"


# =========================
# Telegram UI helpers
# =========================

BOT_USERNAME = None  # set on startup

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Сгенерировать фото", callback_data="m:gen_photo"),
         InlineKeyboardButton("🎥 Сгенерировать видео", callback_data="m:gen_video")],
        [InlineKeyboardButton("📌 Промт дня", callback_data="m:pod"),
         InlineKeyboardButton("🏆 Челлендж 30 дней", callback_data="m:challenge")],
        [InlineKeyboardButton("🧠 Спросить у ИИ", callback_data="m:ask_ai"),
         InlineKeyboardButton("🎁 Пригласить друга", callback_data="m:ref")],
        [InlineKeyboardButton("🛒 VIP / Магазин", callback_data="m:shop"),
         InlineKeyboardButton("🧾 Мой статус", callback_data="m:status")],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="m:menu")]])

def subscribe_gate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton("✅ Я подписался", callback_data="m:check_sub")],
    ])

async def safe_edit_or_send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup: InlineKeyboardMarkup | None = None):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
            return
        except BadRequest as e:
            # Important fix: "Message is not modified" should not crash
            if "Message is not modified" in str(e):
                try:
                    await update.callback_query.answer("Ок ✅")
                except Exception:
                    pass
                return
            # If cannot edit (old message etc) — send a new one
        except Exception:
            pass
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, disable_web_page_preview=True)

async def user_in_channel(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        # If bot not admin in channel, Telegram may deny
        return False

def format_status(u: sqlite3.Row, user_id: int) -> str:
    vip = is_vip(user_id)
    vip_until = u["vip_until"] or "—"
    ask_used = int(u["ask_used_count"] or 0)
    gen_used = int(u["gen_used_count"] or 0)
    b_ask = int(u["bonus_ask"] or 0)
    b_gen = int(u["bonus_gen"] or 0)
    day = challenge_day(user_id)
    done = int(u["challenge_done_day"] or 0)
    refc = referral_count(user_id)

    limit_ask = VIP_ASK_PER_DAY if vip else FREE_ASK_PER_DAY
    limit_gen = VIP_GEN_PER_DAY if vip else FREE_GEN_PER_DAY

    return (
        f"🧾 *Твой статус*\n\n"
        f"👑 VIP: {'активен' if vip else 'нет'}\n"
        f"⏳ VIP до: `{vip_until}`\n\n"
        f"🧠 Вопросы ИИ сегодня: {ask_used}/{limit_ask} (бонус: {b_ask})\n"
        f"🎬 Генерации сегодня: {gen_used}/{limit_gen} (бонус: {b_gen})\n\n"
        f"🏆 Челлендж: {'не начат' if day==0 else f'день {day}/30, выполнено до {done}'}\n"
        f"🎁 Рефералы: {refc}\n"
    )


# =========================
# Telegram Handlers
# =========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id)

    # Referral parse: /start ref_12345
    if context.args:
        m = re.match(r"^ref_(\d+)$", context.args[0])
        if m:
            referrer_id = int(m.group(1))
            if referrer_id != user.id:
                inserted = add_referral(referrer_id, user.id)
                if inserted:
                    # apply rewards based on count
                    cnt = referral_count(referrer_id)
                    # 1st referral
                    if cnt == 1:
                        add_bonus_ask(referrer_id, REF_BONUS_ASK_ON_1)
                        try:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=f"🎉 У тебя 1 реферал! Начислено +{REF_BONUS_ASK_ON_1} вопросов к ИИ ✅",
                            )
                        except Exception:
                            pass
                    # 3rd referral
                    if cnt == 3:
                        set_vip(referrer_id, REF_VIP_DAYS_ON_3)
                        try:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=f"🔥 У тебя 3 реферала! VIP на {REF_VIP_DAYS_ON_3} дня активирован ✅",
                            )
                        except Exception:
                            pass
                    # 5th referral
                    if cnt == 5:
                        add_bonus_gen(referrer_id, REF_BONUS_GEN_ON_5)
                        try:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=f"🚀 У тебя 5 рефералов! Начислено +{REF_BONUS_GEN_ON_5} генерации ✅",
                            )
                        except Exception:
                            pass

    # Gate by channel subscription
    ok = await user_in_channel(context, user.id)
    if not ok:
        text = (
            "Чтобы открыть функции бота ✅\n\n"
            f"1) Подпишись на канал: {REQUIRED_CHANNEL}\n"
            "2) Нажми «✅ Я подписался»\n\n"
            "Так ты получишь доступ к промтам, челленджу и генерации."
        )
        await safe_edit_or_send(update, context, text, subscribe_gate_kb())
        return

    text = (
        "Привет! Я бот Кристины 👋\n\n"
        "Что умею:\n"
        "• Генерация фото/видео (1 бесплатно в день)\n"
        "• Промт дня\n"
        "• Челлендж 30 дней\n"
        "• Рефералка (приглашай друзей → бонусы)\n\n"
        "Выбирай в меню 👇"
    )
    await safe_edit_or_send(update, context, text, main_menu_kb())

async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ok = await user_in_channel(context, user_id)
    if not ok:
        await safe_edit_or_send(update, context, "Сначала подпишись на канал 👇", subscribe_gate_kb())
        return
    await safe_edit_or_send(update, context, "Главное меню 👇", main_menu_kb())

async def check_sub_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ok = await user_in_channel(context, user_id)
    if not ok:
        await update.callback_query.answer("Пока не вижу подписку 😔", show_alert=True)
        await safe_edit_or_send(update, context, "Подпишись и нажми ещё раз ✅", subscribe_gate_kb())
        return
    await update.callback_query.answer("Отлично! Доступ открыт ✅", show_alert=True)
    await safe_edit_or_send(update, context, "Главное меню 👇", main_menu_kb())

async def prompt_of_day_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ok = await user_in_channel(context, user_id)
    if not ok:
        await safe_edit_or_send(update, context, "Сначала подпишись на канал 👇", subscribe_gate_kb())
        return

    # rotate by day-of-year
    day_index = int(utcnow().strftime("%j")) % len(PROMPT_OF_DAY_POOL)
    title, body = PROMPT_OF_DAY_POOL[day_index]

    text = (
        f"📌 *Промт дня*\n"
        f"Тема: *{title}*\n\n"
        f"`{body}`\n\n"
        "💡 Хочешь «как у Кристины»? Возьми этот промт и добавь:\n"
        "— *super realistic skin, pores, no plastic*\n"
        "— *cinematic lighting, 85mm, shallow depth of field*\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📌 Скопировать", callback_data="a:copy_pod")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="m:menu")],
    ])
    await safe_edit_or_send(update, context, text, kb)

async def copy_pod_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    day_index = int(utcnow().strftime("%j")) % len(PROMPT_OF_DAY_POOL)
    _, body = PROMPT_OF_DAY_POOL[day_index]
    await update.callback_query.answer("Скопировано ✅", show_alert=False)
    await update.callback_query.message.reply_text(f"`{body}`")

async def challenge_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ok = await user_in_channel(context, user_id)
    if not ok:
        await safe_edit_or_send(update, context, "Сначала подпишись на канал 👇", subscribe_gate_kb())
        return

    u = get_user(user_id)
    day = challenge_day(user_id)
    if day == 0:
        text = (
            "🏆 *Челлендж 30 дней*\n\n"
            "Хочешь реально прокачаться и делать вирусные результаты?\n"
            "Нажми «Старт» — и каждый день получай задание.\n\n"
            "✅ Можно отмечать «Готово» и идти дальше."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Старт челленджа", callback_data="c:start")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="m:menu")],
        ])
        await safe_edit_or_send(update, context, text, kb)
        return

    title, task = CHALLENGE_30[day - 1]
    done = int(u["challenge_done_day"] or 0)

    text = (
        f"🏆 *Челлендж 30 дней*\n\n"
        f"*{title}*\n"
        f"{task}\n\n"
        f"Текущий день: {day}/30\n"
        f"Выполнено до: {done}\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готово", callback_data="c:done")],
        [InlineKeyboardButton("🔁 Сбросить челлендж", callback_data="c:reset")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="m:menu")],
    ])
    await safe_edit_or_send(update, context, text, kb)

async def challenge_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    set_challenge_start(user_id)
    await update.callback_query.answer("Стартовали! День 1 ✅", show_alert=True)
    await challenge_cb(update, context)

async def challenge_done_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    mark_challenge_done(user_id)
    await update.callback_query.answer("Засчитано ✅", show_alert=False)
    await challenge_cb(update, context)

async def challenge_reset_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET challenge_started=NULL, challenge_done_day=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    await update.callback_query.answer("Сброшено ✅", show_alert=True)
    await challenge_cb(update, context)

async def ref_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ok = await user_in_channel(context, user_id)
    if not ok:
        await safe_edit_or_send(update, context, "Сначала подпишись на канал 👇", subscribe_gate_kb())
        return

    global BOT_USERNAME
    username = BOT_USERNAME or (await context.bot.get_me()).username
    link = f"https://t.me/{username}?start=ref_{user_id}"
    cnt = referral_count(user_id)

    text = (
        "🎁 *Пригласи друга и получай бонусы*\n\n"
        f"Твоя ссылка:\n{link}\n\n"
        "Награды:\n"
        f"• 1 друг → +{REF_BONUS_ASK_ON_1} вопросов к ИИ\n"
        f"• 3 друга → VIP на {REF_VIP_DAYS_ON_3} дня\n"
        f"• 5 друзей → +{REF_BONUS_GEN_ON_5} генерации\n\n"
        f"У тебя сейчас рефералов: {cnt}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={link}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="m:menu")],
    ])
    await safe_edit_or_send(update, context, text, kb)

async def status_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = get_user(user_id)
    await safe_edit_or_send(update, context, format_status(u, user_id), back_kb())

async def shop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛒 *VIP / Магазин*\n\n"
        "VIP даёт больше лимитов + приоритет.\n\n"
        f"VIP 7 дней — {VIP_7_STARS} ⭐\n"
        f"VIP 30 дней — {VIP_30_STARS} ⭐\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Купить VIP 7 дней ({VIP_7_STARS}⭐)", callback_data="pay:vip7")],
        [InlineKeyboardButton(f"Купить VIP 30 дней ({VIP_30_STARS}⭐)", callback_data="pay:vip30")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="m:menu")],
    ])
    await safe_edit_or_send(update, context, text, kb)

async def send_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, days: int, stars: int, payload: str):
    chat_id = update.effective_chat.id
    title = f"VIP на {days} дней"
    desc = "VIP доступ в боте (увеличенные лимиты + приоритет)"
    prices = [LabeledPrice(title, stars)]
    # Telegram Stars: currency="XTR", provider_token="" (empty)
    await context.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=desc,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=prices,
    )

async def pay_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "pay:vip7":
        await send_invoice(update, context, 7, VIP_7_STARS, "vip_7")
    elif q.data == "pay:vip30":
        await send_invoice(update, context, 30, VIP_30_STARS, "vip_30")

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    user_id = update.effective_user.id
    if sp.invoice_payload == "vip_7":
        set_vip(user_id, 7)
        await update.message.reply_text("✅ VIP на 7 дней активирован!")
    elif sp.invoice_payload == "vip_30":
        set_vip(user_id, 30)
        await update.message.reply_text("✅ VIP на 30 дней активирован!")
    else:
        await update.message.reply_text("✅ Оплата получена!")

async def gen_photo_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ok = await user_in_channel(context, user_id)
    if not ok:
        await safe_edit_or_send(update, context, "Сначала подпишись на канал 👇", subscribe_gate_kb())
        return

    ok2, msg = can_consume_generation(user_id)
    if not ok2:
        await safe_edit_or_send(update, context, msg + "\n\nХочешь больше? Возьми VIP 👇", InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 VIP / Магазин", callback_data="m:shop")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="m:menu")],
        ]))
        return

    context.user_data["mode"] = "gen_photo"
    await safe_edit_or_send(update, context,
        "📸 Напиши *текст-промт*, по которому сгенерировать фото.\n\n(Подсказка: добавь стиль, свет, камеру, реализм кожи.)",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="m:menu")]])
    )

async def gen_video_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ok = await user_in_channel(context, user_id)
    if not ok:
        await safe_edit_or_send(update, context, "Сначала подпишись на канал 👇", subscribe_gate_kb())
        return

    ok2, msg = can_consume_generation(user_id)
    if not ok2:
        await safe_edit_or_send(update, context, msg + "\n\nХочешь больше? Возьми VIP 👇", InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 VIP / Магазин", callback_data="m:shop")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="m:menu")],
        ]))
        return

    context.user_data["mode"] = "gen_video"
    await safe_edit_or_send(update, context,
        "🎥 Напиши *текст-промт*, по которому сгенерировать видео.\n\nПо умолчанию: 4 секунды, 720x1280.",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="m:menu")]])
    )

async def ask_ai_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ok = await user_in_channel(context, user_id)
    if not ok:
        await safe_edit_or_send(update, context, "Сначала подпишись на канал 👇", subscribe_gate_kb())
        return

    ok2, msg = can_consume_ask(user_id)
    if not ok2:
        await safe_edit_or_send(update, context, msg + "\n\nХочешь больше? Возьми VIP 👇", InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 VIP / Магазин", callback_data="m:shop")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="m:menu")],
        ]))
        return

    context.user_data["mode"] = "ask_ai"
    await safe_edit_or_send(update, context,
        "🧠 Напиши вопрос. Я отвечу коротко и по делу.",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="m:menu")]])
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)

    mode = context.user_data.get("mode")
    text = (update.message.text or "").strip()
    if not mode:
        await update.message.reply_text("Открой меню: /start")
        return

    if mode == "gen_photo":
        context.user_data["mode"] = None
        await update.message.reply_text("⏳ Генерирую фото…")
        img, err = await generate_image(text, size="1024x1024")
        if err:
            await update.message.reply_text(
                "❌ Не удалось сгенерировать фото.\n\n"
                f"Ошибка: {err}\n\n"
                "Проверь:\n"
                "• правильный ключ OPENAI_API_KEY\n"
                f"• доступ к модели {IMAGE_MODEL}\n"
                "• что баланс/лимиты не исчерпаны"
            )
            return
        bio = BytesIO(img)
        bio.name = "image.png"
        await update.message.reply_photo(photo=bio, caption="✅ Готово! Хочешь ещё — /start")

    elif mode == "gen_video":
        context.user_data["mode"] = None
        await update.message.reply_text("⏳ Запускаю генерацию видео…")
        vid, err = await create_video_job(text, seconds=4, size="720x1280")
        if err or not vid:
            await update.message.reply_text(
                "❌ Не удалось создать задачу видео.\n\n"
                f"Ошибка: {err}\n\n"
                f"Проверь доступ к видео-модели ({VIDEO_MODEL}) и лимиты."
            )
            return
        await update.message.reply_text("⏳ Жду готовность видео…")
        ok_done, err2 = await wait_video_done(vid, max_wait_sec=120)
        if not ok_done:
            await update.message.reply_text(f"❌ Видео не готово: {err2}")
            return
        bytes_video, err3 = await download_video(vid)
        if err3 or not bytes_video:
            await update.message.reply_text(f"❌ Не удалось скачать видео: {err3}")
            return
        bio = BytesIO(bytes_video)
        bio.name = "video.mp4"
        await update.message.reply_video(video=bio, caption="✅ Готово! /start")

    elif mode == "ask_ai":
        context.user_data["mode"] = None
        await update.message.reply_text("⏳ Думаю…")
        ans, err = await chat_answer(text)
        if err:
            await update.message.reply_text(f"❌ Ошибка: {err}")
            return
        await update.message.reply_text(ans, reply_markup=back_kb())

    else:
        context.user_data["mode"] = None
        await update.message.reply_text("Ок. /start")

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data

    # main menu
    if data == "m:menu":
        return await menu_cb(update, context)
    if data == "m:check_sub":
        return await check_sub_cb(update, context)

    if data == "m:pod":
        return await prompt_of_day_cb(update, context)
    if data == "a:copy_pod":
        return await copy_pod_cb(update, context)

    if data == "m:challenge":
        return await challenge_cb(update, context)
    if data == "c:start":
        return await challenge_start_cb(update, context)
    if data == "c:done":
        return await challenge_done_cb(update, context)
    if data == "c:reset":
        return await challenge_reset_cb(update, context)

    if data == "m:ref":
        return await ref_cb(update, context)

    if data == "m:status":
        return await status_cb(update, context)

    if data == "m:shop":
        return await shop_cb(update, context)

    if data.startswith("pay:"):
        return await pay_cb(update, context)

    if data == "m:gen_photo":
        return await gen_photo_cb(update, context)
    if data == "m:gen_video":
        return await gen_video_cb(update, context)

    if data == "m:ask_ai":
        return await ask_ai_cb(update, context)

    await update.callback_query.answer("Ок")

# =========================
# FastAPI + PTB init
# =========================

app = FastAPI()
telegram_app: Application | None = None

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "ok"}

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    global telegram_app, BOT_USERNAME
    init_db()

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start_cmd))
    telegram_app.add_handler(CallbackQueryHandler(callback_router))
    telegram_app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    telegram_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    await telegram_app.initialize()
    await telegram_app.start()

    me = await telegram_app.bot.get_me()
    BOT_USERNAME = me.username
    print("Bot username:", BOT_USERNAME)

    if WEBHOOK_URL:
        await telegram_app.bot.set_webhook(WEBHOOK_URL)
        print("Webhook set:", WEBHOOK_URL)
    else:
        print("WARN: WEBHOOK_URL not set (no PUBLIC_BASE_URL). Set webhook manually if needed.")

@app.on_event("shutdown")
async def on_shutdown():
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
