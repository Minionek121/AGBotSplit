import os, json, random, asyncio, aiosqlite, discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta, UTC
from typing import Optional

import common
from common import (
    get_db, db_lock, setup_database, log_event, _log_embed, command_enabled,
    is_allowed_to_giveaway, _is_allowed_ctx, is_system_enabled,
    get_balance, add_balance, get_exp, add_exp, get_level, _add_chest_spending,
    inventory_add, inventory_remove, inventory_get, get_item,
    get_tickets, add_tickets, _weighted_sample_without_replacement,
    distribute_prizes, build_reward_summary,
    FakeInteraction,
    VIP_CHEST_KEY, GAMBLE_TOKEN, MEGA_TICKET_PRICE, MEGA_TICKET_CAP, CHEST_COST,
    DEFAULT_CHEST_PRIZES, DEFAULT_VIP_PRIZES, RARE_CHEST_PRIZES, RARE_VIP_PRIZES,
    disabled_commands, global_disabled_commands, load_disabled_commands,
    prefix_channel_rules, _prefix_channel_allowed, load_prefix_restrictions,
    register_bot_instance,
)

TOKEN = os.getenv("TOKEN_DROPS")
_GUILD_ID = int(os.getenv("GUILD_ID", "0"))
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix=common.get_prefix, intents=intents, help_command=None)
register_bot_instance(bot)

# ═══════════════════════════════════════════════════════
# CHEST PRIZE HELPERS
# ═══════════════════════════════════════════════════════

async def get_chest_prizes(guild_id: int, chest_type: str) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT id,name,exp,balance,chance FROM chest_prizes WHERE guild_id=? AND chest_type=?",
            (guild_id, chest_type)) as cur:
            rows = await cur.fetchall()
    if rows:
        return [{"id": r[0], "name": r[1], "exp": r[2], "balance": r[3], "chance": r[4]} for r in rows]
    return DEFAULT_CHEST_PRIZES if chest_type == "chest" else DEFAULT_VIP_PRIZES

async def get_rare_drop_channel(guild_id: int):
    async with get_db() as db:
        async with db.execute("SELECT channel_id FROM rare_drop_config WHERE guild_id=?", (guild_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None

async def get_rare_chest_names(guild_id: int, chest_type: str) -> set[str]:
    async with get_db() as db:
        async with db.execute(
            "SELECT prize_name FROM rare_chest_config WHERE guild_id=? AND chest_type=?",
            (guild_id, chest_type)) as cur:
            rows = await cur.fetchall()
    if rows: return {r[0] for r in rows}
    return RARE_CHEST_PRIZES if chest_type == "chest" else RARE_VIP_PRIZES

async def get_rare_box_ids(guild_id: int, box_name: str) -> set[int]:
    async with get_db() as db:
        async with db.execute(
            "SELECT prize_id FROM rare_box_config WHERE guild_id=? AND box_name=?", (guild_id, box_name)) as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}

# ═══════════════════════════════════════════════════════
# CHEST PRIZE ADMIN
# ═══════════════════════════════════════════════════════

_CHEST_CHOICES = [
    app_commands.Choice(name="EXP Chest", value="chest"),
    app_commands.Choice(name="VIP Chest", value="vipchest"),
]

@bot.tree.command(name="addchestprize", description="Add a custom prize to the chest or VIP chest loot table")
@app_commands.describe(chest_type="chest or vipchest", name="Prize name",
                       exp="EXP (0 for none)", balance="Balance (0 for none)", chance="Weight (e.g. 40)")
@app_commands.choices(chest_type=_CHEST_CHOICES)
@command_enabled()
async def addchestprize(interaction: discord.Interaction, chest_type: str, name: str,
                        exp: int = 0, balance: int = 0, chance: float = 10.0):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if chance <= 0:
        await interaction.response.send_message("❌ Chance must be > 0.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO chest_prizes(guild_id,chest_type,name,exp,balance,chance) VALUES(?,?,?,?,?,?)",
                (interaction.guild.id, chest_type, name, exp, balance, chance))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Added **{name}** to **{chest_type}** (weight: {chance})\n"
        f"ℹ️ Custom prizes are now active — defaults are replaced for this server.")

@bot.command(name="addchestprize")
async def pfx_addchestprize(ctx, chest_type: str, name: str, exp: int = 0, balance: int = 0, chance: float = 10.0):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if chest_type not in ("chest","vipchest"): await ctx.send("❌ Use `chest` or `vipchest`."); return
    await addchestprize._callback(FakeInteraction(ctx), chest_type, name, exp, balance, chance)


@bot.tree.command(name="removechestprize", description="Remove a prize from the chest loot table by ID")
@app_commands.choices(chest_type=_CHEST_CHOICES)
@command_enabled()
async def removechestprize(interaction: discord.Interaction, chest_type: str, prize_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT id FROM chest_prizes WHERE id=? AND guild_id=? AND chest_type=?",
                (prize_id, interaction.guild.id, chest_type)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Prize #{prize_id} not found.", ephemeral=True); return
            await db.execute("DELETE FROM chest_prizes WHERE id=?", (prize_id,))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed prize #{prize_id} from **{chest_type}**.")

@bot.command(name="removechestprize")
async def pfx_removechestprize(ctx, chest_type: str, prize_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if chest_type not in ("chest","vipchest"): await ctx.send("❌ Use `chest` or `vipchest`."); return
    await removechestprize._callback(FakeInteraction(ctx), chest_type, prize_id)


@bot.command(name="listchestprizes")
async def cmd_listchestprizes(ctx, chest_type: str = "chest"):
    if chest_type not in ("chest","vipchest"): await ctx.send("❌ Use `chest` or `vipchest`."); return
    prizes = await get_chest_prizes(ctx.guild.id, chest_type)
    total_w = sum(p["chance"] for p in prizes)
    is_custom = any("id" in p for p in prizes)
    embed = discord.Embed(title=f"{'📦 EXP' if chest_type=='chest' else '💎 VIP'} Chest Prizes", color=discord.Color.purple())
    if not is_custom: embed.set_footer(text="Using default prizes. Use /addchestprize to customise.")
    lines = []
    for p in prizes:
        pct = (p["chance"] / total_w * 100) if total_w > 0 else 0
        desc = []
        if p["exp"] > 0: desc.append(f"⭐{p['exp']:,}")
        if p["balance"] > 0: desc.append(f"💰{p['balance']:,}")
        if not desc: desc.append("✨Special")
        id_str = f"`#{p['id']}` " if "id" in p else ""
        lines.append(f"{id_str}**{p['name']}** — {' + '.join(desc)} — **{pct:.1f}%** (w:{p['chance']})")
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)


@bot.tree.command(name="addrarechestdrop", description="Mark a chest prize as a rare drop")
@app_commands.describe(chest_type="Which chest", prize="Prize name or numeric ID from /listchestprizes")
@app_commands.choices(chest_type=_CHEST_CHOICES)
@command_enabled()
async def addrarechestdrop(interaction: discord.Interaction, chest_type: str, prize: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    prize = prize.strip()
    try:
        pid = int(prize)
        async with get_db() as db:
            async with db.execute(
                "SELECT name FROM chest_prizes WHERE id=? AND guild_id=? AND chest_type=?",
                (pid, interaction.guild.id, chest_type)) as cur:
                row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(f"❌ No custom prize #{pid} found in **{chest_type}**.", ephemeral=True); return
        prize = row[0]
    except ValueError:
        pass
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute(
                    "INSERT INTO rare_chest_config(guild_id,chest_type,prize_name) VALUES(?,?,?)",
                    (interaction.guild.id, chest_type, prize))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(f"❌ **{prize}** is already a rare drop for **{chest_type}**.", ephemeral=True); return
    label = "EXP Chest" if chest_type == "chest" else "VIP Chest"
    await interaction.response.send_message(f"✅ **{prize}** is now a rare drop for the **{label}**.")

@bot.command(name="addrarechestdrop")
async def pfx_addrarechestdrop(ctx, chest_type: str, *, prize: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if chest_type not in ("chest","vipchest"): await ctx.send("❌ Use `chest` or `vipchest`."); return
    await addrarechestdrop._callback(FakeInteraction(ctx), chest_type, prize)


@bot.tree.command(name="removerarechestdrop", description="Unmark a chest prize as a rare drop")
@app_commands.choices(chest_type=_CHEST_CHOICES)
@command_enabled()
async def removerarechestdrop(interaction: discord.Interaction, chest_type: str, prize: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    prize = prize.strip()
    try:
        pid = int(prize)
        async with get_db() as db:
            async with db.execute(
                "SELECT name FROM chest_prizes WHERE id=? AND guild_id=? AND chest_type=?",
                (pid, interaction.guild.id, chest_type)) as cur:
                row = await cur.fetchone()
        if row: prize = row[0]
    except ValueError:
        pass
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM rare_chest_config WHERE guild_id=? AND chest_type=? AND prize_name=?",
                             (interaction.guild.id, chest_type, prize))
            await db.commit()
    label = "EXP Chest" if chest_type == "chest" else "VIP Chest"
    await interaction.response.send_message(f"🗑 **{prize}** removed from rare drops for **{label}**.")

@bot.command(name="removerarechestdrop")
async def pfx_removerarechestdrop(ctx, chest_type: str, *, prize: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if chest_type not in ("chest","vipchest"): await ctx.send("❌ Use `chest` or `vipchest`."); return
    await removerarechestdrop._callback(FakeInteraction(ctx), chest_type, prize)


@bot.tree.command(name="setraredropchannel", description="Set channel for rare drop announcements")
@command_enabled()
async def setraredropchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO rare_drop_config VALUES(?,?)", (interaction.guild.id, channel.id))
            await db.commit()
    await interaction.response.send_message(f"✅ Rare drop announcements → {channel.mention}")

@bot.command(name="setraredropchannel")
async def pfx_setraredropchannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setraredropchannel._callback(FakeInteraction(ctx), channel)

# ═══════════════════════════════════════════════════════
# CHEST COMMANDS
# ═══════════════════════════════════════════════════════

async def _announce_rare(interaction, results, chest_type_label, rare_names):
    rare_won = {n: c for n, c in results.items() if n in rare_names}
    if not rare_won: return
    rcid = await get_rare_drop_channel(interaction.guild.id)
    if not rcid: return
    rc = bot.get_channel(rcid)
    if not rc: return
    text = " and ".join(f"**{c}x {n}**" for n, c in rare_won.items())
    color = discord.Color.gold() if chest_type_label == "chest" else discord.Color.from_rgb(148, 0, 211)
    re = discord.Embed(title="🌟 Rare Drop!" if chest_type_label == "chest" else "💎 VIP Rare Drop!",
        description=f"{interaction.user.mention} got {text} from a {chest_type_label}! 🎉",
        color=color)
    re.set_thumbnail(url=interaction.user.display_avatar.url)
    await rc.send(embed=re)


@bot.tree.command(name="chest", description="Open EXP chest(s)")
@command_enabled()
async def chest(interaction: discord.Interaction, amount: int = 1):
    await interaction.response.defer()
    if amount <= 0: await interaction.followup.send("❌ Amount must be > 0."); return
    exp = await get_exp(interaction.guild.id, interaction.user.id)
    if exp >= 1400: amount = min(amount, exp // CHEST_COST)
    else: amount = 1
    total_cost = CHEST_COST * amount
    if exp < total_cost:
        await interaction.followup.send(f"❌ You need {total_cost:,} EXP (you have {exp:,})."); return

    prizes = await get_chest_prizes(interaction.guild.id, "chest")
    rare_names = await get_rare_chest_names(interaction.guild.id, "chest")
    results: dict = {}; total_balance = 0; total_exp_won = 0
    for _ in range(amount):
        prize = random.choices(prizes, weights=[p["chance"] for p in prizes], k=1)[0]
        results[prize["name"]] = results.get(prize["name"], 0) + 1
        total_balance += prize["balance"]; total_exp_won += prize["exp"]

    gid, uid = interaction.guild.id, interaction.user.id
    await _add_chest_spending(gid, uid, total_cost)
    if total_balance > 0: await add_balance(gid, uid, total_balance, bot=bot)
    if total_exp_won > 0: await add_exp(gid, uid, total_exp_won)
    from common import add_stat
    await add_stat(gid, uid, "chests_opened", amount)

    result_text = "\n".join(f"• {count}x {name}" for name, count in results.items())
    embed = discord.Embed(title="📦 Chest Results", description=result_text, color=discord.Color.purple())
    embed.set_footer(text=f"Opened {amount} chest(s) | Cost: {total_cost:,} EXP")
    await interaction.followup.send(embed=embed)
    results_log = ", ".join(f"{c}x {n}" for n, c in results.items())
    await log_event(gid, "chest", _log_embed("📦 Chest Opened", discord.Color.purple(),
        User=interaction.user.mention, Opened=str(amount), Cost=f"{total_cost:,} EXP", Won=results_log[:1024]))
    await _announce_rare(interaction, results, "chest", rare_names)

@bot.command(name="chest")
async def pfx_chest(ctx, amount: int = 1):
    await chest._callback(FakeInteraction(ctx), amount)


@bot.tree.command(name="vipchest", description="Open VIP Chest(s) — costs 1 VIP Chest Key each")
@command_enabled()
async def vipchest(interaction: discord.Interaction, amount: int = 1):
    if not await is_system_enabled(interaction.guild.id, "vipkey"):
        await interaction.response.send_message("❌ VIP chest system is disabled.", ephemeral=True); return
    await interaction.response.defer()
    if amount <= 0: await interaction.followup.send("❌ Amount must be ≥ 1."); return
    inv = await inventory_get(interaction.guild.id, interaction.user.id)
    owned = {n.lower(): q for n, q in inv}
    available_keys = owned.get(VIP_CHEST_KEY.lower(), 0)
    if available_keys < amount:
        await interaction.followup.send(
            f"❌ Need {amount}x **{VIP_CHEST_KEY}** but only have {available_keys}.\n"
            f"Keys are given by admins; Nitro Boosters get one daily!"); return
    if not await inventory_remove(interaction.guild.id, interaction.user.id, VIP_CHEST_KEY, amount):
        await interaction.followup.send("❌ Failed to consume keys."); return

    prizes = await get_chest_prizes(interaction.guild.id, "vipchest")
    rare_names = await get_rare_chest_names(interaction.guild.id, "vipchest")
    results: dict = {}; total_balance = 0; total_exp_won = 0
    for _ in range(amount):
        prize = random.choices(prizes, weights=[p["chance"] for p in prizes], k=1)[0]
        results[prize["name"]] = results.get(prize["name"], 0) + 1
        total_balance += prize["balance"]; total_exp_won += prize["exp"]

    if total_balance > 0: await add_balance(interaction.guild.id, interaction.user.id, total_balance, bot=bot)
    if total_exp_won > 0: await add_exp(interaction.guild.id, interaction.user.id, total_exp_won)

    result_text = "\n".join(f"• {count}x {name}" for name, count in results.items())
    embed = discord.Embed(title="💎 VIP Chest Results", description=result_text, color=discord.Color.from_rgb(148, 0, 211))
    embed.set_footer(text=f"Opened {amount} VIP chest(s) | {available_keys - amount} key(s) remaining")
    await interaction.followup.send(embed=embed)
    results_log = ", ".join(f"{c}x {n}" for n, c in results.items())
    await log_event(interaction.guild.id, "chest", _log_embed("💎 VIP Chest Opened", discord.Color.from_rgb(148, 0, 211),
        User=interaction.user.mention, Opened=str(amount), Keys_Used=str(amount), Won=results_log[:1024]))
    await _announce_rare(interaction, results, "VIP Chest", rare_names)

@bot.command(name="vipchest")
async def pfx_vipchest(ctx, amount: int = 1):
    await vipchest._callback(FakeInteraction(ctx), amount)


async def daily_key_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(UTC)
        target = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if now >= target: target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        for guild in bot.guilds:
            if not await is_system_enabled(guild.id, "vipkey"): continue
            for member in guild.members:
                if member.bot or member.premium_since is None: continue
                try:
                    async with db_lock:
                        async with get_db() as db:
                            try:
                                await db.execute("INSERT INTO daily_key_log(guild_id,user_id,date) VALUES(?,?,?)",
                                                 (guild.id, member.id, today))
                                await db.commit()
                            except aiosqlite.IntegrityError:
                                continue
                    await inventory_add(guild.id, member.id, VIP_CHEST_KEY, 1)
                except Exception as e:
                    print(f"[DailyKey] {member} / {guild.name}: {e}")

# ═══════════════════════════════════════════════════════
# CHEST CHANNEL PANEL
# ═══════════════════════════════════════════════════════

async def _build_chest_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(title="📦 Chest Shop",
                          description="Open chests to win prizes! Results are only visible to you.",
                          color=discord.Color.purple())
    for chest_type, label, cost_str in [("chest","📦 EXP Chest","Cost: 1,000 EXP"),
                                         ("vipchest","💎 VIP Chest","Cost: 1 VIP Key")]:
        prizes = await get_chest_prizes(guild.id, chest_type)
        total_w = sum(p["chance"] for p in prizes) or 1
        lines = []
        for p in prizes:
            pct = p["chance"] / total_w * 100
            desc = []
            if p["exp"] > 0: desc.append(f"⭐ {p['exp']:,} EXP")
            if p["balance"] > 0: desc.append(f"💰 {p['balance']:,} coins")
            if not desc: desc.append("✨ Special")
            lines.append(f"• **{p['name']}** — {', '.join(desc)} — {pct:.1f}%")
        embed.add_field(name=f"{label} ({cost_str})", value="\n".join(lines) or "*No prizes configured*", inline=False)
    embed.set_footer(text="Use the buttons below • responses are only visible to you")
    return embed


async def _refresh_chest_channel(guild: discord.Guild):
    async with get_db() as db:
        async with db.execute("SELECT channel_id, message_id FROM chest_channel_config WHERE guild_id=?", (guild.id,)) as cur:
            row = await cur.fetchone()
    if not row or not row[0]: return
    ch_id, msg_id = row
    channel = bot.get_channel(ch_id)
    if not channel: return
    embed = await _build_chest_embed(guild)
    view = ChestChannelView()
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view); return
        except discord.NotFound:
            pass
    new_msg = await channel.send(embed=embed, view=view)
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE chest_channel_config SET message_id=? WHERE guild_id=?", (new_msg.id, guild.id))
            await db.commit()


async def _do_open_exp_chests(interaction: discord.Interaction, amount: int):
    await interaction.response.defer(ephemeral=True)
    gid, uid = interaction.guild.id, interaction.user.id
    exp = await get_exp(gid, uid)
    if exp < CHEST_COST:
        await interaction.followup.send(f"❌ You need {CHEST_COST:,} EXP (you have {exp:,}).", ephemeral=True); return
    max_open = exp // CHEST_COST
    amount = min(max_open, 100) if amount == -1 else min(amount, max_open)
    if amount == 0:
        await interaction.followup.send("❌ Not enough EXP.", ephemeral=True); return
    total_cost = CHEST_COST * amount
    prizes = await get_chest_prizes(gid, "chest")
    rare_names = await get_rare_chest_names(gid, "chest")
    results: dict[str, int] = {}; total_balance = total_exp_won = 0
    for _ in range(amount):
        prize = random.choices(prizes, weights=[p["chance"] for p in prizes], k=1)[0]
        results[prize["name"]] = results.get(prize["name"], 0) + 1
        total_balance += prize["balance"]; total_exp_won += prize["exp"]
    await _add_chest_spending(gid, uid, total_cost)
    if total_balance > 0: await add_balance(gid, uid, total_balance, bot=bot)
    if total_exp_won > 0: await add_exp(gid, uid, total_exp_won)
    from common import add_stat
    await add_stat(gid, uid, "chests_opened", amount)
    embed = discord.Embed(title=f"📦 Chest Results ×{amount}",
                          description="\n".join(f"• {c}x **{n}**" for n, c in results.items()), color=discord.Color.purple())
    embed.set_footer(text=f"Cost: {total_cost:,} EXP | Remaining: {exp - total_cost:,} EXP")
    await interaction.followup.send(embed=embed, ephemeral=True)
    results_log = ", ".join(f"{c}x {n}" for n, c in results.items())
    await log_event(gid, "chest", _log_embed("📦 Chest Opened", discord.Color.purple(),
        User=interaction.user.mention, Opened=str(amount), Cost=f"{total_cost:,} EXP", Won=results_log[:1024]))
    await _announce_rare(interaction, results, "chest", rare_names)


async def _do_open_vip_chests(interaction: discord.Interaction, amount: int):
    await interaction.response.defer(ephemeral=True)
    gid, uid = interaction.guild.id, interaction.user.id
    if not await is_system_enabled(gid, "vipkey"):
        await interaction.followup.send("❌ VIP chest system is disabled.", ephemeral=True); return
    inv = await inventory_get(gid, uid)
    keys = next((q for n, q in inv if n.lower() == VIP_CHEST_KEY.lower()), 0)
    if keys < 1:
        await interaction.followup.send(f"❌ You have no **{VIP_CHEST_KEY}** (Nitro Boosters get one daily!).", ephemeral=True); return
    amount = min(amount, keys, 10)
    if not await inventory_remove(gid, uid, VIP_CHEST_KEY, amount):
        await interaction.followup.send("❌ Failed to consume keys.", ephemeral=True); return
    prizes = await get_chest_prizes(gid, "vipchest")
    rare_names = await get_rare_chest_names(gid, "vipchest")
    results: dict[str, int] = {}; total_balance = total_exp_won = 0
    for _ in range(amount):
        prize = random.choices(prizes, weights=[p["chance"] for p in prizes], k=1)[0]
        results[prize["name"]] = results.get(prize["name"], 0) + 1
        total_balance += prize["balance"]; total_exp_won += prize["exp"]
    if total_balance > 0: await add_balance(gid, uid, total_balance, bot=bot)
    if total_exp_won > 0: await add_exp(gid, uid, total_exp_won)
    embed = discord.Embed(title=f"💎 VIP Chest Results ×{amount}",
                          description="\n".join(f"• {c}x **{n}**" for n, c in results.items()),
                          color=discord.Color.from_rgb(148, 0, 211))
    embed.set_footer(text=f"{amount} key(s) used | {keys - amount} remaining")
    await interaction.followup.send(embed=embed, ephemeral=True)
    results_log = ", ".join(f"{c}x {n}" for n, c in results.items())
    await log_event(gid, "chest", _log_embed("💎 VIP Chest Opened", discord.Color.from_rgb(148, 0, 211),
        User=interaction.user.mention, Opened=str(amount), Keys_Used=str(amount), Won=results_log[:1024]))
    await _announce_rare(interaction, results, "VIP Chest", rare_names)


class ChestChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⭐ My EXP", style=discord.ButtonStyle.secondary, custom_id="chest_panel:check_exp", row=0)
    async def check_exp(self, interaction: discord.Interaction, btn):
        gid, uid = interaction.guild.id, interaction.user.id
        exp = await get_exp(gid, uid); lvl = await get_level(gid, uid)
        inv = await inventory_get(gid, uid)
        keys = next((q for n, q in inv if n.lower() == VIP_CHEST_KEY.lower()), 0)
        embed = discord.Embed(title=f"⭐ {interaction.user.display_name}", color=discord.Color.gold())
        embed.add_field(name="Activity Rank", value=str(lvl), inline=True)
        embed.add_field(name="Usable EXP", value=f"{exp:,}", inline=True)
        embed.add_field(name="Chests Available", value=f"{exp // CHEST_COST}", inline=True)
        embed.add_field(name="VIP Keys", value=str(keys), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="📦 ×1", style=discord.ButtonStyle.primary, custom_id="chest_panel:open_exp_1", row=0)
    async def open_exp_1(self, interaction: discord.Interaction, btn):
        await _do_open_exp_chests(interaction, 1)

    @discord.ui.button(label="📦 ×10", style=discord.ButtonStyle.primary, custom_id="chest_panel:open_exp_10", row=0)
    async def open_exp_10(self, interaction: discord.Interaction, btn):
        await _do_open_exp_chests(interaction, 10)

    @discord.ui.button(label="📦 ×Max", style=discord.ButtonStyle.primary, custom_id="chest_panel:open_exp_max", row=0)
    async def open_exp_max(self, interaction: discord.Interaction, btn):
        await _do_open_exp_chests(interaction, -1)

    @discord.ui.button(label="💎 VIP ×1", style=discord.ButtonStyle.success, custom_id="chest_panel:open_vip_1", row=1)
    async def open_vip_1(self, interaction: discord.Interaction, btn):
        await _do_open_vip_chests(interaction, 1)

    @discord.ui.button(label="💎 VIP ×5", style=discord.ButtonStyle.success, custom_id="chest_panel:open_vip_5", row=1)
    async def open_vip_5(self, interaction: discord.Interaction, btn):
        await _do_open_vip_chests(interaction, 5)


@bot.tree.command(name="setchestchannel", description="Post the chest panel embed in a channel")
@app_commands.describe(channel="Channel to post the panel in")
@command_enabled()
async def setchestchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await interaction.response.defer()
    gid = interaction.guild.id
    async with get_db() as db:
        async with db.execute("SELECT channel_id, message_id FROM chest_channel_config WHERE guild_id=?", (gid,)) as cur:
            old = await cur.fetchone()
    if old and old[0] and old[0] != channel.id and old[1]:
        old_ch = bot.get_channel(old[0])
        if old_ch:
            try: await (await old_ch.fetch_message(old[1])).delete()
            except Exception: pass
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO chest_channel_config(guild_id,channel_id,message_id) VALUES(?,?,0) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=0",
                (gid, channel.id))
            await db.commit()
    await _refresh_chest_channel(interaction.guild)
    await interaction.followup.send(f"✅ Chest panel posted in {channel.mention}.")

@bot.command(name="setchestchannel")
async def pfx_setchestchannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setchestchannel._callback(FakeInteraction(ctx), channel)

# ═══════════════════════════════════════════════════════
# VIP KEY ADMIN
# ═══════════════════════════════════════════════════════

@bot.command(name="givekey")
async def cmd_givekey(ctx, user: discord.Member, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    await inventory_add(ctx.guild.id, user.id, VIP_CHEST_KEY, amount)
    await ctx.send(f"🔑 Gave **{amount}x {VIP_CHEST_KEY}** to {user.mention}.")
    await log_event(ctx.guild.id, "item", _log_embed("🔑 VIP Key Given", discord.Color.green(),
        Admin=ctx.author.mention, User=user.mention, Keys=str(amount)))

@bot.command(name="takekey")
async def cmd_takekey(ctx, user: discord.Member, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    if not await inventory_remove(ctx.guild.id, user.id, VIP_CHEST_KEY, amount):
        await ctx.send(f"❌ {user.mention} doesn't have {amount}x {VIP_CHEST_KEY}."); return
    await ctx.send(f"🗑 Took **{amount}x {VIP_CHEST_KEY}** from {user.mention}.")
    await log_event(ctx.guild.id, "item", _log_embed("🔑 VIP Key Taken", discord.Color.red(),
        Admin=ctx.author.mention, User=user.mention, Keys=str(amount)))

@bot.command(name="givekeyrole")
async def cmd_givekeyrole(ctx, role: discord.Role, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    members = [m for m in ctx.guild.members if role in m.roles and not m.bot]
    if not members: await ctx.send(f"❌ No non-bot members with {role.mention}."); return
    async with ctx.typing():
        for m in members:
            await inventory_add(ctx.guild.id, m.id, VIP_CHEST_KEY, amount)
    await ctx.send(f"🔑 Gave **{amount}x {VIP_CHEST_KEY}** to **{len(members)}** member(s) with {role.mention}.")

@bot.command(name="takekeyrole")
async def cmd_takekeyrole(ctx, role: discord.Role, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    members = [m for m in ctx.guild.members if role in m.roles and not m.bot]
    if not members: await ctx.send(f"❌ No non-bot members with {role.mention}."); return
    full_taken = partial_taken = skipped = 0
    async with ctx.typing():
        for m in members:
            inv = await inventory_get(ctx.guild.id, m.id)
            owned = {n.lower(): q for n, q in inv}
            current = owned.get(VIP_CHEST_KEY.lower(), 0)
            if current == 0: skipped += 1; continue
            to_take = min(amount, current)
            await inventory_remove(ctx.guild.id, m.id, VIP_CHEST_KEY, to_take)
            if to_take == amount: full_taken += 1
            else: partial_taken += 1
    lines = [f"🗑 Processed **{len(members)}** member(s) with {role.mention}:"]
    if full_taken: lines.append(f"• **{full_taken}** lost the full **{amount}x** key(s)")
    if partial_taken: lines.append(f"• **{partial_taken}** had fewer — lost all their keys")
    if skipped: lines.append(f"• **{skipped}** had no keys (skipped)")
    await ctx.send("\n".join(lines))

# ═══════════════════════════════════════════════════════
# ADMIN ABUSE BOXES
# ═══════════════════════════════════════════════════════

@bot.command(name="addbox")
async def cmd_addbox(ctx, *, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute("INSERT INTO abuse_boxes VALUES(?,?)", (ctx.guild.id, name))
                await db.commit()
            except aiosqlite.IntegrityError:
                await ctx.send(f"❌ Box **{name}** already exists."); return
    await ctx.send(f"✅ Created box **{name}**.")

@bot.command(name="removebox")
async def cmd_removebox(ctx, *, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                                  (ctx.guild.id, name)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Box **{name}** not found."); return
            await db.execute("DELETE FROM abuse_boxes WHERE guild_id=? AND box_name=?", (ctx.guild.id, name))
            await db.execute("DELETE FROM abuse_box_prizes WHERE guild_id=? AND box_name=?", (ctx.guild.id, name))
            await db.commit()
    await ctx.send(f"🗑 Removed box **{name}** and all its prizes.")

@bot.tree.command(name="addboxprize", description="Add a prize to an abuse box")
@app_commands.describe(box="Box name", prize_type="Type of prize", chance="Weight (e.g. 50)",
                       amount="Amount for balance/exp prizes", item_name="Item name for 'item' prizes",
                       custom_label="Label for 'nothing'/'custom'")
@app_commands.choices(prize_type=[
    app_commands.Choice(name="Balance", value="balance"),
    app_commands.Choice(name="EXP", value="exp"),
    app_commands.Choice(name="Item", value="item"),
    app_commands.Choice(name="Nothing", value="nothing"),
    app_commands.Choice(name="Custom", value="custom"),
])
@command_enabled()
async def addboxprize(interaction: discord.Interaction, box: str, prize_type: str, chance: int,
                      amount: int = 0, item_name: str = None, custom_label: str = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if chance <= 0:
        await interaction.response.send_message("❌ Chance must be > 0.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                              (interaction.guild.id, box)) as cur:
            if not await cur.fetchone():
                await interaction.response.send_message(f"❌ Box **{box}** not found.", ephemeral=True); return
    if prize_type in ("balance", "exp"):
        if amount <= 0:
            await interaction.response.send_message("❌ Provide amount > 0.", ephemeral=True); return
        prize_value = str(amount)
    elif prize_type == "item":
        if not item_name:
            await interaction.response.send_message("❌ Provide item_name.", ephemeral=True); return
        item = await get_item(interaction.guild.id, item_name)
        prize_value = item[1] if item else item_name.strip()
    elif prize_type == "nothing":
        prize_value = custom_label or "Nothing"
    else:
        if not custom_label:
            await interaction.response.send_message("❌ Provide custom_label.", ephemeral=True); return
        prize_value = custom_label
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO abuse_box_prizes(guild_id,box_name,prize_type,prize_value,prize_amount,chance) "
                "VALUES(?,?,?,?,?,?)", (interaction.guild.id, box, prize_type, prize_value, amount, chance))
            await db.commit()
    await interaction.response.send_message(f"✅ Added to **{box}**: `{prize_type}` — **{prize_value}** (weight: {chance})")

@bot.command(name="addboxprize")
async def pfx_addboxprize(ctx, box: str, prize_type: str, chance: int,
                           amount: int = 0, item_name: str = None, *, custom_label: str = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if prize_type not in ("balance","exp","item","nothing","custom"):
        await ctx.send("❌ Valid types: balance, exp, item, nothing, custom"); return
    await addboxprize._callback(FakeInteraction(ctx), box, prize_type, chance, amount, item_name, custom_label)

@bot.command(name="removeboxprize")
async def cmd_removeboxprize(ctx, box: str, prize_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT id FROM abuse_box_prizes WHERE id=? AND guild_id=? AND box_name=?",
                                  (prize_id, ctx.guild.id, box)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Prize #{prize_id} not found in **{box}**."); return
            await db.execute("DELETE FROM abuse_box_prizes WHERE id=?", (prize_id,))
            await db.commit()
    await ctx.send(f"🗑 Removed prize #{prize_id} from **{box}**.")

@bot.command(name="listboxes")
async def cmd_listboxes(ctx, *, box: str = None):
    async with get_db() as db:
        query = "SELECT box_name FROM abuse_boxes WHERE guild_id=?" + (" AND box_name=?" if box else "")
        async with db.execute(query, (ctx.guild.id, box) if box else (ctx.guild.id,)) as cur:
            boxes = await cur.fetchall()
    if not boxes: await ctx.send("❌ No boxes found."); return
    embed = discord.Embed(title="📦 Admin Abuse Boxes", color=discord.Color.orange())
    for (box_name,) in boxes:
        async with get_db() as db:
            async with db.execute("SELECT id,prize_type,prize_value,chance FROM abuse_box_prizes "
                                  "WHERE guild_id=? AND box_name=? ORDER BY id", (ctx.guild.id, box_name)) as cur:
                prizes = await cur.fetchall()
        if not prizes: embed.add_field(name=f"📦 {box_name}", value="*No prizes yet*", inline=False); continue
        total_w = sum(p[3] for p in prizes)
        lines = []
        for p_id, p_type, p_value, p_chance in prizes:
            pct = (p_chance / total_w * 100) if total_w > 0 else 0
            desc = (f"💰 {int(p_value):,} coins" if p_type == "balance" else
                    f"⭐ {int(p_value):,} EXP" if p_type == "exp" else
                    f"🎒 {p_value}" if p_type == "item" else f"✨ {p_value}")
            lines.append(f"`#{p_id}` {desc} — **{pct:.1f}%**")
        embed.add_field(name=f"📦 {box_name}", value="\n".join(lines), inline=False)
    await ctx.send(embed=embed)

@bot.command(name="givebox")
async def cmd_givebox(ctx, role: discord.Role, amount: int, *, box: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                              (ctx.guild.id, box)) as cur:
            if not await cur.fetchone(): await ctx.send(f"❌ Box **{box}** not found."); return
    members = [m for m in ctx.guild.members if role in m.roles and not m.bot]
    if not members: await ctx.send(f"❌ No non-bot members with {role.mention}."); return
    async with ctx.typing():
        for m in members:
            await inventory_add(ctx.guild.id, m.id, box, amount)
    await ctx.send(f"✅ Gave **{amount}x {box}** to **{len(members)}** member(s) with {role.mention}.")

@bot.command(name="addrarebox")
async def cmd_addrarebox(ctx, box: str, prize_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                              (ctx.guild.id, box)) as cur:
            if not await cur.fetchone(): await ctx.send(f"❌ Box **{box}** not found."); return
        async with db.execute("SELECT prize_type, prize_value FROM abuse_box_prizes "
                              "WHERE id=? AND guild_id=? AND box_name=?", (prize_id, ctx.guild.id, box)) as cur:
            row = await cur.fetchone()
    if not row: await ctx.send(f"❌ Prize #{prize_id} not found in **{box}**."); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute("INSERT INTO rare_box_config(guild_id,box_name,prize_id) VALUES(?,?,?)",
                                 (ctx.guild.id, box, prize_id))
                await db.commit()
            except aiosqlite.IntegrityError:
                await ctx.send(f"❌ Prize #{prize_id} already marked as rare."); return
    await ctx.send(f"✅ Prize `#{prize_id}` ({row[0]}: **{row[1]}**) in **{box}** is now a rare drop.")

@bot.command(name="removerarebox")
async def cmd_removerarebox(ctx, box: str, prize_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM rare_box_config WHERE guild_id=? AND box_name=? AND prize_id=?",
                             (ctx.guild.id, box, prize_id))
            await db.commit()
    await ctx.send(f"🗑 Prize #{prize_id} in **{box}** is no longer a rare drop.")


@bot.tree.command(name="openbox", description="Open one or more abuse boxes from your inventory")
@app_commands.describe(box="Box name", amount="How many to open (default 1, max 20)")
@command_enabled()
async def openbox(interaction: discord.Interaction, box: str, amount: int = 1):
    await interaction.response.defer()
    if amount <= 0: await interaction.followup.send("❌ Amount must be ≥ 1."); return
    if amount > 20: await interaction.followup.send("❌ Max 20 boxes at once."); return
    inv = await inventory_get(interaction.guild.id, interaction.user.id)
    owned = {n.lower(): (n, q) for n, q in inv}
    if box.lower() not in owned or owned[box.lower()][1] < amount:
        have = owned.get(box.lower(), (box, 0))[1]
        await interaction.followup.send(f"❌ Need {amount}x **{box}** but you only have {have}."); return
    canonical_box = owned[box.lower()][0]
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                              (interaction.guild.id, canonical_box)) as cur:
            if not await cur.fetchone():
                await interaction.followup.send(f"❌ Box **{canonical_box}** no longer exists."); return
        async with db.execute(
            "SELECT id, prize_type, prize_value, prize_amount, chance FROM abuse_box_prizes "
            "WHERE guild_id=? AND box_name=?", (interaction.guild.id, canonical_box)) as cur:
            prizes = await cur.fetchall()
    if not prizes:
        await interaction.followup.send(f"❌ **{canonical_box}** has no prizes configured."); return
    if not await inventory_remove(interaction.guild.id, interaction.user.id, canonical_box, amount):
        await interaction.followup.send("❌ Failed to remove boxes."); return

    rare_ids = await get_rare_box_ids(interaction.guild.id, canonical_box)
    results: dict[str, int] = {}; rare_wins: dict[str, int] = {}
    total_balance = 0; total_exp = 0; item_grants: dict[str, int] = {}

    for _ in range(amount):
        p_id, p_type, p_value, p_amount, _ = random.choices(prizes, weights=[p[4] for p in prizes], k=1)[0]
        if p_type == "balance":
            amt = int(p_value); total_balance += amt; label = f"💰 {amt:,} coins"
        elif p_type == "exp":
            amt = int(p_value); total_exp += amt; label = f"⭐ {amt:,} EXP"
        elif p_type == "item":
            item_grants[p_value] = item_grants.get(p_value, 0) + 1; label = f"🎒 {p_value}"
        elif p_type == "nothing": label = f"😔 {p_value}"
        else: label = f"✨ {p_value}"
        results[label] = results.get(label, 0) + 1
        if p_id in rare_ids: rare_wins[label] = rare_wins.get(label, 0) + 1

    gid = interaction.guild.id
    if total_balance > 0: await add_balance(gid, interaction.user.id, total_balance, bot=bot)
    if total_exp > 0: await add_exp(gid, interaction.user.id, total_exp)
    for iname, qty in item_grants.items():
        si = await get_item(gid, iname)
        await inventory_add(gid, interaction.user.id, si[1] if si else iname, qty)

    result_text = "\n".join(f"• {count}x {desc}" for desc, count in results.items())
    embed = discord.Embed(title=f"📦 {canonical_box} × {amount}", description=result_text, color=discord.Color.orange())
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)
    await log_event(gid, "box", _log_embed("🎁 Box Opened", discord.Color.orange(),
        User=interaction.user.mention, Box=canonical_box, Amount=str(amount)))

    if rare_wins:
        rcid = await get_rare_drop_channel(gid)
        if rcid:
            rc = bot.get_channel(rcid)
            if rc:
                text = " and ".join(f"**{c}x {n}**" for n, c in rare_wins.items())
                re = discord.Embed(title="🎁 Rare Box Drop!",
                    description=f"{interaction.user.mention} pulled {text} from a **{canonical_box}**! 🎉",
                    color=discord.Color.orange())
                re.set_thumbnail(url=interaction.user.display_avatar.url)
                await rc.send(embed=re)

@bot.command(name="openbox")
async def pfx_openbox(ctx, box: str, amount: int = 1):
    await openbox._callback(FakeInteraction(ctx), box, amount)

# ═══════════════════════════════════════════════════════
# MEGA RAFFLE
# ═══════════════════════════════════════════════════════

async def _process_mega_ticket_message(message: discord.Message):
    if not message.guild: return
    gid = message.guild.id
    if not await is_system_enabled(gid, "mega"): return
    content = message.content.strip()
    if not content or content.startswith(common._BOT_PREFIX): return
    if len(content.split()) < 3: return
    await add_tickets(gid, message.author.id, 1)


@bot.tree.command(name="buytickets",
                  description="Buy mega raffle tickets (capped per round; chat 3+ words to earn unlimited)")
@command_enabled()
async def buytickets(interaction: discord.Interaction, amount: int):
    gid, uid = interaction.guild.id, interaction.user.id
    if not await is_system_enabled(gid, "mega"):
        await interaction.response.send_message("❌ Mega raffle system is disabled.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0."); return
    async with get_db() as db:
        async with db.execute("SELECT bought FROM mega_bought WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
            row = await cur.fetchone()
    current_bought = row[0] if row else 0
    remaining_cap = MEGA_TICKET_CAP - current_bought
    if remaining_cap <= 0:
        await interaction.response.send_message(
            f"❌ You've already bought the maximum **{MEGA_TICKET_CAP}** mega tickets this round."); return
    if amount > remaining_cap:
        await interaction.response.send_message(
            f"❌ You can only buy **{remaining_cap}** more mega ticket(s) this round (max {MEGA_TICKET_CAP})."); return
    price = amount * MEGA_TICKET_PRICE
    bal = await get_balance(gid, uid)
    if bal < price:
        await interaction.response.send_message("❌ Not enough balance."); return
    await add_balance(gid, uid, -price, bot=bot)
    await add_tickets(gid, uid, amount)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO mega_bought(guild_id,user_id,bought) VALUES(?,?,?) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET bought=bought+excluded.bought",
                (gid, uid, amount))
            await db.commit()
    from common import add_stat
    await add_stat(gid, uid, "mega_tickets_bought", amount)
    user_tickets = await get_tickets(gid, uid)
    async with get_db() as db:
        async with db.execute("SELECT SUM(tickets) FROM mega_tickets WHERE guild_id=?", (gid,)) as cur:
            total = (await cur.fetchone())[0] or 0
    chance = (user_tickets / total * 100) if total > 0 else 0
    await interaction.response.send_message(
        f"🎟 Bought {amount} mega ticket(s) for {price:,} coins.\n"
        f"You now have **{user_tickets}** mega ticket(s) total "
        f"({current_bought + amount}/{MEGA_TICKET_CAP} bought this round).\n"
        f"Win chance: **{chance:.2f}%**")
    await log_event(gid, "mega", _log_embed("🎟 Mega Tickets Purchased", discord.Color.gold(),
        User=interaction.user.mention, Tickets=str(amount), Cost=f"{price:,} coins"))

@bot.command(name="buytickets")
async def pfx_buytickets(ctx, amount: int):
    await buytickets._callback(FakeInteraction(ctx), amount)

@bot.command(name="addtickets")
async def cmd_addtickets(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await add_tickets(ctx.guild.id, user.id, amount)
    await ctx.send(f"✅ Added {amount} tickets to {user.mention}.")

@bot.command(name="removetickets")
async def cmd_removetickets(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await add_tickets(ctx.guild.id, user.id, -amount)
    await ctx.send(f"❌ Removed {amount} tickets from {user.mention}.")


@bot.tree.command(name="megachance", description="Check mega ticket count and win probability")
@command_enabled()
async def megachance(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    tickets = await get_tickets(interaction.guild.id, user.id)
    async with get_db() as db:
        async with db.execute("SELECT SUM(tickets) FROM mega_tickets WHERE guild_id=?", (interaction.guild.id,)) as cur:
            total = (await cur.fetchone())[0] or 0
    chance = (tickets / total * 100) if total > 0 else 0
    embed = discord.Embed(title="🎟 Mega Raffle Stats", color=discord.Color.gold())
    embed.add_field(name="User", value=user.mention, inline=False)
    embed.add_field(name="Tickets", value=f"{tickets:,}", inline=False)
    embed.add_field(name="Win Chance", value=f"{chance:.2f}%", inline=False)
    embed.add_field(name="Total Pool", value=f"{total:,}", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.command(name="megachance")
async def pfx_megachance(ctx, user: discord.Member = None):
    await megachance._callback(FakeInteraction(ctx), user)


@bot.tree.command(name="setmegachannel", description="Set the channel where mega raffle winners are announced")
@command_enabled()
async def setmegachannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO mega_announce_config VALUES(?,?)", (interaction.guild.id, channel.id))
            await db.commit()
    await interaction.response.send_message(f"✅ Mega raffle announcements → {channel.mention}")

@bot.command(name="setmegachannel")
async def pfx_setmegachannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setmegachannel._callback(FakeInteraction(ctx), channel)


_DENOM_CHOICES = [
    app_commands.Choice(name="Total Tickets (bought + earned combined)", value="total"),
    app_commands.Choice(name="Tickets Bought Only", value="bought"),
]

@bot.tree.command(name="setmegapayout", description="Configure the mega raffle payout formula")
@app_commands.describe(
    denominator_mode="What the ticket pool is divided by in the payout formula",
    payout_multiplier="Multiplier — must be > 0",
    winners="Winners for the daily draw. Leave blank to keep current.")
@app_commands.choices(denominator_mode=_DENOM_CHOICES)
@command_enabled()
async def setmegapayout(interaction: discord.Interaction, denominator_mode: str,
                        payout_multiplier: float, winners: Optional[int] = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if payout_multiplier <= 0:
        await interaction.response.send_message("❌ Multiplier must be greater than 0.", ephemeral=True); return
    if winners is not None and winners < 1:
        await interaction.response.send_message("❌ Winners must be ≥ 1.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT winners FROM mega_payout_config WHERE guild_id=?", (interaction.guild.id,)) as cur:
            existing = await cur.fetchone()
    final_winners = winners if winners is not None else (existing[0] if existing else 1)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO mega_payout_config(guild_id,denominator_mode,payout_multiplier,winners) "
                "VALUES(?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET "
                "denominator_mode=excluded.denominator_mode, payout_multiplier=excluded.payout_multiplier, "
                "winners=excluded.winners",
                (interaction.guild.id, denominator_mode, payout_multiplier, final_winners))
            await db.commit()
    denom_label = "total tickets in the pool" if denominator_mode == "total" else "tickets that were actually bought"
    await interaction.response.send_message(
        f"✅ Mega raffle payout updated:\n"
        f"`{denom_label} × {MEGA_TICKET_PRICE:,} × {payout_multiplier} ÷ {final_winners} winner(s)`\n")

@bot.command(name="setmegapayout")
async def pfx_setmegapayout(ctx, denominator_mode: str, payout_multiplier: float, winners: int = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    denominator_mode = denominator_mode.strip().lower()
    if denominator_mode not in ("total","bought"):
        await ctx.send("❌ denominator_mode must be `total` or `bought`."); return
    await setmegapayout._callback(FakeInteraction(ctx), denominator_mode, payout_multiplier, winners)


def build_mega_info_embed(guild, total_tickets, top_entries, prev=None):
    now = datetime.now(UTC)
    target = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= target: target += timedelta(days=1)
    end_ts = int(target.timestamp())
    embed = discord.Embed(title="🎟 Live Mega Raffle Status", color=discord.Color.gold())
    embed.add_field(name="⏰ Next Draw", value=f"<t:{end_ts}:R> (<t:{end_ts}:F>)", inline=False)
    embed.add_field(name="🎫 Total Tickets", value=f"{total_tickets:,}", inline=False)
    if top_entries:
        medals = ["🥇","🥈","🥉"]
        lines = []
        for i, (uid, t) in enumerate(top_entries[:5]):
            medal = medals[i] if i < 3 else f"#{i+1}"
            member = guild.get_member(uid)
            name = member.display_name if member else f"<@{uid}>"
            chance = (t / total_tickets * 100) if total_tickets > 0 else 0
            lines.append(f"{medal} **{name}** — {t:,} tickets ({chance:.1f}%)")
        embed.add_field(name="🏆 Top Participants", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🏆 Top Participants", value="No tickets yet.", inline=False)
    if prev:
        draw_dt = datetime.fromtimestamp(prev["ts"], UTC)
        date_str = draw_dt.strftime("%Y-%m-%d %H:%M UTC")
        winners_list = prev.get("winners") or [[prev["winner_id"], None]]
        win_lines = []
        for w_uid, w_amt in winners_list:
            m = guild.get_member(w_uid)
            mn = m.display_name if m else f"<@{w_uid}>"
            win_lines.append(f"🏆 **{mn}**" + (f" — {w_amt:,} coins" if w_amt else ""))
        lines2 = win_lines + [f"📊 Pool: {prev['total']:,} tickets"]
        for i, (uid, t) in enumerate(prev.get("top", [])[:3]):
            m = guild.get_member(uid)
            mn = m.display_name if m else f"<@{uid}>"
            pct2 = (t / prev["total"] * 100) if prev["total"] else 0
            lines2.append(f"{'🥇🥈🥉'[i]} {mn} — {t:,} ({pct2:.1f}%)")
        embed.add_field(name=f"📜 Previous Mega Raffle Draw ({date_str})", value="\n".join(lines2), inline=False)
    embed.set_footer(text=f"Updated: {datetime.now(UTC).strftime('%H:%M:%S UTC')}")
    return embed


@bot.tree.command(name="setmegainfochannel", description="Set channel for the live mega raffle status board")
@command_enabled()
async def setmegainfochannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT user_id,tickets FROM mega_tickets WHERE guild_id=? ORDER BY tickets DESC LIMIT 5",
                              (interaction.guild.id,)) as cur:
            top = await cur.fetchall()
        async with db.execute("SELECT SUM(tickets) FROM mega_tickets WHERE guild_id=?", (interaction.guild.id,)) as cur:
            total = (await cur.fetchone())[0] or 0
    embed = build_mega_info_embed(interaction.guild, total, top)
    info_msg = await channel.send(embed=embed)
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO mega_info_config VALUES(?,?,?)",
                             (interaction.guild.id, channel.id, info_msg.id))
            await db.commit()
    await interaction.response.send_message(f"✅ Live mega raffle board posted in {channel.mention}.")

@bot.command(name="setmegainfochannel")
async def pfx_setmegainfochannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setmegainfochannel._callback(FakeInteraction(ctx), channel)


async def mega_info_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        async with get_db() as db:
            async with db.execute("SELECT guild_id,channel_id,message_id FROM mega_info_config") as cur:
                configs = await cur.fetchall()
        for guild_id, channel_id, message_id in configs:
            try:
                guild = bot.get_guild(guild_id)
                channel = bot.get_channel(channel_id)
                if not guild or not channel: continue
                async with get_db() as db:
                    async with db.execute(
                        "SELECT user_id,tickets FROM mega_tickets WHERE guild_id=? ORDER BY tickets DESC LIMIT 5",
                        (guild_id,)) as cur:
                        top = await cur.fetchall()
                    async with db.execute("SELECT SUM(tickets) FROM mega_tickets WHERE guild_id=?", (guild_id,)) as cur:
                        total = (await cur.fetchone())[0] or 0
                    async with db.execute(
                        "SELECT draw_timestamp,winner_id,winner_tickets,total_tickets,top_json,winners_json "
                        "FROM mega_history WHERE guild_id=? ORDER BY draw_timestamp DESC LIMIT 1", (guild_id,)) as cur:
                        h = await cur.fetchone()
                prev = None
                if h:
                    ts, wid, wt, tot, tj, wj = h
                    prev = {"ts": ts, "winner_id": wid, "winner_tickets": wt, "total": tot,
                            "top": json.loads(tj) if tj else [], "winners": json.loads(wj) if wj else []}
                embed = build_mega_info_embed(guild, total, top, prev)
                try:
                    msg = await channel.fetch_message(message_id)
                    await msg.edit(embed=embed)
                except discord.NotFound:
                    new_msg = await channel.send(embed=embed)
                    async with db_lock:
                        async with get_db() as db:
                            await db.execute("UPDATE mega_info_config SET message_id=? WHERE guild_id=?",
                                             (new_msg.id, guild_id))
                            await db.commit()
            except Exception as e:
                print(f"[MegaInfoLoop] {guild_id}: {e}")
        await asyncio.sleep(60)


async def mega_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(UTC)
        target = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if now >= target: target += timedelta(days=1)
        print(f"[Mega] Next draw in {(target-now).total_seconds():.0f}s")
        await asyncio.sleep((target - now).total_seconds())

        for guild in bot.guilds:
            if not await is_system_enabled(guild.id, "mega"): continue
            async with get_db() as db:
                async with db.execute(
                    "SELECT user_id,tickets FROM mega_tickets WHERE guild_id=? AND tickets>0 ORDER BY tickets DESC",
                    (guild.id,)) as cur:
                    entries = await cur.fetchall()
            if not entries: continue

            total = sum(t for _, t in entries)
            async with get_db() as db:
                async with db.execute("SELECT denominator_mode,payout_multiplier,winners FROM mega_payout_config WHERE guild_id=?",
                                      (guild.id,)) as cur:
                    payout_cfg = await cur.fetchone()
            denom_mode, multiplier, winners_count = payout_cfg if payout_cfg else ("total", 1.0, 1)

            if denom_mode == "bought":
                async with get_db() as db:
                    async with db.execute("SELECT COALESCE(SUM(bought),0) FROM mega_bought WHERE guild_id=?", (guild.id,)) as cur:
                        denominator = (await cur.fetchone())[0]
                if denominator <= 0: denominator = 1
            else:
                denominator = total if total > 0 else 1

            total_payout = denominator * MEGA_TICKET_PRICE * multiplier
            per_winner = round(total_payout / winners_count)

            winner_ids = _weighted_sample_without_replacement(entries, winners_count)
            top5 = entries[:5]

            for wid in winner_ids:
                await add_balance(guild.id, wid, per_winner, bot=bot)

            winners_payload = [[wid, per_winner] for wid in winner_ids]
            async with db_lock:
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO mega_history(guild_id,draw_timestamp,winner_id,winner_tickets,"
                        "total_tickets,top_json,winners_json) VALUES(?,?,?,?,?,?,?)",
                        (guild.id, int(datetime.now(UTC).timestamp()),
                         winner_ids[0] if winner_ids else 0,
                         next((t for uid, t in entries if winner_ids and uid == winner_ids[0]), 0),
                         total, json.dumps([[uid, t] for uid, t in top5]), json.dumps(winners_payload)))
                    await db.commit()

            async with get_db() as db:
                async with db.execute("SELECT channel_id FROM mega_announce_config WHERE guild_id=?", (guild.id,)) as cur:
                    row = await cur.fetchone()
            ann = bot.get_channel(row[0]) if row else guild.system_channel
            if ann and winner_ids:
                mentions = ", ".join(f"<@{wid}>" for wid in winner_ids)
                await ann.send(f"🎉 {mentions} won the daily **Mega Raffle** and will each receive **{per_winner:,} coins**!")
                await log_event(guild.id, "mega", _log_embed(
                    "🎟 Mega Raffle Draw", discord.Color.gold(),
                    Winners=mentions, Per_Winner=f"{per_winner:,}",
                    Total_Pool=f"{total:,} tickets", Guild=guild.name))

            async with db_lock:
                async with get_db() as db:
                    await db.execute("DELETE FROM mega_tickets WHERE guild_id=?", (guild.id,))
                    await db.execute("DELETE FROM mega_bought WHERE guild_id=?", (guild.id,))
                    await db.commit()


@bot.command(name="checkmegahistory")
async def cmd_checkmegahistory(ctx):
    async with get_db() as db:
        async with db.execute(
            "SELECT draw_timestamp,winner_id,winner_tickets,total_tickets,top_json,winners_json "
            "FROM mega_history WHERE guild_id=? ORDER BY draw_timestamp DESC LIMIT 10", (ctx.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows: await ctx.send("❌ No mega raffle history yet."); return
    embed = discord.Embed(title="📜 Recent Mega Raffle Draws (last 10)", color=discord.Color.gold())
    for ts, wid, wt, tot, tj, wj in rows:
        draw_dt = datetime.fromtimestamp(ts, UTC)
        date_str = draw_dt.strftime("%Y-%m-%d %H:%M UTC")
        try: winners_list = json.loads(wj) if wj else [[wid, None]]
        except Exception: winners_list = [[wid, None]]
        names = []
        for w_uid, w_amt in winners_list:
            m = ctx.guild.get_member(w_uid)
            nm = m.display_name if m else "*[Left Server]*"
            names.append(nm + (f" (+{w_amt:,})" if w_amt else ""))
        embed.add_field(name=f"🗓 {date_str}", value=f"🏆 {', '.join(names)} | Pool: {tot:,} tickets", inline=False)
    await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════
# POWER GIVEAWAYS (multi-named)
# ═══════════════════════════════════════════════════════

async def _process_power_giveaway_message(message: discord.Message):
    if not message.guild: return
    if message.content.startswith(common._BOT_PREFIX): return
    gid = message.guild.id
    async with get_db() as db:
        async with db.execute("SELECT name FROM power_giveaway_config WHERE guild_id=? AND running=1", (gid,)) as cur:
            active_names = [r[0] for r in await cur.fetchall()]
    if not active_names: return
    for name in active_names:
        async with get_db() as db:
            async with db.execute(
                "SELECT entries_per_message FROM power_giveaway_channel_rates "
                "WHERE guild_id=? AND name=? AND channel_id=?", (gid, name, message.channel.id)) as cur:
                rate_row = await cur.fetchone()
        if not rate_row or rate_row[0] == 0: continue
        base_gain = rate_row[0]
        bonus = 0.0
        if isinstance(message.author, discord.Member):
            role_ids = {r.id for r in message.author.roles}
            if role_ids:
                placeholders = ",".join("?" * len(role_ids))
                async with get_db() as db:
                    async with db.execute(
                        f"SELECT multiplier FROM power_giveaway_role_boosts "
                        f"WHERE guild_id=? AND name=? AND role_id IN ({placeholders})",
                        (gid, name, *role_ids)) as cur:
                        boost_rows = await cur.fetchall()
                bonus = sum(max(0.0, m - 1) for (m,) in boost_rows)
        final_gain = base_gain * (1 + bonus)
        async with db_lock:
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO power_giveaway_user_entries(guild_id,name,user_id,entries) VALUES(?,?,?,?) "
                    "ON CONFLICT(guild_id,name,user_id) DO UPDATE SET entries=entries+excluded.entries",
                    (gid, name, message.author.id, final_gain))
                await db.commit()


async def _compute_power_entries(guild: discord.Guild, name: str, default_entries: int) -> dict:
    gid = guild.id
    async with get_db() as db:
        async with db.execute("SELECT role_id,entries FROM power_giveaway_role_entries WHERE guild_id=? AND name=?",
                              (gid, name)) as cur:
            role_entry_map = dict(await cur.fetchall())
        async with db.execute("SELECT user_id,entries FROM power_giveaway_user_entries WHERE guild_id=? AND name=?",
                              (gid, name)) as cur:
            chan_entries = dict(await cur.fetchall())
    totals: dict[int, float] = {}
    for member in guild.members:
        if member.bot: continue
        total = float(default_entries)
        for role in member.roles:
            if role.id in role_entry_map: total += role_entry_map[role.id]
        total += chan_entries.get(member.id, 0.0)
        if total > 0: totals[member.id] = total
    return totals


async def _run_power_giveaway_roll(guild: discord.Guild, cfg: tuple):
    (gid, name, prize, winners_count, interval_seconds, embed_ch_id, winners_ch_id,
     default_entries, rb, re_, rt, rgt, rvk, rrole, ritem, riqty,
     running, embed_msg_id, next_roll) = cfg
    totals = await _compute_power_entries(guild, name, default_entries)
    winners_ch = bot.get_channel(winners_ch_id)
    if not totals:
        if winners_ch:
            try: await winners_ch.send(f"🔄 **{name}** rolled, but nobody has any entries — no winners this round.")
            except Exception: pass
    else:
        winner_ids = _weighted_sample_without_replacement(list(totals.items()), winners_count)
        winner_members = [m for m in (guild.get_member(uid) for uid in winner_ids) if m]
        meta = {"label": prize, "balance": rb, "exp": re_, "tickets": rt, "gamble_tokens": rgt,
                "vip_keys": rvk, "role_id": rrole, "item": ritem, "item_qty": riqty if ritem else 0}
        if winner_members: await distribute_prizes(guild, winner_members, meta)
        if winners_ch and winner_members:
            mentions = ", ".join(m.mention for m in winner_members)
            reward_str = build_reward_summary(meta, guild)
            embed = discord.Embed(title=f"🎊 {name} — Winners!",
                                  description=f"**Prize:** {prize}\n**Reward:** {reward_str}\n**Winner(s):** {mentions}",
                                  color=discord.Color.gold())
            try: await winners_ch.send(embed=embed)
            except Exception: pass
        await log_event(gid, "giveaway", _log_embed(f"🎊 Recurring Giveaway Rolled — {name}", discord.Color.gold(),
            Prize=prize, Winners=str(len(winner_members))))
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM power_giveaway_user_entries WHERE guild_id=? AND name=?", (gid, name))
            await db.commit()


async def _build_power_giveaway_embed(guild: discord.Guild, cfg: tuple) -> discord.Embed:
    (gid, name, prize, winners_count, interval_seconds, embed_ch_id, winners_ch_id,
     default_entries, rb, re_, rt, rgt, rvk, rrole, ritem, riqty, running, embed_msg_id, next_roll) = cfg
    meta = {"balance": rb, "exp": re_, "tickets": rt, "gamble_tokens": rgt, "vip_keys": rvk,
            "role_id": rrole, "item": ritem, "item_qty": riqty if ritem else 0}
    reward_str = build_reward_summary(meta, guild)
    totals = await _compute_power_entries(guild, name, default_entries)
    pool_total = sum(totals.values()); participants = len(totals)
    async with get_db() as db:
        async with db.execute("SELECT role_id,entries FROM power_giveaway_role_entries "
                              "WHERE guild_id=? AND name=? ORDER BY entries DESC", (gid, name)) as cur:
            role_rows = await cur.fetchall()
        async with db.execute("SELECT channel_id,entries_per_message FROM power_giveaway_channel_rates "
                              "WHERE guild_id=? AND name=?", (gid, name)) as cur:
            chan_rows = await cur.fetchall()
        async with db.execute("SELECT role_id,multiplier FROM power_giveaway_role_boosts WHERE guild_id=? AND name=?",
                              (gid, name)) as cur:
            boost_rows = await cur.fetchall()
    embed = discord.Embed(title=f"🔥 Recurring Giveaway — {name}",
                          description=f"**Prize:** {prize}\n**Reward:** {reward_str}\n**Winners:** {winners_count}",
                          color=discord.Color.red())
    embed.add_field(name="⏰ Next Roll", value=f"<t:{next_roll}:R>" if running else "⏸ Stopped", inline=False)
    embed.add_field(name="🎟 Current Pool", value=f"{pool_total:,.1f} entries across {participants:,} participant(s)", inline=False)
    if default_entries: embed.add_field(name="👤 Base Entries", value=f"Everyone starts with **{default_entries}**", inline=False)
    if role_rows:
        lines = [f"• {r.mention} — **+{ent}** entries" for rid, ent in role_rows if (r := guild.get_role(rid))]
        if lines: embed.add_field(name="🎭 Role Entries", value="\n".join(lines), inline=False)
    if chan_rows:
        lines = [f"• {c.mention} — **+{rate}** per message" for cid, rate in chan_rows if (c := guild.get_channel(cid))]
        if lines: embed.add_field(name="💬 Chat Entries", value="\n".join(lines), inline=False)
    if boost_rows:
        lines = [f"• {r.mention} — **×{mult}** chat entries" for rid, mult in boost_rows if (r := guild.get_role(rid))]
        if lines: embed.add_field(name="🚀 Entry Boosts", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"Giveaway ID: {name} · Everyone is automatically entered!")
    return embed


async def _refresh_power_giveaway_embed(guild: discord.Guild, cfg: tuple):
    gid, name, embed_ch_id, embed_msg_id = cfg[0], cfg[1], cfg[5], cfg[17]
    channel = bot.get_channel(embed_ch_id)
    if not channel: return
    embed = await _build_power_giveaway_embed(guild, cfg)
    if embed_msg_id:
        try:
            msg = await channel.fetch_message(embed_msg_id)
            await msg.edit(embed=embed); return
        except discord.NotFound: pass
    new_msg = await channel.send(embed=embed)
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE power_giveaway_config SET embed_message_id=? WHERE guild_id=? AND name=?",
                             (new_msg.id, gid, name))
            await db.commit()


async def power_giveaway_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            async with get_db() as db:
                async with db.execute(
                    "SELECT guild_id,name,prize,winners,interval_seconds,embed_channel_id,"
                    "winners_channel_id,default_entries,reward_balance,reward_exp,reward_tickets,"
                    "reward_gamble_tokens,reward_vip_keys,reward_role_id,reward_item,reward_item_qty,"
                    "running,embed_message_id,next_roll_time "
                    "FROM power_giveaway_config WHERE running=1") as cur:
                    configs = await cur.fetchall()
            now = int(datetime.now(UTC).timestamp())
            for cfg in configs:
                gid, name = cfg[0], cfg[1]
                guild = bot.get_guild(gid)
                if not guild: continue
                try:
                    if now >= cfg[18]:
                        await _run_power_giveaway_roll(guild, cfg)
                        new_next = now + cfg[4]
                        async with db_lock:
                            async with get_db() as db:
                                await db.execute(
                                    "UPDATE power_giveaway_config SET next_roll_time=? WHERE guild_id=? AND name=?",
                                    (new_next, gid, name))
                                await db.commit()
                        cfg = cfg[:18] + (new_next,)
                    await _refresh_power_giveaway_embed(guild, cfg)
                except Exception as e:
                    print(f"[PowerGiveaway] guild {gid} name {name}: {e}")
        except Exception as e:
            print(f"[PowerGiveaway] loop error: {e}")
        await asyncio.sleep(30)


power_group = app_commands.Group(name="powergiveaway", description="Recurring giveaways — run several at once")
bot.tree.add_command(power_group)

@power_group.command(name="setup", description="Create or update a named recurring giveaway")
@app_commands.describe(
    name="Unique ID for this giveaway (e.g. 'booster20', 'chatrewards')",
    prize="Prize description", winners="Winners per roll", interval_seconds="Seconds between rolls",
    embed_channel="Live info embed channel", winners_channel="Winners announcement channel",
    default_entries="Base entries for everyone (default 0)",
    reward_balance="Coins per winner", reward_exp="EXP per winner",
    reward_tickets="Mega tickets per winner", reward_gamble_tokens="Gamble tokens per winner",
    reward_vip_keys="VIP keys per winner", reward_role="Role given to each winner",
    reward_item="Item/box per winner", reward_item_qty="Quantity of item (default 1)")
async def power_setup(interaction: discord.Interaction, name: str, prize: str, winners: int,
                       interval_seconds: int, embed_channel: discord.TextChannel,
                       winners_channel: discord.TextChannel, default_entries: int = 0,
                       reward_balance: int = 0, reward_exp: int = 0, reward_tickets: int = 0,
                       reward_gamble_tokens: int = 0, reward_vip_keys: int = 0,
                       reward_role: discord.Role = None, reward_item: str = None, reward_item_qty: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if winners < 1 or interval_seconds < 30:
        await interaction.response.send_message("❌ Winners must be ≥ 1 and interval ≥ 30 seconds.", ephemeral=True); return
    name = name.strip().lower()
    if not name:
        await interaction.response.send_message("❌ Name cannot be empty.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO power_giveaway_config(guild_id,name,prize,winners,interval_seconds,"
                "embed_channel_id,winners_channel_id,default_entries,reward_balance,reward_exp,"
                "reward_tickets,reward_gamble_tokens,reward_vip_keys,reward_role_id,reward_item,"
                "reward_item_qty,running,embed_message_id,next_roll_time) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0) "
                "ON CONFLICT(guild_id,name) DO UPDATE SET "
                "prize=excluded.prize,winners=excluded.winners,interval_seconds=excluded.interval_seconds,"
                "embed_channel_id=excluded.embed_channel_id,winners_channel_id=excluded.winners_channel_id,"
                "default_entries=excluded.default_entries,reward_balance=excluded.reward_balance,"
                "reward_exp=excluded.reward_exp,reward_tickets=excluded.reward_tickets,"
                "reward_gamble_tokens=excluded.reward_gamble_tokens,reward_vip_keys=excluded.reward_vip_keys,"
                "reward_role_id=excluded.reward_role_id,reward_item=excluded.reward_item,"
                "reward_item_qty=excluded.reward_item_qty",
                (interaction.guild.id, name, prize, winners, interval_seconds, embed_channel.id, winners_channel.id,
                 default_entries, reward_balance, reward_exp, reward_tickets, reward_gamble_tokens,
                 reward_vip_keys, reward_role.id if reward_role else 0, reward_item, reward_item_qty))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Power giveaway **{name}** configured: **{prize}**, {winners} winner(s) every {interval_seconds}s.\n"
        f"Use `/powergiveaway start name:{name}` to begin.")

@power_group.command(name="setrole", description="Set how many entries a role gives in a named power giveaway")
@app_commands.describe(name="Which power giveaway", role="Role to configure", entries="Fixed entries this role grants")
async def power_setrole(interaction: discord.Interaction, name: str, role: discord.Role, entries: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if entries < 0:
        await interaction.response.send_message("❌ Entries must be ≥ 0.", ephemeral=True); return
    name = name.strip().lower()
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO power_giveaway_role_entries(guild_id,name,role_id,entries) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id,name,role_id) DO UPDATE SET entries=excluded.entries",
                (interaction.guild.id, name, role.id, entries))
            await db.commit()
    await interaction.response.send_message(f"✅ In **{name}**, {role.mention} now grants **{entries}** entries.")

@power_group.command(name="removerole", description="Remove a role's fixed entries from a named power giveaway")
async def power_removerole(interaction: discord.Interaction, name: str, role: discord.Role):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    name = name.strip().lower()
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM power_giveaway_role_entries WHERE guild_id=? AND name=? AND role_id=?",
                             (interaction.guild.id, name, role.id))
            await db.commit()
    await interaction.response.send_message(f"🗑 {role.mention} no longer grants entries in **{name}**.")

@power_group.command(name="setchannel", description="Set how many entries chatting in a channel gives")
@app_commands.describe(name="Which power giveaway", channel="Channel to configure", entries="Entries per message")
async def power_setchannel(interaction: discord.Interaction, name: str, channel: discord.TextChannel, entries: float):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    name = name.strip().lower()
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO power_giveaway_channel_rates(guild_id,name,channel_id,entries_per_message) "
                "VALUES(?,?,?,?) ON CONFLICT(guild_id,name,channel_id) "
                "DO UPDATE SET entries_per_message=excluded.entries_per_message",
                (interaction.guild.id, name, channel.id, entries))
            await db.commit()
    await interaction.response.send_message(f"✅ In **{name}**, {channel.mention} now grants **{entries}** entries per message.")

@power_group.command(name="removechannel", description="Remove a channel's entry rate from a named power giveaway")
async def power_removechannel(interaction: discord.Interaction, name: str, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    name = name.strip().lower()
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM power_giveaway_channel_rates WHERE guild_id=? AND name=? AND channel_id=?",
                             (interaction.guild.id, name, channel.id))
            await db.commit()
    await interaction.response.send_message(f"🗑 {channel.mention} no longer grants entries in **{name}**.")

@power_group.command(name="setboost", description="Give a role a multiplier on chat-earned entries")
@app_commands.describe(name="Which power giveaway", role="Role to boost", multiplier="e.g. 2.0 = double chat entries")
async def power_setboost(interaction: discord.Interaction, name: str, role: discord.Role, multiplier: float):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if multiplier <= 0:
        await interaction.response.send_message("❌ Multiplier must be > 0.", ephemeral=True); return
    name = name.strip().lower()
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO power_giveaway_role_boosts(guild_id,name,role_id,multiplier) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id,name,role_id) DO UPDATE SET multiplier=excluded.multiplier",
                (interaction.guild.id, name, role.id, multiplier))
            await db.commit()
    await interaction.response.send_message(f"✅ In **{name}**, {role.mention} now earns **×{multiplier}** chat entries.")

@power_group.command(name="removeboost", description="Remove a role's entry boost from a named power giveaway")
async def power_removeboost(interaction: discord.Interaction, name: str, role: discord.Role):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    name = name.strip().lower()
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM power_giveaway_role_boosts WHERE guild_id=? AND name=? AND role_id=?",
                             (interaction.guild.id, name, role.id))
            await db.commit()
    await interaction.response.send_message(f"🗑 {role.mention} no longer has an entry boost in **{name}**.")

@power_group.command(name="start", description="Start a named recurring giveaway")
async def power_start(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    gid = interaction.guild.id; name = name.strip().lower()
    async with get_db() as db:
        async with db.execute("SELECT interval_seconds FROM power_giveaway_config WHERE guild_id=? AND name=?",
                              (gid, name)) as cur:
            row = await cur.fetchone()
    if not row:
        await interaction.response.send_message(
            f"❌ No power giveaway named **{name}** — run `/powergiveaway setup` first.", ephemeral=True); return
    now = int(datetime.now(UTC).timestamp())
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE power_giveaway_config SET running=1,next_roll_time=? WHERE guild_id=? AND name=?",
                             (now + row[0], gid, name))
            await db.commit()
    await interaction.response.send_message(f"✅ Power giveaway **{name}** started!")

@power_group.command(name="stop", description="Stop a named recurring giveaway")
async def power_stop(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    name = name.strip().lower()
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE power_giveaway_config SET running=0 WHERE guild_id=? AND name=?",
                             (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"🛑 Power giveaway **{name}** stopped.")

@power_group.command(name="status", description="View a named power giveaway's full config and current pool")
async def power_status(interaction: discord.Interaction, name: str):
    gid = interaction.guild.id; name = name.strip().lower()
    async with get_db() as db:
        async with db.execute(
            "SELECT guild_id,name,prize,winners,interval_seconds,embed_channel_id,winners_channel_id,"
            "default_entries,reward_balance,reward_exp,reward_tickets,reward_gamble_tokens,reward_vip_keys,"
            "reward_role_id,reward_item,reward_item_qty,running,embed_message_id,next_roll_time "
            "FROM power_giveaway_config WHERE guild_id=? AND name=?", (gid, name)) as cur:
            cfg = await cur.fetchone()
    if not cfg:
        await interaction.response.send_message(f"❌ No power giveaway named **{name}**.", ephemeral=True); return
    embed = await _build_power_giveaway_embed(interaction.guild, cfg)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@power_group.command(name="list", description="List every power giveaway configured in this server")
async def power_list(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute("SELECT name,prize,running,winners FROM power_giveaway_config WHERE guild_id=?",
                              (interaction.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("❌ No power giveaways configured yet.", ephemeral=True); return
    embed = discord.Embed(title="🔥 Power Giveaways in this Server", color=discord.Color.red())
    for name, prize, running, winners in rows:
        status = "✅ Running" if running else "⏸ Stopped"
        embed.add_field(name=name, value=f"{prize} | {winners} winner(s) | {status}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@power_group.command(name="delete", description="Permanently delete a named power giveaway and all its settings")
async def power_delete(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    name = name.strip().lower(); gid = interaction.guild.id
    async with db_lock:
        async with get_db() as db:
            for tbl in ("power_giveaway_config","power_giveaway_role_entries","power_giveaway_channel_rates",
                        "power_giveaway_role_boosts","power_giveaway_user_entries"):
                await db.execute(f"DELETE FROM {tbl} WHERE guild_id=? AND name=?", (gid, name))
            await db.commit()
    await interaction.response.send_message(f"🗑 Power giveaway **{name}** deleted permanently.")


@bot.group(name="powergiveaway", invoke_without_command=True)
async def pfx_powergiveaway(ctx):
    p = common._BOT_PREFIX
    await ctx.send(f"Use `{p}powergiveaway setup/setrole/removerole/setchannel/removechannel/"
                   f"setboost/removeboost/start/stop/status/list/delete <name> ...`")

@pfx_powergiveaway.command(name="setup")
async def pfx_power_setup(ctx, name: str, prize: str, winners: int, interval_seconds: int,
                          embed_channel: discord.TextChannel, winners_channel: discord.TextChannel,
                          default_entries: int = 0, reward_balance: int = 0, reward_exp: int = 0):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await power_setup._callback(FakeInteraction(ctx), name, prize, winners, interval_seconds,
                                embed_channel, winners_channel, default_entries, reward_balance, reward_exp,
                                0, 0, 0, None, None, 1)

@pfx_powergiveaway.command(name="setrole")
async def pfx_power_setrole(ctx, name: str, role: discord.Role, entries: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await power_setrole._callback(FakeInteraction(ctx), name, role, entries)

@pfx_powergiveaway.command(name="removerole")
async def pfx_power_removerole(ctx, name: str, role: discord.Role):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await power_removerole._callback(FakeInteraction(ctx), name, role)

@pfx_powergiveaway.command(name="setchannel")
async def pfx_power_setchannel(ctx, name: str, channel: discord.TextChannel, entries: float):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await power_setchannel._callback(FakeInteraction(ctx), name, channel, entries)

@pfx_powergiveaway.command(name="removechannel")
async def pfx_power_removechannel(ctx, name: str, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await power_removechannel._callback(FakeInteraction(ctx), name, channel)

@pfx_powergiveaway.command(name="setboost")
async def pfx_power_setboost(ctx, name: str, role: discord.Role, multiplier: float):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await power_setboost._callback(FakeInteraction(ctx), name, role, multiplier)

@pfx_powergiveaway.command(name="removeboost")
async def pfx_power_removeboost(ctx, name: str, role: discord.Role):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await power_removeboost._callback(FakeInteraction(ctx), name, role)

@pfx_powergiveaway.command(name="start")
async def pfx_power_start(ctx, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await power_start._callback(FakeInteraction(ctx), name)

@pfx_powergiveaway.command(name="stop")
async def pfx_power_stop(ctx, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await power_stop._callback(FakeInteraction(ctx), name)

@pfx_powergiveaway.command(name="status")
async def pfx_power_status(ctx, name: str):
    await power_status._callback(FakeInteraction(ctx), name)

@pfx_powergiveaway.command(name="list")
async def pfx_power_list(ctx):
    await power_list._callback(FakeInteraction(ctx))

@pfx_powergiveaway.command(name="delete")
async def pfx_power_delete(ctx, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await power_delete._callback(FakeInteraction(ctx), name)

# ═══════════════════════════════════════════════════════
# CORE EVENTS
# ═══════════════════════════════════════════════════════

@bot.event
async def on_message(message):
    if message.author.bot: return
    if message.guild:
        await _process_power_giveaway_message(message)
        await _process_mega_ticket_message(message)
    if message.content.startswith(common._BOT_PREFIX) and not _prefix_channel_allowed(message): return
    await bot.process_commands(message)

@bot.event
async def on_ready():
    await setup_database()
    await common._load_prefix()
    await load_disabled_commands()
    await load_prefix_restrictions()
    bot.add_view(ChestChannelView())

    _guild = discord.Object(id=_GUILD_ID)
    bot.tree.copy_global_to(guild=_guild)
    try:
        synced = await bot.tree.sync(guild=_guild)
        print(f"[Drops Bot] Synced {len(synced)} commands to guild. Logged in as {bot.user}")
    except Exception as e:
        print(f"[Drops Bot] Guild sync failed: {e}")
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()

    for g in bot.guilds:
        try:
            await _refresh_chest_channel(g)
        except Exception as e:
            print(f"[ChestPanel restore] {g.name}: {e}")

    for task_fn in [daily_key_loop, mega_loop, mega_info_loop, power_giveaway_loop]:
        bot.loop.create_task(task_fn())
        

@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except discord.HTTPException as e:
        print(f"[Drops Sync] Failed on join: {e}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure): return
    raise error

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument): await ctx.send(f"❌ Missing argument: `{error.param.name}`")
    elif isinstance(error, commands.BadArgument): await ctx.send(f"❌ Invalid argument: {error}")
    elif isinstance(error, commands.CommandNotFound): pass
    else: raise error

@bot.before_invoke
async def _log_prefix_command(ctx: commands.Context):
    if not ctx.guild: return
    embed = discord.Embed(
        description=f"{ctx.author.mention} used **`{common._BOT_PREFIX}{ctx.command.qualified_name}`**",
        color=discord.Color.light_grey(), timestamp=datetime.now(UTC))
    embed.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
    embed.set_footer(text=f"#{getattr(ctx.channel,'name','DM')} | UID: {ctx.author.id}")
    await log_event(ctx.guild.id, "command", embed)

# ── Abuse box management ──────────────────────────────────────────────────────
@bot.tree.command(name="addbox", description="Admin: create an abuse box")
@app_commands.describe(name="Box name")
@command_enabled()
async def slash_addbox(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute("INSERT INTO abuse_boxes VALUES(?,?)", (interaction.guild.id, name))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(f"❌ Box **{name}** already exists.", ephemeral=True); return
    await interaction.response.send_message(f"✅ Created box **{name}**.")

@bot.tree.command(name="removebox", description="Admin: delete an abuse box and all its prizes")
@app_commands.describe(name="Box name")
@command_enabled()
async def slash_removebox(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                                  (interaction.guild.id, name)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Box **{name}** not found.", ephemeral=True); return
            await db.execute("DELETE FROM abuse_boxes WHERE guild_id=? AND box_name=?", (interaction.guild.id, name))
            await db.execute("DELETE FROM abuse_box_prizes WHERE guild_id=? AND box_name=?", (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed box **{name}** and all its prizes.")

@bot.tree.command(name="removeboxprize", description="Admin: remove a prize from an abuse box by ID")
@app_commands.describe(box="Box name", prize_id="Prize ID from !listboxes")
@command_enabled()
async def slash_removeboxprize(interaction: discord.Interaction, box: str, prize_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT id FROM abuse_box_prizes WHERE id=? AND guild_id=? AND box_name=?",
                                  (prize_id, interaction.guild.id, box)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(
                        f"❌ Prize #{prize_id} not found in **{box}**.", ephemeral=True); return
            await db.execute("DELETE FROM abuse_box_prizes WHERE id=?", (prize_id,))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed prize #{prize_id} from **{box}**.")

@bot.tree.command(name="listboxes", description="List all abuse boxes and their prizes")
@app_commands.describe(box="Specific box to view (leave blank for all)")
@command_enabled()
async def slash_listboxes(interaction: discord.Interaction, box: str = None):
    await interaction.response.defer()
    async with get_db() as db:
        query = "SELECT box_name FROM abuse_boxes WHERE guild_id=?" + (" AND box_name=?" if box else "")
        async with db.execute(query, (interaction.guild.id, box) if box else (interaction.guild.id,)) as cur:
            boxes = await cur.fetchall()
    if not boxes:
        await interaction.followup.send("❌ No boxes found."); return
    embed = discord.Embed(title="📦 Admin Abuse Boxes", color=discord.Color.orange())
    for (box_name,) in boxes:
        async with get_db() as db:
            async with db.execute("SELECT id,prize_type,prize_value,chance FROM abuse_box_prizes "
                                  "WHERE guild_id=? AND box_name=? ORDER BY id",
                                  (interaction.guild.id, box_name)) as cur:
                prizes = await cur.fetchall()
        if not prizes:
            embed.add_field(name=f"📦 {box_name}", value="*No prizes yet*", inline=False); continue
        total_w = sum(p[3] for p in prizes)
        lines = []
        for p_id, p_type, p_value, p_chance in prizes:
            pct = (p_chance / total_w * 100) if total_w > 0 else 0
            desc = (f"💰 {int(p_value):,}" if p_type == "balance" else
                    f"⭐ {int(p_value):,} EXP" if p_type == "exp" else
                    f"🎒 {p_value}" if p_type == "item" else f"✨ {p_value}")
            lines.append(f"`#{p_id}` {desc} — **{pct:.1f}%**")
        embed.add_field(name=f"📦 {box_name}", value="\n".join(lines), inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="givebox", description="Admin: give a box to all members with a role")
@app_commands.describe(role="Role to give boxes to", amount="How many boxes per member", box="Box name")
@command_enabled()
async def slash_givebox(interaction: discord.Interaction, role: discord.Role, amount: int, box: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                              (interaction.guild.id, box)) as cur:
            if not await cur.fetchone():
                await interaction.response.send_message(f"❌ Box **{box}** not found.", ephemeral=True); return
    await interaction.response.defer()
    members = [m for m in interaction.guild.members if role in m.roles and not m.bot]
    if not members:
        await interaction.followup.send(f"❌ No non-bot members with {role.mention}."); return
    for m in members:
        await inventory_add(interaction.guild.id, m.id, box, amount)
    await interaction.followup.send(
        f"✅ Gave **{amount}x {box}** to **{len(members)}** member(s) with {role.mention}.")

@bot.tree.command(name="addrarebox", description="Admin: mark a box prize as a rare drop")
@app_commands.describe(box="Box name", prize_id="Prize ID from /listboxes")
@command_enabled()
async def slash_addrarebox(interaction: discord.Interaction, box: str, prize_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT prize_type,prize_value FROM abuse_box_prizes "
                              "WHERE id=? AND guild_id=? AND box_name=?",
                              (prize_id, interaction.guild.id, box)) as cur:
            row = await cur.fetchone()
    if not row:
        await interaction.response.send_message(
            f"❌ Prize #{prize_id} not found in **{box}**.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute("INSERT INTO rare_box_config(guild_id,box_name,prize_id) VALUES(?,?,?)",
                                 (interaction.guild.id, box, prize_id))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(
                    f"❌ Prize #{prize_id} already marked as rare.", ephemeral=True); return
    await interaction.response.send_message(
        f"✅ Prize `#{prize_id}` ({row[0]}: **{row[1]}**) in **{box}** is now a rare drop.")

@bot.tree.command(name="removerarebox", description="Admin: unmark a box prize as a rare drop")
@app_commands.describe(box="Box name", prize_id="Prize ID")
@command_enabled()
async def slash_removerarebox(interaction: discord.Interaction, box: str, prize_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM rare_box_config WHERE guild_id=? AND box_name=? AND prize_id=?",
                             (interaction.guild.id, box, prize_id))
            await db.commit()
    await interaction.response.send_message(f"🗑 Prize #{prize_id} in **{box}** is no longer a rare drop.")

# ── Ticket admin ──────────────────────────────────────────────────────────────
@bot.tree.command(name="addtickets", description="Admin: give mega tickets to a user")
@app_commands.describe(user="Target user", amount="Tickets to add")
@command_enabled()
async def slash_addtickets(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_tickets(interaction.guild.id, user.id, amount)
    await interaction.response.send_message(f"✅ Added {amount} mega tickets to {user.mention}.")

@bot.tree.command(name="removetickets", description="Admin: remove mega tickets from a user")
@app_commands.describe(user="Target user", amount="Tickets to remove")
@command_enabled()
async def slash_removetickets(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_tickets(interaction.guild.id, user.id, -amount)
    await interaction.response.send_message(f"❌ Removed {amount} mega tickets from {user.mention}.")

# ── VIP key admin ─────────────────────────────────────────────────────────────
@bot.tree.command(name="givekey", description="Admin: give VIP Chest Keys to a user")
@app_commands.describe(user="Target user", amount="Keys to give (default 1)")
@command_enabled()
async def slash_givekey(interaction: discord.Interaction, user: discord.Member, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    await inventory_add(interaction.guild.id, user.id, VIP_CHEST_KEY, amount)
    await interaction.response.send_message(f"🔑 Gave **{amount}x {VIP_CHEST_KEY}** to {user.mention}.")
    await log_event(interaction.guild.id, "item", _log_embed("🔑 VIP Key Given", discord.Color.green(),
        Admin=interaction.user.mention, User=user.mention, Keys=str(amount)))

@bot.tree.command(name="takekey", description="Admin: take VIP Chest Keys from a user")
@app_commands.describe(user="Target user", amount="Keys to take (default 1)")
@command_enabled()
async def slash_takekey(interaction: discord.Interaction, user: discord.Member, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    if not await inventory_remove(interaction.guild.id, user.id, VIP_CHEST_KEY, amount):
        await interaction.response.send_message(
            f"❌ {user.mention} doesn't have {amount}x {VIP_CHEST_KEY}.", ephemeral=True); return
    await interaction.response.send_message(f"🗑 Took **{amount}x {VIP_CHEST_KEY}** from {user.mention}.")
    await log_event(interaction.guild.id, "item", _log_embed("🔑 VIP Key Taken", discord.Color.red(),
        Admin=interaction.user.mention, User=user.mention, Keys=str(amount)))

@bot.tree.command(name="givekeyrole", description="Admin: give VIP Chest Keys to all members with a role")
@app_commands.describe(role="Target role", amount="Keys per member (default 1)")
@command_enabled()
async def slash_givekeyrole(interaction: discord.Interaction, role: discord.Role, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    await interaction.response.defer()
    members = [m for m in interaction.guild.members if role in m.roles and not m.bot]
    if not members:
        await interaction.followup.send(f"❌ No non-bot members with {role.mention}."); return
    for m in members:
        await inventory_add(interaction.guild.id, m.id, VIP_CHEST_KEY, amount)
    await interaction.followup.send(
        f"🔑 Gave **{amount}x {VIP_CHEST_KEY}** to **{len(members)}** member(s) with {role.mention}.")

if __name__ == "__main__":
    bot.run(TOKEN)
