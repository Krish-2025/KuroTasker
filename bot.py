import os
import sqlite3
import logging
from collections import defaultdict
from datetime import datetime, timedelta, date

import pytz
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN")
PORT         = int(os.environ.get("PORT", 8080))
DOMAIN       = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL  = f"https://{DOMAIN}{WEBHOOK_PATH}"
IST          = pytz.timezone("Asia/Kolkata")
DB_PATH      = os.environ.get("DB_PATH", "tasks.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            schedule   TEXT NOT NULL,
            points     INTEGER DEFAULT 10,
            active     INTEGER DEFAULT 1,
            removed_at TEXT DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS task_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id      INTEGER,
            task_name    TEXT,
            status       TEXT,
            points       INTEGER DEFAULT 0,
            scheduled_at TEXT,
            responded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    for col in [
        ("tasks",    "points",     "INTEGER DEFAULT 10"),
        ("tasks",    "removed_at", "TEXT DEFAULT NULL"),
        ("task_log", "points",     "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE {col[0]} ADD COLUMN {col[1]} {col[2]}")
            conn.commit()
        except Exception:
            pass
    conn.close()

def get_config(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None

def set_config(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, str(value)))
    conn.commit()
    conn.close()

def add_task(name, schedule, points=10):
    conn = get_db()
    conn.execute("INSERT INTO tasks (name, schedule, points) VALUES (?,?,?)", (name, schedule, points))
    conn.commit()
    conn.close()

def get_tasks(active_only=True):
    conn = get_db()
    q = "SELECT id, name, schedule, points, active, removed_at FROM tasks"
    if active_only:
        q += " WHERE active=1"
    rows = conn.execute(q).fetchall()
    conn.close()
    return rows

def get_task_points(task_id):
    conn = get_db()
    row = conn.execute("SELECT points FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return row["points"] if row else 10

def log_response(task_id, task_name, status, scheduled_at):
    pts = get_task_points(task_id) if status == "done" else 0
    conn = get_db()
    conn.execute(
        "INSERT INTO task_log (task_id,task_name,status,points,scheduled_at,responded_at) VALUES (?,?,?,?,?,?)",
        (task_id, task_name, status, pts, scheduled_at, datetime.now(IST).isoformat())
    )
    conn.commit()
    conn.close()

def should_send_now(schedule):
    now          = datetime.now(IST)
    current_time = now.strftime("%H:%M")
    current_day  = now.strftime("%a")
    if " " in schedule:
        days_part, time_part = schedule.split(" ", 1)
        days = [d.strip() for d in days_part.split(",")]
        return current_time == time_part and current_day in days
    return current_time == schedule

def get_stats_data(date_from=None, date_to=None):
    conn = get_db()

    # All tasks (including removed) with their points and removal date
    all_tasks = {r["name"]: {
        "points":     r["points"] or 10,
        "active":     r["active"],
        "removed_at": r["removed_at"],
    } for r in conn.execute("SELECT name, points, active, removed_at FROM tasks").fetchall()}

    # Determine date range
    today = datetime.now(IST).date()
    if date_from and date_to:
        try:
            d_from = date.fromisoformat(date_from)
            d_to   = date.fromisoformat(date_to)
        except Exception:
            d_from = today - timedelta(days=29)
            d_to   = today
    else:
        d_from = today - timedelta(days=29)
        d_to   = today

    num_days = (d_to - d_from).days + 1
    dates = [(d_from + timedelta(days=i)).isoformat() for i in range(num_days)]

    rows = conn.execute(
        "SELECT task_name, status, points, DATE(scheduled_at) as day FROM task_log "
        "WHERE DATE(scheduled_at) >= ? AND DATE(scheduled_at) <= ? ORDER BY scheduled_at",
        (dates[0], dates[-1])
    ).fetchall()
    conn.close()

    # tasks that have ANY log data in range
    tasks_in_range = list(dict.fromkeys(r["task_name"] for r in rows))

    daily = {}
    for task in tasks_in_range:
        info   = all_tasks.get(task, {"points": 10, "active": 1, "removed_at": None})
        d      = defaultdict(float)
        for r in rows:
            if r["task_name"] == task and r["status"] == "done":
                d[r["day"]] += r["points"] if r["points"] else info["points"]

        # Build scores: None for days after task was removed (discontinues line)
        scores = []
        for day in dates:
            if info["removed_at"] and day > info["removed_at"][:10]:
                scores.append(None)   # null = gap in chart line
            else:
                scores.append(d[day])

        daily[task] = {
            "dates":      dates,
            "points":     info["points"],
            "active":     info["active"],
            "removed_at": info["removed_at"],
            "scores":     scores,
        }

    # totals
    totals = defaultdict(lambda: {"done": 0, "skip": 0, "postpone": 0, "score": 0})
    for r in rows:
        totals[r["task_name"]][r["status"]] += 1
        if r["status"] == "done":
            totals[r["task_name"]]["score"] += r["points"] if r["points"] else all_tasks.get(r["task_name"], {}).get("points", 10)

    overall = []
    for t in tasks_in_range:
        c         = totals[t]
        total     = c["done"] + c["skip"] + c["postpone"]
        pts       = all_tasks.get(t, {}).get("points", 10)
        max_score = total * pts
        overall.append({
            "task":       t,
            "done":       c["done"],
            "skip":       c["skip"],
            "postpone":   c["postpone"],
            "score":      c["score"],
            "max_score":  max_score,
            "points_per": pts,
            "active":     all_tasks.get(t, {}).get("active", 1),
        })

    # earliest log date for date picker min
    conn2 = get_db()
    first = conn2.execute("SELECT MIN(DATE(scheduled_at)) as d FROM task_log").fetchone()
    conn2.close()
    first_date = first["d"] if first and first["d"] else today.isoformat()

    return {
        "overall":    overall,
        "daily":      daily,
        "date_from":  dates[0],
        "date_to":    dates[-1],
        "first_date": first_date,
        "generated_at": datetime.now(IST).isoformat(),
    }

# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    set_config("chat_id", chat_id)
    await update.message.reply_text(
        f"Bot activated! Chat ID: {chat_id}\n\n"
        "Commands:\n"
        "/addtask Name | HH:MM | Points\n"
        "/addweekly Name | Mon,Tue,Wed | HH:MM | Points\n"
        "/listtasks\n"
        "/removetask ID\n"
        "/stats\n\n"
        "Points = score when task is done.\n"
        "Example: /addtask Gym | 18:00 | 30\n"
        "All times IST"
    )

async def add_task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text   = " ".join(context.args)
        parts  = [x.strip() for x in text.split("|")]
        name   = parts[0]
        time_str = parts[1]
        points = int(parts[2]) if len(parts) > 2 else 10
        add_task(name, time_str, points)
        await update.message.reply_text(f"Added: {name} daily at {time_str} IST ({points}pts)")
    except Exception:
        await update.message.reply_text("Usage: /addtask Meditate | 07:30 | 20")

async def add_weekly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text   = " ".join(context.args)
        parts  = [x.strip() for x in text.split("|")]
        name, days, time_str = parts[0], parts[1], parts[2]
        points = int(parts[3]) if len(parts) > 3 else 10
        add_task(name, f"{days} {time_str}", points)
        await update.message.reply_text(f"Added: {name} on {days} at {time_str} IST ({points}pts)")
    except Exception:
        await update.message.reply_text("Usage: /addweekly Gym | Mon,Tue,Wed | 18:00 | 30")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_tasks()
    if not tasks:
        await update.message.reply_text("No tasks yet!")
        return
    msg = "Your Tasks:\n\n"
    for t in tasks:
        msg += f"[{t['id']}] {t['name']} - {t['schedule']} ({t['points']}pts)\n"
    await update.message.reply_text(msg)

async def remove_task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        task_id    = int(context.args[0])
        removed_at = datetime.now(IST).isoformat()
        conn       = get_db()
        conn.execute("UPDATE tasks SET active=0, removed_at=? WHERE id=?", (removed_at, task_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"Task {task_id} removed. Historical data preserved.")
    except Exception:
        await update.message.reply_text("Usage: /removetask 1")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    rows = conn.execute("SELECT task_name, status, points FROM task_log").fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No data yet!")
        return
    summary = defaultdict(lambda: {"done": 0, "skip": 0, "postpone": 0, "score": 0})
    for r in rows:
        summary[r["task_name"]][r["status"]] += 1
        if r["status"] == "done":
            summary[r["task_name"]]["score"] += r["points"] or 0
    msg = "Stats:\n\n"
    for task, c in summary.items():
        total = c["done"] + c["skip"] + c["postpone"]
        pct   = int(c["done"] / total * 100) if total else 0
        msg  += f"{task}\n  Done:{c['done']} Skip:{c['skip']} Post:{c['postpone']} Score:{c['score']}pts ({pct}%)\n\n"
    await update.message.reply_text(msg)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        parts    = query.data.split("|")
        task_id, status, epoch = int(parts[0]), parts[1], int(parts[2])
        scheduled_at = datetime.fromtimestamp(epoch, tz=IST).isoformat()
    except (ValueError, OSError) as exc:
        logger.warning("button_handler: invalid callback %r: %s", query.data, exc)
        return
    conn      = get_db()
    row       = conn.execute("SELECT name FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    task_name = row["name"] if row else "Unknown"
    log_response(task_id, task_name, status, scheduled_at)
    labels = {"done": "Done", "skip": "Skipped", "postpone": "Postponed"}
    await query.edit_message_text(f"{task_name} - {labels[status]}")

# ─── REMINDER JOB ─────────────────────────────────────────────────────────────
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    sent       = context.bot_data.setdefault("sent", set())
    now        = datetime.now(IST)
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    chat_id    = get_config("chat_id")
    logger.info(f"TICK IST={minute_key} tasks={[t['name'] for t in get_tasks()]}")
    if not chat_id:
        return
    for t in get_tasks():
        key = f"{minute_key}_{t['id']}"
        if key not in sent and should_send_now(t["schedule"]):
            sent.add(key)
            at = int(now.timestamp())
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("Done",     callback_data=f"{t['id']}|done|{at}"),
                InlineKeyboardButton("Skip",     callback_data=f"{t['id']}|skip|{at}"),
                InlineKeyboardButton("Postpone", callback_data=f"{t['id']}|postpone|{at}"),
            ]])
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=f"Reminder: {t['name']} ({t['points']}pts)\n{now.strftime('%I:%M %p')} IST",
                reply_markup=kb
            )
    if len(sent) > 2000:
        context.bot_data["sent"] = set()

# ─── DASHBOARD ────────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>KuroTasker</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Fraunces:ital,wght@0,300;0,700;1,300&display=swap');
:root{--bg:#07090e;--s1:#0d1018;--s2:#141820;--bd:#1c2130;--done:#4ade80;--skip:#facc15;--post:#f97316;--txt:#dde3f0;--mut:#4a5568;--acc:#818cf8;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--txt);font-family:'DM Mono',monospace;min-height:100vh;}
body::before{content:'';position:fixed;top:-25%;left:-15%;width:65%;height:65%;background:radial-gradient(ellipse,rgba(99,102,241,.08) 0%,transparent 65%);pointer-events:none;z-index:0;}
body::after{content:'';position:fixed;bottom:-20%;right:-10%;width:55%;height:55%;background:radial-gradient(ellipse,rgba(74,222,128,.06) 0%,transparent 65%);pointer-events:none;z-index:0;}
header{position:relative;z-index:1;padding:2.5rem 3rem 2rem;border-bottom:1px solid var(--bd);display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:1rem;}
h1{font-family:'Fraunces',serif;font-size:clamp(1.8rem,3.5vw,2.8rem);font-weight:700;letter-spacing:-.02em;}
h1 em{font-style:italic;font-weight:300;color:var(--acc);}
.sub{font-size:.68rem;color:var(--mut);letter-spacing:.1em;text-transform:uppercase;margin-top:.35rem;}
.btn{background:var(--s2);border:1px solid var(--bd);color:var(--txt);font-family:'DM Mono',monospace;font-size:.68rem;padding:.5rem 1.2rem;cursor:pointer;letter-spacing:.08em;text-transform:uppercase;transition:all .2s;}
.btn:hover{border-color:var(--acc);color:var(--acc);}
.btn.primary{background:var(--acc);color:#fff;border-color:var(--acc);}
.btn.primary:hover{opacity:.85;}
main{position:relative;z-index:1;padding:2rem 3rem 5rem;max-width:1400px;margin:0 auto;}
@media(max-width:700px){main{padding:1.2rem;}header{padding:1.8rem 1.4rem 1.4rem;}}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:1rem;margin-bottom:2.5rem;}
.card{background:var(--s1);border:1px solid var(--bd);padding:1.4rem;position:relative;overflow:hidden;}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;}
.card.c-acc::before{background:var(--acc);}.card.c-done::before{background:var(--done);}.card.c-skip::before{background:var(--skip);}.card.c-post::before{background:var(--post);}
.clabel{font-size:.58rem;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);margin-bottom:.55rem;}
.cval{font-family:'Fraunces',serif;font-size:2.6rem;font-weight:700;line-height:1;}
.card.c-acc .cval{color:var(--acc);}.card.c-done .cval{color:var(--done);}.card.c-skip .cval{color:var(--skip);}.card.c-post .cval{color:var(--post);}
.csub{font-size:.62rem;color:var(--mut);margin-top:.3rem;}
.stitle{font-size:.6rem;letter-spacing:.15em;text-transform:uppercase;color:var(--mut);margin-bottom:1rem;padding-bottom:.5rem;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:.5rem;}
.stitle span{width:5px;height:5px;border-radius:50%;background:var(--acc);display:inline-block;}
.chart-wrap{background:var(--s1);border:1px solid var(--bd);padding:1.5rem;margin-bottom:2rem;}
.chart-wrap canvas{max-height:380px;}
.cbox-title{font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;color:var(--mut);margin-bottom:1rem;}
table{width:100%;border-collapse:collapse;background:var(--s1);border:1px solid var(--bd);font-size:.76rem;margin-bottom:2rem;}
th{text-align:left;padding:.7rem 1.1rem;font-size:.58rem;letter-spacing:.1em;text-transform:uppercase;color:var(--mut);border-bottom:1px solid var(--bd);}
td{padding:.85rem 1.1rem;border-bottom:1px solid var(--bd);vertical-align:middle;}
tr:last-child td{border-bottom:none;}tr:hover td{background:var(--s2);}
.tname{font-family:'Fraunces',serif;font-size:.95rem;font-weight:700;}
.inactive{opacity:.5;font-style:italic;}
.pill{display:inline-block;padding:.15rem .55rem;font-size:.6rem;border-radius:2px;}
.pd{background:rgba(74,222,128,.15);color:var(--done);}.ps{background:rgba(250,204,21,.15);color:var(--skip);}.pp{background:rgba(249,115,22,.15);color:var(--post);}
.bwrap{width:100%;height:4px;background:var(--bd);border-radius:2px;margin-top:.35rem;overflow:hidden;}
.bfill{height:100%;background:linear-gradient(90deg,var(--done),var(--acc));border-radius:2px;transition:width 1s ease;}
.rate{font-family:'Fraunces',serif;font-weight:700;font-size:.95rem;}
.loading{text-align:center;padding:4rem;color:var(--mut);font-size:.78rem;letter-spacing:.1em;}
.empty{text-align:center;padding:5rem 2rem;color:var(--mut);}
.empty h2{font-family:'Fraunces',serif;font-size:1.6rem;font-style:italic;font-weight:300;opacity:.4;color:var(--txt);margin-bottom:.75rem;}
.legend{display:flex;gap:1.2rem;font-size:.62rem;color:var(--mut);margin-top:.85rem;flex-wrap:wrap;align-items:center;}
.legend span{display:flex;align-items:center;gap:.4rem;cursor:default;}
.ldot{width:16px;height:3px;border-radius:2px;display:inline-block;}

/* Date range + download section */
.download-section{background:var(--s1);border:1px solid var(--bd);padding:1.5rem;margin-bottom:2rem;}
.download-section .cbox-title{margin-bottom:1rem;}
.date-row{display:flex;gap:1rem;align-items:center;flex-wrap:wrap;}
.date-row label{font-size:.65rem;color:var(--mut);letter-spacing:.08em;text-transform:uppercase;}
.date-row input[type=date]{background:var(--s2);border:1px solid var(--bd);color:var(--txt);font-family:'DM Mono',monospace;font-size:.72rem;padding:.4rem .7rem;outline:none;colorscheme:dark;}
.date-row input[type=date]:focus{border-color:var(--acc);}
</style>
</head>
<body>
<header>
  <div><h1>Kuro<em>Tasker</em></h1><div class="sub" id="sub">Loading…</div></div>
  <button class="btn" onclick="applyRange()">↻ Refresh</button>
</header>
<main>
  <div class="loading" id="loading">Fetching your stats…</div>
  <div id="dash" style="display:none"></div>
</main>

<script>
const TASK_COLORS=[
  {solid:'rgba(129,140,248,1)',light:'rgba(129,140,248,0.4)'},
  {solid:'rgba(74,222,128,1)', light:'rgba(74,222,128,0.4)'},
  {solid:'rgba(249,115,22,1)',light:'rgba(249,115,22,0.4)'},
  {solid:'rgba(250,204,21,1)',light:'rgba(250,204,21,0.4)'},
  {solid:'rgba(236,72,153,1)',light:'rgba(236,72,153,0.4)'},
  {solid:'rgba(34,211,238,1)',light:'rgba(34,211,238,0.4)'},
];
const NET={solid:'rgba(255,255,255,1)',fill:'rgba(255,255,255,0.06)'};
const C={bd:'#1c2130',txt:'#94a3b8'};
const font={family:'DM Mono',size:11};
let charts={};
let currentFrom=null, currentTo=null, firstDate=null;

async function load(from, to){
  document.getElementById('loading').style.display='block';
  document.getElementById('dash').style.display='none';
  let url='/api/stats';
  if(from && to) url+=`?from=${from}&to=${to}`;
  try{const r=await fetch(url);const d=await r.json();render(d);}
  catch(e){document.getElementById('loading').textContent='Failed to load. Try refreshing.';}
}

function applyRange(){
  const f=document.getElementById('date-from');
  const t=document.getElementById('date-to');
  if(f&&t) load(f.value, t.value);
  else load();
}

function render(d){
  document.getElementById('loading').style.display='none';
  const dash=document.getElementById('dash');
  dash.style.display='block';
  const overall=d.overall||[];
  const daily=d.daily||{};
  currentFrom=d.date_from; currentTo=d.date_to; firstDate=d.first_date;

  if(!overall.length){
    dash.innerHTML='<div class="empty"><h2>No data yet</h2><p>Add tasks and respond to reminders on Telegram, then refresh.</p></div>';
    document.getElementById('sub').textContent='No data yet';
    return;
  }

  let td=0,ts=0,tp=0,totalScore=0,maxScore=0;
  overall.forEach(o=>{td+=o.done;ts+=o.skip;tp+=o.postpone;totalScore+=o.score;maxScore+=o.max_score;});
  const tot=td+ts+tp;
  const rate=tot?Math.round(td/tot*100):0;
  const scorePct=maxScore?Math.round(totalScore/maxScore*100):0;

  document.getElementById('sub').textContent=`${overall.length} task${overall.length!==1?'s':''} · ${new Date(d.generated_at).toLocaleTimeString()}`;
  Object.values(charts).forEach(c=>c.destroy());charts={};

  dash.innerHTML=`
    <div class="cards">
      <div class="card c-acc"><div class="clabel">Score Index</div><div class="cval">${scorePct}%</div><div class="csub">${totalScore}/${maxScore} pts earned</div></div>
      <div class="card c-done"><div class="clabel">Completed</div><div class="cval">${td}</div><div class="csub">${rate}% completion rate</div></div>
      <div class="card c-skip"><div class="clabel">Skipped</div><div class="cval">${ts}</div><div class="csub">${tot?Math.round(ts/tot*100):0}% of total</div></div>
      <div class="card c-post"><div class="clabel">Postponed</div><div class="cval">${tp}</div><div class="csub">${tot?Math.round(tp/tot*100):0}% of total</div></div>
    </div>

    <div class="stitle"><span></span> Progress Over Time</div>
    <div class="chart-wrap">
      <div class="cbox-title">Net progress (white · bold) + individual tasks (colored · lighter) · points earned per day</div>
      <canvas id="progress-chart"></canvas>
      <div class="legend" id="progress-legend"></div>
    </div>

    <div class="stitle"><span></span> Task Breakdown</div>
    <table>
      <thead><tr><th>Task</th><th>Pts/Task</th><th>Done</th><th>Skipped</th><th>Postponed</th><th>Score</th><th>Completion</th></tr></thead>
      <tbody>${overall.map(o=>{
        const t=o.done+o.skip+o.postpone;
        const pct=t?Math.round(o.done/t*100):0;
        const spct=o.max_score?Math.round(o.score/o.max_score*100):0;
        const col=pct>=70?'var(--done)':pct>=40?'var(--skip)':'var(--post)';
        const inactive=!o.active?'inactive':'';
        return `<tr>
          <td><div class="tname ${inactive}">${o.task}${!o.active?' (removed)':''}</div></td>
          <td style="color:var(--acc);font-weight:600">${o.points_per}pts</td>
          <td><span class="pill pd">✓ ${o.done}</span></td>
          <td><span class="pill ps">⏭ ${o.skip}</span></td>
          <td><span class="pill pp">⏰ ${o.postpone}</span></td>
          <td><span class="rate" style="color:var(--acc)">${spct}%</span><div class="csub">${o.score}/${o.max_score}pts</div></td>
          <td><span class="rate" style="color:${col}">${pct}%</span><div class="bwrap"><div class="bfill" style="width:${pct}%"></div></div></td>
        </tr>`;
      }).join('')}</tbody>
    </table>

    <div class="stitle"><span></span> Download Progress Chart</div>
    <div class="download-section">
      <div class="cbox-title">Select date range and download as image</div>
      <div class="date-row">
        <div><label>From</label><br><input type="date" id="date-from" value="${currentFrom}" min="${firstDate}" max="${currentTo}"></div>
        <div><label>To</label><br><input type="date" id="date-to" value="${currentTo}" min="${firstDate}" max="${new Date().toISOString().slice(0,10)}"></div>
        <div style="padding-top:1.4rem;display:flex;gap:.75rem;">
          <button class="btn" onclick="applyRange()">Apply Range</button>
          <button class="btn primary" onclick="downloadChart()">⬇ Download PNG</button>
        </div>
      </div>
    </div>
  `;

  // ── Build chart ──
  const firstKey=Object.keys(daily)[0];
  if(!firstKey) return;
  const labels=daily[firstKey].dates.map(d=>{
    const dt=new Date(d+'T00:00:00');
    return `${dt.getMonth()+1}/${dt.getDate()}`;
  });

  const datasets=[];

  // Individual task lines (lighter, behind)
  overall.forEach((o,i)=>{
    const td2=daily[o.task];if(!td2)return;
    const col=TASK_COLORS[i%TASK_COLORS.length];
    datasets.push({
      label:o.task,
      data:td2.scores,
      borderColor:col.light,
      backgroundColor:'transparent',
      borderWidth:1.5,
      pointRadius:2,
      pointHoverRadius:4,
      tension:0.4,
      spanGaps:false,  // gap where null = discontinued
      order:2,
    });
  });

  // Net progress line (white, bold, front)
  const netScores=labels.map((_,i)=>
    Object.values(daily).reduce((sum,t)=>{
      const v=t.scores[i];
      return sum+(v!=null?v:0);
    },0)
  );
  datasets.push({
    label:'Net Progress',
    data:netScores,
    borderColor:NET.solid,
    backgroundColor:NET.fill,
    borderWidth:3,
    pointRadius:3,
    pointHoverRadius:6,
    tension:0.4,
    fill:true,
    order:1,
  });

  charts.progress=new Chart(document.getElementById('progress-chart').getContext('2d'),{
    type:'line',
    data:{labels,datasets},
    options:{
      responsive:true,
      maintainAspectRatio:true,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false}},
      scales:{
        x:{ticks:{color:C.txt,font,maxTicksLimit:15},grid:{color:C.bd}},
        y:{ticks:{color:C.txt,font},grid:{color:C.bd},beginAtZero:true,
           title:{display:true,text:'Points Earned',color:C.txt,font}}
      }
    }
  });

  // Legend
  const legendEl=document.getElementById('progress-legend');
  let lh=`<span><div class="ldot" style="background:${NET.solid}"></div>Net Progress</span>`;
  overall.forEach((o,i)=>{
    const col=TASK_COLORS[i%TASK_COLORS.length];
    const tag=!o.active?` <em style="font-size:.58rem">(removed)</em>`:'';
    lh+=`<span><div class="ldot" style="background:${col.light}"></div>${o.task}${tag}</span>`;
  });
  legendEl.innerHTML=lh;
}

function downloadChart(){
  const canvas=document.getElementById('progress-chart');
  if(!canvas){alert('No chart to download!');return;}
  const from=document.getElementById('date-from')?.value||currentFrom;
  const to=document.getElementById('date-to')?.value||currentTo;

  // Draw on white-bg offscreen canvas for clean PNG
  const off=document.createElement('canvas');
  off.width=canvas.width; off.height=canvas.height;
  const ctx=off.getContext('2d');
  ctx.fillStyle='#07090e';
  ctx.fillRect(0,0,off.width,off.height);
  ctx.drawImage(canvas,0,0);

  const a=document.createElement('a');
  a.href=off.toDataURL('image/png');
  a.download=`kurotasker_${from}_to_${to}.png`;
  a.click();
}

load();
</script>
</body>
</html>"""

# ─── AIOHTTP ROUTES ───────────────────────────────────────────────────────────
async def handle_webhook(request):
    app  = request.app["tg_app"]
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return web.Response(text="ok")

async def handle_dashboard(request):
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")

async def handle_stats(request):
    date_from = request.rel_url.query.get("from")
    date_to   = request.rel_url.query.get("to")
    data = get_stats_data(date_from, date_to)
    return web.json_response(data)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    init_db()

    tg_app = Application.builder().token(BOT_TOKEN).updater(None).build()
    tg_app.add_handler(CommandHandler("start",      start))
    tg_app.add_handler(CommandHandler("addtask",    add_task_cmd))
    tg_app.add_handler(CommandHandler("addweekly",  add_weekly_cmd))
    tg_app.add_handler(CommandHandler("listtasks",  list_tasks))
    tg_app.add_handler(CommandHandler("removetask", remove_task_cmd))
    tg_app.add_handler(CommandHandler("stats",      stats_cmd))
    tg_app.add_handler(CallbackQueryHandler(button_handler))
    tg_app.job_queue.run_repeating(reminder_job, interval=60, first=10)

    web_app = web.Application()
    web_app["tg_app"] = tg_app
    web_app.router.add_post(WEBHOOK_PATH, handle_webhook)
    web_app.router.add_get("/",           handle_dashboard)
    web_app.router.add_get("/api/stats",  handle_stats)

    async def on_startup(app):
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.bot.set_webhook(WEBHOOK_URL)
        logger.info(f"Webhook set: {WEBHOOK_URL}")

    async def on_shutdown(app):
        await tg_app.stop()
        await tg_app.shutdown()

    web_app.on_startup.append(on_startup)
    web_app.on_shutdown.append(on_shutdown)

    logger.info(f"Starting on port {PORT}")
    web.run_app(web_app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
