import os
import json
import random
import asyncio
import aiosqlite
from datetime import datetime, timedelta, UTC
from contextlib import asynccontextmanager
from typing import Optional
 
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
 
load_dotenv()
 
# ═══════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════
 
DATABASE = os.getenv("DATABASE_PATH", "/app/data/giveaways.db")
db_lock  = asyncio.Lock()
 
 
@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DATABASE)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA busy_timeout=30000")
    try:
        yield db
    finally:
        await db.close()
 
 
async def setup_database():
    """Clean final schema. Safe to call from every bot's on_ready —
    CREATE TABLE IF NOT EXISTS is idempotent regardless of call order."""
    async with db_lock:
        async with get_db() as db:
 
            # ── Economy core ────────────────────────────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS balances(
                guild_id INTEGER, user_id INTEGER, balance INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, user_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS exp_history(
                guild_id INTEGER, user_id INTEGER, amount INTEGER,
                timestamp INTEGER, is_bonus INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS user_stats(
                guild_id INTEGER, user_id INTEGER,
                total_exp INTEGER DEFAULT 0, gifted_balance INTEGER DEFAULT 0,
                chests_opened INTEGER DEFAULT 0, mega_tickets_bought INTEGER DEFAULT 0,
                hosted_balance INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, user_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS inventory(
                guild_id INTEGER, user_id INTEGER, item_name TEXT, quantity INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, user_id, item_name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS item_store(
                guild_id INTEGER, item_name TEXT, price INTEGER, role_id INTEGER, description TEXT,
                PRIMARY KEY(guild_id, item_name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS exp_boosts(
                guild_id INTEGER, role_id INTEGER, boost_percent REAL,
                channel_id INTEGER DEFAULT 0, category_id INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, role_id, channel_id, category_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS balance_ranks(
                guild_id INTEGER, role_id INTEGER, threshold INTEGER,
                PRIMARY KEY(guild_id, role_id))""")
 
            # ── Giveaways (regular + auto + host) ───────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS giveaway_roles(
                guild_id INTEGER, role_id INTEGER)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS giveaways(
                message_id INTEGER, channel_id INTEGER, prize TEXT, winners INTEGER,
                reward INTEGER, end_time INTEGER, required_role INTEGER, template TEXT,
                ended INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS giveaway_winners(
                message_id INTEGER PRIMARY KEY, winner_id INTEGER, reward INTEGER)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS auto_giveaway_pool(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, prize TEXT,
                winners INTEGER DEFAULT 1, chance REAL DEFAULT 1.0,
                reward_balance INTEGER DEFAULT 0, reward_exp INTEGER DEFAULT 0,
                reward_tickets INTEGER DEFAULT 0, reward_gamble_tokens INTEGER DEFAULT 0,
                reward_vip_keys INTEGER DEFAULT 0, reward_role_id INTEGER DEFAULT 0,
                reward_item TEXT, reward_item_qty INTEGER DEFAULT 1)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS auto_giveaway_config(
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL, duration_seconds INTEGER NOT NULL,
                running INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS auto_entry_roles(
                guild_id INTEGER, role_id INTEGER, message_requirement INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, role_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS auto_entry_users(
                guild_id INTEGER, user_id INTEGER, enabled INTEGER DEFAULT 1,
                PRIMARY KEY(guild_id, user_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS auto_entry_threshold(
                guild_id INTEGER PRIMARY KEY,
                min_prize_balance INTEGER DEFAULT 0, recent_message_window INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS giveaway_game_notify_config(
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER)""")
 
            # ── Power Giveaways (multi-named) ───────────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS power_giveaway_config(
                guild_id INTEGER, name TEXT, prize TEXT, winners INTEGER DEFAULT 1,
                interval_seconds INTEGER DEFAULT 3600, embed_channel_id INTEGER DEFAULT 0,
                winners_channel_id INTEGER DEFAULT 0, default_entries INTEGER DEFAULT 0,
                reward_balance INTEGER DEFAULT 0, reward_exp INTEGER DEFAULT 0,
                reward_tickets INTEGER DEFAULT 0, reward_gamble_tokens INTEGER DEFAULT 0,
                reward_vip_keys INTEGER DEFAULT 0, reward_role_id INTEGER DEFAULT 0,
                reward_item TEXT, reward_item_qty INTEGER DEFAULT 1, running INTEGER DEFAULT 0,
                embed_message_id INTEGER DEFAULT 0, next_roll_time INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS power_giveaway_role_entries(
                guild_id INTEGER, name TEXT, role_id INTEGER, entries INTEGER,
                PRIMARY KEY(guild_id, name, role_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS power_giveaway_channel_rates(
                guild_id INTEGER, name TEXT, channel_id INTEGER, entries_per_message REAL,
                PRIMARY KEY(guild_id, name, channel_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS power_giveaway_role_boosts(
                guild_id INTEGER, name TEXT, role_id INTEGER, multiplier REAL,
                PRIMARY KEY(guild_id, name, role_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS power_giveaway_user_entries(
                guild_id INTEGER, name TEXT, user_id INTEGER, entries REAL DEFAULT 0,
                PRIMARY KEY(guild_id, name, user_id))""")
 
            # ── Mega Raffle ──────────────────────────────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS mega_tickets(
                guild_id INTEGER, user_id INTEGER, tickets INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, user_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS mega_bought(
                guild_id INTEGER, user_id INTEGER, bought INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, user_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS mega_payout_config(
                guild_id INTEGER PRIMARY KEY,
                denominator_mode TEXT DEFAULT 'total', payout_multiplier REAL DEFAULT 1,
                winners INTEGER DEFAULT 1)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS mega_announce_config(
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS mega_info_config(
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER, message_id INTEGER)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS mega_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, draw_timestamp INTEGER,
                winner_id INTEGER, winner_tickets INTEGER, total_tickets INTEGER,
                top_json TEXT, winners_json TEXT)""")
 
            # ── Chests / boxes / rare drops ──────────────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS chest_prizes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, chest_type TEXT,
                name TEXT, exp INTEGER DEFAULT 0, balance INTEGER DEFAULT 0, chance REAL)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS rare_chest_config(
                guild_id INTEGER, chest_type TEXT, prize_name TEXT,
                PRIMARY KEY(guild_id, chest_type, prize_name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS rare_drop_config(
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS chest_channel_config(
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER DEFAULT 0, message_id INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS daily_key_log(
                guild_id INTEGER, user_id INTEGER, date TEXT, PRIMARY KEY(guild_id, user_id, date))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS abuse_boxes(
                guild_id INTEGER, box_name TEXT, PRIMARY KEY(guild_id, box_name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS abuse_box_prizes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, box_name TEXT,
                prize_type TEXT, prize_value TEXT, prize_amount INTEGER DEFAULT 0, chance REAL)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS rare_box_config(
                guild_id INTEGER, box_name TEXT, prize_id INTEGER,
                PRIMARY KEY(guild_id, box_name, prize_id))""")
 
            # ── Random Games ─────────────────────────────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS games(
                guild_id INTEGER, game_name TEXT, enabled INTEGER DEFAULT 1,
                reward_balance INTEGER DEFAULT 0, reward_exp INTEGER DEFAULT 0,
                reward_tickets INTEGER DEFAULT 0, reward_gamble_tokens INTEGER DEFAULT 0,
                reward_vip_keys INTEGER DEFAULT 0, reward_item TEXT, reward_item_qty INTEGER DEFAULT 1,
                reward_role_id INTEGER DEFAULT 0, chance REAL DEFAULT 1.0, answer_time INTEGER DEFAULT 30,
                PRIMARY KEY(guild_id, game_name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS game_answers(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, game_name TEXT, answer TEXT)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS game_hints(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, game_name TEXT,
                answer_id INTEGER, hint_text TEXT, hint_order INTEGER DEFAULT 1)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS game_config(
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER,
                interval_seconds INTEGER DEFAULT 60, hint_delays TEXT)""")
 
            # ── Gambling ─────────────────────────────────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS daily_gamble_log(
                guild_id INTEGER, user_id INTEGER, date TEXT, PRIMARY KEY(guild_id, user_id, date))""")
 
            # ── Redeem codes ─────────────────────────────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS redeem_codes(
                guild_id INTEGER, code TEXT, prize_json TEXT, uses_left INTEGER DEFAULT 1,
                min_level INTEGER DEFAULT 0, min_balance INTEGER DEFAULT 0, required_role_id INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, code))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS code_uses(
                guild_id INTEGER, code TEXT, user_id INTEGER, PRIMARY KEY(guild_id, code, user_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS global_redeem_codes(
                code TEXT PRIMARY KEY, prize_json TEXT, uses_left INTEGER DEFAULT -1,
                min_level INTEGER DEFAULT 0, min_balance INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS global_code_uses(
                code TEXT, user_id INTEGER, PRIMARY KEY(code, user_id))""")
 
            # ── Counting ─────────────────────────────────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS counting_config(
                guild_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 0,
                channel_id INTEGER DEFAULT 0, announce_channel_id INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS counting_state(
                guild_id INTEGER PRIMARY KEY, current_count INTEGER DEFAULT 0,
                last_user_id INTEGER DEFAULT 0, last_message_id INTEGER DEFAULT 0,
                record INTEGER DEFAULT 0, notify_message_id INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS counting_bans(
                guild_id INTEGER, user_id INTEGER, unban_time INTEGER, PRIMARY KEY(guild_id, user_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS counting_prizes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, prize_type TEXT,
                prize_value TEXT, prize_amount INTEGER DEFAULT 0, weight_formula TEXT DEFAULT '1')""")
            await db.execute("""CREATE TABLE IF NOT EXISTS counting_special_prizes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, number INTEGER,
                prize_type TEXT, prize_value TEXT, prize_amount INTEGER DEFAULT 0, label TEXT)""")
 
            # ── Verification / Welcome ───────────────────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS verification_config(
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER DEFAULT 0, message_id INTEGER DEFAULT 0,
                verified_role_id INTEGER DEFAULT 0, unverified_role_id INTEGER DEFAULT 0, message TEXT)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS welcome_config(
                guild_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 0, message TEXT,
                channel_id INTEGER DEFAULT 0, channel_enabled INTEGER DEFAULT 0, channel_message TEXT)""")
 
            # ── Logging / admin / system ─────────────────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS log_channels(
                guild_id INTEGER, log_type TEXT, channel_id INTEGER, PRIMARY KEY(guild_id, log_type))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS disabled_commands_persist(
                guild_id INTEGER, command_name TEXT, PRIMARY KEY(guild_id, command_name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS global_disabled_commands(
                command_name TEXT PRIMARY KEY)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS system_flags(
                guild_id INTEGER, flag_name TEXT, enabled INTEGER DEFAULT 1,
                PRIMARY KEY(guild_id, flag_name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS global_system_flags(
                flag_name TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS prefix_restrictions(
                guild_id INTEGER, channel_id INTEGER, role_id INTEGER, allowed INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, channel_id, role_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS admin_panel_config(
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER DEFAULT 0, message_id INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS stats_channel_config(
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER DEFAULT 0, message_id INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS bot_config(
                key TEXT PRIMARY KEY, value TEXT)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS daily_message_counts(
                guild_id INTEGER, user_id INTEGER, date TEXT, count INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, user_id, date))""")
 
            # ── Auto-reset on leave ──────────────────────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS auto_reset_config(
                guild_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS auto_reset_rules(
                guild_id INTEGER, reset_type TEXT, delay_seconds INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, reset_type))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS auto_reset_pending(
                guild_id INTEGER, user_id INTEGER, reset_type TEXT, reset_after INTEGER,
                PRIMARY KEY(guild_id, user_id, reset_type))""")
 
            await db.commit()
 
 
# ═══════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════
 
VIP_CHEST_KEY        = "VIP Chest Key"
GAMBLE_TOKEN         = "Gamble Token"
MEGA_TICKET_PRICE    = 400000
MEGA_TICKET_CAP      = 250
CHEST_COST           = 1000
LEVEL_DIVISOR        = 700
BOT_OWNER_ID         = 906291437895843901
COUNTING_BOT_ID      = 510016054391734273
_COUNTING_FAIL_EMOJI = frozenset({'❌', '⚠️', '⚠'})
 
TEMPLATES = {
    "gold":  discord.Color.gold(),
    "red":   discord.Color.red(),
    "blue":  discord.Color.blue(),
    "green": discord.Color.green(),
}
 
DEFAULT_CHEST_PRIZES = [
    {"name": "250 EXP",     "exp": 250,   "balance": 0,     "chance": 40},
    {"name": "450 EXP",     "exp": 450,   "balance": 0,     "chance": 30},
    {"name": "1k EXP",      "exp": 1000,  "balance": 0,     "chance": 6},
    {"name": "1k Balance",  "exp": 0,     "balance": 1000,  "chance": 15},
    {"name": "1 Huge",      "exp": 0,     "balance": 0,     "chance": 4},
    {"name": "25m Gems",    "exp": 0,     "balance": 0,     "chance": 4},
    {"name": "40k Balance", "exp": 0,     "balance": 40000, "chance": 1},
]
DEFAULT_VIP_PRIZES = [
    {"name": "2k EXP",       "exp": 2000,  "balance": 0,      "chance": 28},
    {"name": "5k EXP",       "exp": 5000,  "balance": 0,      "chance": 18},
    {"name": "5k Balance",   "exp": 0,     "balance": 5000,   "chance": 18},
    {"name": "15k Balance",  "exp": 0,     "balance": 15000,  "chance": 12},
    {"name": "1 Huge",       "exp": 0,     "balance": 0,      "chance": 10},
    {"name": "25m Gems",     "exp": 0,     "balance": 0,      "chance": 9},
    {"name": "100k Balance", "exp": 0,     "balance": 100000, "chance": 5},
]
RARE_CHEST_PRIZES = {"1 Huge", "25m Gems", "40k Balance"}
RARE_VIP_PRIZES   = {"1 Huge", "25m Gems", "100k Balance"}
 
_SYSTEM_LABELS = {"mega": "🎟 Mega Raffle system", "vipkey": "🔑 VIP Key system", "gamble": "🎲 Gambling system"}
_SYSTEM_CHOICES = [
    app_commands.Choice(name="Mega Raffle (buying tickets, daily draw)", value="mega"),
    app_commands.Choice(name="VIP Key (daily keys, vipchest command)",    value="vipkey"),
    app_commands.Choice(name="Gambling (tokens, blackjack, roulette)",    value="gamble"),
]
 
# ═══════════════════════════════════════════════════════
# PREFIX  (mutable — see module docstring for how to change it safely)
# ═══════════════════════════════════════════════════════
 
_BOT_PREFIX = "!"
 
def get_prefix(bot, message):
    return _BOT_PREFIX
 
async def _load_prefix():
    """Call from each bot's on_ready. Last one to load wins (they'll all load the same value)."""
    global _BOT_PREFIX
    async with get_db() as db:
        async with db.execute("SELECT value FROM bot_config WHERE key='prefix'") as cur:
            row = await cur.fetchone()
    if row:
        _BOT_PREFIX = row[0]
 
async def set_prefix(new_prefix: str):
    """Used by the owner-only /setprefix command in bot_admin."""
    global _BOT_PREFIX
    _BOT_PREFIX = new_prefix
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO bot_config VALUES('prefix',?)", (new_prefix,))
            await db.commit()
 
 
# ═══════════════════════════════════════════════════════
# DISABLED COMMANDS / SYSTEM TOGGLES  (shared mutable state)
# ═══════════════════════════════════════════════════════
 
disabled_commands: dict[int, set[str]] = {}
global_disabled_commands: set[str] = set()
 
def _cmd_disabled(guild_id: int, cmd_name: str) -> bool:
    return (cmd_name in global_disabled_commands or
            cmd_name in disabled_commands.get(guild_id, set()))
 
def command_enabled():
    async def predicate(interaction: discord.Interaction) -> bool:
        name     = interaction.command.name if interaction.command else ""
        guild_id = interaction.guild.id if interaction.guild else 0
        if _cmd_disabled(guild_id, name):
            await interaction.response.send_message(
                "❌ This command is currently disabled.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)
 
async def load_disabled_commands():
    async with get_db() as db:
        async with db.execute("SELECT guild_id, command_name FROM disabled_commands_persist") as cur:
            for guild_id, cmd in await cur.fetchall():
                disabled_commands.setdefault(guild_id, set()).add(cmd)
        async with db.execute("SELECT command_name FROM global_disabled_commands") as cur:
            global_disabled_commands.update(r[0] for r in await cur.fetchall())
 
async def is_system_enabled(guild_id: int, flag: str) -> bool:
    async with get_db() as db:
        async with db.execute(
            "SELECT enabled FROM global_system_flags WHERE flag_name=?", (flag,)) as cur:
            grow = await cur.fetchone()
    if grow is not None and grow[0] == 0:
        return False
    async with get_db() as db:
        async with db.execute(
            "SELECT enabled FROM system_flags WHERE guild_id=? AND flag_name=?",
            (guild_id, flag)) as cur:
            row = await cur.fetchone()
    return row[0] == 1 if row else True
 
async def set_system_flag(guild_id: int, flag: str, enabled: bool):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO system_flags(guild_id, flag_name, enabled) VALUES(?,?,?)",
                (guild_id, flag, 1 if enabled else 0))
            await db.commit()
 
 
# ═══════════════════════════════════════════════════════
# PERMISSIONS
# ═══════════════════════════════════════════════════════
 
async def is_allowed_to_giveaway(interaction: discord.Interaction) -> bool:
    if interaction.user.id == BOT_OWNER_ID:
        return True
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if any(role.name.lower() == "bot developer" for role in member.roles):
        return True
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT role_id FROM giveaway_roles WHERE guild_id=?",
                                  (interaction.guild.id,)) as cur:
                rows = await cur.fetchall()
    allowed = {r[0] for r in rows}
    return any(role.id in allowed for role in member.roles)
 
 
async def _is_allowed_ctx(ctx: commands.Context) -> bool:
    if ctx.author.id == BOT_OWNER_ID:
        return True
    if any(r.name.lower() == "bot developer" for r in ctx.author.roles):
        return True
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT role_id FROM giveaway_roles WHERE guild_id=?",
                                  (ctx.guild.id,)) as cur:
                rows = await cur.fetchall()
    allowed = {r[0] for r in rows}
    return any(role.id in allowed for role in ctx.author.roles)
 
 
# ═══════════════════════════════════════════════════════
# PREFIX-CHANNEL RESTRICTIONS  (shared mutable state)
# ═══════════════════════════════════════════════════════
 
# {(guild_id, channel_id): {role_id: allowed_bool}}  — role_id 0 = "everyone" default
prefix_channel_rules: dict[tuple[int, int], dict[int, bool]] = {}
 
async def load_prefix_restrictions():
    async with get_db() as db:
        async with db.execute(
            "SELECT guild_id,channel_id,role_id,allowed FROM prefix_restrictions") as cur:
            for gid, cid, rid, allowed in await cur.fetchall():
                key = (gid, cid)
                prefix_channel_rules.setdefault(key, {})[rid] = bool(allowed)
 
def _prefix_channel_allowed(message: discord.Message) -> bool:
    if not message.guild:
        return True
    if message.author.id == BOT_OWNER_ID:
        return True
    key   = (message.guild.id, message.channel.id)
    rules = prefix_channel_rules.get(key, {})
    if not rules:
        return True
    user_role_ids = (
        {role.id for role in message.author.roles}
        if isinstance(message.author, discord.Member) else set()
    )
    for rid in user_role_ids:
        if rules.get(rid) is True:
            return True
    if rules.get(0) is False:
        return False
    for rid in user_role_ids:
        if rules.get(rid) is False:
            return False
    return True
 
 
# ═══════════════════════════════════════════════════════
# BALANCE  (guild-scoped) + balance ranks
# ═══════════════════════════════════════════════════════
 
async def get_balance(guild_id: int, user_id: int) -> int:
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT balance FROM balances WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)) as cur:
                row = await cur.fetchone()
            if row is None:
                await db.execute("INSERT INTO balances VALUES(?,?,?)", (guild_id, user_id, 0))
                await db.commit()
                return 0
            return row[0]
 
 
async def add_balance(guild_id: int, user_id: int, amount: int, bot=None):
    """`bot` is optional. If omitted, we auto-find any registered bot
    that's in this guild — so balance rank updates work from every caller."""
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO balances VALUES(?,?,0)", (guild_id, user_id))
            await db.execute(
                "UPDATE balances SET balance=balance+? WHERE guild_id=? AND user_id=?",
                (amount, guild_id, user_id))
            await db.execute(
                "UPDATE balances SET balance=0 WHERE guild_id=? AND user_id=? AND balance<0",
                (guild_id, user_id))
            await db.commit()
 
    # Use the provided bot, or find any registered bot that can see this guild
    _bot = bot
    if _bot is None:
        for b in _bot_instances:
            if b.get_guild(guild_id):
                _bot = b
                break
 
    if _bot is not None:
        await _update_balance_rank(_bot, guild_id, user_id)
 
 
async def _update_balance_rank(bot: commands.Bot, guild_id: int, user_id: int):
    """Re-evaluate which balance-rank role (if any) a user should hold, based
    on CURRENT balance, and swap roles if needed. Logs failures visibly
    (instead of only printing) so role-hierarchy/permission issues are easy
    to spot."""
    async with get_db() as db:
        async with db.execute(
            "SELECT role_id, threshold FROM balance_ranks "
            "WHERE guild_id=? ORDER BY threshold DESC", (guild_id,)) as cur:
            ranks = await cur.fetchall()
    if not ranks:
        return
 
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    member = guild.get_member(user_id)
    if not member:
        return
 
    bal = await get_balance(guild_id, user_id)
    target_role_id = None
    for role_id, threshold in ranks:
        if bal >= threshold:
            target_role_id = role_id
            break
 
    rank_role_ids     = {r[0] for r in ranks}
    member_rank_roles = {r.id for r in member.roles if r.id in rank_role_ids}
    target_set        = {target_role_id} if target_role_id else set()
    if member_rank_roles == target_set:
        return
 
    to_remove = [r for rid in (member_rank_roles - target_set) if (r := guild.get_role(rid))]
    to_add    = [r for rid in (target_set - member_rank_roles) if (r := guild.get_role(rid))]
 
    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="Balance rank update")
        if to_add:
            await member.add_roles(*to_add, reason="Balance rank update")
    except Exception as e:
        print(f"[BalanceRank] {member}: {e}")
        await log_event(guild_id, "admin", _log_embed(
            "⚠️ Balance Rank Assignment FAILED", discord.Color.red(),
            User=f"{member} ({member.mention})",
            Attempted_Role=(to_add[0].mention if to_add else "?"),
            Error=str(e)[:200],
            Likely_Cause="Bot's role is below the rank role, or missing Manage Roles"))
        return
 
    if to_add:
        await log_event(guild_id, "admin", _log_embed(
            "📈 Balance Rank Updated", discord.Color.gold(),
            User=member.mention, New_Rank=to_add[0].mention, Balance=f"{bal:,}"))
    elif to_remove and not to_add:
        await log_event(guild_id, "admin", _log_embed(
            "📉 Balance Rank Lost", discord.Color.orange(),
            User=member.mention, Lost_Rank=to_remove[0].mention, Balance=f"{bal:,}"))
 
 
# ═══════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════
 
async def ensure_stats(guild_id: int, user_id: int):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO user_stats(guild_id,user_id) VALUES(?,?)",
                (guild_id, user_id))
            await db.commit()
 
async def add_stat(guild_id: int, user_id: int, column: str, amount: int):
    await ensure_stats(guild_id, user_id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                f"UPDATE user_stats SET {column}={column}+? WHERE guild_id=? AND user_id=?",
                (amount, guild_id, user_id))
            await db.commit()
 
 
# ═══════════════════════════════════════════════════════
# EXP
# ═══════════════════════════════════════════════════════
 
async def add_exp(guild_id: int, user_id: int, amount: int, is_bonus: bool = False):
    if amount > 0 and not is_bonus:
        await add_stat(guild_id, user_id, "total_exp", amount)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                (guild_id, user_id, amount, int(datetime.now(UTC).timestamp()), 1 if is_bonus else 0))
            await db.commit()
 
async def get_exp(guild_id: int, user_id: int) -> int:
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    async with get_db() as db:
        async with db.execute(
            "SELECT SUM(amount) FROM exp_history WHERE guild_id=? AND user_id=? AND timestamp>=?",
            (guild_id, user_id, week_ago)) as cur:
            row = await cur.fetchone()
    return max(row[0] or 0, 0)
 
async def get_level_exp(guild_id: int, user_id: int) -> int:
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    async with get_db() as db:
        async with db.execute(
            "SELECT SUM(amount) FROM exp_history "
            "WHERE guild_id=? AND user_id=? AND timestamp>=? AND amount>0 AND is_bonus=0",
            (guild_id, user_id, week_ago)) as cur:
            row = await cur.fetchone()
    return max(row[0] or 0, 0)
 
async def get_level(guild_id: int, user_id: int) -> int:
    return min((await get_level_exp(guild_id, user_id)) // LEVEL_DIVISOR + 1, 100)
 
async def _add_chest_spending(guild_id: int, user_id: int, amount: int):
    """Insert negative entries timestamped to match the oldest positive
    entries they consume, so both sides expire together."""
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    remaining = amount
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT rowid, amount, timestamp FROM exp_history "
                "WHERE guild_id=? AND user_id=? AND timestamp>=? AND amount>0 "
                "ORDER BY timestamp ASC",
                (guild_id, user_id, week_ago)) as cur:
                entries = await cur.fetchall()
            for rowid, entry_amount, entry_ts in entries:
                if remaining <= 0:
                    break
                consume = min(entry_amount, remaining)
                await db.execute(
                    "INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) "
                    "VALUES(?,?,?,?,?)",
                    (guild_id, user_id, -consume, entry_ts, 0))
                remaining -= consume
            await db.commit()
 
 
# ═══════════════════════════════════════════════════════
# INVENTORY
# ═══════════════════════════════════════════════════════
 
async def inventory_add(guild_id: int, user_id: int, item_name: str, quantity: int = 1):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO inventory(guild_id,user_id,item_name,quantity) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id,user_id,item_name) DO UPDATE SET quantity=quantity+excluded.quantity",
                (guild_id, user_id, item_name, quantity))
            await db.commit()
 
async def inventory_remove(guild_id: int, user_id: int, item_name: str, quantity: int = 1) -> bool:
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT quantity FROM inventory WHERE guild_id=? AND user_id=? AND item_name=?",
                (guild_id, user_id, item_name)) as cur:
                row = await cur.fetchone()
            if not row or row[0] < quantity:
                return False
            new_qty = row[0] - quantity
            if new_qty == 0:
                await db.execute(
                    "DELETE FROM inventory WHERE guild_id=? AND user_id=? AND item_name=?",
                    (guild_id, user_id, item_name))
            else:
                await db.execute(
                    "UPDATE inventory SET quantity=? WHERE guild_id=? AND user_id=? AND item_name=?",
                    (new_qty, guild_id, user_id, item_name))
            await db.commit()
    return True
 
async def inventory_get(guild_id: int, user_id: int) -> list[tuple[str, int]]:
    async with get_db() as db:
        async with db.execute(
            "SELECT item_name,quantity FROM inventory WHERE guild_id=? AND user_id=? ORDER BY item_name",
            (guild_id, user_id)) as cur:
            return await cur.fetchall()
 
 
# ═══════════════════════════════════════════════════════
# ITEM STORE
# ═══════════════════════════════════════════════════════
 
async def add_item(guild_id: int, item_name: str, price: int, role_id: int, description: str):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO item_store VALUES(?,?,?,?,?)",
                (guild_id, item_name, price, role_id, description))
            await db.commit()
 
async def remove_item(guild_id: int, item_name: str):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM item_store WHERE guild_id=? AND item_name=?", (guild_id, item_name))
            await db.commit()
 
async def get_item(guild_id: int, item_name: str):
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM item_store WHERE guild_id=? AND LOWER(item_name)=LOWER(?)",
            (guild_id, item_name)) as cur:
            return await cur.fetchone()
 
async def get_all_items(guild_id: int) -> list:
    async with get_db() as db:
        async with db.execute("SELECT * FROM item_store WHERE guild_id=?", (guild_id,)) as cur:
            return await cur.fetchall()
 
 
# ═══════════════════════════════════════════════════════
# GAMBLE TOKENS / MEGA TICKETS
# ═══════════════════════════════════════════════════════
 
async def get_gamble_tokens(guild_id: int, user_id: int) -> int:
    inv   = await inventory_get(guild_id, user_id)
    owned = {n.lower(): q for n, q in inv}
    return owned.get(GAMBLE_TOKEN.lower(), 0)
 
async def get_tickets(guild_id, user_id):
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT tickets FROM mega_tickets WHERE guild_id=? AND user_id=?",
                                  (guild_id, user_id)) as cur:
                row = await cur.fetchone()
            if not row:
                await db.execute("INSERT INTO mega_tickets VALUES(?,?,?)", (guild_id, user_id, 0))
                await db.commit()
                return 0
            return row[0]
 
async def add_tickets(guild_id, user_id, amount):
    tickets = await get_tickets(guild_id, user_id)
    new_tickets = max(0, tickets + amount)
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE mega_tickets SET tickets=? WHERE guild_id=? AND user_id=?",
                             (new_tickets, guild_id, user_id))
            await db.commit()
 
def _weighted_sample_without_replacement(items_weights: list, k: int) -> list:
    """Pick up to k unique items from [(item, weight), ...] without replacement."""
    pool = [(it, w) for it, w in items_weights if w > 0]
    chosen = []
    for _ in range(min(k, len(pool))):
        total = sum(w for _, w in pool)
        if total <= 0:
            break
        r = random.uniform(0, total)
        upto = 0.0
        for i, (it, w) in enumerate(pool):
            upto += w
            if upto >= r:
                chosen.append(it)
                pool.pop(i)
                break
    return chosen
 
 
# ═══════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════
 
_bot_instances: list = []   # populated by each bot file via register_bot_instance()
 
def register_bot_instance(bot: commands.Bot):
    """Each bot file calls this once at import time so log_event/get_channel
    lookups work no matter which bot's channel cache actually has the channel."""
    _bot_instances.append(bot)
 
def _find_channel(channel_id: int):
    for b in _bot_instances:
        ch = b.get_channel(channel_id)
        if ch:
            return ch
    return None
 
async def log_event(guild_id: int, log_type: str, embed: discord.Embed):
    try:
        async with get_db() as db:
            async with db.execute(
                "SELECT channel_id FROM log_channels WHERE guild_id=? AND log_type=?",
                (guild_id, log_type)) as cur:
                row = await cur.fetchone()
        if not row:
            return
        ch = _find_channel(row[0])
        if ch:
            await ch.send(embed=embed)
    except Exception as e:
        print(f"[Log:{log_type}] {e}")
 
def _log_embed(title: str, color: discord.Color, **fields) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(UTC))
    for name, value in fields.items():
        embed.add_field(name=name, value=str(value), inline=True)
    return embed
 
def find_guild(guild_id: int):
    for b in _bot_instances:
        g = b.get_guild(guild_id)
        if g:
            return g
    return None
 
 
# ═══════════════════════════════════════════════════════
# GIVEAWAY PRIZE DISTRIBUTION  (shared by /giveaway, /host, auto giveaways,
# and power giveaways)
# ═══════════════════════════════════════════════════════
 
async def distribute_prizes(guild, winners, meta):
    prize_balance  = int(meta.get("balance", 0))
    prize_exp      = int(meta.get("exp", 0))
    prize_tickets  = int(meta.get("tickets", 0))
    prize_gamble   = int(meta.get("gamble_tokens", 0))
    prize_vip_keys = int(meta.get("vip_keys", 0))
    prize_role_id  = int(meta.get("role_id", 0))
    prize_item     = meta.get("item")
    prize_item_qty = int(meta.get("item_qty", 1))
    for winner in winners:
        if prize_balance > 0:
            await add_balance(guild.id, winner.id, prize_balance)
        if prize_exp > 0:
            await add_exp(guild.id, winner.id, prize_exp)
        if prize_tickets > 0:
            await add_tickets(guild.id, winner.id, prize_tickets)
        if prize_gamble > 0:
            await inventory_add(guild.id, winner.id, GAMBLE_TOKEN, prize_gamble)
        if prize_vip_keys > 0:
            await inventory_add(guild.id, winner.id, VIP_CHEST_KEY, prize_vip_keys)
        if prize_role_id:
            role   = guild.get_role(prize_role_id)
            member = guild.get_member(winner.id)
            if role and member:
                try: await member.add_roles(role)
                except Exception: pass
        if prize_item:
            await inventory_add(guild.id, winner.id, prize_item, prize_item_qty)
 
def build_reward_summary(meta, guild=None) -> str:
    parts = []
    if int(meta.get("balance", 0)) > 0:       parts.append(f"💰 {int(meta['balance']):,} coins")
    if int(meta.get("exp", 0)) > 0:           parts.append(f"⭐ {int(meta['exp']):,} EXP")
    if int(meta.get("tickets", 0)) > 0:       parts.append(f"🎟 {meta['tickets']} ticket(s)")
    if int(meta.get("gamble_tokens", 0)) > 0: parts.append(f"🎲 {meta['gamble_tokens']} gamble token(s)")
    if int(meta.get("vip_keys", 0)) > 0:      parts.append(f"🔑 {meta['vip_keys']} VIP key(s)")
    if int(meta.get("role_id", 0)) > 0 and guild:
        role = guild.get_role(int(meta["role_id"]))
        if role: parts.append(f"👑 {role.mention}")
    if meta.get("item"):                       parts.append(f"🎒 {meta['item_qty']}x {meta['item']}")
    return " + ".join(parts) if parts else "No reward"
 
 
# ═══════════════════════════════════════════════════════
# RESET HELPER  (used by admin panel + /resetuser + /resetrole, all in bot_admin)
# ═══════════════════════════════════════════════════════
 
async def _do_reset(guild_id: int, user_id: int, reset_type: str):
    async with db_lock:
        async with get_db() as db:
            if reset_type in ("balance", "all"):
                await db.execute(
                    "UPDATE balances SET balance=0 WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
            if reset_type in ("exp", "all"):
                await db.execute(
                    "DELETE FROM exp_history WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
            if reset_type in ("inventory", "all"):
                await db.execute(
                    "DELETE FROM inventory WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
            if reset_type in ("tickets", "all"):
                await db.execute(
                    "UPDATE mega_tickets SET tickets=0 WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
            if reset_type in ("stats", "all"):
                await db.execute(
                    "UPDATE user_stats SET total_exp=0, gifted_balance=0, chests_opened=0, "
                    "mega_tickets_bought=0, hosted_balance=0 WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
            await db.commit()
 
 
# ═══════════════════════════════════════════════════════
# DAILY MESSAGE COUNT  (written by bot_admin's EXP-from-chat hook,
# read by bot_games' /autoentry message-requirement check)
# ═══════════════════════════════════════════════════════
 
_msg_buf: dict[tuple[int, int], int] = {}
_msg_buf_date: str = ""
 
def bump_msg_count(guild_id: int, user_id: int):
    global _msg_buf_date
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if today != _msg_buf_date:
        _msg_buf.clear()
        _msg_buf_date = today
    key = (guild_id, user_id)
    _msg_buf[key] = _msg_buf.get(key, 0) + 1
 
async def get_today_msg_count(guild_id: int, user_id: int) -> int:
    """Combines the in-memory buffer (only non-empty in the SAME process that's
    calling bump_msg_count — i.e. bot_admin) with whatever's been flushed to
    the DB so far. Cross-bot reads (e.g. from bot_games) only see DB-flushed
    counts, which lag by up to ~2 minutes — acceptable for a daily threshold
    check."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    mem = _msg_buf.get((guild_id, user_id), 0) if _msg_buf_date == today else 0
    async with get_db() as db:
        async with db.execute(
            "SELECT count FROM daily_message_counts WHERE guild_id=? AND user_id=? AND date=?",
            (guild_id, user_id, today)) as cur:
            row = await cur.fetchone()
    return mem + (row[0] if row else 0)
 
async def msg_count_flush_loop(bot: commands.Bot):
    """Run this from whichever bot calls bump_msg_count (bot_admin)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(120)
        if not _msg_buf:
            continue
        today = _msg_buf_date or datetime.now(UTC).strftime("%Y-%m-%d")
        snapshot = {k: v for k, v in _msg_buf.items() if v > 0}
        for k in snapshot:
            _msg_buf.pop(k, None)
        if not snapshot:
            continue
        async with db_lock:
            async with get_db() as db:
                for (gid, uid), cnt in snapshot.items():
                    await db.execute(
                        "INSERT INTO daily_message_counts(guild_id,user_id,date,count) VALUES(?,?,?,?) "
                        "ON CONFLICT(guild_id,user_id,date) DO UPDATE SET count=count+?",
                        (gid, uid, today, cnt, cnt))
                await db.commit()
 
 
# ═══════════════════════════════════════════════════════
# FAKE INTERACTION  — lets prefix commands call slash callbacks directly
# ═══════════════════════════════════════════════════════
 
class FakeInteraction:
    class _Resp:
        def __init__(self, ctx): self._ctx = ctx
        async def send_message(self, content=None, *, embed=None, embeds=None, ephemeral=False, view=None):
            kw = {}
            if content is not None: kw['content'] = content
            if embed   is not None: kw['embed']   = embed
            if embeds  is not None: kw['embeds']  = embeds
            if view    is not None: kw['view']    = view
            await self._ctx.send(**kw)
        async def defer(self, ephemeral=False):
            await self._ctx.trigger_typing()
        async def send_modal(self, modal):
            await self._ctx.send("❌ This command requires the slash version "
                                 "(it opens a pop-up form that prefix commands can't show).")
 
    class _Follow:
        def __init__(self, ctx): self._ctx = ctx
        async def send(self, content=None, *, embed=None, embeds=None, ephemeral=False, view=None):
            kw = {}
            if content is not None: kw['content'] = content
            if embed   is not None: kw['embed']   = embed
            if embeds  is not None: kw['embeds']  = embeds
            if view    is not None: kw['view']    = view
            await self._ctx.send(**kw)
 
    def __init__(self, ctx: commands.Context):
        self._ctx     = ctx
        self.guild    = ctx.guild
        self.user     = ctx.author
        self.channel  = ctx.channel
        self.command  = ctx.command
        self.data     = {}
        self.type     = discord.InteractionType.application_command
        self.response = self._Resp(ctx)
        self.followup = self._Follow(ctx)
 
    async def original_response(self):
        async for msg in self._ctx.channel.history(limit=1):
            return msg
 
 
class _MC:
    """Mock app_commands.Choice."""
    def __init__(self, value: str):
        self.value = value
        self.name  = value
