import os
import logging
import asyncio
from datetime import datetime, timedelta
import pytz
import httpx
from telegram import Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
LTA_API_KEY      = os.environ["LTA_API_KEY"]

BUS_STOP_CODE = "81189"
BUS_SERVICES  = {"10", "16", "16M"}

SGT = pytz.timezone("Asia/Singapore")

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── LTA DataMall — Bus ────────────────────────────────────────────────────────
LTA_BUS_URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"

async def fetch_bus_arrivals() -> list[dict]:
    headers = {"AccountKey": LTA_API_KEY.strip()}
    params  = {"BusStopCode": BUS_STOP_CODE}
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
        return "Arr" if mins <= 0 else f"{mins} min"
    except Exception:
        return "?"

def build_bus_section(services: list[dict]) -> str:
    lines = ["🚌 *Buses at Stop 81189*\n"]
    matched = {s["ServiceNo"].upper(): s for s in services
               if s["ServiceNo"].upper() in BUS_SERVICES}
    if not matched:
        lines.append("_No data for buses 10, 16, 16M._")
    else:
        for bus in sorted(matched.keys()):
            svc = matched[bus]
            nb1 = svc.get("NextBus",  {})
            nb2 = svc.get("NextBus2", {})
            nb3 = svc.get("NextBus3", {})
            lines.append(
                f"*Bus {bus}:* {format_eta(nb1)}  ·  {format_eta(nb2)}  ·  {format_eta(nb3)}"
            )
    return "\n".join(lines)

# ── Circle Line timetable — Dakota towards Dhoby Ghaut ───────────────────────
# Departure times from Dakota (CC8) towards Dhoby Ghaut (counter-clockwise)
# Source: SMRT official timetable, weekday morning window
# Format: (hour, minute)
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

def next_trains_from_timetable(n: int = 3) -> list[str]:
    """Return next n train arrival times as 'X min' strings."""
    now = datetime.now(SGT)
    upcoming = []
    for (h, m) in CCL_DHOBY_WEEKDAY:
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        diff_mins = int((t - now).total_seconds() / 60)
        if diff_mins >= -1:  # include trains arriving now
            upcoming.append(diff_mins)
        if len(upcoming) >= n:
            break
    results = []
    for mins in upcoming[:n]:
        if mins <= 0:
            results.append("Arr")
        else:
            results.append(f"{mins} min")
    return results if results else ["–"]

def build_train_section() -> str:
    now = datetime.now(SGT)
    lines = ["\n🚇 *Circle Line at Dakota (→ Dhoby Ghaut)*\n"]

    # Only show on weekdays
    if now.weekday() >= 5:
        lines.append("_No service — weekend schedule not loaded._")
        return "\n".join(lines)

    trains = next_trains_from_timetable(3)
    if not trains or trains == ["–"]:
        lines.append("_No more trains in timetable window._")
    else:
        first = f"*{trains[0]}*"
        rest  = "  ·  ".join(trains[1:])
        lines.append(f"Next trains: {first}" + (f"  ·  {rest}" if rest else ""))

    return "\n".join(lines)

# ── Combined message ───────────────────────────────────────────────────────────
async def send_update(bot: Bot) -> None:
    now_str = datetime.now(SGT).strftime("%I:%M %p")
    header  = f"🕐 *Morning Commute Update* — {now_str}\n{'─' * 30}"

    try:
        bus_services = await fetch_bus_arrivals()
        bus_section  = build_bus_section(bus_services)
        log.info("Bus data OK")
    except Exception as e:
        log.error("Bus fetch error: %s", e)
        bus_section = "🚌 _Bus data unavailable._"

    train_section = build_train_section()

    full_msg = f"{header}\n\n{bus_section}{train_section}"
    await bot.send_message(
        chat_id    = TELEGRAM_CHAT_ID,
        text       = full_msg,
        parse_mode = "Markdown",
    )
    log.info("Message sent.")

# ── Commands ───────────────────────────────────────────────────────────────────
async def cmd_now(update, context: ContextTypes.DEFAULT_TYPE):
    await send_update(context.bot)

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Morning Commute Bot*\n\n"
        "Automatic updates Mon–Fri, 8:15–9:00 am:\n\n"
        "🚌 Buses *10, 16 & 16M* at stop 81189\n"
        "🚇 Circle Line at *Dakota* → Dhoby Ghaut\n\n"
        "Use /now for an instant update anytime.",
        parse_mode="Markdown"
    )

# ── Scheduler ─────────────────────────────────────────────────────────────────
def setup_scheduler(app: Application) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=SGT)
    scheduler.add_job(
        lambda: asyncio.ensure_future(send_update(app.bot)),
        trigger="cron", day_of_week="mon-fri",
        hour="8", minute="15,20,25,30,35,40,45,50,55",
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(send_update(app.bot)),
        trigger="cron", day_of_week="mon-fri",
        hour="9", minute="0",
    )
    return scheduler

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("now",   cmd_now))
    scheduler = setup_scheduler(app)
    scheduler.start()
    log.info("Bot started. Scheduled Mon–Fri 08:15–09:00 SGT.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
