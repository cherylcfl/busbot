import os
import logging
import asyncio
import re
from datetime import datetime
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

# Dakota CC8 — towards Dhoby Ghaut (counter-clockwise)
# MyTransport.sg uses numeric station ID: Dakota = 10008
MRT_STATION_ID   = "10008"
MRT_STATION_NAME = "Dakota"
MRT_DIRECTION    = "towards Dhoby Ghaut"

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

def load_icon(load: str) -> str:
    return {"SEA": "🟢", "SDA": "🟡", "LSD": "🔴"}.get(load, "⬜")

def build_bus_section(services: list[dict]) -> str:
    lines = ["🚌 *Buses at Stop 81189*\n"]
    matched = {s["ServiceNo"].upper(): s for s in services
               if s["ServiceNo"].upper() in BUS_SERVICES}
    if not matched:
        lines.append("_No data for buses 10, 16, 16M._")
    else:
        for bus in sorted(matched.keys()):
            svc  = matched[bus]
            nb1  = svc.get("NextBus",  {})
            nb2  = svc.get("NextBus2", {})
            nb3  = svc.get("NextBus3", {})
            lines.append(
                f"*Bus {bus}*\n"
                f"  {load_icon(nb1.get('Load',''))} {format_eta(nb1)}"
                f"   {load_icon(nb2.get('Load',''))} {format_eta(nb2)}"
                f"   {load_icon(nb3.get('Load',''))} {format_eta(nb3)}"
            )
        lines.append("\n_🟢 Seats  🟡 Standing  🔴 Limited_")
    return "\n".join(lines)

# ── MyTransport.sg — MRT ──────────────────────────────────────────────────────
# Public endpoint used by mytransport.sg journey planner
MYTRANSPORT_URL = (
    "https://www.mytransport.sg/content/mytransport/home/commuting/"
    "train-time-table.html"
)
TRAIN_API_URL = "https://www.mytransport.sg/api/TrainArrival/GetTrainArrival"

async def fetch_train_arrivals() -> list[dict]:
    """
    Calls the MyTransport.sg train arrival API.
    Returns list of arrival dicts with keys: Line, Direction, Timing
    """
    params = {"stationCode": f"CC{MRT_STATION_ID}"}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.mytransport.sg/",
    }
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        r = await client.get(TRAIN_API_URL, params=params, headers=headers)
        r.raise_for_status()
        data = r.json()
        return data.get("TrainServiceInformations", [])

def build_train_section(arrivals: list[dict]) -> str:
    lines = ["\n🚇 *Circle Line at Dakota (→ Dhoby Ghaut)*\n"]

    # Filter for CCL towards Dhoby Ghaut — direction typically "1" or contains "Dhoby"
    dhoby = [a for a in arrivals
             if "dhoby" in str(a.get("Destination", "")).lower()
             or str(a.get("Direction", "")) == "1"]

    if not dhoby:
        # Show all CCL arrivals if direction filter finds nothing
        dhoby = arrivals

    if not dhoby:
        lines.append("_No train data available._")
        return "\n".join(lines)

    times = []
    for a in dhoby[:3]:
        t = a.get("Timing") or a.get("ArrivalTime") or a.get("EstimatedArrival", "")
        if not t:
            continue
        try:
            # Try parsing as ISO datetime
            dt   = datetime.fromisoformat(t).astimezone(SGT)
            mins = int((dt - datetime.now(SGT)).total_seconds() / 60)
            times.append("Arr" if mins <= 0 else f"{mins} min")
        except Exception:
            # If it's already a "X min" string, use as-is
            times.append(str(t))

    if times:
        first = f"*{times[0]}*"
        rest  = "  ·  ".join(times[1:])
        lines.append(f"Next trains: {first}" + (f"  ·  {rest}" if rest else ""))
    else:
        lines.append("_No arrival times available._")

    return "\n".join(lines)

# ── Combined message ───────────────────────────────────────────────────────────
async def send_update(bot: Bot) -> None:
    now_str = datetime.now(SGT).strftime("%I:%M %p")
    header  = f"🕐 *Morning Commute Update* — {now_str}\n{'─' * 30}"

    try:
        bus_services = await fetch_bus_arrivals()
        bus_section  = build_bus_section(bus_services)
        log.info("Bus data fetched OK")
    except Exception as e:
        log.error("Bus fetch error: %s", e)
        bus_section = "🚌 _Bus data unavailable._"

    try:
        train_arrivals = await fetch_train_arrivals()
        train_section  = build_train_section(train_arrivals)
        log.info("Train data fetched OK")
    except Exception as e:
        log.error("Train fetch error: %s", e)
        train_section = "\n🚇 _Train data unavailable._"

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
