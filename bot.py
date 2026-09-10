#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🤖 AI Video Telegram Bot — أداة متكاملة في ملف واحد
يدعم: توليد فيديو، صور، محادثة، TTS، ترجمة، سجل، حد يومي.

التشغيل:
    pip install python-telegram-bot==21.6 requests aiosqlite
    python bot.py
"""

import os
import asyncio
import logging
import sqlite3
from datetime import date
from collections import deque

import requests
import aiosqlite
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ============================================================
#  ⚙️ الإعدادات (عدّل هنا مباشرة)
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FREE_AI_API_KEY    = os.getenv("FREE_AI_API_KEY")
FREE_AI_BASE       = os.getenv("FREE_AI_BASE", "https://api.free.ai")

DB_PATH        = "bot_data.db"
DAILY_LIMIT    = 20            # الحد اليومي لكل مستخدم
MAX_PROMPT_LEN = 500
VIDEO_DURATION = 5             # ثواني
MAX_VIDEO_CONCURRENT = 3       # طلبات فيديو متزامنة

# الموديلات
CHAT_MODEL  = "qwen7b"
TTS_MODEL   = "kokoro"
TTS_VOICE   = "af_heart"

VIDEO_MODELS = {
    "kling_pro": "premium/kling-video/v2.6/pro/text-to-video",
    "kling_std": "premium/kling-video/v2.6/standard/text-to-video",
}
IMAGE_MODELS = {
    "sdxl":     "sdxl",
    "flux_pro": "premium/flux-pro/kontext",
}

DEFAULT_VIDEO_MODEL = VIDEO_MODELS["kling_pro"]
DEFAULT_IMAGE_MODEL = IMAGE_MODELS["sdxl"]

# ============================================================
#  🔧 Logger
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ai_bot")

# ============================================================
#  🌐 Free.ai API Client
# ============================================================

HEADERS = {
    "Authorization": f"Bearer {FREE_AI_API_KEY}",
    "Content-Type": "application/json",
}

class FreeAI:
    def __init__(self, base=FREE_AI_BASE, timeout=180):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _post(self, path, payload):
        try:
            r = requests.post(
                f"{self.base}{path}", headers=HEADERS,
                json=payload, timeout=self.timeout
            )
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            body = e.response.text[:300] if e.response is not None else ""
            return {"error": f"HTTP {e.response.status_code}: {body}"}
        except requests.exceptions.RequestException as e:
            return {"error": f"Network error: {e}"}

    def chat(self, prompt):
        d = self._post("/v1/chat/", {
            "messages": [{"role": "user", "content": prompt}],
            "model": CHAT_MODEL,
        })
        if "error" in d:
            return d
        try:
            return d["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            return {"error": f"Unexpected response: {d}"}

    def generate_image(self, prompt, model=DEFAULT_IMAGE_MODEL, aspect_ratio="16:9"):
        d = self._post("/v1/image/generate/", {
            "prompt": prompt, "model": model, "aspect_ratio": aspect_ratio,
        })
        if "error" in d:
            return d
        return d.get("image_url") or d.get("url") or d

    def generate_video(self, prompt, duration=VIDEO_DURATION, model=DEFAULT_VIDEO_MODEL):
        d = self._post("/v1/video/generate/", {
            "prompt": prompt, "duration": duration, "model": model,
        })
        if "error" in d:
            return d
        return d.get("video_url") or d.get("url") or d

    def tts(self, text, voice=TTS_VOICE, model=TTS_MODEL):
        d = self._post("/v1/tts/", {"text": text, "voice": voice, "model": model})
        if "error" in d:
            return d
        return d.get("audio_url") or d.get("url") or d

    def translate(self, text, target="ar"):
        d = self._post("/v1/translate/", {"text": text, "target": target})
        if "error" in d:
            return d
        return d.get("translation") or d.get("text") or d


api = FreeAI()

# ============================================================
#  💾 قاعدة البيانات (SQLite — تلقائي)
# ============================================================

def _sync_init():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            video_model TEXT,
            image_model TEXT,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            prompt TEXT,
            result_url TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_counter (
            user_id INTEGER,
            day TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )
    """)
    conn.commit()
    conn.close()


async def db_init():
    await asyncio.to_thread(_sync_init)


async def db_register_user(user_id, username, first_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
        """, (user_id, username or "", first_name or ""))
        await db.commit()


async def db_get_settings(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT video_model, image_model FROM users WHERE user_id = ?",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return {"video_model": row[0], "image_model": row[1]} if row else {}


async def db_set_model(user_id, field, value):
    if field not in ("video_model", "image_model"):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id)
        )
        await db.commit()


async def db_check_and_increment(user_id):
    """يرجع (مسموح, المتبقي)."""
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT count FROM daily_counter WHERE user_id = ? AND day = ?",
            (user_id, today)
        ) as cur:
            row = await cur.fetchone()
            count = row[0] if row else 0

        if count >= DAILY_LIMIT:
            return False, 0

        await db.execute("""
            INSERT INTO daily_counter (user_id, day, count) VALUES (?, ?, 1)
            ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1
        """, (user_id, today))
        await db.commit()
        return True, DAILY_LIMIT - count - 1


async def db_log(user_id, action, prompt, result_url=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO usage (user_id, action, prompt, result_url)
            VALUES (?, ?, ?, ?)
        """, (user_id, action, prompt[:500], result_url))
        await db.commit()


async def db_history(user_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT action, prompt, created_at FROM usage
            WHERE user_id = ? ORDER BY id DESC LIMIT ?
        """, (user_id, limit)) as cur:
            return await cur.fetchall()


async def db_get_usage(user_id):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT count FROM daily_counter WHERE user_id = ? AND day = ?",
            (user_id, today)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

# ============================================================
#  🚦 قائمة انتظار الفيديو
# ============================================================

_video_semaphore = asyncio.Semaphore(MAX_VIDEO_CONCURRENT)


async def run_video_job(func, *args, **kwargs):
    """ينفّذ الطلب داخل semaphore + thread pool."""
    async with _video_semaphore:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

# ============================================================
#  🎛️ أوامر البوت
# ============================================================

async def _touch_user(update):
    u = update.effective_user
    await db_register_user(u.id, u.username, u.first_name)
    return u


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await _touch_user(update)
    text = (
        f"👋 مرحباً {u.first_name}!\n\n"
        "🎬 *بوت الذكاء الاصطناعي المتكامل*\n\n"
        "*الأوامر:*\n"
        "• `/video <وصف>` — توليد فيديو 🎥\n"
        "• `/image <وصف>` — توليد صورة 🖼\n"
        "• `/chat <سؤال>` — محادثة 💭\n"
        "• `/voice <نص>` — تحويل لصوت 🔊\n"
        "• `/translate <نص>` — ترجمة 🌐\n"
        "• `/settings` — الموديلات ⚙️\n"
        "• `/history` — آخر ١٠ عمليات 📜\n"
        "• `/usage` — الاستهلاك 📊\n\n"
        f"🔒 الحد اليومي: *{DAILY_LIMIT}* عملية\n\n"
        "✍️ مثال: `/video قطة تجري في الحديقة`"
    )
    await update.message.reply_markdown(text)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start(update, ctx)


async def settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _touch_user(update)
    kb = [
        [InlineKeyboardButton("🎥 Kling Pro (فيديو)", callback_data="m:video:kling_pro")],
        [InlineKeyboardButton("🎥 Kling Std (فيديو)", callback_data="m:video:kling_std")],
        [InlineKeyboardButton("🖼 SDXL (صورة - مجاني)", callback_data="m:image:sdxl")],
        [InlineKeyboardButton("🖼 FLUX Pro (صورة - مدفوع)", callback_data="m:image:flux_pro")],
    ]
    await update.message.reply_text(
        "⚙️ *اختر الموديل الافتراضي:*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    if len(parts) != 3 or parts[0] != "m":
        return
    kind, key = parts[1], parts[2]
    uid = q.from_user.id

    if kind == "video" and key in VIDEO_MODELS:
        await db_set_model(uid, "video_model", VIDEO_MODELS[key])
        await q.edit_message_text(f"✅ موديل الفيديو: `{key}`", parse_mode="Markdown")
    elif kind == "image" and key in IMAGE_MODELS:
        await db_set_model(uid, "image_model", IMAGE_MODELS[key])
        await q.edit_message_text(f"✅ موديل الصورة: `{key}`", parse_mode="Markdown")
    else:
        await q.edit_message_text("❌ خيار غير معروف.")


async def _get_models(uid):
    s = await db_get_settings(uid)
    return (
        s.get("video_model") or DEFAULT_VIDEO_MODEL,
        s.get("image_model") or DEFAULT_IMAGE_MODEL,
    )


async def video_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await _touch_user(update)
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        return await update.message.reply_text(
            "❗ مثال: `/video قطة تجري في الحديقة`", parse_mode="Markdown"
        )
    if len(prompt) > MAX_PROMPT_LEN:
        return await update.message.reply_text(f"❗ الوصف طويل (الحد {MAX_PROMPT_LEN}).")

    ok, remaining = await db_check_and_increment(u.id)
    if not ok:
        return await update.message.reply_text(f"🚫 وصلت للحد اليومي ({DAILY_LIMIT}).")

    msg = await update.message.reply_text("🎬 جاري توليد الفيديو... قد يستغرق دقيقة ⏳")
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)

    vmodel, _ = await _get_models(u.id)
    result = await run_video_job(api.generate_video, prompt, VIDEO_DURATION, vmodel)

    if isinstance(result, dict) and "error" in result:
        return await msg.edit_text(f"❌ خطأ: {result['error']}")

    try:
        await msg.edit_text("📤 جاري رفع الفيديو...")
        await update.message.reply_video(
            video=result,
            caption=f"🎬 *{prompt}*\n🤖 `{vmodel}`\n📊 المتبقي: {remaining}",
            parse_mode="Markdown",
            supports_streaming=True,
        )
        await db_log(u.id, "video", prompt, str(result))
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"⚠️ فشل الإرسال:\n{result}\n`{e}`")


async def image_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await _touch_user(update)
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        return await update.message.reply_text(
            "❗ مثال: `/image غروب على الجبال`", parse_mode="Markdown"
        )

    ok, remaining = await db_check_and_increment(u.id)
    if not ok:
        return await update.message.reply_text(f"🚫 وصلت للحد اليومي ({DAILY_LIMIT}).")

    msg = await update.message.reply_text("🖼 جاري توليد الصورة...")
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)

    _, imodel = await _get_models(u.id)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: api.generate_image(prompt, imodel))

    if isinstance(result, dict) and "error" in result:
        return await msg.edit_text(f"❌ خطأ: {result['error']}")

    try:
        await update.message.reply_photo(
            photo=result,
            caption=f"🖼 {prompt}\n📊 المتبقي: {remaining}",
        )
        await db_log(u.id, "image", prompt, str(result))
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"⚠️ الرابط: {result}\n`{e}`")


async def chat_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await _touch_user(update)
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        return await update.message.reply_text(
            "❗ مثال: `/chat ما هي بايثون؟`", parse_mode="Markdown"
        )

    ok, remaining = await db_check_and_increment(u.id)
    if not ok:
        return await update.message.reply_text(f"🚫 وصلت للحد اليومي ({DAILY_LIMIT}).")

    msg = await update.message.reply_text("💭 يفكر...")
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    loop = asyncio.get_event_loop()
    reply = await loop.run_in_executor(None, lambda: api.chat(prompt))

    if isinstance(reply, dict) and "error" in reply:
        return await msg.edit_text(f"❌ خطأ: {reply['error']}")

    await msg.edit_text(
        reply[:4000] + f"\n\n_📊 المتبقي: {remaining}_",
        parse_mode="Markdown",
    )
    await db_log(u.id, "chat", prompt)


async def voice_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await _touch_user(update)
    text = " ".join(ctx.args).strip()
    if not text:
        return await update.message.reply_text(
            "❗ مثال: `/voice مرحباً بكم`", parse_mode="Markdown"
        )

    ok, remaining = await db_check_and_increment(u.id)
    if not ok:
        return await update.message.reply_text(f"🚫 وصلت للحد اليومي ({DAILY_LIMIT}).")

    msg = await update.message.reply_text("🔊 جاري توليد الصوت...")
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VOICE)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: api.tts(text))

    if isinstance(result, dict) and "error" in result:
        return await msg.edit_text(f"❌ خطأ: {result['error']}")

    try:
        await update.message.reply_voice(voice=result, caption=text[:100])
        await db_log(u.id, "tts", text, str(result))
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"⚠️ الرابط: {result}\n`{e}`")


async def translate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args).strip()
    if not text:
        return await update.message.reply_text(
            "❗ مثال: `/translate Hello world`", parse_mode="Markdown"
        )
    msg = await update.message.reply_text("🌐 يترجم...")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: api.translate(text, "ar"))
    if isinstance(result, dict) and "error" in result:
        await msg.edit_text(f"❌ خطأ: {result['error']}")
    else:
        await msg.edit_text(f"🌐 {result}")


async def history_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = await db_history(update.effective_user.id, 10)
    if not rows:
        return await update.message.reply_text("📜 لا يوجد سجل بعد.")
    lines = ["📜 *آخر ١٠ عمليات:*\n"]
    for action, prompt, created in rows:
        lines.append(f"• `{action}` — {prompt[:40]} _({created[:16]})_")
    await update.message.reply_markdown("\n".join(lines))


async def usage_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user.id
    used = await db_get_usage(u)
    await update.message.reply_text(
        f"📊 *استهلاك اليوم:*\n\n"
        f"• المستخدم: `{used}` / `{DAILY_LIMIT}`\n"
        f"• المتبقي: `{DAILY_LIMIT - used}`",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """أي رسالة نصية عادية = محادثة."""
    u = await _touch_user(update)
    text = update.message.text
    if not text:
        return

    ok, remaining = await db_check_and_increment(u.id)
    if not ok:
        return await update.message.reply_text(f"🚫 وصلت للحد اليومي ({DAILY_LIMIT}).")

    msg = await update.message.reply_text("💭 يفكر...")
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    loop = asyncio.get_event_loop()
    reply = await loop.run_in_executor(None, lambda: api.chat(text))

    if isinstance(reply, dict) and "error" in reply:
        return await msg.edit_text(f"❌ خطأ: {reply['error']}")

    await msg.edit_text(
        reply[:4000] + f"\n\n_📊 المتبقي: {remaining}_",
        parse_mode="Markdown",
    )
    await db_log(u.id, "chat", text)

# ============================================================
#  🚀 نقطة الدخول
# ============================================================

async def post_init(app: Application):
    await db_init()
    await app.bot.set_my_commands([
        ("start", "بدء البوت"),
        ("video", "توليد فيديو 🎥"),
        ("image", "توليد صورة 🖼"),
        ("chat", "محادثة ذكية 💭"),
        ("voice", "تحويل نص لصوت 🔊"),
        ("translate", "ترجمة 🌐"),
        ("settings", "الإعدادات ⚙️"),
        ("history", "السجل 📜"),
        ("usage", "الاستهلاك 📊"),
        ("help", "المساعدة"),
    ])
    log.info("✅ البوت جاهز — القاعدة: %s", DB_PATH)


def main():
    if "ضع_توكن" in TELEGRAM_BOT_TOKEN:
        raise SystemExit("❌ ضع TELEGRAM_BOT_TOKEN في أعلى الملف.")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("video", video_cmd))
    app.add_handler(CommandHandler("image", image_cmd))
    app.add_handler(CommandHandler("chat", chat_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("translate", translate_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("usage", usage_cmd))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^m:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("🚀 تشغيل البوت...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()