"""
Main scheduler — runs all 4 agents on their schedules and sends daily Telegram digests.

Usage:
    python orchestrator.py           # run continuously (cron mode)
    python orchestrator.py --run-now # run all agents once and exit
"""

import argparse
import json
import logging
import time
import schedule
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("factory.log")
    ]
)
log = logging.getLogger("orchestrator")


def run_discovery():
    log.info("=== Discovery Agent starting ===")
    try:
        from agents.discovery_agent import run
        result = run()
        log.info(f"Discovery complete: {result}")
    except Exception as e:
        log.error(f"Discovery agent failed: {e}", exc_info=True)


def run_priority():
    log.info("=== Priority Agent starting ===")
    try:
        from agents.priority_agent import run
        result = run()
        log.info(f"Priority complete: {result}")
    except Exception as e:
        log.error(f"Priority agent failed: {e}", exc_info=True)


def run_builder(no_approval_wait: bool = False):
    log.info("=== Builder Agent starting ===")
    try:
        from agents.builder_agent import run
        result = run(no_approval_wait=no_approval_wait)
        log.info(f"Builder complete: {result}")
    except Exception as e:
        log.error(f"Builder agent failed: {e}", exc_info=True)


def run_refiner():
    log.info("=== Refiner Agent starting ===")
    try:
        from agents.refiner_agent import run
        result = run()
        log.info(f"Refiner complete: {result}")
    except Exception as e:
        log.error(f"Refiner agent failed: {e}", exc_info=True)


def send_daily_digest():
    log.info("=== Sending daily digest ===")
    try:
        from agents.base import send_telegram
        from storage.backlog import get_todays_discoveries, get_todays_themes, get_pending_backlog, log_digest

        discoveries = get_todays_discoveries()
        themes = get_todays_themes()
        top_items = get_pending_backlog(min_score=5.0)[:5]

        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        msg = f"📊 *Daily Agent Factory Digest — {date_str}*\n\n"

        msg += f"*Today's Discovery Run*\n"
        msg += f"  • {len(discoveries)} new posts scraped\n"
        msg += f"  • {len(themes)} themes updated/added\n\n"

        if themes:
            msg += "*New/Updated Themes Today*\n"
            for t in themes[:5]:
                msg += f"  • {t['name']} ({t['frequency']} mentions)\n"
            msg += "\n"

        if top_items:
            msg += "*Top Backlog Items (to build)*\n"
            for it in top_items:
                msg += f"  {it['priority_score']:.1f}  {it['title']}\n"
            msg += "\n"

        msg += "_Next build check: tomorrow 9am_"

        send_telegram(msg)
        log_digest(msg)
        log.info("Daily digest sent")

    except Exception as e:
        log.error(f"Daily digest failed: {e}", exc_info=True)


def run_discovery_then_priority():
    """Run discovery then immediately re-score."""
    run_discovery()
    run_priority()


def setup_schedules():
    # Discovery + priority: 4 times a day
    schedule.every(6).hours.do(run_discovery_then_priority)

    # Builder: once a day at 9am
    schedule.every().day.at("09:00").do(run_builder)

    # Refiner: every Monday
    schedule.every().monday.at("10:00").do(run_refiner)

    # Daily digest: 8am every day
    schedule.every().day.at("08:00").do(send_daily_digest)

    log.info("Schedules set:")
    log.info("  Discovery+Priority: every 6h")
    log.info("  Builder: daily 09:00")
    log.info("  Refiner: every Monday 10:00")
    log.info("  Digest: daily 08:00")


def run_all_now():
    log.info("Running all agents immediately...")
    from storage.backlog import init_db
    init_db()
    run_discovery_then_priority()
    run_builder()
    send_daily_digest()
    log.info("Full run complete")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-now", action="store_true", help="Run all agents once and exit")
    parser.add_argument("--discovery", action="store_true", help="Run only discovery agent")
    parser.add_argument("--priority", action="store_true", help="Run only priority agent")
    parser.add_argument("--builder", action="store_true", help="Run only builder agent")
    parser.add_argument("--no-approval-wait", action="store_true", help="Skip Telegram approval gate (for CI/GitHub Actions)")
    parser.add_argument("--refiner", action="store_true", help="Run only refiner agent")
    parser.add_argument("--digest", action="store_true", help="Send daily digest now")
    args = parser.parse_args()

    from storage.backlog import init_db
    init_db()

    if args.run_now:
        run_all_now()
        return
    if args.discovery:
        run_discovery()
        return
    if args.priority:
        run_priority()
        return
    if args.builder:
        run_builder(no_approval_wait=args.no_approval_wait)
        return
    if args.refiner:
        run_refiner()
        return
    if args.digest:
        send_daily_digest()
        return

    # Continuous scheduler mode
    log.info("Starting AI Agent Product Factory scheduler")
    setup_schedules()

    # Run an initial discovery on startup
    log.info("Running initial discovery pass...")
    run_discovery_then_priority()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
