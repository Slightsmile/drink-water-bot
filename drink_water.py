import os
import json
import logging
from datetime import datetime, time, timedelta

import pytz
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --- Config ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATA_FILE = "reminders.json"
SERVER_TZ = os.environ.get("TZ", "UTC")
# --------------

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
ASK_TZ, ASK_START, ASK_END, ASK_FREQ = range(4)

# in-memory stores
reminders = {}   # key = str(chat_id) -> {"tz": "Asia/Dhaka", "start": "09:00", "end": "18:00", "freq": 60}
jobs = {}        # key = str(chat_id) -> Job

# ---------- persistence ----------
def load_data():
    global reminders
    try:
        with open(DATA_FILE, "r") as f:
            reminders = json.load(f)
            logger.info("Loaded reminders (%d entries).", len(reminders))
    except FileNotFoundError:
        reminders = {}
    except Exception as e:
        logger.exception("Failed loading data: %s", e)
        reminders = {}

def save_data():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(reminders, f, indent=2)
    except Exception as e:
        logger.exception("Failed saving data: %s", e)

# ---------- helpers ----------
def parse_hm(s: str) -> time:
    return datetime.strptime(s.strip(), "%H:%M").time()

def is_within_window(now_time: time, start_t: time, end_t: time) -> bool:
    if start_t < end_t:
        return start_t <= now_time < end_t
    else:
        # overnight window
        return now_time >= start_t or now_time < end_t

def next_run_after(t: time, tzinfo):
    now = datetime.now(tzinfo)
    candidate = tzinfo.localize(datetime.combine(now.date(), t))
    if candidate < now:
        candidate += timedelta(days=1)
    return candidate

# ---------- reminder job ----------
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id
    entry = reminders.get(str(chat_id))
    if not entry:
        return
    try:
        tz = pytz.timezone(entry.get("tz", SERVER_TZ))
    except Exception:
        tz = pytz.timezone(SERVER_TZ)
    now_dt = datetime.now(tz)
    now_t = now_dt.time()
    start_t = parse_hm(entry["start"])
    end_t = parse_hm(entry["end"])
    if is_within_window(now_t, start_t, end_t):
        try:
            await context.bot.send_message(chat_id=chat_id, text="💧 Time to drink water! Stay hydrated. 🚰")
        except Exception as e:
            logger.warning("Failed to send to %s: %s", chat_id, e)
    else:
        logger.debug("Chat %s: now %s not in window %s-%s", chat_id, now_t, start_t, end_t)

def schedule_for_chat(app, chat_id):
    key = str(chat_id)
    # cancel existing job
    if key in jobs:
        jobs[key].schedule_removal()
        del jobs[key]
    entry = reminders.get(key)
    if not entry:
        return
    try:
        tz = pytz.timezone(entry.get("tz", SERVER_TZ))
    except Exception:
        tz = pytz.timezone(SERVER_TZ)

    start_t = parse_hm(entry["start"])
    # compute first run time aligned to the schedule:
    first_run = next_run_after(start_t, tz)
    interval_seconds = int(entry.get("freq", 60)) * 60  # freq in minutes stored
    job = app.job_queue.run_repeating(reminder_job, interval=interval_seconds, first=first_run, chat_id=chat_id, name=key)
    jobs[key] = job
    logger.info("Scheduled chat %s: start=%s end=%s tz=%s freq=%dmin first=%s",
                chat_id, entry["start"], entry["end"], entry.get("tz", SERVER_TZ), entry.get("freq"), first_run.isoformat())

# ---------- handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi — I'm WaterBuddy 💧\n"
        "I will remind you to drink water. Use /set to configure your timezone, start/end time and frequency.\n"
        "Commands: /set, /status, /stop, /cancel"
    )

async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ask timezone
    # Give users an easy hint: common zones plus option to type any IANA timezone
    common = ["Asia/Dhaka", "UTC", "Europe/London", "America/New_York", "Asia/Kolkata"]
    kb = ReplyKeyboardMarkup([[z] for z in common], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Which timezone should I use for your schedule? Send an IANA timezone like `Asia/Dhaka` or pick one below.", reply_markup=kb)
    return ASK_TZ

async def ask_tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz_input = update.message.text.strip()
    if tz_input not in pytz.all_timezones:
        await update.message.reply_text("I couldn't recognize that timezone. Send a valid IANA timezone like `Asia/Dhaka` or `Europe/Berlin`.", reply_markup=ReplyKeyboardRemove())
        return ASK_TZ
    context.user_data["tz"] = tz_input
    await update.message.reply_text("Great. When should reminders **start** each day? Send time in 24-hour format `HH:MM` (e.g. `09:00`).")
    return ASK_START

async def ask_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        _ = parse_hm(txt)
    except Exception:
        await update.message.reply_text("Bad format. Send start time like `09:00` (24-hour).")
        return ASK_START
    context.user_data["start"] = txt
    await update.message.reply_text("Now send the **end** time in `HH:MM` (24-hour). Example: `18:00`. (Reminders are up to but not including the end time.)")
    return ASK_END

async def ask_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        _ = parse_hm(txt)
    except Exception:
        await update.message.reply_text("Bad format. Send end time like `18:00` (24-hour).")
        return ASK_END
    context.user_data["end"] = txt
    # ask frequency
    kb = ReplyKeyboardMarkup([["Every hour"], ["Every 30 minutes"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("How often should I remind you?", reply_markup=kb)
    return ASK_FREQ

async def ask_freq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if txt in ["every hour", "hourly", "1 hour", "1"]:
        freq_min = 60
    elif txt in ["every 30 minutes", "30 minutes", "30", "half"]:
        freq_min = 30
    else:
        await update.message.reply_text("Please reply `Every hour` or `Every 30 minutes`.")
        return ASK_FREQ

    chat_id = update.effective_chat.id
    # save
    reminders[str(chat_id)] = {
        "tz": context.user_data.get("tz", SERVER_TZ),
        "start": context.user_data.get("start"),
        "end": context.user_data.get("end"),
        "freq": freq_min
    }
    save_data()
    # schedule job
    schedule_for_chat(context.application, chat_id)
    await update.message.reply_text(
        f"All set! I'll remind you every {freq_min} minutes between {reminders[str(chat_id)]['start']} and {reminders[str(chat_id)]['end']} (timezone: {reminders[str(chat_id)]['tz']}).",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    entry = reminders.get(str(chat_id))
    if not entry:
        await update.message.reply_text("No reminders configured. Use /set to create one.")
    else:
        await update.message.reply_text(f"Reminders: {entry['start']} → {entry['end']} every {entry['freq']} minutes (timezone: {entry['tz']}).")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    key = str(chat_id)
    if key in reminders:
        del reminders[key]
        save_data()
    if key in jobs:
        jobs[key].schedule_removal()
        del jobs[key]
    await update.message.reply_text("Your reminders were stopped. Use /set to start again.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ---------- restore jobs ----------
def restore(app):
    load_data()
    for k in list(reminders.keys()):
        try:
            schedule_for_chat(app, int(k))
        except Exception as e:
            logger.exception("Failed scheduling %s: %s", k, e)

# ---------- main ----------
def main():
    if TOKEN is None:
        logger.error("Set TELEGRAM_TOKEN environment variable.")
        return
    app = ApplicationBuilder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("set", set_cmd)],
        states={
            ASK_TZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_tz)],
            ASK_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_start)],
            ASK_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_end)],
            ASK_FREQ: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_freq)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("cancel", cancel))

    app.post_init = lambda a: restore(a)
    logger.info("Starting drink_water bot (TZ=%s)", SERVER_TZ)
    app.run_polling()

if __name__ == "__main__":
    main()


import threading
from server import app

def run_flask():
    app.run(host="0.0.0.0", port=8000)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask)
    t.start()
    main()