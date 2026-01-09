import os
import re
import io
import json
import base64
import sqlite3
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

import httpx
from fastapi import FastAPI, Request

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    InputFile,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# ============================
# CONFIG (env vars)
# ============================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Chat model (Q&A, промт дня, разборы)
OPENAI_CHAT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Media models
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")       # images endpoint
VIDEO_MODEL = os.getenv("VIDEO_MODEL", "sora-2")            # videos endpoint

TG_CHANNEL = os.getenv("TG_CHANNEL", "@gurenko_kristina_ai")
TZ_NAME = os.getenv("TZ", "Asia/Tokyo")

# Limits
DAILY_LIMIT_ASK = int(os.getenv("DAILY_LIMIT", "3"))         # текстовые вопросы в день (free)
DAILY_LIMIT_MEDIA = int(os.getenv("DAILY_LIMIT_MEDIA", "1")) # фото/видео в день (free)

# VIP
VIP_DAYS = int(os.getenv("VIP_DAYS", "30"))
VIP_PRICE_STARS = int(os.getenv("VIP_PRICE_STARS", "299"))

# Webhook base
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")

tz = ZoneInfo(TZ_NAME)

SYSTEM_PROMPT = """Ты — AI-агент Кристины.
Тема: нейросети для реалистичных фото/видео (Sora/HeyGen/Meta AI), промты, сценарии Reels.
Отвечай коротко, по шагам, без воды.
Если нужно — дай 1-2 примера промтов.
Если вопрос про Reels — начинай с 'Хук/первые 2 секунды/формат/текст на экране'.
"""

# ============================
# DB (SQLite)
# ============================
DB_PATH = "data.db"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    return col in cols

def _ensure_column(conn: sqlite3.Connection, table: str, col: str, ddl: str):
    # ddl example: "INTEGER DEFAULT 0"
    if not _col_exists(conn, table, col):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

def init_db():
    conn = db()
    cur = conn.cursor()

    # Base users table (старое ядро)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        username TEXT,
        mode TEXT DEFAULT 'menu',

        used_today INTEGER DEFAULT 0,
        last_reset TEXT,

        vip_until TEXT
    )
    """)

    # Add new columns safely
    _ensure_column(conn, "users", "media_used_today", "INTEGER DEFAULT 0")
    _ensure_column(conn, "users", "media_last_reset", "TEXT")
    _ensure_column(conn, "users", "bonus_ask", "INTEGER DEFAULT 0")
    _ensure_column(conn, "users", "bonus_media", "INTEGER DEFAULT 0")
    _ensure_column(conn, "users", "referrals_count", "INTEGER DEFAULT 0")
    _ensure_column(conn, "users", "referred_by", "INTEGER")
    _ensure_column(conn, "users", "referral_credited", "INTEGER DEFAULT 0")

    _ensure_column(conn, "users", "promptday_last_date", "TEXT")
    _ensure_column(conn, "users", "challenge_day", "INTEGER DEFAULT 1")
    _ensure_column(conn, "users", "challenge_done_date", "TEXT")

    # Prompts storage
    cur.execute("""
    CREATE TABLE IF NOT EXISTS prompts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL
    )
    """)

    # Payments
    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER NOT NULL,
        telegram_payment_charge_id TEXT,
        payload TEXT,
        created_at TEXT
    )
    """)

    # Referrals
    cur.execute("""
    CREATE TABLE IF NOT EXISTS referrals (
        referrer_id INTEGER NOT NULL,
        referee_id INTEGER NOT NULL,
        created_at TEXT,
        UNIQUE(referrer_id, referee_id)
    )
    """)

    # Prompt of the day cache
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_prompts (
        day TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        created_at TEXT
    )
    """)

    # Video jobs (async)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS video_jobs (
        video_id TEXT PRIMARY KEY,
        tg_id INTEGER NOT NULL,
        prompt TEXT,
        status TEXT,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()

def upsert_user(tg_id: int, username: Optional[str]):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,))
    exists = cur.fetchone() is not None
    today = datetime.now(tz).date().isoformat()
    if not exists:
        cur.execute(
            "INSERT INTO users (tg_id, username, last_reset, media_last_reset, promptday_last_date) VALUES (?, ?, ?, ?, ?)",
            (tg_id, username or "", today, today, "")
        )
    else:
        cur.execute("UPDATE users SET username=? WHERE tg_id=?", (username or "", tg_id))
    conn.commit()
    conn.close()

def get_user(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    row = cur.fetchone()
    conn.close()
    return row

def set_mode(tg_id: int, mode: str):
    conn = db()
    conn.execute("UPDATE users SET mode=? WHERE tg_id=?", (mode, tg_id))
    conn.commit()
    conn.close()

def is_vip(row) -> bool:
    if not row:
        return False
    vu = row["vip_until"]
    if not vu:
        return False
    try:
        return datetime.fromisoformat(vu).replace(tzinfo=tz) > datetime.now(tz)
    except Exception:
        return False

def reset_limits_if_needed(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT used_today, last_reset, media_used_today, media_last_reset FROM users WHERE tg_id=?", (tg_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return

    today = datetime.now(tz).date().isoformat()

    if r["last_reset"] != today:
        cur.execute(
            "UPDATE users SET used_today=0, last_reset=? WHERE tg_id=?",
            (today, tg_id)
        )
    if r["media_last_reset"] != today:
        cur.execute(
            "UPDATE users SET media_used_today=0, media_last_reset=? WHERE tg_id=?",
            (today, tg_id)
        )

    conn.commit()
    conn.close()

def inc_usage_ask(tg_id: int):
    conn = db()
    conn.execute("UPDATE users SET used_today = used_today + 1 WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()

def inc_usage_media(tg_id: int):
    conn = db()
    conn.execute("UPDATE users SET media_used_today = media_used_today + 1 WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()

def add_bonus(referrer_id: int, bonus_ask: int = 5, bonus_media: int = 1):
    conn = db()
    conn.execute(
        "UPDATE users SET bonus_ask = bonus_ask + ?, bonus_media = bonus_media + ?, referrals_count = referrals_count + 1 WHERE tg_id=?",
        (bonus_ask, bonus_media, referrer_id)
    )
    conn.commit()
    conn.close()

def set_vip(tg_id: int, days: int):
    until = (datetime.now(tz) + timedelta(days=days)).isoformat()
    conn = db()
    conn.execute("UPDATE users SET vip_until=? WHERE tg_id=?", (until, tg_id))
    conn.commit()
    conn.close()

def mark_referred_by(tg_id: int, referrer_id: int):
    conn = db()
    conn.execute(
        "UPDATE users SET referred_by=?, referral_credited=0 WHERE tg_id=?",
        (referrer_id, tg_id)
    )
    conn.commit()
    conn.close()

def credit_referral_once(referrer_id: int, referee_id: int) -> bool:
    # returns True if newly credited
    conn = db()
    try:
        conn.execute(
            "INSERT INTO referrals(referrer_id, referee_id, created_at) VALUES (?,?,?)",
            (referrer_id, referee_id, datetime.now(tz).isoformat())
        )
        conn.execute("UPDATE users SET referral_credited=1 WHERE tg_id=?", (referee_id,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def get_ref_stats(tg_id: int) -> Tuple[int, int, int]:
    row = get_user(tg_id)
    if not row:
        return (0, 0, 0)
    return (int(row["referrals_count"] or 0), int(row["bonus_ask"] or 0), int(row["bonus_media"] or 0))

def seed_prompts_if_empty():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM prompts")
    c = cur.fetchone()["c"]
    if c == 0:
        samples = [
            ("Оживление фото", "Лицо 1:1 (без куклы)", "УЛЬТРА-реалистично, натуральная текстура кожи, без beauty-фильтров. Сохранить личность 1:1: не менять форму лица/глаз/носа/губ, не взрослить. Мягкий ключевой свет + лёгкий контровой, реалистичная оптика 50mm, shallow DOF. Негатив: no face morph, no wax skin, no over-smoothing."),
            ("Sora", "Видео из 1 фото (10 сек)", "Cinematic 4K, 9:16, 10s. Subtle head turn 5°, natural blink, micro-expressions, breathing. Identity locked to reference. Soft film grain, realistic motion blur, no distortion."),
            ("HeyGen", "Говорящая голова (15 сек)", "Friendly confident tone, slight smile. Clean studio lighting, natural skin texture, no over-sharpen. Script: 1 хук + 1 польза + CTA в Telegram."),
            ("Reels-хуки", "3 хука на выбор", "1) 'Смотри, это сделано из 1 фото…' 2) 'Почему у всех лицо кукла — и как исправить' 3) 'Хочешь промт? Напиши ПРОМТ'"),
        ]
        cur.executemany("INSERT INTO prompts(category,title,body) VALUES (?,?,?)", samples)
        conn.commit()
    conn.close()

def list_categories():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT category FROM prompts ORDER BY category")
    cats = [r["category"] for r in cur.fetchall()]
    conn.close()
    return cats

def list_prompts(category: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id,title FROM prompts WHERE category=? ORDER BY id", (category,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_prompt(pid: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM prompts WHERE id=?", (pid,))
    r = cur.fetchone()
    conn.close()
    return r

def log_payment(tg_id: int, charge_id: str, payload: str):
    conn = db()
    conn.execute(
        "INSERT INTO payments(tg_id, telegram_payment_charge_id, payload, created_at) VALUES (?,?,?,?)",
        (tg_id, charge_id, payload, datetime.now(tz).isoformat())
    )
    conn.commit()
    conn.close()

# ============================
# Challenge 30 days
# ============================
CHALLENGE = [
    ("День 1 — Реалистичная кожа", "Сделай фото без 'кукольности': поры, микро-текстуры, мягкий свет.", "ULTRA realistic skin texture, natural pores, no smoothing, soft key light, 50mm, shallow DOF, identity locked."),
    ("День 2 — Лицо 1:1 (анти-искажения)", "Добейся совпадения черт и пропорций, без 'улучшайзинга'.", "identity locked, keep exact face shape, no beautify, no age change, no symmetry boost, realistic lens, subtle grain."),
    ("День 3 — Поза и руки без поломок", "Сгенерируй портрет с руками без артефактов.", "hands correct anatomy, five fingers, natural pose, realistic joints, no extra limbs, photorealistic."),
    ("День 4 — Свет как в глянце", "Сделай свет: key + fill + rim, как fashion/editorial.", "editorial lighting setup, key light + fill + rim light, clean highlights, soft shadows, 8k photoreal."),
    ("День 5 — Киношная картинка", "Сделай cinematic кадр: композиция, глубина, атмосфера.", "cinematic composition, film grain, soft contrast, realistic motion blur, 35mm anamorphic look."),
    ("День 6 — Ночной город", "Сделай ночную сцену с неоном и отражениями.", "night city, neon reflections, wet asphalt, realistic bokeh, high dynamic range."),
    ("День 7 — Снег/зима реалистично", "Сделай снег, чтобы он выглядел настоящим.", "real snowflakes, natural accumulation, cold color temperature, breath vapor, realistic winter clothing texture."),
    ("День 8 — Лук 'как у Кристины'", "Собери образ + промт + настройки.", "fashion winter editorial, sharp skin, identity locked, 9:16, 4k."),
    ("День 9 — Reels: хук 2 секунды", "Придумай хук + текст на экране.", "Hook: 'Это сделано из 1 фото…' On-screen text, fast pacing."),
    ("День 10 — Видео 8–10 сек", "Сделай короткое видео с микро-движениями.", "subtle head turn, blink, breathing, micro expressions, 9:16, cinematic."),
    ("День 11 — Говорящая голова", "Сделай talking-head под голос.", "studio lighting, natural skin, slight smile, clear speech pacing."),
    ("День 12 — До/после (вау)", "Сделай сравнение плохой/хорошей генерации (описание).", "no wax skin vs natural pores, show improvement."),
    ("День 13 — Стилизация 'глянец'", "Сделай обложку/портрет в глянце.", "high fashion cover, clean typography space, editorial pose."),
    ("День 14 — Стилизация 'кино'", "Сделай кадр как постер фильма.", "movie poster look, cinematic lighting, dramatic atmosphere."),
    ("День 15 — Стилизация 'теплый интерьер'", "Сделай уютную сцену с теплым светом.", "warm interior, amber light, soft shadows, realistic fabric folds."),
    ("День 16 — 3 варианта одного промта", "Сделай 3 вариации с разными объективами.", "24mm / 50mm / 85mm versions."),
    ("День 17 — Композиция", "Правило третей / ведущие линии.", "rule of thirds, leading lines, balanced composition."),
    ("День 18 — Цветокор", "Сделай киношный grade.", "cinematic color grading, teal-orange subtle, natural skin tones."),
    ("День 19 — Сценарий Reels", "Хук → процесс → результат → CTA.", "reels structure: hook, steps, reveal, CTA."),
    ("День 20 — Текст для видео", "Сделай текст на экране (3 строки).", "short readable captions, high retention."),
    ("День 21 — Теги/описание", "Сделай подпись + 5 тегов.", "CTA to Telegram, niche tags."),
    ("День 22 — Ошибки (диагностика)", "Опиши: почему лицо 'плывет' и как чинить.", "identity lock, negative prompts, lighting."),
    ("День 23 — Пакет промтов", "Собери мини-пакет из 5 промтов.", "winter pack 5 prompts."),
    ("День 24 — Витрина работ", "Сделай 'лучшие работы' (описание поста).", "community showcase."),
    ("День 25 — Оффер VIP", "Сформулируй выгоды VIP.", "VIP benefits list."),
    ("День 26 — Рефералка", "Сформулируй приглашение другу.", "invite copy + bonus."),
    ("День 27 — Контент-план", "3 идеи роликов на неделю.", "weekly reels plan."),
    ("День 28 — Продающий прогрев", "Сделай прогрев на 3 сторис.", "story sequence."),
    ("День 29 — Автоворонка", "Сделай текст автоответа в Директ/бот.", "auto DM / bot CTA."),
    ("День 30 — Финал", "Итог + следующий шаг.", "final recap + CTA."),
]

# ============================
# OpenAI (chat) + Media (HTTP)
# ============================
oai = OpenAI(api_key=OPENAI_API_KEY)

OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")

async def ask_openai(question: str) -> str:
    def _call():
        return oai.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            temperature=0.7,
        )
    try:
        resp = await asyncio.to_thread(_call)
        text = resp.choices[0].message.content or ""
        return text.strip() or "Пустой ответ. Попробуй переформулировать запрос."
    except Exception as e:
        return f"⚠️ Ошибка GPT: {type(e).__name__}. Проверь Render → Logs."

async def openai_post(path: str, payload: dict) -> Tuple[Optional[dict], Optional[str]]:
    url = f"{OPENAI_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            try:
                j = r.json()
                msg = j.get("error", {}).get("message") or r.text
            except Exception:
                msg = r.text
            return None, f"{r.status_code}: {msg}"
        return r.json(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

async def openai_get_bytes(path: str) -> Tuple[Optional[bytes], Optional[str]]:
    url = f"{OPENAI_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.get(url, headers=headers)
        if r.status_code >= 400:
            return None, f"{r.status_code}: {r.text}"
        return r.content, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

async def generate_image_bytes(prompt: str) -> Tuple[Optional[bytes], Optional[str]]:
    # Docs: /v1/images (gpt-image-1). :contentReference[oaicite:1]{index=1}
    payload = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "size": "1024x1024",
    }
    j, err = await openai_post("/images", payload)
    if err:
        return None, err
    try:
        b64 = j["data"][0]["b64_json"]
        return base64.b64decode(b64), None
    except Exception:
        return None, "Не удалось разобрать ответ images API."

async def create_video_job(prompt: str) -> Tuple[Optional[str], Optional[str]]:
    # Docs: /v1/videos create (example returns model sora-2). :contentReference[oaicite:2]{index=2}
    payload = {
        "model": VIDEO_MODEL,
        "prompt": prompt,
        "size": "1024x1792",
        "seconds": 8,
        "quality": "standard",
    }
    j, err = await openai_post("/videos", payload)
    if err:
        return None, err
    vid = j.get("id")
    if not vid:
        return None, "Видео создано, но ID не найден."
    return vid, None

async def get_video_status(video_id: str) -> Tuple[Optional[dict], Optional[str]]:
    # Docs: GET /v1/videos/{video_id}. :contentReference[oaicite:3]{index=3}
    j, err = await openai_post(f"/videos/{video_id}", {})
    if err:
        return None, err
    return j, None

async def download_video_bytes(video_id: str) -> Tuple[Optional[bytes], Optional[str]]:
    # Docs: GET /v1/videos/{video_id}/content :contentReference[oaicite:4]{index=4}
    return await openai_get_bytes(f"/videos/{video_id}/content")

def get_or_create_prompt_of_day() -> str:
    today = datetime.now(tz).date().isoformat()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT text FROM daily_prompts WHERE day=?", (today,))
    r = cur.fetchone()
    if r:
        conn.close()
        return r["text"]

    # Генерим 1 раз в день текстом (коротко и полезно)
    # (синхронно, чтобы не усложнять; вызывается редко)
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            messages=[
                {"role": "system", "content": "Ты пишешь один лучший 'промт дня' для нейросетей (Sora/Meta/HeyGen)."},
                {"role": "user", "content": "Сделай 'Промт дня' в формате:\n— Название\n— Для чего\n— Промт\n— Негатив\n— Настройки (3 пункта)\nКоротко, без воды."}
            ],
            temperature=0.8,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            text = "Промт дня временно недоступен. Попробуй позже."
    except Exception:
        text = "Промт дня временно недоступен. Попробуй позже."

    cur.execute(
        "INSERT INTO daily_prompts(day, text, created_at) VALUES (?,?,?)",
        (today, text, datetime.now(tz).isoformat())
    )
    conn.commit()
    conn.close()
    return text

# ============================
# Telegram UI
# ============================
BOT_USERNAME = ""  # will be set at startup

def bot_link() -> str:
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}"
    return "https://t.me/"

def channel_link() -> str:
    return f"https://t.me/{TG_CHANNEL.lstrip('@')}"

def kb_subscribe():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url=channel_link())],
        [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")],
        [InlineKeyboardButton("👀 Пример результата", callback_data="sample")],
        [InlineKeyboardButton("📌 Что умеет бот", callback_data="about")],
    ])

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Промт дня", callback_data="prompt_day"),
         InlineKeyboardButton("📅 Челлендж 30 дней", callback_data="challenge")],
        [InlineKeyboardButton("🖼/🎥 Генерация (1/день)", callback_data="gen_media")],
        [InlineKeyboardButton("🎬 База промтов", callback_data="prompts")],
        [InlineKeyboardButton("🧠 Задать вопрос AI-агенту", callback_data="ask")],
        [InlineKeyboardButton("🎁 Пригласить друга (бонусы)", callback_data="invite")],
        [InlineKeyboardButton("⭐ VIP без лимитов", callback_data="vip")],
    ])

def kb_back_main():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="menu")]])

def kb_categories():
    cats = list_categories()
    rows = [[InlineKeyboardButton(c, callback_data=f"cat:{c}")] for c in cats]
    rows.append([InlineKeyboardButton("⬅️ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)

def kb_prompt_list(category: str):
    items = list_prompts(category)
    rows = [[InlineKeyboardButton(r["title"], callback_data=f"p:{r['id']}")] for r in items]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="prompts")])
    return InlineKeyboardMarkup(rows)

def kb_vip_buy():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ Купить VIP на {VIP_DAYS} дней — {VIP_PRICE_STARS} Stars", callback_data="buy_vip")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])

def kb_media_choice():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 Сгенерировать ФОТО", callback_data="gen_image")],
        [InlineKeyboardButton("🎥 Сгенерировать ВИДЕО", callback_data="gen_video")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])

def kb_video_check(video_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Проверить статус", callback_data=f"check_video:{video_id}")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])

def kb_challenge_actions():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я сделал(а) — следующий день", callback_data="challenge_done")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])

def kb_invite_share(ref_link: str):
    share_url = f"https://t.me/share/url?url={ref_link}&text=Забери%20промты%20и%20генерацию%20в%20боте%20Кристины%20🤍"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Поделиться ссылкой", url=share_url)],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])

# ============================
# Safe edit helper
# ============================
async def safe_edit(query, text: str, reply_markup=None, parse_mode=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        # avoid crash on: Message is not modified
        if "Message is not modified" in str(e):
            return
        # other edit errors -> fallback send
        try:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            return

# ============================
# Subscription gate + Referral credit on subscribe
# ============================
async def is_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(chat_id=TG_CHANNEL, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

async def require_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    ok = await is_subscribed(update, context)
    if ok:
        return True
    msg = f"Для доступа подпишись на канал {TG_CHANNEL} и нажми «Проверить подписку»."
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb_subscribe())
    elif update.callback_query:
        await safe_edit(update.callback_query, msg, reply_markup=kb_subscribe())
    return False

async def try_credit_referral_after_sub(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    row = get_user(user_id)
    if not row:
        return
    referred_by = row["referred_by"]
    credited = int(row["referral_credited"] or 0)
    if not referred_by or credited == 1:
        return
    if int(referred_by) == int(user_id):
        return

    # credit once
    if credit_referral_once(int(referred_by), int(user_id)):
        add_bonus(int(referred_by), bonus_ask=5, bonus_media=1)
        # if referrer has 3 referrals -> VIP 3 days
        ref_row = get_user(int(referred_by))
        try:
            if ref_row and int(ref_row["referrals_count"] or 0) >= 3 and not is_vip(ref_row):
                set_vip(int(referred_by), 3)
                await context.bot.send_message(
                    chat_id=int(referred_by),
                    text="🎉 У тебя 3 приглашённых! Я включил VIP на 3 дня 🤍",
                    reply_markup=kb_main()
                )
            else:
                await context.bot.send_message(
                    chat_id=int(referred_by),
                    text="🎁 Новый приглашённый по твоей ссылке!\n+5 AI-вопросов и +1 генерация фото/видео (к дневному лимиту).",
                    reply_markup=kb_main()
                )
        except Exception:
            pass

# ============================
# Commands
# ============================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)

    # Referral parse: /start ref_12345
    if context.args:
        m = re.match(r"^ref_(\d+)$", context.args[0])
        if m:
            referrer_id = int(m.group(1))
            if referrer_id != u.id:
                # store pending referral; credit only after subscription check
                mark_referred_by(u.id, referrer_id)

    text = (
        "Привет! Я AI-бот Кристины 🤍\n\n"
        "Здесь:\n"
        "• 🎁 Промт дня\n"
        "• 📅 Челлендж 30 дней\n"
        "• 🖼/🎥 Генерация фото/видео (1 раз в день бесплатно)\n"
        "• 🎬 База промтов\n"
        "• 🧠 AI-агент (вопросы)\n"
        "• 🎁 Рефералка (бонусы за приглашения)\n\n"
        f"✅ Чтобы открыть доступ — подпишись на канал: {TG_CHANNEL}\n"
        "Нажми «Проверить подписку»."
    )
    await update.message.reply_text(text, reply_markup=kb_subscribe())

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    set_mode(u.id, "menu")
    await update.message.reply_text("Меню:", reply_markup=kb_main())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — запуск\n"
        "/menu — меню\n"
        "/prompts — база промтов\n"
        "/ask — задать вопрос\n"
        "/vip — VIP\n",
        reply_markup=kb_main()
    )

async def prompts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    await update.message.reply_text("Выбери категорию промтов:", reply_markup=kb_categories())

async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    set_mode(u.id, "ask")
    await update.message.reply_text(
        f"Ок ✅ Напиши вопрос одним сообщением.\n\n"
        f"Лимит бесплатно: {DAILY_LIMIT_ASK}/день (+бонусы от рефералок). VIP — без лимитов.",
        reply_markup=kb_back_main()
    )

async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    await update.message.reply_text(
        f"VIP снимает лимиты:\n"
        f"• AI-вопросы без ограничений\n"
        f"• Генерация фото/видео без ограничений\n"
        f"Срок: {VIP_DAYS} дней\n"
        f"Цена: {VIP_PRICE_STARS} Stars",
        reply_markup=kb_vip_buy()
    )

# ============================
# Callbacks + Payments
# ============================
async def cbq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    upsert_user(u.id, u.username)
    data = query.data

    if data == "about":
        await safe_edit(
            query,
            "Я умею:\n"
            "• Проверять подписку на канал\n"
            "• 🎁 Промт дня\n"
            "• 📅 Челлендж 30 дней\n"
            "• 🖼/🎥 Генерация фото/видео (1/день free)\n"
            "• База промтов по кнопкам\n"
            "• AI-агент (лимит/день)\n"
            "• Рефералка (бонусы)\n"
            "• VIP через Telegram Stars",
            reply_markup=kb_subscribe()
        )
        return

    if data == "sample":
        await safe_edit(
            query,
            "Пример (коротко):\n\n"
            "<b>ПРОМТ:</b>\n"
            "<code>Ultra-realistic winter fashion editorial portrait, sharp skin texture, soft key light + rim, 50mm, shallow DOF, identity locked…</code>\n\n"
            "<b>NEGATIVE:</b>\n"
            "<code>no wax skin, no smoothing, no face morph, no extra fingers…</code>",
            reply_markup=kb_subscribe(),
            parse_mode=ParseMode.HTML
        )
        return

    if data == "check_sub":
        ok = await is_subscribed(update, context)
        if ok:
            await try_credit_referral_after_sub(u.id, context)
            set_mode(u.id, "menu")
            await safe_edit(query, "Доступ открыт ✅ Выбирай:", reply_markup=kb_main())
        else:
            await safe_edit(
                query,
                "Пока не вижу подписку 😕\n\n"
                f"1) Подпишись на {TG_CHANNEL}\n"
                "2) Вернись и нажми «Проверить подписку»\n\n"
                "⚠️ Важно: бот должен быть админом канала, чтобы видеть статус подписки.",
                reply_markup=kb_subscribe()
            )
        return

    # gate
    if not await require_sub(update, context):
        return

    if data == "menu":
        set_mode(u.id, "menu")
        await safe_edit(query, "Меню:", reply_markup=kb_main())
        return

    if data == "prompts":
        await safe_edit(query, "Выбери категорию промтов:", reply_markup=kb_categories())
        return

    if data.startswith("cat:"):
        cat = data.split(":", 1)[1]
        await safe_edit(query, f"Категория: {cat}", reply_markup=kb_prompt_list(cat))
        return

    if data.startswith("p:"):
        pid = int(data.split(":", 1)[1])
        p = get_prompt(pid)
        if not p:
            await safe_edit(query, "Промт не найден.", reply_markup=kb_back_main())
            return
        await safe_edit(
            query,
            f"<b>{p['title']}</b>\n\n<code>{p['body']}</code>",
            reply_markup=kb_back_main(),
            parse_mode=ParseMode.HTML
        )
        return

    if data == "ask":
        set_mode(u.id, "ask")
        await safe_edit(
            query,
            f"Ок ✅ Напиши вопрос одним сообщением.\n\n"
            f"Лимит бесплатно: {DAILY_LIMIT_ASK}/день (+бонусы). VIP — без лимитов.",
            reply_markup=kb_back_main()
        )
        return

    if data == "vip":
        await safe_edit(
            query,
            f"VIP снимает лимиты:\n"
            f"• AI-вопросы без ограничений\n"
            f"• Генерация фото/видео без ограничений\n"
            f"Срок: {VIP_DAYS} дней\n"
            f"Цена: {VIP_PRICE_STARS} Stars",
            reply_markup=kb_vip_buy()
        )
        return

    if data == "buy_vip":
        payload = f"vip_{u.id}_{int(datetime.now(tz).timestamp())}"
        prices = [LabeledPrice(label=f"VIP {VIP_DAYS} дней", amount=VIP_PRICE_STARS)]
        await context.bot.send_invoice(
            chat_id=u.id,
            title="VIP-доступ",
            description=f"VIP на {VIP_DAYS} дней: без лимитов + премиум функции",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=prices,
        )
        return

    if data == "invite":
        ref_link = f"{bot_link()}?start=ref_{u.id}"
        refs, bq, bm = get_ref_stats(u.id)
        text = (
            "🎁 Реферальная программа\n\n"
            f"Твоя ссылка:\n{ref_link}\n\n"
            f"Приглашено: {refs}\n"
            f"Бонусы (добавляются к дневным лимитам):\n"
            f"• +AI-вопросы: {bq}\n"
            f"• +генерации: {bm}\n\n"
            "Правила:\n"
            "• 1 приглашённый = +5 AI-вопросов и +1 генерация (к дневному лимиту)\n"
            "• 3 приглашённых = VIP на 3 дня 🤍\n\n"
            "Важно: бонус засчитывается после подписки на канал и проверки подписки."
        )
        await safe_edit(query, text, reply_markup=kb_invite_share(ref_link))
        return

    if data == "prompt_day":
        row = get_user(u.id)
        today = datetime.now(tz).date().isoformat()
        vip = is_vip(row)
        last = (row["promptday_last_date"] or "")
        if (not vip) and last == today:
            await safe_edit(query, "🎁 Ты уже забирал(а) «Промт дня» сегодня.\nVIP — без ограничений.", reply_markup=kb_main())
            return

        # mark taken
        conn = db()
        conn.execute("UPDATE users SET promptday_last_date=? WHERE tg_id=?", (today, u.id))
        conn.commit()
        conn.close()

        text = get_or_create_prompt_of_day()
        await safe_edit(query, f"🎁 <b>Промт дня</b>\n\n{text}", reply_markup=kb_main(), parse_mode=ParseMode.HTML)
        return

    if data == "challenge":
        row = get_user(u.id)
        day_idx = int(row["challenge_day"] or 1)
        done_date = row["challenge_done_date"] or ""
        if day_idx > len(CHALLENGE):
            await safe_edit(query, "🏁 Челлендж завершён! Хочешь — начнём заново? Напиши /start", reply_markup=kb_main())
            return

        title, goal, prompt = CHALLENGE[day_idx - 1]
        text = (
            f"📅 <b>Челлендж 30 дней</b>\n"
            f"<b>{title}</b>\n\n"
            f"🎯 Задача: {goal}\n\n"
            f"🧩 Промт-шаблон:\n<code>{prompt}</code>\n\n"
            f"✅ Нажми «Я сделал(а)», чтобы перейти на следующий день.\n"
            f"Ограничение: 1 день = 1 раз в сутки."
        )
        await safe_edit(query, text, reply_markup=kb_challenge_actions(), parse_mode=ParseMode.HTML)
        return

    if data == "challenge_done":
        row = get_user(u.id)
        today = datetime.now(tz).date().isoformat()
        done_date = row["challenge_done_date"] or ""
        if done_date == today:
            await safe_edit(query, "Ты уже отметил(ла) выполнение сегодня ✅\nНовый день откроется завтра.", reply_markup=kb_main())
            return
        day_idx = int(row["challenge_day"] or 1)
        if day_idx >= len(CHALLENGE):
            conn = db()
            conn.execute("UPDATE users SET challenge_day=?, challenge_done_date=? WHERE tg_id=?", (len(CHALLENGE)+1, today, u.id))
            conn.commit()
            conn.close()
            await safe_edit(query, "🏁 Ты прошёл(шла) челлендж 30/30! Красавчик 🤍", reply_markup=kb_main())
            return
        conn = db()
        conn.execute("UPDATE users SET challenge_day=challenge_day+1, challenge_done_date=? WHERE tg_id=?", (today, u.id))
        conn.commit()
        conn.close()
        await safe_edit(query, "✅ Готово! Следующий день откроется — нажми «Челлендж 30 дней».", reply_markup=kb_main())
        return

    if data == "gen_media":
        await safe_edit(query, "Выбери, что генерируем сегодня (free 1/день на выбор):", reply_markup=kb_media_choice())
        return

    if data == "gen_image":
        set_mode(u.id, "gen_image")
        await safe_edit(query, "🖼 Ок! Пришли текстом промт для ФОТО одним сообщением.", reply_markup=kb_back_main())
        return

    if data == "gen_video":
        set_mode(u.id, "gen_video")
        await safe_edit(query, "🎥 Ок! Пришли текстом промт для ВИДЕО одним сообщением.", reply_markup=kb_back_main())
        return

    if data.startswith("check_video:"):
        vid = data.split(":", 1)[1]
        # check status
        status_json, err = await get_video_status(vid)
        if err:
            await safe_edit(query, f"⚠️ Не могу проверить статус: {err}", reply_markup=kb_video_check(vid))
            return
        status = status_json.get("status", "unknown")
        if status != "completed":
            await safe_edit(
                query,
                f"🎥 Статус: <b>{status}</b>\nГотовность: {status_json.get('progress', 0)}%\n\n"
                "Нажми ещё раз через минуту.",
                reply_markup=kb_video_check(vid),
                parse_mode=ParseMode.HTML
            )
            return

        # download and send
        bts, derr = await download_video_bytes(vid)
        if derr:
            await safe_edit(query, f"⚠️ Видео готово, но не скачалось: {derr}", reply_markup=kb_video_check(vid))
            return

        await query.message.reply_video(video=bts, caption="🎥 Готово!", reply_markup=kb_main())
        return

# Payments
async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    await q.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    sp = update.message.successful_payment
    log_payment(u.id, sp.telegram_payment_charge_id, sp.invoice_payload)
    set_vip(u.id, VIP_DAYS)
    await update.message.reply_text(
        f"Оплата прошла ✅ VIP активирован на {VIP_DAYS} дней!\n\n"
        "Теперь:\n• AI-вопросы без лимитов\n• Фото/видео генерация без лимитов",
        reply_markup=kb_main()
    )

# ============================
# Message handler
# ============================
async def text_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)

    if not await require_sub(update, context):
        return

    reset_limits_if_needed(u.id)
    row = get_user(u.id)
    mode = row["mode"] if row else "menu"
    vip = is_vip(row)

    # MEDIA MODES
    if mode in ("gen_image", "gen_video"):
        used_media = int(row["media_used_today"] or 0)
        bonus_media = int(row["bonus_media"] or 0)
        media_limit = 10**9 if vip else (DAILY_LIMIT_MEDIA + bonus_media)

        if (not vip) and used_media >= media_limit:
            await update.message.reply_text(
                f"Лимит генераций исчерпан 😕\n\n"
                f"Free: {DAILY_LIMIT_MEDIA}/день (+бонусы). VIP — без лимитов.",
                reply_markup=kb_vip_buy()
            )
            set_mode(u.id, "menu")
            return

        prompt = (update.message.text or "").strip()
        if len(prompt) < 10:
            await update.message.reply_text("Напиши промт чуть подробнее (хотя бы 1–2 предложения).", reply_markup=kb_back_main())
            return

        if mode == "gen_image":
            await update.message.reply_text("🖼 Генерирую фото…", reply_markup=kb_back_main())
            img_bytes, err = await generate_image_bytes(prompt)
            if err:
                await update.message.reply_text(
                    f"⚠️ Не удалось сгенерировать фото.\nПричина: {err}\n\n"
                    "Проверь:\n• доступ к IMAGE_MODEL\n• лимиты/биллинг\n• корректность промта",
                    reply_markup=kb_main()
                )
                set_mode(u.id, "menu")
                return

            inc_usage_media(u.id)
            set_mode(u.id, "menu")
            await update.message.reply_photo(photo=img_bytes, caption="🖼 Готово!", reply_markup=kb_main())
            return

        if mode == "gen_video":
            await update.message.reply_text("🎥 Создаю задачу на видео…", reply_markup=kb_back_main())
            vid, err = await create_video_job(prompt)
            if err:
                await update.message.reply_text(
                    f"⚠️ Не удалось создать видео.\nПричина: {err}\n\n"
                    "Часто это значит, что у API-ключа нет доступа к видео-модели (Sora) или лимиты.",
                    reply_markup=kb_main()
                )
                set_mode(u.id, "menu")
                return

            # store job
            conn = db()
            conn.execute(
                "INSERT OR REPLACE INTO video_jobs(video_id, tg_id, prompt, status, created_at) VALUES (?,?,?,?,?)",
                (vid, u.id, prompt, "queued", datetime.now(tz).isoformat())
            )
            conn.commit()
            conn.close()

            inc_usage_media(u.id)
            set_mode(u.id, "menu")
            await update.message.reply_text(
                f"🎥 Задача создана: <code>{vid}</code>\nНажми «Проверить статус».",
                reply_markup=kb_video_check(vid),
                parse_mode=ParseMode.HTML
            )
            return

    # ASK MODE
    if mode == "ask":
        used = int(row["used_today"] or 0)
        bonus_ask = int(row["bonus_ask"] or 0)
        ask_limit = 10**9 if vip else (DAILY_LIMIT_ASK + bonus_ask)

        if (not vip) and used >= ask_limit:
            await update.message.reply_text(
                f"Лимит AI-вопросов исчерпан 😕\n\n"
                f"Free: {DAILY_LIMIT_ASK}/день (+бонусы). VIP — без лимитов.",
                reply_markup=kb_vip_buy()
            )
            return

        question = (update.message.text or "").strip()
        await update.message.reply_text("Думаю… 🤍")
        answer = await ask_openai(question)
        if not vip:
            inc_usage_ask(u.id)
        await update.message.reply_text(answer, reply_markup=kb_main())
        return

    # default
    await update.message.reply_text("Выбери действие в меню:", reply_markup=kb_main())

# ============================
# FastAPI + Webhook
# ============================
app = FastAPI()
application = Application.builder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("menu", menu_cmd))
application.add_handler(CommandHandler("help", help_cmd))
application.add_handler(CommandHandler("prompts", prompts_cmd))
application.add_handler(CommandHandler("ask", ask_cmd))
application.add_handler(CommandHandler("vip", vip_cmd))

application.add_handler(CallbackQueryHandler(cbq))
application.add_handler(PreCheckoutQueryHandler(precheckout))
application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_msg))

@app.on_event("startup")
async def on_startup():
    global BOT_USERNAME
    init_db()
    seed_prompts_if_empty()

    await application.initialize()
    await application.start()

    try:
        me = await application.bot.get_me()
        BOT_USERNAME = me.username or ""
        print("Bot username:", BOT_USERNAME)
    except Exception as e:
        print("Could not fetch bot username:", e)

    if WEBHOOK_BASE:
        webhook_url = f"{WEBHOOK_BASE}/webhook"
        await application.bot.set_webhook(webhook_url)
        print("Webhook set:", webhook_url)
    else:
        print("WEBHOOK_BASE is empty. Set it in hosting env and redeploy to enable webhook.")

@app.on_event("shutdown")
async def on_shutdown():
    await application.stop()
    await application.shutdown()

@app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "ok"}

@app.head("/")
async def head_root():
    return {"status": "ok"}
