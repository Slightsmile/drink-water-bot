"""
Water Reminder Telegram Bot
Requires: python-telegram-bot==20.*, pytz
Environment variable: TELEGRAM_TOKEN

Usage:
    pip install python-telegram-bot==20.* pytz
    export TELEGRAM_TOKEN="your_token_here"
    python water_reminder_bot.py
"""

import json
import logging
import os
from datetime import datetime, time, timedelta

import pytz
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ------------ Config ------------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATA_FILE = "reminders.json"    # persistence
SERVER_TZ = os.environ.get("TZ", "UTC")   # set TZ environment variable on server (recommended)
# --------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
ASK_START, ASK_END = range(2)

# In-memory and Job tracking
# reminders_data: { chat_id: {"start": "09:00", "end": "18:00", "timezone": "UTC"} }
reminders_data = {}
# jobs map: chat_id -> Job object
jobs = {}

# ---------------- Persistence ----------------
def load_data():
    global reminders_data
    try:
        with open(DATA_FILE, "r") as f:
            reminders_data = json.load(f)
            logger.info("Loaded reminders from %s", DATA_FILE)
    except FileNotFoundError:
        reminders_data = {}
    except Exception as e:
        logger.error("Failed to load data: %s", e)
        reminders_data = {}

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(reminders_data, f, indent=2)
    logger.info("Saved reminders to %s", DATA_FILE)

# ---------------- Time helpers ----------------
def parse_hm(value: str):
    """Parse HH:MM (24h) string to time object. Raise ValueError on bad format."""
    return datetime.strptime(value.strip(), "%H:%M").time()

def next_run_after(start_t: time, tzinfo):
    """Return next datetime (tz-aware) to schedule first reminder at or after now for given start_t."""
    now = datetime.now(tzinfo)
    candidate = datetime.combine(now.date(), start_t).replace(tzinfo=tzinfo)
    if candidate < now:
        candidate += timedelta(days=1)
    return candidate

def is_within_window(now_time: time, start_t: time, end_t: time):
    """
    Return True if now_time is within [start_t, end_t).
    If end_t <= start_t treat as overnight window (e.g., 22:00 -> 06:00).
    """
    if start_t < end_t:
        return start_t <= now_time < end_t
    else:
        # overnight
        return now_time >= start_t or now_time < end_t

# ---------------- Reminder sending ----------------
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id
    # fetch up-to-date schedule (in case user changed)
    entry = reminders_data.get(str(chat_id))
    if not entry:
        return  # nothing to do
    tz_name = entry.get("timezone", SERVER_TZ)
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz).time()
    start_t = parse_hm(entry["start"])
    end_t = parse_hm(entry["end"])
    if is_within_window(now, start_t, end_t):
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="💧 Time to drink water! Stay hydrated — have a glass now. 🚰",
            )
        except Exception as e:
            logger.warning("Failed to send reminder to %s: %s", chat_id, e)
    else:
        logger.info("Not in window for %s at %s (start %s end %s)", chat_id, now, start_t, end_t)

def schedule_job_for_chat(application, chat_id):
    """Schedule (or reschedule) repeating hourly job for chat_id"""
    # cancel existing
    if str(chat_id) in jobs:
        job = jobs[str(chat_id)]
        job.schedule_removal()
        del jobs[str(chat_id)]

    entry = reminders_data.get(str(chat_id))
    if not entry:
        return

    tz_name = entry.get("timezone", SERVER_TZ)
    tz = pytz.timezone(tz_name)
    start_t = parse_hm(entry["start"])
    # compute first run: the next hour-aligned time >= now that is at start_t or the next hour after start
    now_dt = datetime.now(tz)
    # We want first run at next occurrence of start_t (today or tomorrow) at that minute, but then run every hour.
    first_run = next_run_after(start_t, tz)
    # If first_run is in the past relative to now, push by hours until in future
    # (next_run_after already ensures >= now)
    # Use job queue: run_repeating with interval=3600 seconds
    jq = application.job_queue
    job = jq.run_repeating(send_reminder, interval=3600, first=first_run, chat_id=chat_id, name=str(chat_id))
    jobs[str(chat_id)] = job
    logger.info("Scheduled hourly job for %s starting at %s (%s tz)", chat_id, first_run.isoformat(), tz_name)

# ---------------- Command handlers ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm WaterBuddy 🥤\n"
        "I'll remind you each hour to drink water between a start and end time.\n\n"
        "Use /set to set your schedule, /status to view your schedule, /stop to stop reminders.\n\n"
        "To begin now, send /set"
    )

async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Great — what time should reminders **start** each day? Send in 24-hour format `HH:MM` (e.g. `09:00`).",
        parse_mode="Markdown"
    )
    return ASK_START

async def ask_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        t = parse_hm(txt)
    except Exception:
        await update.message.reply_text("I couldn't parse that. Send start time like `09:00` (24-hour).", parse_mode="Markdown")
        return ASK_START

    context.user_data["proposed_start"] = txt
    # Ask for end
    await update.message.reply_text("Great. Now send the **end** time in `HH:MM` (24-hour). For example `18:00` means reminders up to 17:00 inclusive.", parse_mode="Markdown")
    return ASK_END

async def ask_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        t = parse_hm(txt)
    except Exception:
        await update.message.reply_text("I couldn't parse that. Send end time like `18:00` (24-hour).", parse_mode="Markdown")
        return ASK_END

    start_txt = context.user_data.get("proposed_start")
    if not start_txt:
        await update.message.reply_text("Unexpected error — please run /set again.")
        return ConversationHandler.END

    # Save settings
    chat_id = update.effective_chat.id
    tz_name = SERVER_TZ  # server tz by default — user timezone support could be added
    reminders_data[str(chat_id)] = {"start": start_txt, "end": txt, "timezone": tz_name}
    save_data()

    # schedule job
    schedule_job_for_chat(context.application, chat_id)

    await update.message.reply_text(
        f"All set! I'll remind you every hour between {start_txt} and {txt} (server timezone: {tz_name}).\n"
        "Use /status to check or /stop to stop reminders."
    )
    return ConversationHandler.END

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    entry = reminders_data.get(str(chat_id))
    if not entry:
        await update.message.reply_text("You have no reminders set. Use /set to configure hourly reminders.")
    else:
        await update.message.reply_text(f"Reminders set from {entry['start']} to {entry['end']} (timezone: {entry.get('timezone', SERVER_TZ)}).")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if str(chat_id) in reminders_data:
        del reminders_data[str(chat_id)]
        save_data()
    if str(chat_id) in jobs:
        jobs[str(chat_id)].schedule_removal()
        del jobs[str(chat_id)]
    await update.message.reply_text("Reminders stopped. Use /set to create a new schedule.")

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ---------------- Startup: load data and schedule jobs ----------------
def restore_jobs(application):
    load_data()
    for chat_id_str, entry in reminders_data.items():
        try:
            schedule_job_for_chat(application, int(chat_id_str))
        except Exception as e:
            logger.exception("Failed to schedule job for %s: %s", chat_id_str, e)

# ---------------- Main ----------------
def main():
    if TOKEN is None:
        logger.error("TELEGRAM_TOKEN not set. Exiting.")
        return

    application = ApplicationBuilder().token(TOKEN).build()

    # Conversation handler for /set
    conv = ConversationHandler(
        entry_points=[CommandHandler("set", set_cmd)],
        states={
            ASK_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_start)],
            ASK_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_end)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        per_chat=True,
    )

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(conv)
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))

    # restore persisted jobs after application starts
    application.post_init = lambda app: restore_jobs(app)

    # Run
    logger.info("Starting Water Reminder bot (TZ=%s)", SERVER_TZ)
    application.run_polling()

if __name__ == "__main__":
    main()
