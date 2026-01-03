import os
import re
import json
import time
import math
import sqlite3
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple

import aiohttp
import feedparser
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -----------------------------
# Config
# -----------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kosong. Isi di .env dulu.")

DB_PATH = "bot.db"

COINGECKO_SIMPLE_PRICE = "https://api.coingecko.com/api/v3/simple/price"
ALT_FNG_API = "https://api.alternative.me/fng/"

# Investing RSS (crypto news)
INVESTING_CRYPTO_RSS = "https://www.investing.com/rss/news_301.rss"

# CNBC RSS (general) - will be filtered by crypto keywords
# Commonly used CNBC RSS URLs found in public lists
CNBC_RSS_PRIMARY = "https://www.cnbc.com/id/10000664/device/rss/rss.html"
CNBC_RSS_ALT = "https://www.cnbc.com/id/100003114/device/rss/rss.html"

# Crypto keyword filter for news
CRYPTO_KEYWORDS = [
    "crypto", "cryptocurrency", "bitcoin", "btc", "ethereum", "eth",
    "solana", "sol", "xrp", "ripple", "binance", "bnb",
    "doge", "dogecoin", "stablecoin", "usdt", "tether", "usdc",
    "blockchain", "web3", "defi", "etf", "mining"
]

# Which coins you want to support quickly (you can add more)
COIN_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "DOGE": "dogecoin",
}

# Simple user-agent to reduce RSS/HTTP blocking
UA = "Mozilla/5.0 (TelegramBot; +https://t.me/)"

# -----------------------------
# DB
# -----------------------------
def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        coin_id TEXT NOT NULL,
        direction TEXT NOT NULL,         -- 'above' or 'below'
        target_usd REAL NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL
    )
    """)
    con.commit()
    con.close()

def db_add_alert(chat_id: int, symbol: str, coin_id: str, direction: str, target_usd: float):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO alerts (chat_id, symbol, coin_id, direction, target_usd, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
    """, (chat_id, symbol, coin_id, direction, float(target_usd), int(time.time())))
    con.commit()
    con.close()

def db_list_alerts(chat_id: int) -> List[Tuple]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT id, symbol, direction, target_usd, is_active, created_at
        FROM alerts
        WHERE chat_id = ?
        ORDER BY id DESC
    """, (chat_id,))
    rows = cur.fetchall()
    con.close()
    return rows

def db_deactivate_alert(alert_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("UPDATE alerts SET is_active = 0 WHERE id = ?", (alert_id,))
    con.commit()
    con.close()

def db_get_active_alerts() -> List[Tuple]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT id, chat_id, symbol, coin_id, direction, target_usd
        FROM alerts
        WHERE is_active = 1
    """)
    rows = cur.fetchall()
    con.close()
    return rows

# -----------------------------
# Helpers
# -----------------------------
def fmt_usd(x: float) -> str:
    return f"${x:,.2f}"

def fmt_idr(x: float) -> str:
    # IDR usually no decimals
    return f"Rp{int(round(x)):,.0f}".replace(",", ".")

def normalize_symbol(text: str) -> str:
    t = text.strip().upper()
    t = re.sub(r"[^A-Z0-9]", "", t)
    return t

def contains_crypto_keyword(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in CRYPTO_KEYWORDS)

async def http_get_json(session: aiohttp.ClientSession, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    async with session.get(url, params=params, headers={"User-Agent": UA, "Accept": "application/json"}) as r:
        r.raise_for_status()
        return await r.json()

async def http_get_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, headers={"User-Agent": UA, "Accept": "*/*"}) as r:
        r.raise_for_status()
        return await r.text()

# -----------------------------
# CoinGecko (USD + IDR)
# -----------------------------
async def fetch_prices_usd_idr(coin_id: str) -> Tuple[float, float]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        data = await http_get_json(
            session,
            COINGECKO_SIMPLE_PRICE,
            params={
                "ids": coin_id,
                "vs_currencies": "usd,idr",
                "include_last_updated_at": "false",
            },
        )
    if coin_id not in data:
        raise ValueError("Coin ID tidak ditemukan di CoinGecko.")
    usd = float(data[coin_id]["usd"])
    idr = float(data[coin_id]["idr"])
    return usd, idr

# -----------------------------
# Fear & Greed
# -----------------------------
async def fetch_fear_greed() -> Dict[str, Any]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        data = await http_get_json(session, ALT_FNG_API, params={"limit": 1, "format": "json"})
    # expected: {"data":[{"value":"..","value_classification":"..","timestamp":".."}], ...}
    item = data["data"][0]
    return {
        "value": int(item["value"]),
        "classification": item["value_classification"],
        "timestamp": int(item["timestamp"]),
    }

# -----------------------------
# News (RSS) + filter keyword crypto
# -----------------------------
async def fetch_rss_items(url: str, limit: int = 10) -> List[Dict[str, Any]]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        xml = await http_get_text(session, url)
    feed = feedparser.parse(xml)

    items = []
    for e in feed.entries[:limit]:
        title = getattr(e, "title", "")
        link = getattr(e, "link", "")
        published_parsed = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        published_ts = int(time.mktime(published_parsed)) if published_parsed else 0
        items.append({
            "title": title,
            "link": link,
            "published_ts": published_ts,
        })
    return items

async def fetch_crypto_news(limit_each: int = 12, final_limit: int = 8) -> List[Dict[str, Any]]:
    # Investing crypto rss already crypto-focused; CNBC needs filtering
    investing = await fetch_rss_items(INVESTING_CRYPTO_RSS, limit=limit_each)
    for x in investing:
        x["source"] = "Investing.com"

    # Try CNBC primary; if fails, fallback
    try:
        cnbc_raw = await fetch_rss_items(CNBC_RSS_PRIMARY, limit=limit_each)
    except Exception:
        cnbc_raw = await fetch_rss_items(CNBC_RSS_ALT, limit=limit_each)

    cnbc = []
    for x in cnbc_raw:
        if contains_crypto_keyword(x["title"]):
            x["source"] = "CNBC"
            cnbc.append(x)

    merged = investing + cnbc

    # de-duplicate by link or title
    seen = set()
    uniq = []
    for x in sorted(merged, key=lambda z: z["published_ts"], reverse=True):
        key = (x.get("link") or "").strip() or (x.get("title") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(x)

    return uniq[:final_limit]

# -----------------------------
# UI (Inline Buttons)
# -----------------------------
def main_menu_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("💰 Price", callback_data="MENU_PRICE"),
         InlineKeyboardButton("😱 Fear & Greed", callback_data="MENU_FNG")],
        [InlineKeyboardButton("📰 News", callback_data="MENU_NEWS"),
         InlineKeyboardButton("⏰ Tambah alert harga", callback_data="MENU_ADD_ALERT")],
        [InlineKeyboardButton("📌 Lihat alert", callback_data="MENU_LIST_ALERT")],
    ]
    return InlineKeyboardMarkup(kb)

def coin_pick_kb() -> InlineKeyboardMarkup:
    kb = []
    row = []
    for sym in COIN_MAP.keys():
        row.append(InlineKeyboardButton(sym, callback_data=f"PICK_{sym}"))
        if len(row) == 4:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("⬅️ Kembali", callback_data="BACK_HOME")])
    return InlineKeyboardMarkup(kb)

def alert_direction_kb(symbol: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("📈 Alert kalau DI ATAS", callback_data=f"DIR_{symbol}_above")],
        [InlineKeyboardButton("📉 Alert kalau DI BAWAH", callback_data=f"DIR_{symbol}_below")],
        [InlineKeyboardButton("⬅️ Batal", callback_data="BACK_HOME")],
    ]
    return InlineKeyboardMarkup(kb)

def alert_actions_kb(alert_id: int) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("🛑 Matikan alert ini", callback_data=f"ALERT_OFF_{alert_id}")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="BACK_HOME")],
    ]
    return InlineKeyboardMarkup(kb)

# -----------------------------
# State (simple per-chat)
# -----------------------------
# context.user_data keys:
# - mode: "PRICE_WAIT_SYMBOL" | "ALERT_WAIT_TARGET"
# - alert_symbol, alert_coin_id, alert_direction
# We'll guide user via inline menu + text input for target.
# -----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Siap ✅\nPilih menu di bawah:",
        reply_markup=main_menu_kb()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Perintah:\n"
        "/start - buka menu\n"
        "/price - cek harga (pilih coin)\n"
        "/fng - fear & greed\n"
        "/news - news crypto\n"
        "/alert - tambah alert harga\n"
        "/alerts - lihat alert\n"
    )

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Pilih coin:", reply_markup=coin_pick_kb())

async def fng_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        fng = await fetch_fear_greed()
        msg = (
            f"😱 *Fear & Greed Index*\n"
            f"Value: *{fng['value']}* ({fng['classification']})\n"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())
    except Exception as e:
        await update.message.reply_text(f"Gagal ambil Fear & Greed: {e}", reply_markup=main_menu_kb())

async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_news(update, context)

async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Pilih coin untuk alert:", reply_markup=coin_pick_kb())

async def alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_alert_list(update, context)

# -----------------------------
# Callback query handler
# -----------------------------
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "BACK_HOME":
        context.user_data.clear()
        await q.edit_message_text("Pilih menu:", reply_markup=main_menu_kb())
        return

    if data == "MENU_PRICE":
        context.user_data.clear()
        await q.edit_message_text("Pilih coin:", reply_markup=coin_pick_kb())
        return

    if data == "MENU_FNG":
        context.user_data.clear()
        try:
            fng = await fetch_fear_greed()
            msg = (
                f"😱 *Fear & Greed Index*\n"
                f"Value: *{fng['value']}* ({fng['classification']})\n"
            )
            await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())
        except Exception as e:
            await q.edit_message_text(f"Gagal ambil Fear & Greed: {e}", reply_markup=main_menu_kb())
        return

    if data == "MENU_NEWS":
        context.user_data.clear()
        # can't edit too long safely; just send a new message
        await q.edit_message_text("Mengambil news…", reply_markup=main_menu_kb())
        await q.message.reply_text("📰 News crypto terbaru:", reply_markup=main_menu_kb())
        await send_news(update, context, as_reply=True)
        return

    if data == "MENU_ADD_ALERT":
        context.user_data.clear()
        await q.edit_message_text("Pilih coin untuk alert:", reply_markup=coin_pick_kb())
        return

    if data == "MENU_LIST_ALERT":
        context.user_data.clear()
        await q.edit_message_text("📌 Daftar alert kamu:", reply_markup=main_menu_kb())
        await send_alert_list(update, context, as_reply=True)
        return

    # coin picking
    if data.startswith("PICK_"):
        sym = data.split("_", 1)[1]
        coin_id = COIN_MAP.get(sym)
        if not coin_id:
            await q.edit_message_text("Coin tidak dikenal.", reply_markup=main_menu_kb())
            return

        # Determine flow:
        # If user came from alert menu -> ask direction
        # Otherwise show price directly + menu
        # We'll check a hint: if last text contains "alert" it might not exist. Use a simple flag.
        # We'll set a flag when user pressed MENU_ADD_ALERT or /alert -> but we cleared.
        # So: infer by asking user: show submenu to choose action.
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Lihat harga", callback_data=f"SHOWPRICE_{sym}")],
            [InlineKeyboardButton("⏰ Buat alert harga", callback_data=f"MAKEALERT_{sym}")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="BACK_HOME")]
        ])
        await q.edit_message_text(f"Kamu pilih *{sym}*.\nMau ngapain?", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    if data.startswith("SHOWPRICE_"):
        sym = data.split("_", 1)[1]
        coin_id = COIN_MAP.get(sym)
        try:
            usd, idr = await fetch_prices_usd_idr(coin_id)
            msg = (
                f"💰 *{sym} Price*\n"
                f"USD: *{fmt_usd(usd)}*\n"
                f"IDR: *{fmt_idr(idr)}*\n"
            )
            await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())
        except Exception as e:
            await q.edit_message_text(f"Gagal ambil harga {sym}: {e}", reply_markup=main_menu_kb())
        return

    if data.startswith("MAKEALERT_"):
        sym = data.split("_", 1)[1]
        await q.edit_message_text(
            f"Pilih arah alert untuk *{sym}*:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=alert_direction_kb(sym),
        )
        return

    if data.startswith("DIR_"):
        # DIR_{SYM}_{above/below}
        _, sym, direction = data.split("_", 2)
        coin_id = COIN_MAP.get(sym)
        context.user_data["mode"] = "ALERT_WAIT_TARGET"
        context.user_data["alert_symbol"] = sym
        context.user_data["alert_coin_id"] = coin_id
        context.user_data["alert_direction"] = direction

        await q.edit_message_text(
            f"✅ Oke.\nSekarang ketik *target harga dalam USD* untuk {sym}.\n"
            f"Contoh: `42000`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Batal", callback_data="BACK_HOME")]])
        )
        return

    if data.startswith("ALERT_OFF_"):
        alert_id = int(data.split("_")[-1])
        db_deactivate_alert(alert_id)
        await q.edit_message_text("✅ Alert dimatikan.", reply_markup=main_menu_kb())
        return

# -----------------------------
# Text message handler (for target input)
# -----------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")

    if mode == "ALERT_WAIT_TARGET":
        raw = update.message.text.strip().replace(",", "")
        if not re.fullmatch(r"\d+(\.\d+)?", raw):
            await update.message.reply_text("Format target USD tidak valid. Contoh: 42000")
            return

        target_usd = float(raw)
        sym = context.user_data["alert_symbol"]
        coin_id = context.user_data["alert_coin_id"]
        direction = context.user_data["alert_direction"]

        db_add_alert(update.message.chat_id, sym, coin_id, direction, target_usd)
        context.user_data.clear()

        await update.message.reply_text(
            f"⏰ Alert dibuat!\n"
            f"{sym} {('DI ATAS' if direction=='above' else 'DI BAWAH')} {fmt_usd(target_usd)}\n\n"
            f"Ketik /alerts untuk lihat daftar.",
            reply_markup=main_menu_kb()
        )
        return

    # default: show menu hint
    await update.message.reply_text("Pilih menu ya:", reply_markup=main_menu_kb())

# -----------------------------
# Send News / Alerts list
# -----------------------------
async def send_news(update: Update, context: ContextTypes.DEFAULT_TYPE, as_reply: bool = False):
    chat_id = update.effective_chat.id
    try:
        items = await fetch_crypto_news(limit_each=15, final_limit=8)
        if not items:
            txt = "Belum ada news crypto yang kebaca dari sumber saat ini."
            await context.bot.send_message(chat_id, txt, reply_markup=main_menu_kb())
            return

        lines = ["📰 *Crypto News (CNBC + Investing.com)*\n"]
        for i, it in enumerate(items, 1):
            title = it["title"].strip()
            source = it["source"]
            link = it["link"]
            lines.append(f"{i}. *{title}*\n   _{source}_\n   {link}\n")
        msg = "\n".join(lines).strip()

        await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True, reply_markup=main_menu_kb())
    except Exception as e:
        await context.bot.send_message(chat_id, f"Gagal ambil news: {e}", reply_markup=main_menu_kb())

async def send_alert_list(update: Update, context: ContextTypes.DEFAULT_TYPE, as_reply: bool = False):
    chat_id = update.effective_chat.id
    rows = db_list_alerts(chat_id)

    if not rows:
        await context.bot.send_message(chat_id, "Kamu belum punya alert. Tekan ⏰ Tambah alert harga.", reply_markup=main_menu_kb())
        return

    lines = ["📌 *Alert kamu*\n"]
    for (aid, sym, direction, target_usd, is_active, created_at) in rows[:20]:
        status = "🟢 ON" if is_active else "⚫ OFF"
        arrow = "DI ATAS" if direction == "above" else "DI BAWAH"
        lines.append(f"#{aid} {status} — {sym} {arrow} {fmt_usd(float(target_usd))}")
    msg = "\n".join(lines)

    await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())

# -----------------------------
# Alert checker job
# -----------------------------
async def alert_checker(context: ContextTypes.DEFAULT_TYPE):
    alerts = db_get_active_alerts()
    if not alerts:
        return

    # group by coin_id to reduce API calls
    by_coin: Dict[str, List[Tuple]] = {}
    for row in alerts:
        aid, chat_id, sym, coin_id, direction, target_usd = row
        by_coin.setdefault(coin_id, []).append(row)

    # fetch prices coin by coin (simple + safe)
    for coin_id, rows in by_coin.items():
        try:
            usd, idr = await fetch_prices_usd_idr(coin_id)
        except Exception:
            continue

        for (aid, chat_id, sym, coin_id2, direction, target_usd) in rows:
            trig = False
            if direction == "above" and usd >= target_usd:
                trig = True
            if direction == "below" and usd <= target_usd:
                trig = True

            if trig:
                # deactivate after triggered (simple behavior)
                db_deactivate_alert(aid)
                text = (
                    f"🚨 *ALERT TRIGGERED*\n"
                    f"{sym} sekarang:\n"
                    f"USD: *{fmt_usd(usd)}*\n"
                    f"IDR: *{fmt_idr(idr)}*\n"
                    f"Target: *{fmt_usd(target_usd)}* ({'DI ATAS' if direction=='above' else 'DI BAWAH'})\n"
                    f"\nAlert #{aid} otomatis dimatikan."
                )
                kb = alert_actions_kb(aid)
                await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# -----------------------------
# Main
# -----------------------------
def build_app() -> Application:
    db_init()
    app = Application.builder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("fng", fng_cmd))
    app.add_handler(CommandHandler("news", news_cmd))
    app.add_handler(CommandHandler("alert", alert_cmd))
    app.add_handler(CommandHandler("alerts", alerts_cmd))

    # callbacks + text
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # job queue (check alerts every 60s)
    app.job_queue.run_repeating(alert_checker, interval=60, first=10)

    return app

if __name__ == "__main__":
    app = build_app()
    print("Bot running...")
    app.run_polling(close_loop=False)
