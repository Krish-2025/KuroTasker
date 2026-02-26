import os
import sqlite3
import json
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("8730515219:AAEQzmPPiJYTL7ST-zcXxSpmsSUz0BDcYGY")
print(f"DEBUG TOKEN: '{8730515219:AAEQzmPPiJYTL7ST-zcXxSpmsSUz0BDcYGY}'")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("tasks.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            schedule TEXT NOT NULL,
            active INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS task_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            task_name TEXT,
            status TEXT,
            scheduled_at TEXT,
            responded_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect("tasks.db")

def get_config(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None

def set_config(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, str(value)))
    conn.commit()
    conn.close()

def add_task(name, schedule):
    conn = get_db()
    conn.execute("INSERT INTO tasks (name, schedule) VALUES (?,?)", (name, schedule))
    conn.commit()
    conn.close()

def get_tasks(active_only=True):
    conn = get_db()
    q = "SELECT id, name, schedule, active FROM tasks"
    if active_only:
        q += " WHERE active=1"
    rows = conn.execute(q).fetchall()
    conn.close()
    return rows

def log_response(task_id, task_name, status, scheduled_at):
    conn = get_db()
    conn.execute(
        "INSERT INTO task_log (task_id, task_name, status, scheduled_at, responded_at) VALUES (?,?,?,?,?)",
        (task_id, task_name, status, scheduled_at, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def should_send_now(schedule):
    now = datetime.now()
    current_time = now.strftime("%H:%M")
    current_day = now.strftime("%a")
    if " " in schedule:
        days_part, time_part = schedule.split(" ", 1)
        days = [d.strip() for d in days_part.split(",")]
        return current_time == time_part and current_day in days
    else:
        return current_time == schedule

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    set_config("chat_id", chat_id)
    await update.message.reply_text(
        f"✅ Bot activated! Your Chat ID: {chat_id}\n\n"
        "Commands:\n"
        "/addtask Name | HH:MM  — daily task\n"
        "/addweekly Name | Mon,Tue,Wed | HH:MM  — weekly task\n"
        "/listtasks — show all tasks\n"
        "/removetask ID — remove a task\n"
        "/stats — show stats"
    )

async def add_task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = " ".join(context.args)
        name, time_str = [x.strip() for x in text.split("|")]
        add_task(name, time_str)
        await update.message.reply_text(f"✅ Added: {name} at {time_str} daily")
    except Exception:
        await update.message.reply_text("Usage: /addtask Meditate | 07:30")

async def add_weekly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = " ".join(context.args)
        parts = [x.strip() for x in text.split("|")]
        name, days, time_str = parts[0], parts[1], parts[2]
        add_task(name, f"{days} {time_str}")
        await update.message.reply_text(f"✅ Added: {name} on {days} at {time_str}")
    except Exception:
        await update.message.reply_text("Usage: /addweekly Gym | Mon,Tue,Wed | 08:00")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_tasks()
    if not tasks:
        await update.message.reply_text("No tasks yet. Use /addtask to add one!")
        return
    msg = "Your Tasks:\n\n"
    for t in tasks:
        msg += f"[{t[0]}] {t[1]} — {t[2]}\n"
    await update.message.reply_text(msg)

async def remove_task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        task_id = int(context.args[0])
        conn = get_db()
        conn.execute("UPDATE tasks SET active=0 WHERE id=?", (task_id,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"Task {task_id} removed.")
    except Exception:
        await update.message.reply_text("Usage: /removetask 1")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from collections import defaultdict
    conn = get_db()
    rows = conn.execute("SELECT task_name, status FROM task_log").fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No data yet!")
        return
    summary = defaultdict(lambda: {"done": 0, "skip": 0, "postpone": 0})
    for task_name, status in rows:
        summary[task_name][status] += 1
    msg = "Stats:\n\n"
    for task, counts in summary.items():
        total = sum(counts.values())
        pct = int(counts["done"] / total * 100) if total else 0
        msg += f"{task}\n  Done: {counts['done']}  Skip: {counts['skip']}  Postpone: {counts['postpone']}  ({pct}%)\n\n"
    await update.message.reply_text(msg)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = json.loads(query.data)
    log_response(data["id"], data["name"], data["status"], data["at"])
    emoji = {"done": "✅", "skip": "⏭️", "postpone": "⏰"}
    await query.edit_message_text(f"{emoji[data['status']]} {data['name']} marked as {data['status']}")

# ─── REMINDER JOB ─────────────────────────────────────────────────────────────
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    sent = context.bot_data.setdefault("sent", set())
    now = datetime.now()
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    chat_id = get_config("chat_id")
    if not chat_id:
        return
    for task_id, name, schedule, active in get_tasks():
        key = f"{minute_key}_{task_id}"
        if key not in sent and should_send_now(schedule):
            sent.add(key)
            scheduled_at = now.isoformat()
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Done",     callback_data=json.dumps({"id": task_id, "name": name, "status": "done",     "at": scheduled_at})),
                InlineKeyboardButton("⏭️ Skip",     callback_data=json.dumps({"id": task_id, "name": name, "status": "skip",     "at": scheduled_at})),
                InlineKeyboardButton("⏰ Postpone", callback_data=json.dumps({"id": task_id, "name": name, "status": "postpone", "at": scheduled_at})),
            ]])
            await app.bot.send_message(
                chat_id=int(chat_id),
                text=f"🔔 Reminder: {name}\n{now.strftime('%I:%M %p')}",
                reply_markup=keyboard
            )
    if len(sent) > 1000:
        context.bot_data["sent"] = set()

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("addtask",    add_task_cmd))
    app.add_handler(CommandHandler("addweekly",  add_weekly_cmd))
    app.add_handler(CommandHandler("listtasks",  list_tasks))
    app.add_handler(CommandHandler("removetask", remove_task_cmd))
    app.add_handler(CommandHandler("stats",      stats_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.job_queue.run_repeating(reminder_job, interval=60, first=5)
    logger.info("✅ Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
