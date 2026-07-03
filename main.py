import asyncio
import logging
import os
import sys
from dotenv import load_dotenv
 
load_dotenv()
 
# Force stdout/stderr to flush immediately — belt AND suspenders alongside PYTHONUNBUFFERED=1
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("main")
 
log.info("=== Starting bot process ===")
log.info(f"Python {sys.version}")
log.info(f"DATABASE_PATH = {os.getenv('DATABASE_PATH', 'giveaways.db (default)')}")
 
 
async def run_bot(label: str, module_name: str, token: str):
    """Import and run a single bot, logging any crash without killing the others."""
    log.info(f"[{label}] Importing module...")
    try:
        import importlib
        module = importlib.import_module(module_name)
        bot = module.bot
    except Exception as e:
        log.exception(f"[{label}] IMPORT FAILED: {e}")
        return
 
    log.info(f"[{label}] Module loaded. Connecting to Discord...")
    try:
        await bot.start(token)
    except Exception as e:
        log.exception(f"[{label}] BOT CRASHED: {e}")
 
 
async def main():
    tokens = {
        "Economy": os.getenv("TOKEN_ECONOMY"),
        "Drops":   os.getenv("TOKEN_DROPS"),
        "Games":   os.getenv("TOKEN_GAMES"),
        "Admin":   os.getenv("TOKEN_ADMIN"),
    }
 
    missing = [name for name, tok in tokens.items() if not tok]
    if missing:
        log.error(f"Missing token(s) in environment: {', '.join(missing)}")
        sys.exit(1)
 
    log.info("All tokens present. Launching 4 bots...")
 
    await asyncio.gather(
        run_bot("Economy", "bot_economy", tokens["Economy"]),
        run_bot("Drops",   "bot_drops",   tokens["Drops"]),
        run_bot("Games",   "bot_games",   tokens["Games"]),
        run_bot("Admin",   "bot_admin",   tokens["Admin"]),
    )
 
    log.warning("All bots have exited — process ending.")
 
 
if __name__ == "__main__":
    asyncio.run(main())
