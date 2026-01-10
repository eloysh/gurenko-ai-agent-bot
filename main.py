import os
import re
import sqlite3
import base64
import asyncio
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

from fastapi import FastAPI, Request
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Image generation (OpenAI Images API)
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")

# Video generation model name (depends on your access)
OPENAI_VIDEO_MODEL = os.getenv("OPENAI_VIDEO_MODEL", "sora-2")  # may not be available

TG_CHANNEL = os.getenv("TG_CHANNEL", "@gurenko_kristina_ai")
TZ_NAME = os.getenv("TZ", "Asia/Tokyo")

# limits
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "3"))          # daily Q&A credits (free)
MEDIA_DAILY_FREE = int(os.getenv("MEDIA_DAILY_FREE", "1"))# 1 media/day free (photo or video)

# VIP
VIP_DAYS = int(os.getenv("VIP_DAYS", "30"))
VIP_PRICE_STARS = int(os.getenv("VIP_PRICE_STARS", "299"))

# webhook base, e.g. https://gurenko-ai-agent-bot.onrender.com
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

# Referral rewards
REF_BONUS_QUESTIONS = 5          # +5 вопросов за 1 приглашенного
REF_BONUS_FOR_3DAYS_VIP = 3      # VIP days for milestone
REF_MILESTONE = 3                # after 3 confirmed invites -> +3 days VIP

# ============================
# DB (SQLite)
# ============================
DB_PATH = "data.db"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        mode TEXT DEFAULT 'menu',

        used_today INTEGER DEFAULT 0,
        media_used_today INTEGER DEFAULT 0,
        last_reset TEXT,

        vip_until TEXT,

        referred_by INTEGER,
        ref_awarded INTEGER DEFAULT 0,
        referrals_count INTEGER DEFAULT 0,
        ref_bonus_left INTEGER DEFAULT 0,

        prompt_day_date TEXT,
        prompt_day_claims_today INTEGER DEFAULT 0,

        challenge_day INTEGER DEFAULT 0,           -- 0 = not started
        challenge_last_date TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS prompts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER NOT NULL,
        telegram_payment_charge_id TEXT,
        payload TEXT,
        created_at TEXT
    )
    """)

    # prompt-of-day pool
    cur.execute("""
    CREATE TABLE IF NOT EXISTS prompt_of_day (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        body TEXT NOT NULL
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
            "INSERT INTO users (tg_id, username, last_reset) VALUES (?, ?, ?)",
            (tg_id, username or "", today)
        )
    else:
        cur.execute(
            "UPDATE users SET username=? WHERE tg_id=?",
            (username or "", tg_id)
        )
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
    cur = conn.cursor()
    cur.execute("UPDATE users SET mode=? WHERE tg_id=?", (mode, tg_id))
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

def reset_if_needed(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT used_today, media_used_today, last_reset, prompt_day_date, prompt_day_claims_today FROM users WHERE tg_id=?", (tg_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return

    today = datetime.now(tz).date().isoformat()
    if r["last_reset"] != today:
        cur.execute(
            "UPDATE users SET used_today=0, media_used_today=0, last_reset=? WHERE tg_id=?",
            (today, tg_id)
        )
    # reset prompt-of-day counter daily
    if r["prompt_day_date"] != today:
        cur.execute(
            "UPDATE users SET prompt_day_date=?, prompt_day_claims_today=0 WHERE tg_id=?",
            (today, tg_id)
        )

    conn.commit()
    conn.close()

def set_vip(tg_id: int, days: int):
    until = (datetime.now(tz) + timedelta(days=days)).isoformat()
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET vip_until=? WHERE tg_id=?", (until, tg_id))
    conn.commit()
    conn.close()

def add_ref_bonus(inviter_id: int, bonus_questions: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET ref_bonus_left = ref_bonus_left + ? WHERE tg_id=?", (bonus_questions, inviter_id))
    conn.commit()
    conn.close()

def inc_referrals(inviter_id: int) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE tg_id=?", (inviter_id,))
    conn.commit()
    cur.execute("SELECT referrals_count FROM users WHERE tg_id=?", (inviter_id,))
    count = int(cur.fetchone()["referrals_count"])
    conn.close()
    return count

def mark_ref_awarded(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET ref_awarded=1 WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()

def set_referred_by(tg_id: int, inviter_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT referred_by FROM users WHERE tg_id=?", (tg_id,))
    r = cur.fetchone()
    if r and r["referred_by"] is None:
        cur.execute("UPDATE users SET referred_by=? WHERE tg_id=?", (inviter_id, tg_id))
        conn.commit()
    conn.close()

def take_question_credit(tg_id: int) -> Tuple[bool, str]:
    """
    Returns (ok, reason). If VIP -> ok.
    If free: first use used_today until DAILY_LIMIT, then use ref_bonus_left credits.
    """
    reset_if_needed(tg_id)
    row = get_user(tg_id)
    if not row:
        return False, "user_not_found"

    if is_vip(row):
        return True, "vip"

    used = int(row["used_today"])
    bonus = int(row["ref_bonus_left"])

    conn = db()
    cur = conn.cursor()

    if used < DAILY_LIMIT:
        cur.execute("UPDATE users SET used_today = used_today + 1 WHERE tg_id=?", (tg_id,))
        conn.commit()
        conn.close()
        return True, "free"

    if bonus > 0:
        cur.execute("UPDATE users SET ref_bonus_left = ref_bonus_left - 1 WHERE tg_id=?", (tg_id,))
        conn.commit()
        conn.close()
        return True, "ref_bonus"

    conn.close()
    return False, "limit"

def take_media_credit(tg_id: int) -> Tuple[bool, str]:
    reset_if_needed(tg_id)
    row = get_user(tg_id)
    if not row:
        return False, "user_not_found"
    if is_vip(row):
        return True, "vip"

    used = int(row["media_used_today"])
    if used >= MEDIA_DAILY_FREE:
        return False, "limit"

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET media_used_today = media_used_today + 1 WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()
    return True, "free"

def seed_prompts_if_empty():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM prompts")
    c = int(cur.fetchone()["c"])
    if c == 0:
        samples = [
            ("Оживление фото", "Лицо 1:1 (без куклы)", "УЛЬТРА-реалистично, натуральная текстура кожи, без beauty-фильтров. Сохранить личность 1:1: не менять форму лица/глаз/носа/губ, не взрослить. Мягкий ключевой свет + лёгкий контровой, реалистичная оптика 50mm, shallow DOF. Негатив: no face morph, no wax skin, no over-smoothing."),
            ("Sora", "Видео из 1 фото (10 сек)", "Cinematic 4K, 9:16, 10s. Subtle head turn 5°, natural blink, micro-expressions, breathing. Identity locked to reference. Soft film grain, realistic motion blur, no distortion."),
            ("HeyGen", "Говорящая голова (15 сек)", "Friendly confident tone, slight smile. Clean studio lighting, natural skin texture, no over-sharpen. Script: 1 хук + 1 польза + CTA в Telegram."),
            ("Suno", "Вирусный хук (12–18 сек)", "Modern pop/edm hook, 124 bpm, punchy drums, catchy topline, Russian lyrics, 1 hook line repeated. No kids choir."),
            ("Reels-хуки", "3 хука на выбор", "1) 'Смотри, это сделано из 1 фото…' 2) 'Почему у всех лицо кукла — и как исправить' 3) 'Хочешь промт? Напиши ПРОМТ'"),
        ]
        cur.executemany("INSERT INTO prompts(category,title,body) VALUES (?,?,?)", samples)
        conn.commit()
    conn.close()

def seed_prompt_of_day_if_empty():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM prompt_of_day")
    c = int(cur.fetchone()["c"])
    if c == 0:
        pool = [
            ("Промт дня: Анти-кукла кожа",
             "УЛЬТРА-реалистично, натуральная кожа, поры видны, без пластика. Сохранить личность 1:1. Мягкий ключевой свет, 50mm, shallow DOF. Негатив: wax skin, over-smooth, doll face, face morph."),
            ("Промт дня: Зимний глянец",
             "Winter fashion-editorial, cinematic 4K, natural skin texture, soft film grain, backlight snow sparkles, 85mm portrait look. Негатив: over-sharpen, plastic skin, distorted face."),
            ("Промт дня: Ночной город",
             "Night city cinematic, wet asphalt reflections, neon rim light, realistic motion blur, natural micro-expressions, no beauty filter. Негатив: AI artifacts, face warping."),
            ("Промт дня: Reels-хук",
             "Хук (первые 2 сек): 'Это не съёмка — это 1 фото…' → 3 кадра до/после → CTA: 'Хочешь промт? Напиши ПРОМТ в коммент'.")
        ]
        cur.executemany("INSERT INTO prompt_of_day(title, body) VALUES (?,?)", pool)
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
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO payments(tg_id, telegram_payment_charge_id, payload, created_at) VALUES (?,?,?,?)",
        (tg_id, charge_id, payload, datetime.now(tz).isoformat())
    )
    conn.commit()
    conn.close()

def get_prompt_of_day_for_today() -> Tuple[str, str]:
    """Simple rotation by date ordinal."""
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM prompt_of_day")
    c = int(cur.fetchone()["c"])
    if c <= 0:
        conn.close()
        return ("Промт дня", "Пул пуст. Добавь записи в prompt_of_day.")
    idx = date.today().toordinal() % c
    cur.execute("SELECT title, body FROM prompt_of_day ORDER BY id LIMIT 1 OFFSET ?", (idx,))
    row = cur.fetchone()
    conn.close()
    return (row["title"], row["body"])

def inc_prompt_day_claim(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET prompt_day_claims_today = prompt_day_claims_today + 1 WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()

# ============================
# Challenge 30 days
# ============================
CHALLENGE_30 = [
    "День 1: Реалистичная кожа (анти-кукла) — сделай 1 фото и напиши 3 ошибки, которые были раньше.",
    "День 2: 10 секунд видео из 1 фото — микро-движения + моргание.",
    "День 3: Говорящая голова (HeyGen) — 1 хук + 1 польза + CTA.",
    "День 4: 3 варианта света (soft / hard / backlight) — сравни результат.",
    "День 5: Кино-кадр 24fps — настроение/цвет/зерно.",
    "День 6: Ночной город — неон + отражения + атмосферный дождь.",
    "День 7: Зимний глянец — снег, контровой, микроблики.",
    "День 8: Reels структура — хук/показ/CTA (15 сек).",
    "День 9: Промт под стиль — 'Тёплый интерьер' (3 вариации).",
    "День 10: Face consistency — запреты на морфинг лица.",
    "День 11: Камера 35mm vs 85mm — сравнение.",
    "День 12: Текст на экране — 3 формулы (любопытство/выгода/боль).",
    "День 13: Переход 'до/после' — 1 сек, без дерганий.",
    "День 14: Сценарий 30 сек — 5 кадров по 6 сек.",
    "День 15: 'Сделай как у Кристины' — фирменный шаблон (хук+промт+настройки+подпись+5 тегов).",
    "День 16: Шаблон для подписчиков — 'нажми ПРОМТ'.",
    "День 17: Ошибки реализма — список 10 анти-ошибок.",
    "День 18: Референсы — как задавать стиль без потери лица.",
    "День 19: Коммерческий оффер — 3 пакета услуг.",
    "День 20: Видеопетля 10 сек — бесшовная.",
    "День 21: Монтаж — 3 правила темпа (0–2/2–6/6–12 сек).",
    "День 22: Аудио-озвучка — эмоции, темп, паузы.",
    "День 23: Подбор музыки — 5 вариантов под один ролик.",
    "День 24: Воронка в Telegram — 3 сообщения авто-цепочки.",
    "День 25: Витрина результатов — как собирать работы подписчиков.",
    "День 26: Мини-пакет промтов — собери 10 и оформи.",
    "День 27: Разбор 'почему не залетело' — чек-лист.",
    "День 28: Повторение — улучшение лучшего ролика.",
    "День 29: Серия из 3 Reels — одна тема, разный хук.",
    "День 30: Итог — упакуй оффер + закреп + CTA."
]

def challenge_get_day_text(day: int) -> str:
    if day <= 0:
        return "Челлендж ещё не начат."
    if day > 30:
        return "Челлендж завершён 🎉"
    return CHALLENGE_30[day-1]

def challenge_start(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET challenge_day=1, challenge_last_date=? WHERE tg_id=?", (date.today().isoformat(), tg_id))
    conn.commit()
    conn.close()

def challenge_done(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT challenge_day FROM users WHERE tg_id=?", (tg_id,))
    r = cur.fetchone()
    day = int(r["challenge_day"]) if r else 0
    if day <= 0:
        conn.close()
        return
    day = min(day + 1, 31)  # 31 means finished
    cur.execute("UPDATE users SET challenge_day=?, challenge_last_date=? WHERE tg_id=?", (day, date.today().isoformat(), tg_id))
    conn.commit()
    conn.close()

# ============================
# OpenAI client
# ============================
oai = OpenAI(api_key=OPENAI_API_KEY)

async def ask_openai_text(question: str) -> str:
    def _call():
        return oai.chat.completions.create(
            model=OPENAI_MODEL,
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

async def generate_image(prompt: str) -> Tuple[bool, str, Optional[bytes]]:
    """
    Returns (ok, message, image_bytes)
    """
    def _call():
        # OpenAI Images API
        return oai.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024",
            response_format="b64_json",
        )

    try:
        resp = await asyncio.to_thread(_call)
        b64 = resp.data[0].b64_json
        img_bytes = base64.b64decode(b64)
        return True, "✅ Готово", img_bytes
    except Exception as e:
        return False, f"⚠️ Не удалось сгенерировать фото: {type(e).__name__}. Проверь модель/доступ/лимиты API.", None

async def generate_video(prompt: str) -> Tuple[bool, str, Optional[str]]:
    """
    Video API depends on account access. We try a few likely SDK shapes.
    Returns (ok, message, video_url_or_id)
    """
    try:
        # Try common shapes safely
        if hasattr(oai, "videos"):
            videos = getattr(oai, "videos")
            if hasattr(videos, "generate"):
                def _call():
                    return videos.generate(model=OPENAI_VIDEO_MODEL, prompt=prompt)
                resp = await asyncio.to_thread(_call)
                # Best effort extraction
                url = getattr(resp, "url", None)
                if not url and hasattr(resp, "data") and resp.data:
                    url = getattr(resp.data[0], "url", None) or getattr(resp.data[0], "id", None)
                return True, "✅ Видео поставлено в генерацию.", str(url) if url else None

        return False, "⚠️ Видео-модель недоступна в SDK/аккаунте. Это не ошибка бота — нужен доступ к Sora video API.", None
    except Exception as e:
        return False, f"⚠️ Не удалось сгенерировать видео: {type(e).__name__}. Возможно нет доступа к Sora.", None

# ============================
# Telegram UI
# ============================
BOT_USERNAME: str = ""  # filled on startup

def safe_edit(query, text: str, reply_markup=None, parse_mode=None):
    async def _do():
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
            raise
    return _do()

def kb_subscribe():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")],
        [InlineKeyboardButton("📌 Что умеет бот", callback_data="about")],
        [InlineKeyboardButton("🎁 Пригласить друга", callback_data="invite")],
    ])

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 База промтов", callback_data="prompts")],
        [InlineKeyboardButton("🧠 Задать вопрос AI-агенту", callback_data="ask")],
        [InlineKeyboardButton("🎁 Промт дня", callback_data="daily")],
        [InlineKeyboardButton("🏁 Челлендж 30 дней", callback_data="challenge")],
        [InlineKeyboardButton("🖼️ Sora: Фото/Видео", callback_data="sora")],
        [InlineKeyboardButton("🎁 Пригласить друга", callback_data="invite")],
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

def kb_sora_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Сгенерировать ФОТО (1/день бесплатно)", callback_data="sora_photo")],
        [InlineKeyboardButton("🎞️ Сгенерировать ВИДЕО (1/день бесплатно)", callback_data="sora_video")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])

def kb_challenge_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готово / следующий день", callback_data="challenge_done")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])

def referral_link(inviter_id: int) -> str:
    if not BOT_USERNAME:
        return f"t.me/{BOT_USERNAME}?start=ref_{inviter_id}"
    return f"https://t.me/{BOT_USERNAME}?start=ref_{inviter_id}"

def kb_invite(inviter_id: int):
    link = referral_link(inviter_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Поделиться ботом", url=link)],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])

# ============================
# Subscription gating
# ============================
async def is_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(chat_id=TG_CHANNEL, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except BadRequest:
        return False
    except Exception:
        return False

async def require_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    ok = await is_subscribed(update, context)
    if ok:
        return True

    text = (
        f"Для доступа подпишись на канал {TG_CHANNEL} и нажми «Проверить подписку» ✅\n\n"
        "⚠️ Если подписка есть, но не проходит — добавь бота админом в канал (можно без прав постинга)."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=kb_subscribe())
    elif update.callback_query:
        await safe_edit(update.callback_query, text, reply_markup=kb_subscribe())
    return False

# ============================
# Commands
# ============================
def parse_ref_arg(args_text: str) -> Optional[int]:
    m = re.search(r"(?:^| )ref_(\d+)", args_text.strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)

    # referral from /start ref_123
    inviter_id = None
    if context.args:
        inviter_id = parse_ref_arg(" ".join(context.args))
    if inviter_id and inviter_id != u.id:
        set_referred_by(u.id, inviter_id)

    text = (
        "Привет! Я AI-бот Кристины 🤍\n\n"
        "Здесь:\n"
        "• База промтов (Sora/HeyGen/Meta AI)\n"
        "• Промт дня\n"
        "• Челлендж 30 дней\n"
        "• AI-ответы как ChatGPT\n"
        "• Генерация фото/видео (если доступно в API)\n\n"
        f"✅ Чтобы открыть доступ — подпишись на канал: {TG_CHANNEL}\n"
        "И нажми «Проверить подписку»."
    )
    await update.message.reply_text(text, reply_markup=kb_subscribe())

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    set_mode(u.id, "menu")
    await update.message.reply_text("Меню:", reply_markup=kb_main())

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
    row = get_user(u.id)
    bonus = int(row["ref_bonus_left"]) if row else 0
    await update.message.reply_text(
        f"Ок ✅ Напиши свой вопрос одним сообщением.\n\n"
        f"Бесплатно: {DAILY_LIMIT}/день + бонусы за приглашения (сейчас: {bonus}).\n"
        "VIP — без лимитов.",
        reply_markup=kb_back_main()
    )

async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    await update.message.reply_text(
        f"VIP снимает лимиты и открывает максимум функций.\n"
        f"Срок: {VIP_DAYS} дней\n"
        f"Цена: {VIP_PRICE_STARS} Stars",
        reply_markup=kb_vip_buy()
    )

async def paysupport_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Поддержка по оплатам ⭐\n"
        "Если платеж прошёл, но VIP не включился — пришли:\n"
        "• свой @username\n"
        "• время оплаты\n"
        "• скрин чека Stars\n\n"
        "Мы проверим и включим доступ.",
        reply_markup=kb_main()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — запуск\n"
        "/menu — меню\n"
        "/prompts — база промтов\n"
        "/ask — задать вопрос\n"
        "/daily — промт дня\n"
        "/challenge — челлендж 30 дней\n"
        "/invite — пригласить друга\n"
        "/sora — генерация фото/видео\n"
        "/vip — VIP\n"
        "/paysupport — поддержка по оплатам",
        reply_markup=kb_main()
    )

async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    await send_prompt_of_day(update, context, u.id)

async def invite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    link = referral_link(u.id)
    await update.message.reply_text(
        "🎁 Пригласи друга и получи бонусы:\n"
        f"• за 1 друга: +{REF_BONUS_QUESTIONS} вопросов\n"
        f"• за {REF_MILESTONE} друзей: VIP на {REF_BONUS_FOR_3DAYS_VIP} дня\n\n"
        f"Твоя ссылка:\n{link}",
        reply_markup=kb_invite(u.id)
    )

async def challenge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return

    row = get_user(u.id)
    day = int(row["challenge_day"]) if row else 0
    if day <= 0:
        challenge_start(u.id)
        day = 1

    text = f"🏁 Челлендж 30 дней\n\n<b>День {day}/30</b>\n{challenge_get_day_text(day)}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_challenge_menu())

async def sora_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    await update.message.reply_text(
        "🖼️ Sora: генерация\n\nВыбери, что сделать (бесплатно 1 раз в день на выбор фото/видео; VIP — без ограничений):",
        reply_markup=kb_sora_menu()
    )

# ============================
# Prompt of day
# ============================
async def send_prompt_of_day(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_id: int, via_query=None):
    reset_if_needed(tg_id)
    row = get_user(tg_id)
    vip = is_vip(row)
    claims = int(row["prompt_day_claims_today"]) if row else 0

    max_claims = 3 if vip else 1
    if claims >= max_claims:
        text = f"🎁 Промт дня уже получен сегодня 🙂\n\nVIP может брать до 3/день."
        if via_query:
            await safe_edit(via_query, text, reply_markup=kb_main())
        else:
            await update.message.reply_text(text, reply_markup=kb_main())
        return

    title, body = get_prompt_of_day_for_today()
    inc_prompt_day_claim(tg_id)

    msg = f"🎁 <b>{title}</b>\n\n<code>{body}</code>"
    if via_query:
        await safe_edit(via_query, msg, reply_markup=kb_main(), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(msg, reply_markup=kb_main(), parse_mode=ParseMode.HTML)

# ============================
# Callbacks + Payments + flows
# ============================
async def cbq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    upsert_user(u.id, u.username)

    data = query.data

    if data == "about":
        text = (
            "Я умею:\n"
            "• Проверка подписки на канал\n"
            "• База промтов по кнопкам\n"
            "• Промт дня\n"
            "• Челлендж 30 дней\n"
            "• Рефералка (бонусы за друзей)\n"
            "• AI-ответы как ChatGPT\n"
            "• Генерация фото/видео (если доступно в API)\n"
        )
        await safe_edit(query, text, reply_markup=kb_subscribe())
        return

    if data == "check_sub":
        ok = await is_subscribed(update, context)
        if ok:
            # Award referral if exists and not yet awarded
            row = get_user(u.id)
            if row and row["referred_by"] and int(row["ref_awarded"]) == 0:
                inviter = int(row["referred_by"])
                add_ref_bonus(inviter, REF_BONUS_QUESTIONS)
                count = inc_referrals(inviter)
                mark_ref_awarded(u.id)

                # milestone VIP
                if count % REF_MILESTONE == 0:
                    set_vip(inviter, REF_BONUS_FOR_3DAYS_VIP)
                    try:
                        await context.bot.send_message(
                            chat_id=inviter,
                            text=f"🎉 У тебя {count} приглашённых! Дарю VIP на {REF_BONUS_FOR_3DAYS_VIP} дня 💛"
                        )
                    except Exception:
                        pass
                else:
                    try:
                        await context.bot.send_message(
                            chat_id=inviter,
                            text=f"🎁 Новый подписчик по твоей ссылке! +{REF_BONUS_QUESTIONS} вопросов ✅"
                        )
                    except Exception:
                        pass

            set_mode(u.id, "menu")
            await safe_edit(query, "Доступ открыт ✅ Выбирай:", reply_markup=kb_main())
        else:
            await safe_edit(
                query,
                "Пока не вижу подписку 😕\n\n"
                f"1) Подпишись на {TG_CHANNEL}\n"
                "2) Вернись и нажми «Проверить подписку»\n\n"
                "⚠️ Если подписка есть, но не проходит — добавь бота админом в канал.",
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
        row = get_user(u.id)
        bonus = int(row["ref_bonus_left"]) if row else 0
        await safe_edit(
            query,
            f"Ок ✅ Напиши свой вопрос одним сообщением.\n\n"
            f"Бесплатно: {DAILY_LIMIT}/день + бонусы (сейчас: {bonus}). VIP — без лимитов.",
            reply_markup=kb_back_main()
        )
        return

    if data == "daily":
        await send_prompt_of_day(update, context, u.id, via_query=query)
        return

    if data == "challenge":
        row = get_user(u.id)
        day = int(row["challenge_day"]) if row else 0
        if day <= 0:
            challenge_start(u.id)
            day = 1
        text = f"🏁 Челлендж 30 дней\n\n<b>День {day}/30</b>\n{challenge_get_day_text(day)}"
        await safe_edit(query, text, reply_markup=kb_challenge_menu(), parse_mode=ParseMode.HTML)
        return

    if data == "challenge_done":
        challenge_done(u.id)
        row = get_user(u.id)
        day = int(row["challenge_day"]) if row else 0
        if day >= 31:
            await safe_edit(query, "🎉 Челлендж завершён! Хочешь — начнём заново? Напиши /challenge", reply_markup=kb_main())
        else:
            text = f"✅ Отлично! Следующий шаг:\n\n<b>День {day}/30</b>\n{challenge_get_day_text(day)}"
            await safe_edit(query, text, reply_markup=kb_challenge_menu(), parse_mode=ParseMode.HTML)
        return

    if data == "invite":
        link = referral_link(u.id)
        await safe_edit(
            query,
            "🎁 Пригласи друга и получи бонусы:\n"
            f"• за 1 друга: +{REF_BONUS_QUESTIONS} вопросов\n"
            f"• за {REF_MILESTONE} друзей: VIP на {REF_BONUS_FOR_3DAYS_VIP} дня\n\n"
            f"Твоя ссылка:\n{link}",
            reply_markup=kb_invite(u.id)
        )
        return

    if data == "vip":
        await safe_edit(
            query,
            f"VIP снимает лимиты и открывает максимум функций.\n"
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

    if data == "sora":
        await safe_edit(
            query,
            "🖼️ Sora: генерация\n\nВыбери, что сделать (бесплатно 1 раз в день на выбор фото/видео; VIP — без ограничений):",
            reply_markup=kb_sora_menu()
        )
        return

    if data == "sora_photo":
        set_mode(u.id, "sora_photo")
        await safe_edit(query, "🖼️ Ок! Напиши промт одним сообщением (что сгенерировать).", reply_markup=kb_back_main())
        return

    if data == "sora_video":
        set_mode(u.id, "sora_video")
        await safe_edit(query, "🎞️ Ок! Напиши промт одним сообщением (что за видео).", reply_markup=kb_back_main())
        return

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
        "Теперь лимиты сняты.",
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

    row = get_user(u.id)
    mode = row["mode"] if row else "menu"

    txt = (update.message.text or "").strip()

    # Menu fallback
    if mode == "menu":
        await update.message.reply_text("Выбери действие в меню:", reply_markup=kb_main())
        return

    # Ask
    if mode == "ask":
        ok, why = take_question_credit(u.id)
        if not ok:
            await update.message.reply_text(
                f"Лимит {DAILY_LIMIT}/день исчерпан 😕\n\n"
                "⭐ Хочешь без лимитов? Подключи VIP.\n"
                "🎁 Или пригласи друга — получишь бонусы.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⭐ VIP", callback_data="vip")],
                    [InlineKeyboardButton("🎁 Пригласить друга", callback_data="invite")],
                    [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
                ])
            )
            return

        await update.message.reply_text("Думаю… 🤍")
        answer = await ask_openai_text(txt)
        await update.message.reply_text(answer, reply_markup=kb_main())
        return

    # Sora photo
    if mode == "sora_photo":
        ok, why = take_media_credit(u.id)
        if not ok:
            await update.message.reply_text(
                f"Лимит медиа на сегодня исчерпан 😕 (бесплатно {MEDIA_DAILY_FREE}/день)\n\n"
                "⭐ VIP снимает ограничения.",
                reply_markup=kb_vip_buy()
            )
            return

        await update.message.reply_text("Генерирую фото… 🖼️")
        ok2, msg, img_bytes = await generate_image(txt)
        if not ok2 or not img_bytes:
            await update.message.reply_text(msg, reply_markup=kb_main())
            return

        await update.message.reply_photo(photo=img_bytes, caption="✅ Готово", reply_markup=kb_main())
        set_mode(u.id, "menu")
        return

    # Sora video
    if mode == "sora_video":
        ok, why = take_media_credit(u.id)
        if not ok:
            await update.message.reply_text(
                f"Лимит медиа на сегодня исчерпан 😕 (бесплатно {MEDIA_DAILY_FREE}/день)\n\n"
                "⭐ VIP снимает ограничения.",
                reply_markup=kb_vip_buy()
            )
            return

        await update.message.reply_text("Ставлю видео в генерацию… 🎞️")
        ok2, msg, video_ref = await generate_video(txt)
        if not ok2:
            await update.message.reply_text(msg, reply_markup=kb_main())
            set_mode(u.id, "menu")
            return

        if video_ref:
            await update.message.reply_text(f"{msg}\n\nРезультат/ID: {video_ref}", reply_markup=kb_main())
        else:
            await update.message.reply_text(f"{msg}\n\n(Если ссылка не пришла — значит API отдал задачу асинхронно.)", reply_markup=kb_main())
        set_mode(u.id, "menu")
        return

    # Default
    await update.message.reply_text("Выбери действие в меню:", reply_markup=kb_main())
    set_mode(u.id, "menu")

# ============================
# FastAPI + Webhook
# ============================
app = FastAPI()
application = Application.builder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("menu", menu_cmd))
application.add_handler(CommandHandler("prompts", prompts_cmd))
application.add_handler(CommandHandler("ask", ask_cmd))
application.add_handler(CommandHandler("daily", daily_cmd))
application.add_handler(CommandHandler("challenge", challenge_cmd))
application.add_handler(CommandHandler("invite", invite_cmd))
application.add_handler(CommandHandler("sora", sora_cmd))
application.add_handler(CommandHandler("vip", vip_cmd))
application.add_handler(CommandHandler("paysupport", paysupport_cmd))
application.add_handler(CommandHandler("help", help_cmd))

application.add_handler(CallbackQueryHandler(cbq))
application.add_handler(PreCheckoutQueryHandler(precheckout))
application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_msg))

@app.on_event("startup")
async def on_startup():
    global BOT_USERNAME
    init_db()
    seed_prompts_if_empty()
    seed_prompt_of_day_if_empty()

    await application.initialize()
    await application.start()

    me = await application.bot.get_me()
    BOT_USERNAME = me.username or ""
    print("Bot username:", BOT_USERNAME)

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
