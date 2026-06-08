import os
import logging
import asyncio
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

# Dakota CC8 — platform A = towards Dhoby Ghaut (counter-clockwise)
MRT_STATION_NAME = "Dakota"
MRT_PLATFORM_ID  = "CDKT_A"   # A = towards Dhoby Ghaut

SGT = pytz.timezone("Asia/Singapore")

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── LTA DataMall — Bus ────────────────────────────────────────────────────────
LTA_BUS_URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"

async def fetch_bus_arrivals(bus_stop: str) -> list[dict]:
    headers = {"AccountKey": LTA_API_KEY}
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
            nb1, nb2, nb3 = svc.get("NextBus",{}), svc.get("NextBus2",{}), svc.get("NextBus3",{})
            lines.append(
                f"*Bus {bus}*\n"
                f"  {load_icon(nb1.get('Load',''))} {format_eta(nb1)}"
                f"   {load_icon(nb2.get('Load',''))} {format_eta(nb2)}"
                f"   {load_icon(nb3.get('Load',''))} {format_eta(nb3)}"
            )
        lines.append("\n_🟢 Seats  🟡 Standing  🔴 Limited_")
    return "\n".join(lines)

# ── SMRT Train Arrival API ─────────────────────────────────────────────────────
# Unofficial but widely used; returns next_train_arr in minutes for each platform
SMRT_TRAIN_URL = "https://trainarrivalweb.smrt.com.sg/webapi/rv1/TrainArrival/{station}"

async def fetch_train_arrivals(station_name: str) -> list[dict]:
    """
    Returns list of platform dicts like:
    {"platform_ID": "CDKT_A", "next_train_arr": "3", "subsequent_train_arr": "8", ...}
    """
    url = SMRT_TRAIN_URL.format(station=station_name)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        return data.get("results", [])

def build_train_section(platforms: list[dict]) -> str:
    lines = ["\n🚇 *Circle Line at Dakota (→ Dhoby Ghaut)*\n"]
    target = [p for p in platforms if p.get("platform_ID") == MRT_PLATFORM_ID]
    if not target:
        lines.append("_No train data available._")
        return "\n".join(lines)

    p = target[0]
    status = p.get("status", 1)  # 0 = disruption

    if status == 0:
        lines.append("⚠️ _Service disruption on this platform._")
        return "\n".join(lines)

    t1 = p.get("next_train_arr", "")
    t2 = p.get("subsequent_train_arr", "")
    t3 = p.get("third_train_arr", "")

    def fmt(t):
        if not t or t in ("", "-"):
            return "–"
        try:
            mins = int(t)
            return "Arr" if mins <= 0 else f"{mins} min"
        except:
            return str(t)

    lines.append(f"Next trains: *{fmt(t1)}*  ·  {fmt(t2)}  ·  {fmt(t3)}")
    return "\n".join(lines)

# ── Combined message ───────────────────────────────────────────────────────────
async def send_update(bot: Bot) -> None:
    now_str = datetime.now(SGT).strftime("%I:%M %p")
    header  = f"🕐 *Morning Commute Update* — {now_str}\n{'─'*30}"
    try:
        bus_services = await fetch_bus_arrivals(BUS_STOP_CODE)
        bus_section  = build_bus_section(bus_services)
    except Exception as e:
        log.error("Bus fetch error: %s", e)
        bus_section = "🚌 _Bus data unavailable._"

    try:
        train_platforms = await fetch_train_arrivals(MRT_STATION_NAME)
        train_section   = build_train_section(train_platforms)
    except Exception as e:
        log.error("Train fetch error: %s", e)
        train_section = "\n🚇 _Train data unavailable._"

    full_msg = f"{header}\n\n{bus_section}{train_section}"
    try:
        await bot.send_message(
            chat_id    = TELEGRAM_CHAT_ID,
            text       = full_msg,
            parse_mode = "Markdown",
        )
        log.info("Sent combined update.")
    except Exception as e:
        log.error("Telegram send error: %s", e)

# ── Command handlers ───────────────────────────────────────────────────────────
async def cmd_now(update, context: ContextTypes.DEFAULT_TYPE):
    await send_update(context.bot)

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Morning Commute Bot*\n\n"
        "I send combined updates every 5 min, Mon–Fri 8:15–9:00 am:\n\n"
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
        id="commute_morning",
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(send_update(app.bot)),
        trigger="cron", day_of_week="mon-fri",
        hour="9", minute="0",
        id="commute_nine",
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
