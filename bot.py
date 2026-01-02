import os
import telebot
from datetime import datetime

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN belum diset. Jalankan: export BOT_TOKEN='TOKEN_KAMU'")

bot = telebot.TeleBot(TOKEN)

# Simpan sesi per chat + per user biar aman di grup
# sessions[(chat_id, user_id)] = {"start": datetime}
sessions = {}


def key_of(message):
    return (message.chat.id, message.from_user.id)


def hms(t: datetime) -> str:
    return t.strftime("%H:%M:%S")


def dur(td) -> str:
    s = int(td.total_seconds())
    if s < 0:
        s = 0
    h = s // 3600
    m = (s % 3600) // 60
    s = s % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


@bot.message_handler(commands=["start"])
def start_cmd(message):
    bot.reply_to(
        message,
        "✅ Bot aktif.\n\n"
        "Perintah:\n"
        "/out = mulai break (BREAK SESSION)\n"
        "/in  = selesai break (BACK IN)\n"
        "/status = cek status"
    )


# ✅ /out = MULAI BREAK  (BREAK SESSION)
@bot.message_handler(commands=["out"])
def break_start(message):
    k = key_of(message)

    # kalau sudah break, jangan mulai lagi
    if k in sessions:
        start = sessions[k]["start"]
        bot.reply_to(message, f"⚠️ Kamu sudah break.\nBreak Start: {hms(start)}")
        return

    now = datetime.now()
    sessions[k] = {"start": now}

    bot.reply_to(
        message,
        f"BREAK SESSION\n"
        f"Break Start : {hms(now)}"
    )


# ✅ /in = SELESAI BREAK (BACK IN)
@bot.message_handler(commands=["in"])
def break_end(message):
    k = key_of(message)

    if k not in sessions:
        bot.reply_to(message, "❌ Kamu belum /out (belum mulai break)")
        return

    start = sessions[k]["start"]
    end = datetime.now()

    bot.reply_to(
        message,
        f"BACK IN\n"
        f"Break Start : {hms(start)}\n"
        f"Back In     : {hms(end)}\n"
        f"Duration    : {dur(end - start)}"
    )

    del sessions[k]


@bot.message_handler(commands=["status"])
def status_cmd(message):
    k = key_of(message)
    if k not in sessions:
        bot.reply_to(message, "✅ Kamu tidak sedang break.")
        return

    start = sessions[k]["start"]
    now = datetime.now()
    bot.reply_to(
        message,
        f"⏳ Kamu sedang break.\n"
        f"Break Start : {hms(start)}\n"
        f"Elapsed     : {dur(now - start)}"
    )


bot.infinity_polling()
