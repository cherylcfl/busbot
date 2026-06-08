import os
import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path
import pytz
import httpx
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
LTA_API_KEY      = os.environ["LTA_API_KEY"]

SGT = pytz.timezone("Asia/Singapore")

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── Profiles ──────────────────────────────────────────────────────────────────
PROFILES = {
    "home": {
        "label":    "🏠 Home → Office",
        "bus_stop": "81189",
        "buses":    {"10", "16", "16M"},
        "mrt":      True,
    },
    "office": {
        "label":    "🏢 Office → Home",
        "bus_stop": "80151",
        "buses":    {"10", "16", "16M"},
        "mrt":      False,
    },
}

# ── Persistent settings ────────────────────────────────────────────────────────
SETTINGS_FILE = Path("/tmp/settings.json")

DEFAULT_SETTINGS = {
    "profile":   "home",
    "start_h":   8,
    "start_m":   15,
    "end_h":     9,
    "end_m":     0,
    "days":      ["mon", "tue", "wed", "thu", "fri"],
}

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(s: dict):
    SETTINGS_FILE.write_text(json.dumps(s))

# ── LTA Bus ───────────────────────────────────────────────────────────────────
LTA_BUS_URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"

async def fetch_bus_arrivals(bus_stop: str) -> list[dict]:
    headers = {"AccountKey": LTA_API_KEY.strip()}
    params  = {"BusStopCode": bus_stop}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(LTA_BUS_URL, headers=headers, params=params)
        r.raise_for_status()
        return r.json().get("Services", [])

def format_eta(next_bus: dict) -> str:
    eta_str = next_bus.get("EstimatedArrival", "")
    if not eta_str:
        return "–"
    try:
        eta_dt  = datetime.fromisoformat(eta_str).astimezone(SGT)
        now_sgt = datetime.now(SGT)
        mins    = int((eta_dt - now_sgt).total_seconds() / 60)
        time_str = eta_dt.strftime("%I:%M%p").lstrip("0").lower()
        if mins <= 0:
            return f"Arr ({time_str})"
        return f"{mins} min ({time_str})"
    except Exception:
        return "?"

def build_bus_section(services: list[dict], buses: set) -> str:
    lines = ["🚌 *Buses*"]
    matched = {s["ServiceNo"].upper(): s for s in services
               if s["ServiceNo"].upper() in buses}
    if not matched:
        lines.append("_No data._")
    else:
        for bus in sorted(matched.keys()):
            svc = matched[bus]
            nb1 = svc.get("NextBus",  {})
            nb2 = svc.get("NextBus2", {})
            nb3 = svc.get("NextBus3", {})
            lines.append(
                f"*Bus {bus}:* {format_eta(nb1)}  ·  {format_eta(nb2)}  ·  {format_eta(nb3)}"
            )
    return "\n".join(lines) + "\n"

# ── CCL Timetable ─────────────────────────────────────────────────────────────
CCL_DHOBY_WEEKDAY = [
    (5,16),(5,22),(5,28),(5,34),(5,40),(5,46),(5,52),(5,58),
    (6, 4),(6,10),(6,16),(6,21),(6,26),(6,31),(6,36),(6,40),
    (6,44),(6,48),(6,52),(6,56),(7, 0),(7, 3),(7, 6),(7, 9),
    (7,12),(7,15),(7,18),(7,21),(7,24),(7,27),(7,30),(7,33),
    (7,36),(7,39),(7,42),(7,45),(7,48),(7,51),(7,54),(7,57),
    (8, 0),(8, 3),(8, 6),(8, 9),(8,12),(8,15),(8,18),(8,21),
    (8,24),(8,27),(8,30),(8,33),(8,36),(8,39),(8,42),(8,45),
    (8,48),(8,51),(8,54),(8,57),(9, 0),(9, 3),(9, 6),(9, 9),
    (9,12),(9,15),(9,18),(9,21),(9,24),(9,27),(9,30),
]

def next_trains(n: int = 3) -> list[str]:
    now = datetime.now(SGT)
    upcoming = []
    for (h, m) in CCL_DHOBY_WEEKDAY:
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        mins = int((t - now).total_seconds() / 60)
        if mins >= -1:
            time_str = t.strftime("%I:%M%p").lstrip("0").lower()
            if mins <= 0:
                upcoming.append(f"Arr ({time_str})")
            else:
                upcoming.append(f"{mins} min ({time_str})")
        if len(upcoming) >= n:
            break
    return upcoming or ["–"]

def build_train_section() -> str:
    lines = ["🚇 *Circle Line (→ Dhoby Ghaut)*"]
    if datetime.now(SGT).weekday() >= 5:
        lines.append("_Weekend schedule not loaded._")
        return "\n".join(lines)
    trains = next_trains(3)
    first = f"*{trains[0]}*"
    rest  = "  ·  ".join(trains[1:])
    lines.append(f"Next trains: {first}" + (f"  ·  {rest}" if rest else ""))
    return "\n".join(lines)

# ── Combined update ────────────────────────────────────────────────────────────
async def send_update(bot: Bot, profile_key: str = None):
    s = load_settings()
    key     = profile_key or s["profile"]
    profile = PROFILES[key]

    now_str = datetime.now(SGT).strftime("%I:%M %p")
    header  = f"🕐 *{profile['label']}* — {now_str}\n{'─' * 15}"

    try:
        services    = await fetch_bus_arrivals(profile["bus_stop"])
        bus_section = build_bus_section(services, profile["buses"])
        log.info("Bus data OK")
    except Exception as e:
        log.error("Bus error: %s", e)
        bus_section = "🚌 _Bus data unavailable._"

    sections = [header, "", bus_section]

    if profile.get("mrt"):
        sections.append(build_train_section())

    await bot.send_message(
        chat_id    = TELEGRAM_CHAT_ID,
        text       = "\n".join(sections),
        parse_mode = "Markdown",
    )
    log.info("Sent update for profile: %s", key)

# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone=SGT)

def rebuild_schedule(app: Application, loop: asyncio.AbstractEventLoop):
    """Remove and recreate scheduled jobs from current settings."""
    for job in scheduler.get_jobs():
        job.remove()

    s    = load_settings()
    days = ",".join(s["days"]) if s["days"] else "mon"

    # Generate minute list from start to end every 5 mins
    minutes = []
    h, m = s["start_h"], s["start_m"]
    while (h, m) < (s["end_h"], s["end_m"]):
        minutes.append((h, m))
        m += 5
        if m >= 60:
            m -= 60
            h += 1
    # Add end time
    minutes.append((s["end_h"], s["end_m"]))

    # Group by hour
    from collections import defaultdict
    by_hour = defaultdict(list)
    for (hh, mm) in minutes:
        by_hour[hh].append(mm)

    def make_fire():
        def fire():
            asyncio.run_coroutine_threadsafe(send_update(app.bot), loop)
        return fire

    for hh, mins in by_hour.items():
        minute_str = ",".join(str(m) for m in mins)
        scheduler.add_job(
            make_fire(),
            trigger="cron",
            day_of_week=days,
            hour=str(hh),
            minute=minute_str,
        )
    log.info("Schedule rebuilt: %s-%s:%s, days=%s",
             s["start_h"], s["end_h"], s["end_m"], days)

# ── Commands ───────────────────────────────────────────────────────────────────
DAYS_MAP = {
    "mon": "mon", "tue": "tue", "wed": "wed",
    "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun",
}

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_settings()
    await update.message.reply_text(
        "👋 *Morning Commute Bot*\n\n"
        "Commands:\n"
        "/now — instant update (current profile)\n"
        "/home — switch to home→office profile\n"
        "/office — switch to office→home profile\n"
        "/settime 08:15 09:00 — change alert window\n"
        "/setdays mon tue wed thu fri — set active days\n"
        "/settings — show current settings",
        parse_mode="Markdown"
    )

async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_update(context.bot)

async def cmd_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_settings()
    s["profile"] = "home"
    save_settings(s)
    await send_update(context.bot, "home")

async def cmd_office(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_settings()
    s["profile"] = "office"
    save_settings(s)
    await send_update(context.bot, "office")

async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /settime 08:15 09:00"""
    try:
        start_str, end_str = context.args[0], context.args[1]
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        s = load_settings()
        s["start_h"], s["start_m"] = sh, sm
        s["end_h"],   s["end_m"]   = eh, em
        save_settings(s)
        rebuild_schedule(context.application, asyncio.get_event_loop())
        await update.message.reply_text(
            f"✅ Alert window updated: {start_str} – {end_str}"
        )
    except Exception:
        await update.message.reply_text(
            "Usage: /settime 08:15 09:00"
        )

async def cmd_setdays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /setdays mon tue wed thu fri"""
    try:
        days = [d.lower() for d in context.args if d.lower() in DAYS_MAP]
        if not days:
            raise ValueError
        s = load_settings()
        s["days"] = days
        save_settings(s)
        rebuild_schedule(context.application, asyncio.get_event_loop())
        await update.message.reply_text(
            f"✅ Active days updated: {', '.join(days)}"
        )
    except Exception:
        await update.message.reply_text(
            "Usage: /setdays mon tue wed thu fri\n"
            "Available: mon tue wed thu fri sat sun"
        )

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s    = load_settings()
    p    = PROFILES[s["profile"]]
    days = ", ".join(s["days"])
    await update.message.reply_text(
        f"⚙️ *Current Settings*\n\n"
        f"Profile: {p['label']}\n"
        f"Window: {s['start_h']:02d}:{s['start_m']:02d} – {s['end_h']:02d}:{s['end_m']:02d}\n"
        f"Days: {days}",
        parse_mode="Markdown"
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("now",      cmd_now))
    app.add_handler(CommandHandler("home",     cmd_home))
    app.add_handler(CommandHandler("office",   cmd_office))
    app.add_handler(CommandHandler("settime",  cmd_settime))
    app.add_handler(CommandHandler("setdays",  cmd_setdays))
    app.add_handler(CommandHandler("settings", cmd_settings))

    loop = asyncio.get_event_loop()
    rebuild_schedule(app, loop)
    scheduler.start()

    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
