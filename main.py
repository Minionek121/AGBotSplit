import asyncio
import os
from dotenv import load_dotenv
 
load_dotenv()
 
import bot_economy
import bot_drops
import bot_games
import bot_admin
 
 
async def main():
    tokens = {
        "Economy": os.getenv("TOKEN_ECONOMY"),
        "Drops":   os.getenv("TOKEN_DROPS"),
        "Games":   os.getenv("TOKEN_GAMES"),
        "Admin":   os.getenv("TOKEN_ADMIN"),
    }
    missing = [name for name, tok in tokens.items() if not tok]
    if missing:
        raise RuntimeError(f"Missing token(s) in .env: {', '.join(missing)}")
 
    await asyncio.gather(
        bot_economy.bot.start(tokens["Economy"]),
        bot_drops.bot.start(tokens["Drops"]),
        bot_games.bot.start(tokens["Games"]),
        bot_admin.bot.start(tokens["Admin"]),
    )
 
 
if __name__ == "__main__":
    asyncio.run(main())
