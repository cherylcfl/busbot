import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
import pytz
import httpx
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

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
    },
    "office": {
        "label":    "🏢 Office → Home",
        "bus_stop": "80151",
        "buses":    {"10", "16", "16M"},
    },
}

# ── Persistent state ───────────────────────────────────────────────────────────
STATE_FILE = Path("/tmp/state.json")

DEFAULT_STATE = {
    "profile":       "home",
    "start_h":       8,
    "start_m":       15,
    "end_h":         9,
    "end_m":         0,
    "skip_tomorrow": False,
}

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return {**DEFAULT_STATE, **json.loads(STATE_FILE.read_text())}
        except Exception:
            pass
    return DEFAULT_STATE.copy()

def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s))

# ── LTA Bus ───────────────────────────────────────────────────────────────────
LTA_BUS_URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"

async def fetch_bus_arrivals(bus_stop: str) -> list[dict]:
    headers = {"AccountKey": LTA_API_KEY.strip()}
    params  = {"BusStopCode": bus_stop}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(LTA_BUS_URL, headers=headers, params=params)
        r.raise_for_status()
        return r.json().get("Services", [])

def parse_eta_dt(next_bus: dict):
    eta_str = next_bus.get("EstimatedArrival", "")
    if not eta_str:
        return None
    try:
        return datetime.fromisoformat(eta_str).astimezone(SGT)
    except Exception:
        return None

def fmt_arrival(eta_dt, now) -> tuple:
    mins     = int((eta_dt - now).total_seconds() / 60)
    time_str = eta_dt.strftime("%I:%M%p").lstrip("0").lower()
    return time_str, mins

def build_bus_section(services: list[dict], buses: set = None, limit: int = 5) -> str:
    """Chronological list format for default /now view."""
    now    = datetime.now(SGT)
    events = []

    for svc in services:
        bus_no = svc["ServiceNo"].upper()
        if buses and bus_no not in buses:
            continue
        for key in ("NextBus", "NextBus2", "NextBus3"):
            nb = svc.get(key, {})
            eta_dt = parse_eta_dt(nb)
            if eta_dt:
                events.append((eta_dt, bus_no))

    events.sort(key=lambda x: x[0])
    seen = 0
    for eta_dt, bus_no in events:
        if seen >= limit:
            break
        time_str, mins = fmt_arrival(eta_dt, now)
        if mins < -1:
            continue
        lines.append(f"{time_str} — Bus {bus_no}")
        seen += 1

    if seen == 0:
        lines.append("_No data._")

    return "\n".join(lines)

def build_bus_section_expand(services: list[dict], label: str) -> str:
    """Per-bus row with 3 timings for /expand view."""
    now   = datetime.now(SGT)
    lines = [f"*{label}*"]

    bus_times = {}
    for svc in services:
        bus_no = svc["ServiceNo"].upper()
        times  = []
        for key in ("NextBus", "NextBus2", "NextBus3"):
            eta_dt = parse_eta_dt(svc.get(key, {}))
            if eta_dt:
                time_str, mins = fmt_arrival(eta_dt, now)
                if mins >= -1:
                    times.append(time_str)
        if times:
            bus_times[bus_no] = times

    if not bus_times:
        lines.append("_No data._")
    else:
        for bus_no in sorted(bus_times.keys()):
            times_str = "  ·  ".join(bus_times[bus_no])
            lines.append(f"Bus {bus_no}: {times_str}")

    return "\n".join(lines)
# ── Combined messages ──────────────────────────────────────────────────────────
async def build_message(profile_key: str, expand: bool = False) -> str:
    profile = PROFILES[profile_key]
    now_str = datetime.now(SGT).strftime("%I:%M %p")
    header  = f"🕐 *{profile['label']}* — {now_str}\n{'─' * 15}"

    sections = [header, ""]

    if not expand:
        # Default: filtered buses at main stop
        try:
            services    = await fetch_bus_arrivals(profile["bus_stop"])
            bus_section = build_bus_section(services, buses=profile["buses"], limit=5)
            log.info("Bus data OK: %s", profile["bus_stop"])
        except Exception as e:
            log.error("Bus error: %s", e)
            bus_section = "🚌 _Bus data unavailable._"
        sections.append(bus_section)

    else:
        if profile_key == "home":
            # Expand: all buses at 81189 + all buses at 81181
            for stop, label in [("81189", "🚌 All Buses at Dakota Stn Exit B (81189)"),
                                 ("81181", "🚌 All Buses at Dakota Stn Exit A (81181)")]:
                try:
                    services    = await fetch_bus_arrivals(stop)
                    bus_section = build_bus_section_expand(services, label)
                    log.info("Bus data OK: %s", stop)
                except Exception as e:
                    log.error("Bus error %s: %s", stop, e)
                    bus_section = f"🚌 _Bus data unavailable for {stop}._"
                sections.append(bus_section)
                sections.append("")  # spacer between stops
        else:
            # Office expand: all buses at 80151
            try:
                services    = await fetch_bus_arrivals(profile["bus_stop"])
                bus_section = build_bus_section_expand(services, "🚌 All Buses at Stop 80151")
                log.info("Bus data OK: %s", profile["bus_stop"])
            except Exception as e:
                log.error("Bus error: %s", e)
                bus_section = "🚌 _Bus data unavailable._"
            sections.append(bus_section)

    return "\n".join(sections)

async def send_update(bot: Bot, profile_key: str = None, expand: bool = False):
    s   = load_state()
    key = profile_key or s["profile"]
    msg = await build_message(key, expand=expand)
    await bot.send_message(
        chat_id    = TELEGRAM_CHAT_ID,
        text       = msg,
        parse_mode = "Markdown",
    )
    log.info("Sent update: profile=%s expand=%s", key, expand)

# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone=SGT)

def rebuild_schedule(app, loop):
    for job in scheduler.get_jobs():
        if job.id.startswith("commute_"):
            job.remove()

    s    = load_state()
    mins = []
    h, m = s["start_h"], s["start_m"]
    while (h, m) <= (s["end_h"], s["end_m"]):
        mins.append((h, m))
        m += 5
        if m >= 60:
            m -= 60
            h += 1

    by_hour = defaultdict(list)
    for (hh, mm) in mins:
        by_hour[hh].append(mm)

    def make_fire():
        def fire():
            st = load_state()
            if st.get("skip_tomorrow"):
                log.info("Skipping update (skip_tomorrow=True)")
                return
            asyncio.run_coroutine_threadsafe(send_update(app.bot), loop)
        return fire

    for hh, mm_list in by_hour.items():
        scheduler.add_job(
            make_fire(),
            trigger    = "cron",
            day_of_week= "mon-fri",
            hour       = str(hh),
            minute     = ",".join(str(m) for m in mm_list),
            id         = f"commute_{hh}",
            replace_existing=True,
        )
    log.info("Schedule rebuilt: %02d:%02d–%02d:%02d",
             s["start_h"], s["start_m"], s["end_h"], s["end_m"])

def setup_fixed_jobs(app, loop):
    def evening_checkin():
        asyncio.run_coroutine_threadsafe(_evening_checkin(app.bot), loop)

    def reset_skip():
        s = load_state()
        s["skip_tomorrow"] = False
        save_state(s)
        log.info("Reset skip_tomorrow to False")

    scheduler.add_job(
        evening_checkin,
        trigger    = "cron",
        day_of_week= "sun,mon,tue,wed,thu",
        hour       = "22",
        minute     = "0",
        id         = "evening_checkin",
        replace_existing=True,
    )
    scheduler.add_job(
        reset_skip,
        trigger  = "cron",
        hour     = "0",
        minute   = "1",
        id       = "reset_skip",
        replace_existing=True,
    )

async def _evening_checkin(bot: Bot):
    tomorrow = (datetime.now(SGT) + timedelta(days=1)).strftime("%A")
    await bot.send_message(
        chat_id    = TELEGRAM_CHAT_ID,
        text       = (
            f"🌙 Good evening! Do you want commute updates tomorrow ({tomorrow})?\n\n"
            f"Reply /no to skip, /yes to confirm, or just ignore this and "
            f"I'll send updates as usual."
        ),
    )

# ── Commands ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Morning Commute Bot*\n\n"
        "I send scheduled bus updates Mon–Fri and check in every evening to confirm.\n\n"
        "*Profiles*\n"
        "/home — Home → Office (stop 81189, buses 10/16/16M)\n"
        "/office — Office → Home (stop 80151, buses 10/16/16M)\n\n"
        "*On-demand*\n"
        "/now — instant update (current profile)\n"
        "/expand — all buses at both stops near home (81189 + Dakota Stn Exit A 81181)\n\n"
        "*Schedule*\n"
        "/settime 08:15 09:00 — change alert window\n"
        "/settings — show current settings\n\n"
        "*Tomorrow*\n"
        "/yes — re-enable updates for tomorrow\n"
        "/no — skip updates tomorrow (e.g. WFH day)\n\n"
        "Every Sun–Thu at 10pm I'll ask if you want updates the next day. "
        "No reply = updates will send as usual.",
        parse_mode="Markdown"
    )

async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_update(context.bot)

async def cmd_expand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_state()
    await send_update(context.bot, profile_key=s["profile"], expand=True)

async def cmd_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_state()
    s["profile"] = "home"
    save_state(s)
    await send_update(context.bot, "home")

async def cmd_office(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_state()
    s["profile"] = "office"
    save_state(s)
    await send_update(context.bot, "office")

async def cmd_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_state()
    s["skip_tomorrow"] = False
    save_state(s)
    await update.message.reply_text("✅ Got it — updates are on for tomorrow!")

async def cmd_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_state()
    s["skip_tomorrow"] = True
    save_state(s)
    tomorrow = (datetime.now(SGT) + timedelta(days=1)).strftime("%A")
    await update.message.reply_text(f"👍 No updates tomorrow ({tomorrow}). Enjoy your day off!")

async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sh, sm = map(int, context.args[0].split(":"))
        eh, em = map(int, context.args[1].split(":"))
        s = load_state()
        s["start_h"], s["start_m"] = sh, sm
        s["end_h"],   s["end_m"]   = eh, em
        save_state(s)
        rebuild_schedule(context.application, asyncio.get_event_loop())
        await update.message.reply_text(
            f"✅ Alert window updated: {context.args[0]} – {context.args[1]}"
        )
    except Exception:
        await update.message.reply_text("Usage: /settime 08:15 09:00")

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s    = load_state()
    p    = PROFILES[s["profile"]]
    skip = "Yes — skipping tomorrow" if s.get("skip_tomorrow") else "No"
    await update.message.reply_text(
        f"⚙️ *Current Settings*\n\n"
        f"Profile: {p['label']}\n"
        f"Window: {s['start_h']:02d}:{s['start_m']:02d} – {s['end_h']:02d}:{s['end_m']:02d}\n"
        f"Skip tomorrow: {skip}",
        parse_mode="Markdown"
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("now",      cmd_now))
    app.add_handler(CommandHandler("expand",   cmd_expand))
    app.add_handler(CommandHandler("home",     cmd_home))
    app.add_handler(CommandHandler("office",   cmd_office))
    app.add_handler(CommandHandler("yes",      cmd_yes))
    app.add_handler(CommandHandler("no",       cmd_no))
    app.add_handler(CommandHandler("settime",  cmd_settime))
    app.add_handler(CommandHandler("settings", cmd_settings))

    loop = asyncio.get_event_loop()
    rebuild_schedule(app, loop)
    setup_fixed_jobs(app, loop)
    scheduler.start()

    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
