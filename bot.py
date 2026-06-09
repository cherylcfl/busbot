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
        "mrt":      "home",
    },
    "office": {
        "label":    "🏢 Office → Home",
        "bus_stop": "80151",
        "buses":    {"10", "16", "16M"},
        "mrt":      "office",
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

def fmt_arrival(eta_dt, now) -> str:
    mins     = int((eta_dt - now).total_seconds() / 60)
    time_str = eta_dt.strftime("%I:%M%p").lstrip("0").lower()
    return time_str, mins

def build_bus_section(services: list[dict], buses: set, limit: int = 5, all_buses: bool = False) -> str:
    now    = datetime.now(SGT)
    label  = "🚌 All Buses" if all_buses else "🚌 Buses"
    lines  = [f"{label}\n"]
    events = []

    filter_set = None if all_buses else buses

    for svc in services:
        bus_no = svc["ServiceNo"].upper()
        if filter_set and bus_no not in filter_set:
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

# ── CCL Timetables ─────────────────────────────────────────────────────────────
# Dakota (CC8) → Dhoby Ghaut (counter-clockwise / platform A)
DAKOTA_TO_DG = [
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

# Dakota (CC8) → Marina Bay (clockwise / platform B)
DAKOTA_TO_MB = [
    (5,20),(5,26),(5,32),(5,38),(5,44),(5,50),(5,56),
    (6, 2),(6, 8),(6,14),(6,20),(6,25),(6,30),(6,35),(6,40),
    (6,44),(6,48),(6,52),(6,56),(7, 0),(7, 4),(7, 7),(7,10),
    (7,13),(7,16),(7,19),(7,22),(7,25),(7,28),(7,31),(7,34),
    (7,37),(7,40),(7,43),(7,46),(7,49),(7,52),(7,55),(7,58),
    (8, 1),(8, 4),(8, 7),(8,10),(8,13),(8,16),(8,19),(8,22),
    (8,25),(8,28),(8,31),(8,34),(8,37),(8,40),(8,43),(8,46),
    (8,49),(8,52),(8,55),(8,58),(9, 1),(9, 4),(9, 7),(9,10),
    (9,13),(9,16),(9,19),(9,22),(9,25),(9,28),(9,31),
]

# Dakota (CC8) → HarbourFront (counter-clockwise continues / same platform A)
DAKOTA_TO_HF = [
    (5,18),(5,24),(5,30),(5,36),(5,42),(5,48),(5,54),
    (6, 0),(6, 6),(6,12),(6,18),(6,23),(6,28),(6,33),(6,38),
    (6,42),(6,46),(6,50),(6,54),(6,58),(7, 2),(7, 5),(7, 8),
    (7,11),(7,14),(7,17),(7,20),(7,23),(7,26),(7,29),(7,32),
    (7,35),(7,38),(7,41),(7,44),(7,47),(7,50),(7,53),(7,56),
    (7,59),(8, 2),(8, 5),(8, 8),(8,11),(8,14),(8,17),(8,20),
    (8,23),(8,26),(8,29),(8,32),(8,35),(8,38),(8,41),(8,44),
    (8,47),(8,50),(8,53),(8,56),(8,59),(9, 2),(9, 5),(9, 8),
]

# Esplanade (CC3) → HarbourFront (clockwise)
ESPLANADE_TO_HF = [
    (5,30),(5,36),(5,42),(5,48),(5,54),
    (6, 0),(6, 6),(6,12),(6,18),(6,24),(6,30),(6,35),(6,40),
    (6,45),(6,50),(6,55),(7, 0),(7, 4),(7, 8),(7,12),(7,16),
    (7,20),(7,24),(7,28),(7,32),(7,36),(7,40),(7,44),(7,48),
    (7,52),(7,56),(8, 0),(8, 4),(8, 8),(8,12),(8,16),(8,20),
    (8,24),(8,28),(8,32),(8,36),(8,40),(8,44),(8,48),(8,52),
    (8,56),(9, 0),(9, 4),(9, 8),(9,12),(9,16),(9,20),(9,24),
    (9,28),(9,32),(9,36),(9,40),(9,44),(9,48),(9,52),(9,56),
    (10, 0),(10, 4),(10, 8),(10,12),(10,16),(10,20),(10,24),
    (17, 0),(17, 4),(17, 8),(17,12),(17,16),(17,20),(17,24),
    (17,28),(17,32),(17,36),(17,40),(17,44),(17,48),(17,52),
    (17,56),(18, 0),(18, 4),(18, 8),(18,12),(18,16),(18,20),
    (18,24),(18,28),(18,32),(18,36),(18,40),(18,44),(18,48),
    (18,52),(18,56),(19, 0),(19, 4),(19, 8),(19,12),(19,16),
    (19,20),(19,24),(19,28),(19,32),(19,36),(19,40),(19,44),
]

def get_next_from_timetable(timetable: list, n: int) -> list[tuple]:
    """Returns list of (eta_dt, mins) for next n trains."""
    now     = datetime.now(SGT)
    results = []
    for (h, m) in timetable:
        t    = now.replace(hour=h, minute=m, second=0, microsecond=0)
        mins = int((t - now).total_seconds() / 60)
        if mins >= -1:
            results.append((t, mins))
        if len(results) >= n:
            break
    return results

def fmt_train_line(t: datetime, mins: int, label: str = None) -> str:
    time_str = t.strftime("%I:%M%p").lstrip("0").lower()
    mins_str = "Arr" if mins <= 0 else f"{mins} min"
    if label:
        return f"{time_str} — {label} ({mins_str})"
    return f"{time_str} — {mins_str}"

def build_home_mrt_default() -> str:
    """5 arrivals merging DG and MB directions chronologically."""
    lines  = ["🚇 Circle Line (to DG/MB)\n"]
    trains = []
    for t, mins in get_next_from_timetable(DAKOTA_TO_DG, 8):
        trains.append((t, mins, "to Dhoby Ghaut"))
    for t, mins in get_next_from_timetable(DAKOTA_TO_MB, 8):
        trains.append((t, mins, "to Marina Bay"))
    trains.sort(key=lambda x: x[0])
    for t, mins, label in trains[:5]:
        lines.append(fmt_train_line(t, mins, label))
    return "\n".join(lines)

def build_home_mrt_expand() -> str:
    """5 DG/MB merged + 3 HarbourFront."""
    section1 = build_home_mrt_default()
    lines2   = ["\n🚇 Circle Line (to Harbourfront)\n"]
    for t, mins in get_next_from_timetable(DAKOTA_TO_HF, 3):
        lines2.append(fmt_train_line(t, mins))
    return section1 + "\n" + "\n".join(lines2)

def build_office_mrt() -> str:
    """3 arrivals at Esplanade towards HarbourFront."""
    lines = ["🚇 Circle Line at Esplanade (to Harbourfront)\n"]
    for t, mins in get_next_from_timetable(ESPLANADE_TO_HF, 3):
        lines.append(fmt_train_line(t, mins))
    return "\n".join(lines)

# ── Combined messages ──────────────────────────────────────────────────────────
async def build_message(profile_key: str, expand: bool = False) -> str:
    s       = load_state()
    profile = PROFILES[profile_key]
    now_str = datetime.now(SGT).strftime("%I:%M %p")
    header  = f"🕐 *{profile['label']}* — {now_str}\n{'─' * 30}"

    try:
        services    = await fetch_bus_arrivals(profile["bus_stop"])
        if expand:
            bus_section = build_bus_section(services, profile["buses"], limit=10, all_buses=True)
        else:
            bus_section = build_bus_section(services, profile["buses"], limit=5)
        log.info("Bus data OK")
    except Exception as e:
        log.error("Bus error: %s", e)
        bus_section = "🚌 _Bus data unavailable._"

    if profile_key == "home":
        mrt_section = build_home_mrt_expand() if expand else build_home_mrt_default()
    else:
        mrt_section = build_office_mrt()

    return f"{header}\n\n{bus_section}\n\n{mrt_section}"

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
    log.info("Schedule rebuilt: %02d:%02d–%02d:%02d", s["start_h"], s["start_m"], s["end_h"], s["end_m"])

def setup_fixed_jobs(app, loop):
    """11pm check-in Sun–Thu, and midnight reset of skip_tomorrow."""

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
        day_of_week= "sun-thu",
        hour       = "23",
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
        parse_mode = "Markdown",
    )

# ── Commands ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Morning Commute Bot*\n\n"
        "I send scheduled commute updates Mon–Fri and check in every evening to confirm.\n\n"
        "*Profiles*\n"
        "/home — Home → Office (stop 81189, buses 10/16/16M + Dakota CCL)\n"
        "/office — Office → Home (stop 80151, buses 10/16/16M + Esplanade CCL)\n\n"
        "*On-demand*\n"
        "/now — instant update (current profile)\n"
        "/expand — full update: all buses + extra MRT direction (home profile only)\n\n"
        "*Schedule*\n"
        "/settime 08:15 09:00 — change alert window\n"
        "/settings — show current settings\n\n"
        "*Tomorrow*\n"
        "/yes — re-enable updates for tomorrow\n"
        "/no — skip updates tomorrow (e.g. WFH day)\n\n"
        "Every Sun–Thu at 11pm I'll ask if you want updates the next day. "
        "If you don't reply, I'll send them anyway.",
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
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("now",     cmd_now))
    app.add_handler(CommandHandler("expand",  cmd_expand))
    app.add_handler(CommandHandler("home",    cmd_home))
    app.add_handler(CommandHandler("office",  cmd_office))
    app.add_handler(CommandHandler("yes",     cmd_yes))
    app.add_handler(CommandHandler("no",      cmd_no))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CommandHandler("settings",cmd_settings))

    loop = asyncio.get_event_loop()
    rebuild_schedule(app, loop)
    setup_fixed_jobs(app, loop)
    scheduler.start()

    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
