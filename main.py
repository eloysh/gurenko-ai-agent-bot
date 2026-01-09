import os
import sqlite3
import asyncio
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote

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
TG_CHANNEL = os.getenv("TG_CHANNEL", "@gurenko_kristina_ai")
TZ_NAME = os.getenv("TZ", "Asia/Tokyo")

DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "3"))
VIP_DAYS = int(os.getenv("VIP_DAYS", "30"))
VIP_PRICE_STARS = int(os.getenv("VIP_PRICE_STARS", "299"))
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")

# Referrals / Challenge
REF_BONUS_CREDITS = int(os.getenv("REF_BONUS_CREDITS", "5"))     # за 1 приглашенного +5 запросов
REF_VIP_INVITES = int(os.getenv("REF_VIP_INVITES", "3"))         # за 3 приглашенных VIP
REF_VIP_DAYS = int(os.getenv("REF_VIP_DAYS", "3"))               # VIP дней за 3 приглашенных
CHALLENGE_DAILY_GATE = os.getenv("CHALLENGE_DAILY_GATE", "1") == "1"  # 1 = шаг в день

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

REELS_AUDIT_PROMPT = """Ты — эксперт по Reels/Shorts.
Пользователь присылает ссылку/описание ролика и жалобу "не залетело".
Выдай:
1) Хук (0–2с): 3 варианта
2) Текст на экране: 3 варианта (до 5 слов)
3) Структура 8–12с (тайминг)
4) Монтаж/переходы (коротко)
5) CTA (1 фраза)
6) Ошибки (до 5 пунктов)
Пиши очень прикладно.
"""

PHOTO_PROMPT_PROMPT = """Ты — промт-режиссёр.
Пользователь отправляет фото как референс (анализировать лицо не нужно).
Сделай промт под выбранный инструмент (Sora / Meta AI / HeyGen).
Обязательно:
- "identity locked to reference photo 1:1"
- негатив-промт "анти-кукла/анти-искажения"
- 3 настройки (свет/камера/качество)
Дай 3 варианта промта: A/B/C.
Ответ форматируй так:
PROMPT A:
NEGATIVE:
SETTINGS:
и так 3 раза.
"""

# ============================
# PROMPT OF THE DAY (7 days loop)
# ============================
DAILY_PACK = [
    {
        "title": "Промт дня — Анти-кукла (реалистичная кожа)",
        "prompt": "Ultra-realistic close-up portrait, natural skin texture with pores and micro-details, subtle imperfections, realistic highlights, no beauty retouch. Identity locked to reference photo 1:1. Soft cinematic lighting, 50mm, shallow DOF, 8K.",
        "negative": "no smoothing, no wax skin, no doll face, no plastic skin, no enlarged eyes, no AI glamour, no face morph, no identity drift",
        "tip": "Движение/ретушь минимальные — так меньше «пластика».",
    },
    {
        "title": "Промт дня — Sora (видео 10s из 1 фото)",
        "prompt": "Cinematic 4K video, 9:16, 10 seconds. Identity locked 1:1 to the reference photo. Subtle head turn 5°, natural blink, micro-expressions, gentle breathing, slight hair movement. Film grain, realistic motion blur.",
        "negative": "no face morph, no jitter, no warping, no uncanny smile, no distorted eyes, no identity drift",
        "tip": "В Sora лучше микро-движение, чем активная мимика.",
    },
    {
        "title": "Промт дня — Дорогой глянец (studio)",
        "prompt": "High-end fashion editorial portrait, clean studio background, softbox key light + gentle rim light, crisp detail, natural skin texture, luxury look, neutral grading, 85mm lens, f/2.0, 8K. Identity locked 1:1.",
        "negative": "no glossy plastic skin, no overcontrast, no oversharpen, no heavy beauty filter, no identity drift",
        "tip": "‘Neutral grading’ + ‘softbox’ = ощущение дорогой съемки.",
    },
    {
        "title": "Промт дня — Снег без CGI",
        "prompt": "Ultra realistic winter portrait outdoors, gentle snowfall, snow crystals on hair and jacket, cold breath visible, natural skin texture preserved, cinematic lighting, realistic shadows, 8K. Identity locked 1:1.",
        "negative": "no fake snow overlay, no CGI snow, no blur face, no skin smoothing, no face morph, no identity drift",
        "tip": "Пиши ‘gentle snowfall’, а не ‘heavy particles’.",
    },
    {
        "title": "Промт дня — Кино-кадр (тёплый интерьер)",
        "prompt": "Cinematic portrait, warm amber practical lights in background (bokeh), soft key light, realistic skin pores, subtle film grain, 35mm lens, f/1.8, 8K, identity locked 1:1.",
        "negative": "no orange skin, no harsh HDR, no beauty filter, no wax skin, no identity drift",
        "tip": "Bokeh на фоне почти всегда усиливает «киношность».",
    },
    {
        "title": "Промт дня — 3 ракурса, одно лицо (1:1)",
        "prompt": "Create three ultra-realistic portraits of the same person with identity preserved 1:1: (1) front, (2) 3/4, (3) profile. Keep facial proportions identical, consistent hairstyle, natural skin texture. Cinematic soft lighting, 8K.",
        "negative": "no identity drift, no different person, no age change, no face morph, no doll face",
        "tip": "Добавляй ‘same person’ и ‘no identity drift’ обязательно.",
    },
    {
        "title": "Промт дня — Упаковка Reels (чтобы залетало)",
        "prompt": "Сценарий 10 сек: 0–1с «Это 1 промт», 1–3с до/после, 3–6с «убираем куклу (negative)», 6–8с «пиши СНЕГ в бота», 8–10с CTA «подпишись на канал».",
        "negative": "",
        "tip": "Текст на экране крупно (3–5 слов). Первые 2 секунды — хук.",
    },
]


def get_daily_item():
    today = datetime.now(tz).date()
    idx = today.toordinal() % len(DAILY_PACK)
    return DAILY_PACK[idx]


# ============================
# CHALLENGE 30 DAYS
# ============================
CHALLENGE_30 = [
    {"title": "День 1 — Реалистичная кожа", "task": "Сделай портрет без ‘куклы’ (поры/микродетали).",
     "prompt": "Ultra-realistic portrait, natural skin pores, micro texture, subtle imperfections, soft cinematic light, identity locked 1:1 to reference photo, 8K.",
     "tip": "Убери ‘beauty’, добавь ‘natural pores’."},
    {"title": "День 2 — 3 ракурса 1:1", "task": "Фронт / 3/4 / профиль — одно лицо.",
     "prompt": "Same person, identity locked 1:1, three angles: front, 3/4, profile. Consistent facial proportions, natural skin texture, 85mm, soft light, 8K.",
     "tip": "Запрети identity drift и ‘different person’."},
    {"title": "День 3 — Sora 10 секунд", "task": "Сделай видео из 1 фото: микро-движение.",
     "prompt": "Cinematic 4K video 9:16 10s, identity locked 1:1, subtle blink, micro-expressions, gentle breathing, slight head turn 5°, realistic motion blur.",
     "tip": "Микро-движение = меньше искажений."},
    {"title": "День 4 — Снег на волосах", "task": "Снег реалистично: кристаллы + дыхание.",
     "prompt": "Ultra realistic winter portrait, gentle snowfall, snow crystals on hair, visible cold breath, cinematic lighting, identity locked 1:1, 8K.",
     "tip": "Пиши ‘gentle snowfall’, не ‘particle storm’."},
    {"title": "День 5 — Тёплый интерьер (кино)", "task": "Сделай ‘кино-кадр’ дома с bokeh.",
     "prompt": "Cinematic portrait, warm practical lights bokeh, soft key light, film grain, 35mm f/1.8, identity locked 1:1, 8K.",
     "tip": "‘Warm practical lights’ даёт магию."},
    {"title": "День 6 — Глянец (studio)", "task": "Дорогая студийная картинка.",
     "prompt": "High-end fashion editorial studio portrait, softbox key + rim, neutral grading, crisp detail, identity locked 1:1, 85mm f/2, 8K.",
     "tip": "Не завышай contrast/clarity."},
    {"title": "День 7 — Говорящая голова", "task": "Скрипт 15с: хук → польза → CTA.",
     "prompt": "Clean studio talking head, natural skin texture, slight smile, friendly confident tone. Script: 1 hook + 1 value + CTA to Telegram. Identity locked to reference photo 1:1.",
     "tip": "15 секунд максимум — удержание выше."},
    {"title": "День 8 — Ночь/улица", "task": "Ночной городской портрет ‘дорого’.",
     "prompt": "Night street portrait, neon reflections, realistic skin texture, cinematic lighting, 50mm, shallow DOF, identity locked 1:1, 8K.",
     "tip": "Добавь ‘neon reflections’ + ‘realistic shadows’."},
    {"title": "День 9 — Контровой свет", "task": "Сделай контровой свет, но без пересветов.",
     "prompt": "Portrait with gentle rim light, soft key, natural skin pores, cinematic look, identity locked 1:1, 8K.",
     "tip": "‘gentle rim’ лучше чем ‘strong rim’."},
    {"title": "День 10 — 2 варианта одежды", "task": "Одинаковое лицо, разная одежда (2 лука).",
     "prompt": "Same person identity locked 1:1, two outfits variations, consistent facial proportions, realistic skin texture, studio soft lighting, 8K.",
     "tip": "Попроси ‘consistent hairstyle’."},
    {"title": "День 11 — Макро-деталь", "task": "Крупный план глаз/ресницы/снег.",
     "prompt": "Ultra-realistic macro close-up, eyelashes sharp, snow crystals detail, natural skin texture, 100mm macro, identity locked 1:1, 8K.",
     "tip": "Ставь ‘macro lens’ и ‘micro details’."},
    {"title": "День 12 — Стоп-кадр ‘как камера’", "task": "Фотореализм без ‘AI-глянца’.",
     "prompt": "Documentary realistic portrait, natural lighting, no beauty retouch, true-to-life colors, identity locked 1:1, 8K.",
     "tip": "Негатив: ‘no glamour, no smoothing’."},
    {"title": "День 13 — Движение волос", "task": "Едва заметный ветер в видео.",
     "prompt": "Cinematic video 9:16 8–10s, slight wind moving hair, subtle blink, identity locked 1:1, realistic motion blur.",
     "tip": "Слишком сильный ветер ломает лицо."},
    {"title": "День 14 — Портрет + текст на экране", "task": "Сделай кадр под Reels + 1 фраза (3–5 слов).",
     "prompt": "Portrait composition with clean negative space for text overlay, cinematic, identity locked 1:1, 8K.",
     "tip": "Оставь ‘negative space’ сверху."},
    {"title": "День 15 — До/после (2 кадра)", "task": "Сравнение ‘до’ и ‘после’ в одном стиле.",
     "prompt": "Split-screen before/after style, left: raw, right: ultra-realistic improved, natural skin texture, identity locked 1:1, 8K.",
     "tip": "Не меняй выражение лица."},
    {"title": "День 16 — Профи-версия (3 варианта)", "task": "Сделай A/B/C промта под один образ.",
     "prompt": "Provide three prompt variants A/B/C for the same concept, identity locked 1:1, natural skin texture, cinematic lighting, 8K.",
     "tip": "Вариации: свет/камера/фон."},
    {"title": "День 17 — 10 хуков", "task": "Сгенерируй 10 хуков под твой стиль.",
     "prompt": "Generate 10 short hooks (Russian) for reels about AI photo/video prompts, 3–7 words each, punchy, curiosity gap.",
     "tip": "Коротко = лучше удержание."},
    {"title": "День 18 — Пакет ‘Снег’", "task": "Собери 5 промтов зимы.",
     "prompt": "Generate 5 winter prompt templates with identity locked 1:1 and strong negatives anti-doll, include settings.",
     "tip": "Сохрани их в ‘Мои промты’ (скоро добавим)."},
    {"title": "День 19 — Тренд ‘медленный поворот’", "task": "Видео: поворот головы + взгляд в камеру.",
     "prompt": "Cinematic 9:16 10s, slow head turn, eye contact, subtle smile, identity locked 1:1, realistic motion blur.",
     "tip": "Поворот 3–5° максимум."},
    {"title": "День 20 — Разбор Reels", "task": "Кинь ссылку на ролик и сделай разбор.",
     "prompt": "Paste reel link and ask for an audit: hook, text, timing, CTA, mistakes.",
     "tip": "Первые 2 секунды решают всё."},
    {"title": "День 21 — Мини-сценарий 10s", "task": "Сценарий: 0–2 хук, 2–7 показ, 7–10 CTA.",
     "prompt": "Write a 10-second reels script with timing and on-screen text, for AI photo/video result reveal.",
     "tip": "Сделай CTA в один глагол."},
    {"title": "День 22 — Ритм монтажа", "task": "3 склейки и 1 зум — без перегруза.",
     "prompt": "Editing plan: 3 cuts + 1 subtle zoom, include on-screen text timing, for 9:16 reels.",
     "tip": "Перебор эффектов убивает доверие."},
    {"title": "День 23 — ‘Сделай как у Кристины’", "task": "Формат: хук → промт → настройки → CTA.",
     "prompt": "Produce a branded template: hook + prompt + negative + settings + caption + 5 tags.",
     "tip": "Повтори фирменные слова/эмодзи."},
    {"title": "День 24 — Подбор тэгов", "task": "5 тэгов под ролик (без мусора).",
     "prompt": "Generate 5 highly relevant Telegram/AI reels hashtags in Russian, no repeats, no generic spam.",
     "tip": "5–8 тегов лучше чем 30."},
    {"title": "День 25 — Упаковка профиля", "task": "3 варианта шапки/описания.",
     "prompt": "Write 3 IG bio variants for AI prompts creator, clear CTA to Telegram, Russian.",
     "tip": "Первой строкой — кто ты и что даёшь."},
    {"title": "День 26 — ‘Пакет промтов’", "task": "Собери мини-пакет из 10 промтов.",
     "prompt": "Create a pack of 10 prompt templates by theme, include negatives + settings.",
     "tip": "Люди любят ‘пачки’."},
    {"title": "День 27 — FAQ", "task": "Сделай ответы на 10 вопросов новичков.",
     "prompt": "Generate 10 FAQ Q/A for beginners using AI photo/video tools, concise and practical.",
     "tip": "FAQ повышает доверие и удержание."},
    {"title": "День 28 — Рефералка", "task": "Пригласи 1 друга и получи бонус запросов.",
     "prompt": "Use referral button to share personal link.",
     "tip": "Люди охотно делятся, если есть бонус."},
    {"title": "День 29 — ‘Промт по фото’ как сервис", "task": "Прогони 3 фото через кнопку ‘Промт по фото’.",
     "prompt": "Send photo + goal, get prompt A/B/C + negatives + settings.",
     "tip": "Это самый ‘залипательный’ функционал."},
    {"title": "День 30 — Итог", "task": "Собери 5 лучших работ и сделай пост-итог.",
     "prompt": "Write a recap post and CTA to the bot/channel, include 5 tags.",
     "tip": "Итоговый пост часто залетает лучше обычных."},
]


# ============================
# DB (SQLite)
# ============================
DB_PATH = "data.db"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table: str, col: str, ddl: str):
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = {r[1] for r in cur.fetchall()}
    if col not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    conn = db()
    cur = conn.cursor()

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

    # миграции
    ensure_column(conn, "users", "credits", "credits INTEGER DEFAULT 0")
    ensure_column(conn, "users", "challenge_day", "challenge_day INTEGER DEFAULT 0")
    ensure_column(conn, "users", "challenge_last_claim", "challenge_last_claim TEXT")
    ensure_column(conn, "users", "temp_photo_file_id", "temp_photo_file_id TEXT")
    ensure_column(conn, "users", "temp_tool", "temp_tool TEXT")

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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS referrals (
        inviter_id INTEGER NOT NULL,
        invited_id INTEGER NOT NULL UNIQUE,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()


def upsert_user(tg_id: int, username: str | None):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,))
    exists = cur.fetchone() is not None
    if not exists:
        cur.execute(
            "INSERT INTO users (tg_id, username, last_reset) VALUES (?, ?, ?)",
            (tg_id, username or "", date.today().isoformat())
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
    cur = conn.cursor()
    cur.execute("UPDATE users SET mode=? WHERE tg_id=?", (mode, tg_id))
    conn.commit()
    conn.close()


def set_temp_photo(tg_id: int, file_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET temp_photo_file_id=? WHERE tg_id=?", (file_id, tg_id))
    conn.commit()
    conn.close()


def set_temp_tool(tg_id: int, tool: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET temp_tool=? WHERE tg_id=?", (tool, tg_id))
    conn.commit()
    conn.close()


def add_credits(tg_id: int, n: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET credits = COALESCE(credits,0) + ? WHERE tg_id=?", (n, tg_id))
    conn.commit()
    conn.close()


def take_credit_if_any(tg_id: int) -> bool:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(credits,0) AS c FROM users WHERE tg_id=?", (tg_id,))
    r = cur.fetchone()
    c = int(r["c"]) if r else 0
    if c > 0:
        cur.execute("UPDATE users SET credits = credits - 1 WHERE tg_id=?", (tg_id,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


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


def extend_vip(tg_id: int, days: int):
    row = get_user(tg_id)
    now = datetime.now(tz)
    if row and row["vip_until"]:
        try:
            cur_until = datetime.fromisoformat(row["vip_until"]).replace(tzinfo=tz)
            base = cur_until if cur_until > now else now
        except Exception:
            base = now
    else:
        base = now
    until = (base + timedelta(days=days)).isoformat()
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET vip_until=? WHERE tg_id=?", (until, tg_id))
    conn.commit()
    conn.close()


def reset_if_needed(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT used_today, last_reset FROM users WHERE tg_id=?", (tg_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return
    last_reset = r["last_reset"]
    today = datetime.now(tz).date().isoformat()
    if last_reset != today:
        cur.execute("UPDATE users SET used_today=0, last_reset=? WHERE tg_id=?", (today, tg_id))
        conn.commit()
    conn.close()


def inc_usage(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET used_today = used_today + 1 WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()


def set_vip(tg_id: int, days: int):
    until = (datetime.now(tz) + timedelta(days=days)).isoformat()
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET vip_until=? WHERE tg_id=?", (until, tg_id))
    conn.commit()
    conn.close()


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
            ("Suno", "Вирусный хук (12–18 сек)", "Modern pop/edm hook, 124 bpm, punchy drums, catchy topline, Russian lyrics, 1 hook line repeated. No kids choir."),
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
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO payments(tg_id, telegram_payment_charge_id, payload, created_at) VALUES (?,?,?,?)",
        (tg_id, charge_id, payload, datetime.now(tz).isoformat())
    )
    conn.commit()
    conn.close()


def referral_try_add(inviter_id: int, invited_id: int) -> bool:
    if inviter_id == invited_id:
        return False
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO referrals(inviter_id, invited_id, created_at) VALUES (?,?,?)",
            (inviter_id, invited_id, datetime.now(tz).isoformat())
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False


def referral_count(inviter_id: int) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM referrals WHERE inviter_id=?", (inviter_id,))
    c = int(cur.fetchone()["c"])
    conn.close()
    return c


# ============================
# OpenAI
# ============================
oai = OpenAI(api_key=OPENAI_API_KEY)


async def ask_openai(question: str, system: str = SYSTEM_PROMPT) -> str:
    def _call():
        return oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
            temperature=0.7,
        )

    try:
        resp = await asyncio.to_thread(_call)
        text = resp.choices[0].message.content or ""
        return text.strip() or "Пустой ответ. Попробуй переформулировать запрос."
    except Exception as e:
        print("OpenAI error:", repr(e))
        return "⚠️ Сейчас не получилось получить ответ от GPT. Попробуй ещё раз через минуту."


# ============================
# Telegram UI
# ============================
def kb_subscribe():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")],
        [InlineKeyboardButton("📌 Что умеет бот", callback_data="about")],
    ])


def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Промт дня", callback_data="daily")],
        [InlineKeyboardButton("🏁 Челлендж 30 дней", callback_data="challenge")],
        [InlineKeyboardButton("📷 Промт по фото", callback_data="photo")],
        [InlineKeyboardButton("📈 Разбор Reels (почему не залетело)", callback_data="reels")],
        [InlineKeyboardButton("🎬 База промтов", callback_data="prompts")],
        [InlineKeyboardButton("🧠 Задать вопрос AI-агенту", callback_data="ask")],
        [InlineKeyboardButton("🎁 Пригласить друга (бонус)", callback_data="ref")],
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


def kb_challenge_start():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Старт челленджа", callback_data="challenge_start")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])


def kb_challenge_done():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готово (следующий шаг)", callback_data="challenge_done")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])


def kb_photo_tool():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Sora", callback_data="photo_tool:sora"),
         InlineKeyboardButton("Meta AI", callback_data="photo_tool:meta"),
         InlineKeyboardButton("HeyGen", callback_data="photo_tool:heygen")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])


def kb_refer_share(bot_username: str, user_id: int):
    bot_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    share_text = f"Забирай AI-бот Кристины: промты Sora/Meta/HeyGen + Промт дня + челлендж 🤍 {bot_link}"
    share_link = f"https://t.me/share/url?url={quote(bot_link)}&text={quote(share_text)}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Поделиться ссылкой", url=share_link)],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])


# ============================
# Helpers
# ============================
async def safe_edit(query, text: str, reply_markup=None, parse_mode=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise


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
    if update.message:
        await update.message.reply_text(
            f"Для доступа подпишись на канал {TG_CHANNEL} и нажми «Проверить подписку».",
            reply_markup=kb_subscribe()
        )
    elif update.callback_query:
        await safe_edit(
            update.callback_query,
            f"Для доступа подпишись на канал {TG_CHANNEL} и нажми «Проверить подписку».",
            reply_markup=kb_subscribe()
        )
    return False


def challenge_text_for(day: int) -> str:
    # day: 1..30
    item = CHALLENGE_30[day - 1]
    return (
        f"<b>🏁 {item['title']}</b>\n\n"
        f"<b>Задание:</b> {item['task']}\n\n"
        f"<b>PROMPT:</b>\n<code>{item['prompt']}</code>\n\n"
        f"<b>Подсказка:</b> {item['tip']}\n\n"
        f"Когда сделаешь — нажми ✅ <b>Готово</b>."
    )


def today_str():
    return datetime.now(tz).date().isoformat()


def challenge_can_advance(row) -> bool:
    if not CHALLENGE_DAILY_GATE:
        return True
    last = row["challenge_last_claim"] if row else None
    return last != today_str()


def challenge_set_day(tg_id: int, day: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET challenge_day=?, challenge_last_claim=? WHERE tg_id=?",
        (day, today_str(), tg_id)
    )
    conn.commit()
    conn.close()


# ============================
# Commands
# ============================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)

    # referral param: /start ref_12345
    if context.args:
        arg = context.args[0].strip()
        if arg.startswith("ref_"):
            try:
                inviter_id = int(arg.split("_", 1)[1])
                added = referral_try_add(inviter_id, u.id)
                if added:
                    add_credits(inviter_id, REF_BONUS_CREDITS)
                    cnt = referral_count(inviter_id)
                    if cnt >= REF_VIP_INVITES:
                        extend_vip(inviter_id, REF_VIP_DAYS)
            except Exception:
                pass

    text = (
        "Привет! Я AI-бот Кристины 🤍\n\n"
        "Тут:\n"
        "• 🎁 Промт дня\n"
        "• 🏁 Челлендж на 30 дней\n"
        "• 📷 Промт по фото (Sora/Meta/HeyGen)\n"
        "• 📈 Разбор Reels (почему не залетело)\n\n"
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

    row = get_user(u.id)
    credits = int(row["credits"]) if row and row["credits"] is not None else 0
    vip = is_vip(row)

    extra = f"\n\n🎟️ Бонус-запросы: {credits}" if credits else ""
    extra += "\n⭐ VIP: активен" if vip else ""

    await update.message.reply_text("Меню:" + extra, reply_markup=kb_main())


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
        f"Ок ✅ Напиши вопрос одним сообщением.\n\nЛимит бесплатно: {DAILY_LIMIT}/день (VIP — без лимитов).",
        reply_markup=kb_back_main()
    )


async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    await update.message.reply_text(
        f"VIP снимает лимиты и открывает быстрые шаблоны.\n"
        f"Срок: {VIP_DAYS} дней\n"
        f"Цена: {VIP_PRICE_STARS} Stars",
        reply_markup=kb_vip_buy()
    )


async def paysupport_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Поддержка по оплатам ⭐\n"
        "Если платеж прошёл, но VIP не включился — напиши сюда:\n"
        "• свой @username\n"
        "• время оплаты\n"
        "• скрин чека Stars\n\n"
        "Мы проверим и включим доступ.",
        reply_markup=kb_main()
    )


async def challenge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    row = get_user(u.id)
    day = int(row["challenge_day"]) if row and row["challenge_day"] is not None else 0
    if day <= 0:
        await update.message.reply_text(
            "🏁 Челлендж на 30 дней.\n\nКаждый день — одно задание и промт.\n"
            "Нажми «Старт», чтобы начать с Дня 1.",
            reply_markup=kb_challenge_start()
        )
        return
    day = max(1, min(30, day))
    await update.message.reply_text(
        challenge_text_for(day),
        parse_mode=ParseMode.HTML,
        reply_markup=kb_challenge_done()
    )


async def refer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    bot_username = context.bot.username
    cnt = referral_count(u.id)
    await update.message.reply_text(
        f"🎁 Приглашай друзей и получай бонусы!\n\n"
        f"За 1 приглашенного: +{REF_BONUS_CREDITS} запросов\n"
        f"За {REF_VIP_INVITES} приглашенных: VIP на {REF_VIP_DAYS} дня\n\n"
        f"Твои приглашения: {cnt}",
        reply_markup=kb_refer_share(bot_username, u.id)
    )


async def reels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    set_mode(u.id, "reels")
    await update.message.reply_text(
        "📈 Ок! Пришли ссылку на Reels (или опиши ролик текстом).\n"
        "Я сделаю разбор: хук / текст / тайминг / монтаж / CTA / ошибки.",
        reply_markup=kb_back_main()
    )


async def photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    set_mode(u.id, "photo_wait")
    await update.message.reply_text(
        "📷 Отправь фото одним сообщением.\n"
        "После фото выберем инструмент (Sora/Meta/HeyGen) и цель.",
        reply_markup=kb_back_main()
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
            "• 🏁 Челлендж 30 дней\n"
            "• 📷 Промт по фото (Sora/Meta/HeyGen)\n"
            "• 📈 Разбор Reels\n"
            "• Рефералка с бонусами\n"
            "• VIP через Telegram Stars",
            reply_markup=kb_subscribe()
        )
        return

    if data == "check_sub":
        ok = await is_subscribed(update, context)
        if ok:
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

    if data == "daily":
        item = get_daily_item()
        text = f"<b>{item['title']}</b>\n\n<b>PROMPT:</b>\n<code>{item['prompt']}</code>"
        if item["negative"]:
            text += f"\n\n<b>NEGATIVE:</b>\n<code>{item['negative']}</code>"
        text += f"\n\n<b>Подсказка:</b> {item['tip']}"
        await safe_edit(query, text, parse_mode=ParseMode.HTML, reply_markup=kb_back_main())
        return

    if data == "challenge":
        row = get_user(u.id)
        day = int(row["challenge_day"]) if row and row["challenge_day"] is not None else 0
        if day <= 0:
            await safe_edit(
                query,
                "🏁 Челлендж на 30 дней.\n\nКаждый день — одно задание и промт.\n"
                "Нажми «Старт», чтобы начать с Дня 1.",
                reply_markup=kb_challenge_start()
            )
        else:
            day = max(1, min(30, day))
            await safe_edit(
                query,
                challenge_text_for(day),
                parse_mode=ParseMode.HTML,
                reply_markup=kb_challenge_done()
            )
        return

    if data == "challenge_start":
        challenge_set_day(u.id, 1)
        await safe_edit(
            query,
            challenge_text_for(1),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_challenge_done()
        )
        return

    if data == "challenge_done":
        row = get_user(u.id)
        day = int(row["challenge_day"]) if row and row["challenge_day"] is not None else 0
        day = max(0, min(30, day))
        if day <= 0:
            await safe_edit(query, "Сначала нажми «Старт челленджа».", reply_markup=kb_challenge_start())
            return

        if not challenge_can_advance(row):
            await safe_edit(
                query,
                "✅ Сегодня уже отмечено. Возвращайся завтра за следующим заданием 🤍",
                reply_markup=kb_back_main()
            )
            return

        if day >= 30:
            await safe_edit(
                query,
                "🏁 Челлендж завершён! Ты прошла все 30 дней 🔥\n\n"
                "Хочешь — сделаю для тебя итоговый пост и план контента на неделю.",
                reply_markup=kb_back_main()
            )
            return

        next_day = day + 1
        challenge_set_day(u.id, next_day)
        await safe_edit(
            query,
            f"🔥 Отлично! Переходим дальше.\n\n{challenge_text_for(next_day)}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_challenge_done()
        )
        return

    if data == "photo":
        set_mode(u.id, "photo_wait")
        await safe_edit(
            query,
            "📷 Отправь фото одним сообщением.\n"
            "После этого выберем инструмент (Sora/Meta/HeyGen).",
            reply_markup=kb_back_main()
        )
        return

    if data.startswith("photo_tool:"):
        tool = data.split(":", 1)[1]
        set_temp_tool(u.id, tool)
        set_mode(u.id, "photo_goal")
        await safe_edit(
            query,
            f"✅ Инструмент: {tool.upper()}\n\nТеперь одним сообщением напиши цель:\n"
            "Например: «оживить фото, лёгкий поворот головы и улыбка» / «глянцевый портрет» / «говорящая голова»",
            reply_markup=kb_back_main()
        )
        return

    if data == "reels":
        set_mode(u.id, "reels")
        await safe_edit(
            query,
            "📈 Пришли ссылку на Reels (или опиши ролик текстом).\n"
            "Сделаю разбор: хук / текст / тайминг / монтаж / CTA / ошибки.",
            reply_markup=kb_back_main()
        )
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
            f"Ок ✅ Напиши свой вопрос одним сообщением.\n\nЛимит бесплатно: {DAILY_LIMIT}/день (VIP — без лимитов).",
            reply_markup=kb_back_main()
        )
        return

    if data == "ref":
        bot_username = context.bot.username
        cnt = referral_count(u.id)
        await safe_edit(
            query,
            f"🎁 Приглашай друзей и получай бонусы!\n\n"
            f"За 1 приглашенного: +{REF_BONUS_CREDITS} запросов\n"
            f"За {REF_VIP_INVITES} приглашенных: VIP на {REF_VIP_DAYS} дня\n\n"
            f"Твои приглашения: {cnt}",
            reply_markup=kb_refer_share(bot_username, u.id)
        )
        return

    if data == "vip":
        await safe_edit(
            query,
            f"VIP снимает лимиты и открывает быстрые шаблоны.\n"
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
            description=f"VIP на {VIP_DAYS} дней: без лимитов + премиум промты",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=prices,
        )
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
        "Можешь задавать вопросы без лимитов.",
        reply_markup=kb_main()
    )


# ============================
# Handlers: PHOTO + TEXT
# ============================
async def photo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)

    if not await require_sub(update, context):
        return

    row = get_user(u.id)
    mode = row["mode"] if row else "menu"

    if mode != "photo_wait":
        await update.message.reply_text("Фото принято ✅ Но выбери «📷 Промт по фото» в меню.", reply_markup=kb_main())
        return

    # берём самое большое фото
    ph = update.message.photo[-1]
    set_temp_photo(u.id, ph.file_id)

    await update.message.reply_text(
        "✅ Фото получено.\n\nВыбери инструмент, для которого сделать промт:",
        reply_markup=kb_photo_tool()
    )


async def text_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)

    if not await require_sub(update, context):
        return

    row = get_user(u.id)
    mode = row["mode"] if row else "menu"
    txt = (update.message.text or "").strip()

    # --- Reels audit mode ---
    if mode == "reels":
        await update.message.reply_text("Секунду… разбираю 🤍")
        answer = await ask_openai(f"Входные данные:\n{txt}", system=REELS_AUDIT_PROMPT)
        await update.message.reply_text(answer, reply_markup=kb_main())
        set_mode(u.id, "menu")
        return

    # --- Photo prompt mode (goal step) ---
    if mode == "photo_goal":
        row = get_user(u.id)
        tool = (row["temp_tool"] or "sora") if row else "sora"
        # мы не анализируем фото, просто делаем промт-шаблон под референс
        question = (
            f"Инструмент: {tool}\n"
            f"Цель: {txt}\n\n"
            "Сделай промты, учитывая, что пользователь будет использовать своё фото как референс."
        )
        await update.message.reply_text("Делаю промт… 🤍")
        answer = await ask_openai(question, system=PHOTO_PROMPT_PROMPT)
        await update.message.reply_text(answer, reply_markup=kb_main(), parse_mode=ParseMode.HTML)
        set_mode(u.id, "menu")
        return

    # --- Ask mode ---
    if mode == "ask":
        reset_if_needed(u.id)
        row = get_user(u.id)
        vip = is_vip(row)
        used = int(row["used_today"]) if row and row["used_today"] is not None else 0

        if (not vip) and used >= DAILY_LIMIT:
            # пробуем списать бонус-кредит
            if take_credit_if_any(u.id):
                await update.message.reply_text("🎟️ Использую бонус-запрос…")
            else:
                await update.message.reply_text(
                    f"Лимит {DAILY_LIMIT}/день исчерпан 😕\n\n"
                    "🎁 Можно получить бонус-запросы через «Пригласить друга» или взять VIP.",
                    reply_markup=kb_vip_buy()
                )
                return
        else:
            # обычный расход
            if not vip:
                inc_usage(u.id)

        await update.message.reply_text("Думаю… 🤍")
        answer = await ask_openai(txt)
        await update.message.reply_text(answer, reply_markup=kb_main())
        return

    # default
    await update.message.reply_text("Выбирай действие в меню 👇", reply_markup=kb_main())


# ============================
# FastAPI + Webhook
# ============================
app = FastAPI()
application = Application.builder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("menu", menu_cmd))
application.add_handler(CommandHandler("prompts", prompts_cmd))
application.add_handler(CommandHandler("ask", ask_cmd))
application.add_handler(CommandHandler("vip", vip_cmd))
application.add_handler(CommandHandler("paysupport", paysupport_cmd))
application.add_handler(CommandHandler("challenge", challenge_cmd))
application.add_handler(CommandHandler("refer", refer_cmd))
application.add_handler(CommandHandler("reels", reels_cmd))
application.add_handler(CommandHandler("photo", photo_cmd))

application.add_handler(CallbackQueryHandler(cbq))
application.add_handler(PreCheckoutQueryHandler(precheckout))
application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

application.add_handler(MessageHandler(filters.PHOTO, photo_msg))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_msg))


@app.on_event("startup")
async def on_startup():
    init_db()
    seed_prompts_if_empty()
    await application.initialize()
    await application.start()

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
