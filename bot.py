import os
import sqlite3
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta

import pytz
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN")
PORT        = int(os.environ.get("PORT", 8080))
DOMAIN      = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
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
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL,
            schedule TEXT NOT NULL,
            active   INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS task_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id      INTEGER,
            task_name    TEXT,
            status       TEXT,
            scheduled_at TEXT,
            responded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()
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
        "INSERT INTO task_log (task_id,task_name,status,scheduled_at,responded_at) VALUES (?,?,?,?,?)",
        (task_id, task_name, status, scheduled_at, datetime.now(IST).isoformat())
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

def get_stats_data():
    conn = get_db()
    rows = conn.execute(
        "SELECT task_name, status, DATE(scheduled_at) as day FROM task_log ORDER BY scheduled_at"
    ).fetchall()
    conn.close()
    tasks = list(dict.fromkeys(r["task_name"] for r in rows))
    today = datetime.now(IST).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    daily = {}
    for task in tasks:
        d = defaultdict(lambda: {"done": 0, "skip": 0, "postpone": 0})
        for r in rows:
            if r["task_name"] == task and str(r["day"]) in dates:
                d[str(r["day"])][r["status"]] += 1
        daily[task] = {"dates": dates, "done": [d[x]["done"] for x in dates],
                       "skip": [d[x]["skip"] for x in dates], "postpone": [d[x]["postpone"] for x in dates]}
    totals = defaultdict(lambda: {"done": 0, "skip": 0, "postpone": 0})
    for r in rows:
        totals[r["task_name"]][r["status"]] += 1
    overall = [{"task": t, "done": totals[t]["done"], "skip": totals[t]["skip"],
                "postpone": totals[t]["postpone"]} for t in tasks]
    return {"overall": overall, "daily": daily, "generated_at": datetime.now(IST).isoformat()}

# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    set_config("chat_id", chat_id)
    await update.message.reply_text(
        f"Bot activated! Chat ID: {chat_id}\n\n"
        "/addtask Name | HH:MM\n"
        "/addweekly Name | Mon,Tue,Wed | HH:MM\n"
        "/listtasks\n"
        "/removetask ID\n"
        "/stats\n\n"
        "All times IST"
    )

async def add_task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = " ".join(context.args)
        name, time_str = [x.strip() for x in text.split("|")]
        add_task(name, time_str)
        await update.message.reply_text(f"Added: {name} daily at {time_str} IST")
    except Exception:
        await update.message.reply_text("Usage: /addtask Meditate | 07:30")

async def add_weekly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = " ".join(context.args)
        parts = [x.strip() for x in text.split("|")]
        name, days, time_str = parts[0], parts[1], parts[2]
        add_task(name, f"{days} {time_str}")
        await update.message.reply_text(f"Added: {name} on {days} at {time_str} IST")
    except Exception:
        await update.message.reply_text("Usage: /addweekly Gym | Mon,Tue,Wed | 18:00")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_tasks()
    if not tasks:
        await update.message.reply_text("No tasks yet!")
        return
    msg = "Your Tasks:\n\n"
    for t in tasks:
        msg += f"[{t['id']}] {t['name']} - {t['schedule']}\n"
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
    conn = get_db()
    rows = conn.execute("SELECT task_name, status FROM task_log").fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No data yet!")
        return
    summary = defaultdict(lambda: {"done": 0, "skip": 0, "postpone": 0})
    for r in rows:
        summary[r["task_name"]][r["status"]] += 1
    msg = "Stats:\n\n"
    for task, c in summary.items():
        total = sum(c.values())
        pct = int(c["done"] / total * 100) if total else 0
        score = c["done"]*3 + c["postpone"]
        msg += f"{task}\n  Done:{c['done']} Skip:{c['skip']} Post:{c['postpone']} Score:{score}pts ({pct}%)\n\n"
    await update.message.reply_text(msg)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = json.loads(query.data)
    log_response(data["id"], data["name"], data["status"], data["at"])
    labels = {"done": "Done", "skip": "Skipped", "postpone": "Postponed"}
    await query.edit_message_text(f"{data['name']} - {labels[data['status']]}")

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
            at = now.isoformat()
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("Done",     callback_data=json.dumps({"id": t["id"], "name": t["name"], "status": "done",     "at": at})),
                InlineKeyboardButton("Skip",     callback_data=json.dumps({"id": t["id"], "name": t["name"], "status": "skip",     "at": at})),
                InlineKeyboardButton("Postpone", callback_data=json.dumps({"id": t["id"], "name": t["name"], "status": "postpone", "at": at})),
            ]])
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=f"Reminder: {t['name']}\n{now.strftime('%I:%M %p')} IST",
                reply_markup=kb
            )
    if len(sent) > 2000:
        context.bot_data["sent"] = set()

# ─── DASHBOARD HTML ───────────────────────────────────────────────────────────
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
.chart-full{background:var(--s1);border:1px solid var(--bd);padding:1.5rem;margin-bottom:2rem;}
.chart-full canvas{max-height:320px;}
.crow{display:grid;grid-template-columns:2fr 1fr;gap:1.5rem;margin-bottom:2rem;}
@media(max-width:800px){.crow{grid-template-columns:1fr;}}
.cbox{background:var(--s1);border:1px solid var(--bd);padding:1.5rem;}
.cbox canvas{max-height:260px;}
.cbox-title{font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;color:var(--mut);margin-bottom:1rem;}
table{width:100%;border-collapse:collapse;background:var(--s1);border:1px solid var(--bd);font-size:.76rem;margin-bottom:2rem;}
th{text-align:left;padding:.7rem 1.1rem;font-size:.58rem;letter-spacing:.1em;text-transform:uppercase;color:var(--mut);border-bottom:1px solid var(--bd);}
td{padding:.85rem 1.1rem;border-bottom:1px solid var(--bd);vertical-align:middle;}
tr:last-child td{border-bottom:none;}tr:hover td{background:var(--s2);}
.tname{font-family:'Fraunces',serif;font-size:.95rem;font-weight:700;}
.pill{display:inline-block;padding:.15rem .55rem;font-size:.6rem;border-radius:2px;}
.pd{background:rgba(74,222,128,.15);color:var(--done);}.ps{background:rgba(250,204,21,.15);color:var(--skip);}.pp{background:rgba(249,115,22,.15);color:var(--post);}
.bwrap{width:100%;height:4px;background:var(--bd);border-radius:2px;margin-top:.35rem;overflow:hidden;}
.bfill{height:100%;background:linear-gradient(90deg,var(--done),var(--acc));border-radius:2px;transition:width 1s ease;}
.rate{font-family:'Fraunces',serif;font-weight:700;font-size:.95rem;}
.loading{text-align:center;padding:4rem;color:var(--mut);font-size:.78rem;letter-spacing:.1em;}
.empty{text-align:center;padding:5rem 2rem;color:var(--mut);}
.empty h2{font-family:'Fraunces',serif;font-size:1.6rem;font-style:italic;font-weight:300;opacity:.4;color:var(--txt);margin-bottom:.75rem;}
.score-legend{display:flex;gap:1.5rem;font-size:.62rem;color:var(--mut);margin-top:.75rem;flex-wrap:wrap;}
.score-legend span{display:flex;align-items:center;gap:.4rem;}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;}
</style>
</head>
<body>
<header>
  <div><h1>Kuro<em>Tasker</em></h1><div class="sub" id="sub">Loading…</div></div>
  <button class="btn" onclick="load()">↻ Refresh</button>
</header>
<main>
  <div class="loading" id="loading">Fetching your stats…</div>
  <div id="dash" style="display:none"></div>
</main>
<script>
const C={bd:'#1c2130',done:'rgba(74,222,128,.85)',skip:'rgba(250,204,21,.75)',post:'rgba(249,115,22,.75)',acc:'rgba(129,140,248,.85)',txt:'#94a3b8'};
const font={family:'DM Mono',size:11};
let charts={};
async function load(){
  document.getElementById('loading').style.display='block';
  document.getElementById('dash').style.display='none';
  try{const r=await fetch('/api/stats');const d=await r.json();render(d);}
  catch(e){document.getElementById('loading').textContent='Failed to load. Try refreshing.';}
}
function render(d){
  document.getElementById('loading').style.display='none';
  const dash=document.getElementById('dash');
  dash.style.display='block';
  const overall=d.overall||[];const daily=d.daily||{};
  if(!overall.length){
    dash.innerHTML='<div class="empty"><h2>No data yet</h2><p>Add tasks and respond to reminders on Telegram, then refresh.</p></div>';
    document.getElementById('sub').textContent='No data yet';return;
  }
  let td=0,ts=0,tp=0;
  overall.forEach(o=>{td+=o.done;ts+=o.skip;tp+=o.postpone;});
  const tot=td+ts+tp;
  const rate=tot?Math.round(td/tot*100):0;
  const totalScore=overall.reduce((s,o)=>s+o.done*3+o.postpone,0);
  const maxScore=tot*3;
  const scorePct=maxScore?Math.round(totalScore/maxScore*100):0;
  document.getElementById('sub').textContent=`${overall.length} task${overall.length!==1?'s':''} · ${new Date(d.generated_at).toLocaleTimeString()}`;
  Object.values(charts).forEach(c=>c.destroy());charts={};
  dash.innerHTML=`
    <div class="cards">
      <div class="card c-acc"><div class="clabel">Score Index</div><div class="cval">${scorePct}%</div><div class="csub">${totalScore}/${maxScore} pts</div></div>
      <div class="card c-done"><div class="clabel">Completed</div><div class="cval">${td}</div><div class="csub">${rate}% rate</div></div>
      <div class="card c-skip"><div class="clabel">Skipped</div><div class="cval">${ts}</div><div class="csub">${tot?Math.round(ts/tot*100):0}% of total</div></div>
      <div class="card c-post"><div class="clabel">Postponed</div><div class="cval">${tp}</div><div class="csub">${tot?Math.round(tp/tot*100):0}% of total</div></div>
    </div>
    <div class="stitle"><span></span> Score Over Time</div>
    <div class="chart-full">
      <div class="cbox-title">Done=3pts · Postponed=1pt · Skipped=0pts</div>
      <canvas id="score-chart"></canvas>
      <div class="score-legend">${overall.map((o,i)=>`<span><div class="dot" style="background:${lc(i)}"></div>${o.task}</span>`).join('')}</div>
    </div>
    <div class="stitle"><span></span> 30-Day Activity</div>
    <div class="crow">
      <div class="cbox"><div class="cbox-title">Daily breakdown</div><canvas id="bar-chart"></canvas></div>
      <div class="cbox"><div class="cbox-title">Distribution</div><canvas id="donut-chart"></canvas></div>
    </div>
    <div class="stitle"><span></span> Task Breakdown</div>
    <table><thead><tr><th>Task</th><th>Done</th><th>Skipped</th><th>Postponed</th><th>Score</th><th>Completion</th></tr></thead>
    <tbody>${overall.map(o=>{
      const t=o.done+o.skip+o.postpone;const pct=t?Math.round(o.done/t*100):0;
      const sc=o.done*3+o.postpone;const maxSc=t*3;const spct=maxSc?Math.round(sc/maxSc*100):0;
      const col=pct>=70?'var(--done)':pct>=40?'var(--skip)':'var(--post)';
      return `<tr><td><div class="tname">${o.task}</div></td><td><span class="pill pd">✓ ${o.done}</span></td><td><span class="pill ps">⏭ ${o.skip}</span></td><td><span class="pill pp">⏰ ${o.postpone}</span></td><td><span class="rate" style="color:var(--acc)">${spct}%</span><div class="csub">${sc}/${maxSc}pts</div></td><td><span class="rate" style="color:${col}">${pct}%</span><div class="bwrap"><div class="bfill" style="width:${pct}%"></div></div></td></tr>`;
    }).join('')}</tbody></table>`;
  const fk=Object.keys(daily)[0];
  if(fk){
    const labels=daily[fk].dates.map(d=>{const dt=new Date(d);return `${dt.getMonth()+1}/${dt.getDate()}`;});
    charts.score=new Chart(document.getElementById('score-chart').getContext('2d'),{type:'line',data:{labels,datasets:overall.map((o,i)=>{const td2=daily[o.task];if(!td2)return null;return{label:o.task,data:td2.dates.map((_,idx)=>td2.done[idx]*3+td2.postpone[idx]),borderColor:lc(i),backgroundColor:lc(i).replace('1)','0.08)'),borderWidth:2,pointRadius:3,tension:0.4,fill:false};}).filter(Boolean)},options:{responsive:true,maintainAspectRatio:true,interaction:{mode:'index',intersect:false},plugins:{legend:{display:false}},scales:{x:{ticks:{color:C.txt,font,maxTicksLimit:12},grid:{color:C.bd}},y:{ticks:{color:C.txt,font},grid:{color:C.bd},beginAtZero:true}}}});
    const dD=labels.map((_,i)=>Object.values(daily).reduce((s,t)=>s+(t.done[i]||0),0));
    const sD=labels.map((_,i)=>Object.values(daily).reduce((s,t)=>s+(t.skip[i]||0),0));
    const pD=labels.map((_,i)=>Object.values(daily).reduce((s,t)=>s+(t.postpone[i]||0),0));
    charts.bar=new Chart(document.getElementById('bar-chart').getContext('2d'),{type:'bar',data:{labels,datasets:[{label:'Done',data:dD,backgroundColor:C.done,borderRadius:2},{label:'Skip',data:sD,backgroundColor:C.skip,borderRadius:2},{label:'Postpone',data:pD,backgroundColor:C.post,borderRadius:2}]},options:{responsive:true,maintainAspectRatio:true,plugins:{legend:{labels:{color:C.txt,font,boxWidth:10}}},scales:{x:{stacked:true,ticks:{color:C.txt,font,maxTicksLimit:10},grid:{color:C.bd}},y:{stacked:true,ticks:{color:C.txt,font},grid:{color:C.bd}}}}});
    charts.donut=new Chart(document.getElementById('donut-chart').getContext('2d'),{type:'doughnut',data:{labels:['Done','Skipped','Postponed'],datasets:[{data:[td,ts,tp],backgroundColor:['rgba(74,222,128,.8)','rgba(250,204,21,.7)','rgba(249,115,22,.7)'],borderColor:'#07090e',borderWidth:3,hoverOffset:8}]},options:{responsive:true,maintainAspectRatio:true,cutout:'70%',plugins:{legend:{position:'bottom',labels:{color:C.txt,font,padding:14,boxWidth:10}}}}});
  }
}
function lc(i){return['rgba(129,140,248,1)','rgba(74,222,128,1)','rgba(249,115,22,1)','rgba(250,204,21,1)','rgba(236,72,153,1)','rgba(34,211,238,1)'][i%6];}
load();
</script>
</body>
</html>"""

# ─── AIOHTTP ROUTES ───────────────────────────────────────────────────────────
async def handle_webhook(request):
    app = request.app["tg_app"]
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return web.Response(text="ok")

async def handle_dashboard(request):
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")

async def handle_stats(request):
    data = get_stats_data()
    return web.json_response(data)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    init_db()

    # Build telegram app (no updater — webhook mode)
    tg_app = Application.builder().token(BOT_TOKEN).updater(None).build()
    tg_app.add_handler(CommandHandler("start",      start))
    tg_app.add_handler(CommandHandler("addtask",    add_task_cmd))
    tg_app.add_handler(CommandHandler("addweekly",  add_weekly_cmd))
    tg_app.add_handler(CommandHandler("listtasks",  list_tasks))
    tg_app.add_handler(CommandHandler("removetask", remove_task_cmd))
    tg_app.add_handler(CommandHandler("stats",      stats_cmd))
    tg_app.add_handler(CallbackQueryHandler(button_handler))
    tg_app.job_queue.run_repeating(reminder_job, interval=60, first=10)

    # Build aiohttp web app
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
