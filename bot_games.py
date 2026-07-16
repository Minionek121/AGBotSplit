import os, json, random, asyncio, discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta, UTC
from typing import Optional

import common
from common import (
    get_db, db_lock, setup_database, log_event, _log_embed, command_enabled,
    is_allowed_to_giveaway, _is_allowed_ctx,
    get_balance, add_balance, get_exp, add_exp, get_level,
    inventory_add, inventory_remove, inventory_get,
    get_tickets, add_tickets, _weighted_sample_without_replacement,
    distribute_prizes, build_reward_summary, add_stat, ensure_stats,
    get_today_msg_count,
    FakeInteraction, _MC,
    VIP_CHEST_KEY, GAMBLE_TOKEN,
    disabled_commands, global_disabled_commands, load_disabled_commands,
    prefix_channel_rules, _prefix_channel_allowed, load_prefix_restrictions,
    register_bot_instance,
)

TOKEN = os.getenv("TOKEN_GAMES")
_GUILD_ID = int(os.getenv("GUILD_ID", "0"))
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix=common.get_prefix, intents=intents, help_command=None)
register_bot_instance(bot)

active_game_sessions: dict[int, dict] = {}
game_tasks: dict[int, asyncio.Task] = {}
auto_giveaway_tasks: dict[int, asyncio.Task] = {}

# ═══════════════════════════════════════════════════════
# GIVEAWAY HELPERS
# ═══════════════════════════════════════════════════════

async def _is_auto_enterable(guild_id: int, reward_balance: int) -> bool:
    async with get_db() as db:
        async with db.execute(
            "SELECT min_prize_balance FROM auto_entry_threshold WHERE guild_id=?", (guild_id,)) as cur:
            row = await cur.fetchone()
    if not row: return True
    return reward_balance >= row[0]

async def _get_recent_message_window(guild_id: int) -> int:
    async with get_db() as db:
        async with db.execute(
            "SELECT recent_message_window FROM auto_entry_threshold WHERE guild_id=?", (guild_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0

async def _get_eligible_giveaway_participants(channel, reaction, required_role: int, meta: dict) -> list:
    guild = channel.guild
    reward_balance = int(meta.get("balance", 0))
    big_enough = await _is_auto_enterable(guild.id, reward_balance)

    users = []
    async for user in reaction.users():
        if user.bot: continue
        member = guild.get_member(user.id)
        if not member: continue
        if required_role and required_role not in {r.id for r in member.roles}: continue
        users.append(user)

    if not big_enough:
        window = await _get_recent_message_window(guild.id)
        if window > 0:
            try:
                recent_authors = set()
                async for hist_msg in channel.history(limit=window):
                    if not hist_msg.author.bot: recent_authors.add(hist_msg.author.id)
                users = [u for u in users if u.id in recent_authors]
            except Exception as e:
                print(f"[GiveawayEligibility] history fetch failed: {e}")
        return users

    async with get_db() as db:
        async with db.execute(
            "SELECT user_id FROM auto_entry_users WHERE guild_id=? AND enabled=1", (guild.id,)) as cur:
            auto_uids = {r[0] for r in await cur.fetchall()}
        async with db.execute(
            "SELECT role_id FROM auto_entry_roles WHERE guild_id=?", (guild.id,)) as cur:
            auto_role_ids = {r[0] for r in await cur.fetchall()}
    existing_uids = {u.id for u in users}
    for auid in auto_uids:
        if auid in existing_uids: continue
        ae_member = guild.get_member(auid)
        if not ae_member or ae_member.bot: continue
        member_rids = {r.id for r in ae_member.roles}
        if auto_role_ids and not (auto_role_ids & member_rids): continue
        if required_role and required_role not in member_rids: continue
        users.append(ae_member)
    return users

async def _send_giveaway_game_notify(guild_id: int, prize_text: str, channel, extra_line: str = None) -> None:
    async with get_db() as db:
        async with db.execute(
            "SELECT channel_id FROM giveaway_game_notify_config WHERE guild_id=?", (guild_id,)) as cur:
            row = await cur.fetchone()
    if not row or not row[0]: return
    notify_ch = bot.get_channel(row[0])
    if not notify_ch: return
    lines = [f"🎉 **{prize_text}**", f"📍 {channel.mention}"]
    if extra_line: lines.append(extra_line)
    try: await notify_ch.send("\n".join(lines))
    except Exception as e: print(f"[NotifyChannel] {e}")

# =======================================================
# GAME HELPER
# =======================================================

    def _parse_game_name_and_rest(args: str) -> tuple[str, str]:
        args = args.strip()
        if args.startswith('"'):
            end = args.find('"', 1)
            if end != -1:
                return args[1:end], args[end+1:].strip()
        parts = args.split(None, 1)
        return parts[0], (parts[1] if len(parts) > 1 else "")

# ═══════════════════════════════════════════════════════
# CREATE GIVEAWAY
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="giveaway", description="Create a giveaway")
@app_commands.describe(
    prize="Prize description", seconds="Duration in seconds", winners="Number of winners",
    reward_balance="Coin reward per winner", reward_exp="EXP reward per winner",
    reward_tickets="Mega tickets per winner", reward_gamble_tokens="Gamble tokens per winner",
    reward_vip_keys="VIP Chest Keys per winner", reward_role="Role to give each winner",
    reward_item="Item/box name per winner", reward_item_qty="How many of the item (default 1)",
    channel="Channel to post in", required_role="Required role to enter", template="Color (gold/red/blue/green)")
@command_enabled()
async def giveaway(interaction: discord.Interaction, prize: str, seconds: int, winners: int,
    reward_balance: int = 0, reward_exp: int = 0, reward_tickets: int = 0,
    reward_gamble_tokens: int = 0, reward_vip_keys: int = 0,
    reward_role: discord.Role = None, reward_item: str = None, reward_item_qty: int = 1,
    channel: discord.TextChannel = None, required_role: discord.Role = None, template: str = "gold"):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if seconds <= 0:
        await interaction.response.send_message("❌ Duration must be > 0 seconds.", ephemeral=True); return
    if reward_role and reward_role >= interaction.user.top_role:
        await interaction.response.send_message(
            f"❌ You can only give away roles below your highest role ({interaction.user.top_role.mention}).", ephemeral=True); return

    resolved_item = reward_item.strip() if reward_item else None
    if resolved_item and reward_item_qty < 1: reward_item_qty = 1
    target_channel = channel or interaction.channel
    end_time = datetime.now(UTC) + timedelta(seconds=seconds)

    reward_parts = []
    if reward_balance > 0:       reward_parts.append(f"💰 {reward_balance:,} coins")
    if reward_exp > 0:           reward_parts.append(f"⭐ {reward_exp:,} EXP")
    if reward_tickets > 0:       reward_parts.append(f"🎟 {reward_tickets} ticket(s)")
    if reward_gamble_tokens > 0: reward_parts.append(f"🎲 {reward_gamble_tokens} gamble token(s)")
    if reward_vip_keys > 0:      reward_parts.append(f"🔑 {reward_vip_keys} VIP key(s)")
    if reward_role:              reward_parts.append(f"👑 {reward_role.mention}")
    if resolved_item:            reward_parts.append(f"🎒 {reward_item_qty}x {resolved_item}")
    reward_summary = " + ".join(reward_parts) if reward_parts else "No reward"

    TEMPLATES = {"gold": discord.Color.gold(), "red": discord.Color.red(),
                 "blue": discord.Color.blue(), "green": discord.Color.green()}
    embed = discord.Embed(title="🎉 GIVEAWAY 🎉",
        description=(f"React with 🎉 to enter\n\n**Prize:** {prize}\n**Reward:** {reward_summary}\n"
                     f"**Winners:** {winners}\n**Ends:** <t:{int(end_time.timestamp())}:R>"),
        color=TEMPLATES.get(template, discord.Color.gold()))
    if required_role: embed.add_field(name="Required Role", value=required_role.mention, inline=False)

    message = await target_channel.send(embed=embed)
    await message.add_reaction("🎉")

    prize_meta = json.dumps({
        "label": prize, "balance": reward_balance, "exp": reward_exp,
        "tickets": reward_tickets, "gamble_tokens": reward_gamble_tokens, "vip_keys": reward_vip_keys,
        "role_id": reward_role.id if reward_role else 0,
        "item": resolved_item, "item_qty": reward_item_qty if resolved_item else 0,
    })
    async with get_db() as db:
        await db.execute(
            "INSERT INTO giveaways(message_id,channel_id,prize,winners,reward,end_time,required_role,template,ended) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (message.id, target_channel.id, prize_meta, winners, reward_balance,
             int(end_time.timestamp()), required_role.id if required_role else 0, template, 0))
        await db.commit()

    if await _is_auto_enterable(interaction.guild.id, reward_balance):
        await _send_giveaway_game_notify(interaction.guild.id, prize, target_channel,
                                         extra_line=f"👤 Hosted by {interaction.user.mention}")

    await interaction.response.send_message("✅ Giveaway created.", ephemeral=True)
    asyncio.create_task(giveaway_timer(message.id, seconds))
    await log_event(interaction.guild.id, "giveaway", _log_embed(
        "🎉 Giveaway Created", discord.Color.gold(),
        By=interaction.user.mention, Prize=prize, Duration=f"{seconds}s", Winners=str(winners),
        Channel=target_channel.mention))

@bot.command(name="giveaway")
async def pfx_giveaway(ctx, prize: str, seconds: int, winners: int = 1,
                        reward_balance: int = 0, reward_exp: int = 0):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await giveaway._callback(FakeInteraction(ctx), prize, seconds, winners, reward_balance, reward_exp,
                              0, 0, 0, None, None, 1, None, None, "gold")

# ═══════════════════════════════════════════════════════
# /HOST — user-hosted balance giveaway
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="host", description="Host a giveaway from your own balance — deducted immediately")
@app_commands.describe(
    amount="Amount to give away (deducted from your balance right now)",
    winners="Number of winners (each gets amount ÷ winners)",
    seconds="Duration in seconds (default 60)",
    prize="Optional label shown in the embed (default: 'Balance Giveaway')",
    channel="Channel to post in (default: current channel)",
    required_role="Restrict entries to members with this role")
@command_enabled()
async def host(interaction: discord.Interaction, amount: int, winners: int = 1,
               seconds: int = 60, prize: str = "Balance Giveaway",
               channel: discord.TextChannel = None, required_role: discord.Role = None):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    if winners < 1:
        await interaction.response.send_message("❌ Winners must be ≥ 1.", ephemeral=True); return
    if seconds <= 0:
        await interaction.response.send_message("❌ Duration must be > 0 seconds.", ephemeral=True); return

    gid, uid = interaction.guild.id, interaction.user.id
    bal = await get_balance(gid, uid)
    if bal < amount:
        await interaction.response.send_message(
            f"❌ You need **{amount:,}** coins but only have **{bal:,}**.", ephemeral=True); return

    per_winner = amount // winners
    if per_winner < 1:
        await interaction.response.send_message(
            f"❌ Amount ÷ winners = {per_winner} — each winner must receive at least 1 coin.", ephemeral=True); return

    # Deduct upfront
    await add_balance(gid, uid, -amount, bot=bot)
    # Track hosted_balance stat
    await add_stat(gid, uid, "hosted_balance", amount)

    target_channel = channel or interaction.channel
    end_time = datetime.now(UTC) + timedelta(seconds=seconds)

    embed = discord.Embed(title="🎁 HOSTED GIVEAWAY 🎁",
        description=(f"React with 🎉 to enter\n\n"
                     f"**Prize:** {prize}\n"
                     f"**Reward:** 💰 {per_winner:,} coins per winner\n"
                     f"**Winners:** {winners}\n"
                     f"**Ends:** <t:{int(end_time.timestamp())}:R>"),
        color=discord.Color.purple())
    embed.set_footer(text=f"Hosted by {interaction.user.display_name} · Total pot: {amount:,} coins")
    if required_role: embed.add_field(name="Required Role", value=required_role.mention, inline=False)

    message = await target_channel.send(embed=embed)
    await message.add_reaction("🎉")

    prize_meta = json.dumps({
        "label": prize, "balance": per_winner, "exp": 0, "tickets": 0,
        "gamble_tokens": 0, "vip_keys": 0, "role_id": 0, "item": None, "item_qty": 0,
    })
    async with get_db() as db:
        await db.execute(
            "INSERT INTO giveaways(message_id,channel_id,prize,winners,reward,end_time,required_role,template,ended) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (message.id, target_channel.id, prize_meta, winners, per_winner,
             int(end_time.timestamp()), required_role.id if required_role else 0, "gold", 0))
        await db.commit()

    await _send_giveaway_game_notify(gid, prize, target_channel,
                                     extra_line=f"🎁 Hosted by {interaction.user.mention} · Pot: {amount:,} coins")

    await interaction.response.send_message(
        f"✅ Hosted giveaway started! **{amount:,}** coins deducted from your balance.\n"
        f"{winners} winner(s) will each receive **{per_winner:,}** coins.", ephemeral=True)
    asyncio.create_task(giveaway_timer(message.id, seconds))
    await log_event(gid, "giveaway", _log_embed(
        "🎁 Hosted Giveaway Created", discord.Color.purple(),
        Host=interaction.user.mention, Prize=prize, Amount=f"{amount:,}",
        Per_Winner=f"{per_winner:,}", Winners=str(winners), Channel=target_channel.mention))

@bot.command(name="host")
async def pfx_host(ctx, amount: int, winners: int = 1, seconds: int = 60, *, prize: str = "Balance Giveaway"):
    await host._callback(FakeInteraction(ctx), amount, winners, seconds, prize, None, None)

# ═══════════════════════════════════════════════════════
# GIVEAWAY ENGINE
# ═══════════════════════════════════════════════════════

async def giveaway_timer(message_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        await end_giveaway(message_id)
    except Exception as e:
        print(f"[GiveawayTimer] {message_id}: {e}")

async def giveaway_watcher():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = int(datetime.now(UTC).timestamp())
        async with get_db() as db:
            async with db.execute("SELECT message_id FROM giveaways WHERE ended=0 AND end_time<=?", (now,)) as cur:
                rows = await cur.fetchall()
        for (mid,) in rows:
            try: await end_giveaway(mid)
            except Exception as e: print(f"[Watcher] {mid}: {e}")
        await asyncio.sleep(15)

async def end_giveaway(message_id, reroll=False):
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT message_id,channel_id,prize,winners,reward,end_time,required_role,template,ended "
                "FROM giveaways WHERE message_id=?", (message_id,)) as cur:
                data = await cur.fetchone()
            if not data: print(f"[Giveaway] Not found: {message_id}"); return
            (message_id, channel_id, prize_raw, winner_count, legacy_reward,
             end_time, required_role, template, ended) = data
            if ended and not reroll: print(f"[Giveaway] Already ended: {message_id}"); return
            if not reroll:
                await db.execute("UPDATE giveaways SET ended=1 WHERE message_id=?", (message_id,))
                await db.commit()

    try:
        meta = json.loads(prize_raw)
        if not isinstance(meta, dict): raise TypeError
        prize_label = meta.get("label", prize_raw)
    except (json.JSONDecodeError, TypeError, AttributeError):
        meta = {"label": str(prize_raw), "balance": legacy_reward}
        prize_label = str(prize_raw)

    channel = bot.get_channel(channel_id)
    if not channel: print(f"[Giveaway] Channel not found: {channel_id}"); return
    try: message = await channel.fetch_message(message_id)
    except Exception as e: print(f"[Giveaway] Fetch failed {message_id}: {e}"); return

    reaction = next((r for r in message.reactions if str(r.emoji) == "🎉"), None)
    if not reaction: await channel.send("❌ Giveaway reaction was missing."); return

    users = await _get_eligible_giveaway_participants(channel, reaction, required_role, meta)
    if not users: await channel.send("No valid participants."); return

    weighted = []
    for user in users:
        lvl = await get_level(channel.guild.id, user.id)
        weighted.extend([user] * random.randint(1, max(1, lvl // 4)))

    winners = []
    while len(winners) < min(winner_count, len(users)) and weighted:
        s = random.choice(weighted)
        if s not in winners: winners.append(s)

    async with db_lock:
        async with get_db() as db:
            for w in winners:
                await db.execute("INSERT OR REPLACE INTO giveaway_winners VALUES(?,?,?)",
                                 (message_id, w.id, int(meta.get("balance", 0))))
            await db.commit()

    await distribute_prizes(channel.guild, winners, meta)

    reward_summary = build_reward_summary(meta, channel.guild)
    winner_mentions = ", ".join(w.mention for w in winners)
    embed = discord.Embed(title="🎊 Giveaway Ended",
        description=f"**Prize:** {prize_label}\n**Reward:** {reward_summary}\n**Winners:** {winner_mentions}",
        color=discord.Color.green())
    await channel.send(embed=embed)
    await log_event(channel.guild.id, "giveaway", _log_embed(
        "🎊 Giveaway Ended", discord.Color.green(),
        Prize=prize_label, Winners=winner_mentions, Channel=channel.mention))

# ═══════════════════════════════════════════════════════
# REROLL
# ═══════════════════════════════════════════════════════

@bot.command(name="reroll")
async def cmd_reroll(ctx, message_id: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    try: mid = int(message_id)
    except ValueError: await ctx.send("❌ Invalid message ID."); return
    async with get_db() as db:
        async with db.execute("SELECT * FROM giveaways WHERE message_id=?", (mid,)) as cur:
            data = await cur.fetchone()
    if not data: await ctx.send("❌ Giveaway not found."); return
    (_mid, channel_id, prize_raw, winner_count, legacy_reward,
     end_time, required_role, template, ended) = data
    channel = bot.get_channel(channel_id)
    if not channel: await ctx.send("❌ Channel not found."); return
    try: message = await channel.fetch_message(mid)
    except discord.NotFound: await ctx.send("❌ Message not found."); return
    reaction = discord.utils.get(message.reactions, emoji="🎉")
    if not reaction: await ctx.send("❌ Reaction not found."); return

    try:
        meta = json.loads(prize_raw)
        if not isinstance(meta, dict): raise TypeError
        prize_label = meta.get("label", prize_raw)
    except Exception:
        meta = {"label": str(prize_raw), "balance": legacy_reward}
        prize_label = str(prize_raw)

    users = await _get_eligible_giveaway_participants(channel, reaction, required_role, meta)
    if not users: await ctx.send("❌ No participants."); return
    weighted = []
    for user in users:
        lvl = await get_level(ctx.guild.id, user.id)
        weighted.extend([user] * random.randint(1, max(1, lvl // 4)))
    new_winner = random.choice(weighted)
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO giveaway_winners VALUES(?,?,?)",
                             (mid, new_winner.id, int(meta.get("balance", 0))))
            await db.commit()
    await distribute_prizes(channel.guild, [new_winner], meta)
    embed = discord.Embed(title="🔄 Giveaway Rerolled",
        description=f"**Prize:** {prize_label}\n**Reward:** {build_reward_summary(meta, channel.guild)}\n**New Winner:** {new_winner.mention}",
        color=discord.Color.orange())
    await channel.send(embed=embed)
    await ctx.send("✅ Giveaway rerolled.")

# ═══════════════════════════════════════════════════════
# AUTO GIVEAWAY
# ═══════════════════════════════════════════════════════

async def auto_giveaway_loop(guild_id: int):
    await bot.wait_until_ready()
    while not bot.is_closed():
        async with get_db() as db:
            async with db.execute(
                "SELECT channel_id,interval_seconds,duration_seconds,running FROM auto_giveaway_config WHERE guild_id=?",
                (guild_id,)) as cur:
                cfg = await cur.fetchone()
        if not cfg or not cfg[3]: auto_giveaway_tasks.pop(guild_id, None); break
        channel_id, interval_secs, duration_secs, _ = cfg
        channel = bot.get_channel(channel_id)
        if not channel: await asyncio.sleep(30); continue

        async with get_db() as db:
            async with db.execute(
                "SELECT id,prize,winners,chance,reward_balance,reward_exp,reward_tickets,"
                "reward_gamble_tokens,reward_vip_keys,reward_role_id,reward_item,reward_item_qty "
                "FROM auto_giveaway_pool WHERE guild_id=?", (guild_id,)) as cur:
                pool = await cur.fetchall()
        if not pool: await asyncio.sleep(interval_secs); continue

        gd = random.choices(pool, weights=[r[3] for r in pool], k=1)[0]
        (_id, prize, winners, chance, rb, re, rt, rgt, rvk, rrole, ri, riq) = gd
        guild = bot.get_guild(guild_id)
        end_time = datetime.now(UTC) + timedelta(seconds=duration_secs)

        reward_parts = []
        if rb > 0:  reward_parts.append(f"💰 {rb:,} coins")
        if re > 0:  reward_parts.append(f"⭐ {re:,} EXP")
        if rt > 0:  reward_parts.append(f"🎟 {rt} ticket(s)")
        if rgt > 0: reward_parts.append(f"🎲 {rgt} gamble token(s)")
        if rvk > 0: reward_parts.append(f"🔑 {rvk} VIP key(s)")
        if rrole and guild:
            role = guild.get_role(rrole)
            if role: reward_parts.append(f"👑 {role.mention}")
        if ri: reward_parts.append(f"🎒 {riq}x {ri}")
        reward_summary = " + ".join(reward_parts) if reward_parts else "No reward"

        embed = discord.Embed(title="🎉 AUTOMATIC GIVEAWAY 🎉",
            description=(f"React with 🎉 to enter\n\n**Prize:** {prize}\n**Reward:** {reward_summary}\n"
                         f"**Winners:** {winners}\n**Ends:** <t:{int(end_time.timestamp())}:R>"),
            color=discord.Color.gold())
        msg = await channel.send(embed=embed)
        await msg.add_reaction("🎉")

        if await _is_auto_enterable(guild_id, rb):
            await _send_giveaway_game_notify(guild_id, prize, channel, extra_line="🤖 Hosted by Auto Giveaway")

        prize_meta = json.dumps({"label": prize, "balance": rb, "exp": re, "tickets": rt,
                                  "gamble_tokens": rgt, "vip_keys": rvk, "role_id": rrole,
                                  "item": ri, "item_qty": riq if ri else 0})
        async with get_db() as db:
            await db.execute(
                "INSERT INTO giveaways(message_id,channel_id,prize,winners,reward,end_time,required_role,template,ended) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (msg.id, channel_id, prize_meta, winners, rb, int(end_time.timestamp()), 0, "gold", 0))
            await db.commit()
        asyncio.create_task(giveaway_timer(msg.id, duration_secs))
        await asyncio.sleep(interval_secs)


@bot.tree.command(name="addautogiveaway", description="Add a giveaway to the auto pool")
@app_commands.describe(
    prize="Prize description", winners="Number of winners (default 1)",
    chance="Selection weight — higher = picked more often (default 1.0)",
    reward_balance="Coin reward per winner", reward_exp="EXP reward per winner",
    reward_tickets="Mega tickets per winner", reward_gamble_tokens="Gamble tokens per winner",
    reward_vip_keys="VIP Chest Keys per winner", reward_role="Role to give each winner",
    reward_item="Item or box name per winner", reward_item_qty="Quantity of item reward (default 1)")
@command_enabled()
async def addautogiveaway(interaction: discord.Interaction, prize: str, winners: int = 1, chance: float = 1.0,
    reward_balance: int = 0, reward_exp: int = 0, reward_tickets: int = 0, reward_gamble_tokens: int = 0,
    reward_vip_keys: int = 0, reward_role: discord.Role = None, reward_item: str = None, reward_item_qty: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if winners < 1:
        await interaction.response.send_message("❌ Winners must be ≥ 1.", ephemeral=True); return
    if chance <= 0:
        await interaction.response.send_message("❌ Chance must be > 0.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            cur = await db.execute(
                "INSERT INTO auto_giveaway_pool(guild_id,prize,winners,chance,reward_balance,reward_exp,"
                "reward_tickets,reward_gamble_tokens,reward_vip_keys,reward_role_id,reward_item,reward_item_qty) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (interaction.guild.id, prize, winners, chance, reward_balance, reward_exp, reward_tickets,
                 reward_gamble_tokens, reward_vip_keys, reward_role.id if reward_role else 0,
                 reward_item, reward_item_qty))
            new_id = cur.lastrowid
            await db.commit()
    parts = []
    if reward_balance > 0: parts.append(f"💰 {reward_balance:,}")
    if reward_exp > 0: parts.append(f"⭐ {reward_exp:,} EXP")
    if reward_tickets > 0: parts.append(f"🎟 {reward_tickets}")
    if reward_gamble_tokens > 0: parts.append(f"🎲 {reward_gamble_tokens}")
    if reward_vip_keys > 0: parts.append(f"🔑 {reward_vip_keys}")
    if reward_role: parts.append(f"👑 {reward_role.mention}")
    if reward_item: parts.append(f"🎒 {reward_item_qty}x {reward_item}")
    await interaction.response.send_message(
        f"✅ Added **{prize}** to auto pool (`#{new_id}`)\n"
        f"Winners: {winners} | Weight: {chance} | Reward: {' + '.join(parts) or 'None'}")

@bot.command(name="addautogiveaway")
async def pfx_addautogiveaway(ctx, prize: str, winners: int = 1, chance: float = 1.0,
                               reward_balance: int = 0, reward_exp: int = 0):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await addautogiveaway._callback(FakeInteraction(ctx), prize, winners, chance, reward_balance, reward_exp,
                                    0, 0, 0, None, None, 1)

@bot.tree.command(name="startgiveaways", description="Start automatic giveaways")
@app_commands.describe(interval_seconds="Seconds between giveaways",
                       giveaway_duration_seconds="How long each lasts",
                       channel="Channel (default current)")
@command_enabled()
async def startgiveaways(interaction: discord.Interaction, interval_seconds: int,
                         giveaway_duration_seconds: int, channel: discord.TextChannel = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    gid = interaction.guild.id
    if gid in auto_giveaway_tasks and not auto_giveaway_tasks[gid].done():
        await interaction.response.send_message("❌ Already running.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM auto_giveaway_pool WHERE guild_id=?", (gid,)) as cur:
            if (await cur.fetchone())[0] == 0:
                await interaction.response.send_message(
                    "❌ No auto giveaways in the pool. Use `/addautogiveaway` first.", ephemeral=True); return
    target = channel or interaction.channel
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO auto_giveaway_config VALUES(?,?,?,?,?)",
                             (gid, target.id, interval_seconds, giveaway_duration_seconds, 1))
            await db.commit()
    auto_giveaway_tasks[gid] = asyncio.create_task(auto_giveaway_loop(gid))
    await interaction.response.send_message(
        f"✅ Automatic giveaways started in {target.mention}!\n"
        f"Interval: **{interval_seconds}s** | Duration: **{giveaway_duration_seconds}s**")

@bot.command(name="startgiveaways")
async def pfx_startgiveaways(ctx, interval_seconds: int, giveaway_duration_seconds: int,
                               channel: discord.TextChannel = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await startgiveaways._callback(FakeInteraction(ctx), interval_seconds, giveaway_duration_seconds, channel)

# ═══════════════════════════════════════════════════════
# MY WINNINGS
# ═══════════════════════════════════════════════════════

_WINS_PER_PAGE = 8

class WinningsView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], user_id: int):
        super().__init__(timeout=120)
        self.pages = pages; self.current = 0; self.user_id = user_id; self._sync()

    def _sync(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.label == "◀": item.disabled = self.current == 0
                elif item.label == "▶": item.disabled = self.current >= len(self.pages) - 1

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, disabled=True)
    async def prev_page(self, interaction: discord.Interaction, btn):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your list.", ephemeral=True); return
        self.current -= 1; self._sync()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, btn):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your list.", ephemeral=True); return
        self.current += 1; self._sync()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def on_timeout(self):
        for item in self.children: item.disabled = True


@bot.tree.command(name="mywinnings", description="Check your giveaway win history in this server")
@app_commands.describe(user="User to check (defaults to yourself)")
@command_enabled()
async def mywinnings(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    await interaction.response.defer()
    guild_channel_set = {c.id for c in interaction.guild.channels}
    try:
        async with get_db() as db:
            async with db.execute(
                "SELECT gw.message_id, g.prize, g.end_time, g.channel_id "
                "FROM giveaway_winners gw JOIN giveaways g ON gw.message_id=g.message_id "
                "WHERE gw.winner_id=? ORDER BY g.end_time DESC", (user.id,)) as cur:
                all_rows = await cur.fetchall()
        rows = [r for r in all_rows if r[3] in guild_channel_set]
    except Exception as e:
        await interaction.followup.send(f"❌ Database error: {e}"); return

    if not rows:
        embed = discord.Embed(title=f"🏆 {user.display_name}'s Wins",
                              description="No giveaway wins found in this server yet.", color=discord.Color.gold())
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.followup.send(embed=embed); return

    async with get_db() as db:
        async with db.execute(
            "SELECT enabled FROM auto_entry_users WHERE guild_id=? AND user_id=?",
            (interaction.guild.id, user.id)) as cur:
            ae_row = await cur.fetchone()
    ae_status = "✅ On" if (ae_row and ae_row[0]) else "🔒 Off"

    totals: dict[str, int] = {"balance": 0, "exp": 0, "tickets": 0, "gamble_tokens": 0, "vip_keys": 0}
    for _, prize_raw, _, _ in rows:
        try:
            meta = json.loads(prize_raw)
            if isinstance(meta, dict):
                for key in totals: totals[key] += int(meta.get(key, 0))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    reward_parts = []
    if totals["balance"] > 0:       reward_parts.append(f"💰 {totals['balance']:,} coins")
    if totals["exp"] > 0:           reward_parts.append(f"⭐ {totals['exp']:,} EXP")
    if totals["tickets"] > 0:       reward_parts.append(f"🎟 {totals['tickets']:,} tickets")
    if totals["gamble_tokens"] > 0: reward_parts.append(f"🎲 {totals['gamble_tokens']:,} tokens")
    if totals["vip_keys"] > 0:      reward_parts.append(f"🔑 {totals['vip_keys']:,} VIP keys")

    summary = (f"**Total wins:** {len(rows):,} | **Auto-entry:** {ae_status}\n"
               + (("**Total won:** " + " · ".join(reward_parts)) if reward_parts else "")
               + "\n\u200b")

    pages: list[discord.Embed] = []
    for page_start in range(0, len(rows), _WINS_PER_PAGE):
        chunk = rows[page_start:page_start + _WINS_PER_PAGE]
        page_num = page_start // _WINS_PER_PAGE + 1
        total_pages = (len(rows) + _WINS_PER_PAGE - 1) // _WINS_PER_PAGE
        embed = discord.Embed(title=f"🏆 {user.display_name}'s Wins", color=discord.Color.gold())
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.description = summary
        for msg_id, prize_raw, end_time, ch_id in chunk:
            try:
                meta = json.loads(prize_raw)
                prize_label = meta.get("label", str(prize_raw))
                reward_str = build_reward_summary(meta, interaction.guild)
            except (json.JSONDecodeError, TypeError, AttributeError):
                prize_label = str(prize_raw); reward_str = "—"
            ch = interaction.guild.get_channel(ch_id)
            ch_str = ch.mention if ch else "*(deleted channel)*"
            date_str = f"<t:{end_time}:D>" if end_time else "Unknown date"
            embed.add_field(name=f"🎉 {prize_label[:64]}", value=f"📅 {date_str} · {ch_str}\n💰 {reward_str}", inline=False)
        embed.set_footer(text=f"Page {page_num}/{total_pages} · {len(rows)} total win(s)")
        pages.append(embed)

    view = WinningsView(pages, interaction.user.id)
    await interaction.followup.send(embed=pages[0], view=view if len(pages) > 1 else None)

@bot.command(name="mywinnings")
async def pfx_mywinnings(ctx, user: discord.Member = None):
    await mywinnings._callback(FakeInteraction(ctx), user)

# ═══════════════════════════════════════════════════════
# GIVEAWAY ROLES
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="addgiveawayrole", description="Allow a role to manage giveaways")
@command_enabled()
async def addgiveawayrole(interaction: discord.Interaction, role: discord.Role):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT INTO giveaway_roles VALUES(?,?)", (interaction.guild.id, role.id))
            await db.commit()
    await interaction.response.send_message(f"✅ {role.mention} can now manage giveaways.")

@bot.tree.command(name="removegiveawayrole", description="Remove giveaway permissions from a role")
@command_enabled()
async def removegiveawayrole(interaction: discord.Interaction, role: discord.Role):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM giveaway_roles WHERE guild_id=? AND role_id=?",
                             (interaction.guild.id, role.id))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed giveaway permissions from {role.mention}")

@bot.command(name="addgiveawayrole")
async def pfx_addgiveawayrole(ctx, role: discord.Role):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await addgiveawayrole._callback(FakeInteraction(ctx), role)

@bot.command(name="removegiveawayrole")
async def pfx_removegiveawayrole(ctx, role: discord.Role):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await removegiveawayrole._callback(FakeInteraction(ctx), role)


# ═══════════════════════════════════════════════════════
# AUTO-ENTRY SYSTEM
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="addautoentryrole",
                  description="Add/update a role that allows auto-entry for giveaways")
@app_commands.describe(role="Role to allow",
                       message_requirement="Messages the user must send today to qualify (0 = no requirement)")
@command_enabled()
async def addautoentryrole(interaction: discord.Interaction, role: discord.Role, message_requirement: int = 0):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO auto_entry_roles(guild_id,role_id,message_requirement) VALUES(?,?,?) "
                "ON CONFLICT(guild_id,role_id) DO UPDATE SET message_requirement=excluded.message_requirement",
                (interaction.guild.id, role.id, message_requirement))
            await db.commit()
    req_str = f" — requires **{message_requirement}** messages today" if message_requirement else ""
    await interaction.response.send_message(f"✅ {role.mention} can use auto-entry{req_str}.")

@bot.command(name="addautoentryrole")
async def pfx_addautoentryrole(ctx, role: discord.Role, message_requirement: int = 0):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await addautoentryrole._callback(FakeInteraction(ctx), role, message_requirement)


@bot.tree.command(name="removeautoentryrole", description="Remove a role from auto-entry eligibility")
@app_commands.describe(role="Role to remove")
@command_enabled()
async def removeautoentryrole(interaction: discord.Interaction, role: discord.Role):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM auto_entry_roles WHERE guild_id=? AND role_id=?",
                             (interaction.guild.id, role.id))
            await db.commit()
    await interaction.response.send_message(f"🗑 {role.mention} removed from auto-entry eligibility.")

@bot.command(name="removeautoentryrole")
async def pfx_removeautoentryrole(ctx, role: discord.Role):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await removeautoentryrole._callback(FakeInteraction(ctx), role)


@bot.tree.command(name="listautoentryroles",
                  description="List roles that allow auto-entry and their requirements")
@command_enabled()
async def listautoentryroles(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute("SELECT role_id,message_requirement FROM auto_entry_roles WHERE guild_id=?",
                              (interaction.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("❌ No auto-entry roles configured.", ephemeral=True); return
    lines = []
    for rid, req in rows:
        r = interaction.guild.get_role(rid)
        name = r.mention if r else f"<@&{rid}>"
        lines.append(f"• {name}" + (f" — **{req}** messages/day required" if req else " — no requirement"))
    embed = discord.Embed(title="🎉 Auto-Entry Roles", description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

@bot.command(name="listautoentryroles")
async def pfx_listautoentryroles(ctx):
    await listautoentryroles._callback(FakeInteraction(ctx))


@bot.tree.command(name="autoentry", description="Toggle automatic entry into all server giveaways")
@command_enabled()
async def autoentry(interaction: discord.Interaction):
    gid, uid = interaction.guild.id, interaction.user.id
    async with get_db() as db:
        async with db.execute("SELECT role_id,message_requirement FROM auto_entry_roles WHERE guild_id=?", (gid,)) as cur:
            all_roles = await cur.fetchall()
    if not all_roles:
        await interaction.response.send_message("❌ Auto-entry is not configured for this server.", ephemeral=True); return

    member = interaction.guild.get_member(uid)
    user_rids = {r.id for r in member.roles} if member else set()
    eligible = False; no_role = True; unmet_reqs = []

    for rid, req in all_roles:
        if rid not in user_rids: continue
        no_role = False
        if req > 0:
            today_count = await get_today_msg_count(gid, uid)
            if today_count < req:
                unmet_reqs.append((interaction.guild.get_role(rid), req, today_count)); continue
        eligible = True; break

    if no_role:
        mentions = []
        for rid, req in all_roles:
            r = interaction.guild.get_role(rid)
            if r:
                suffix = f" *(needs {req} msgs/day)*" if req else ""
                mentions.append(f"{r.mention}{suffix}")
        await interaction.response.send_message(
            f"❌ You need one of these roles to use auto-entry: {', '.join(mentions)}", ephemeral=True); return

    if not eligible:
        lines = ["❌ You don't meet the daily message requirements:"]
        for role, req, got in unmet_reqs:
            name = role.mention if role else "?"
            lines.append(f"• {name}: **{got}/{req}** messages sent today")
        await interaction.response.send_message("\n".join(lines), ephemeral=True); return

    async with get_db() as db:
        async with db.execute("SELECT enabled FROM auto_entry_users WHERE guild_id=? AND user_id=?",
                              (gid, uid)) as cur:
            existing = await cur.fetchone()

    if existing:
        new_val = 0 if existing[0] else 1
        async with db_lock:
            async with get_db() as db:
                await db.execute("UPDATE auto_entry_users SET enabled=? WHERE guild_id=? AND user_id=?",
                                 (new_val, gid, uid))
                await db.commit()
    else:
        new_val = 1
        async with db_lock:
            async with get_db() as db:
                await db.execute("INSERT INTO auto_entry_users(guild_id,user_id,enabled) VALUES(?,?,1)", (gid, uid))
                await db.commit()

    if new_val:
        await interaction.response.send_message(
            "✅ Auto-entry **enabled** — you'll be automatically entered into all giveaways.", ephemeral=True)
    else:
        await interaction.response.send_message("🔒 Auto-entry **disabled**.", ephemeral=True)

@bot.command(name="autoentry")
async def pfx_autoentry(ctx):
    await autoentry._callback(FakeInteraction(ctx))

# ═══════════════════════════════════════════════════════
# AUTO-ENTRY THRESHOLD
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="setautoentrythreshold",
                  description="Set the minimum prize for auto-entry, and the recent-message window for smaller giveaways")
@app_commands.describe(
    min_prize_balance="Minimum coin reward (per winner) for a giveaway to be fully open",
    recent_message_window="For giveaways below the minimum: only users among the last N messages can join")
@command_enabled()
async def setautoentrythreshold(interaction: discord.Interaction, min_prize_balance: int, recent_message_window: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if min_prize_balance < 0 or recent_message_window < 1:
        await interaction.response.send_message(
            "❌ min_prize_balance must be ≥ 0 and recent_message_window must be ≥ 1.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO auto_entry_threshold(guild_id,min_prize_balance,recent_message_window) VALUES(?,?,?)",
                (interaction.guild.id, min_prize_balance, recent_message_window))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Giveaways with a per-winner reward of **{min_prize_balance:,}+ coins** are now fully open.\n"
        f"Giveaways below that: auto-entry is disabled, and only users among the "
        f"**last {recent_message_window}** messages in the giveaway's channel can join.")

@bot.command(name="setautoentrythreshold")
async def pfx_setautoentrythreshold(ctx, min_prize_balance: int, recent_message_window: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setautoentrythreshold._callback(FakeInteraction(ctx), min_prize_balance, recent_message_window)


@bot.tree.command(name="removeautoentrythreshold",
                  description="Remove the min-prize restriction — all giveaways become fully open again")
@command_enabled()
async def removeautoentrythreshold(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM auto_entry_threshold WHERE guild_id=?", (interaction.guild.id,))
            await db.commit()
    await interaction.response.send_message("🗑 Auto-entry threshold removed — all giveaways are now fully open.")

@bot.command(name="removeautoentrythreshold")
async def pfx_removeautoentrythreshold(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await removeautoentrythreshold._callback(FakeInteraction(ctx))

# ═══════════════════════════════════════════════════════
# NOTIFY CHANNEL
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="setnotifychannel",
                  description="Set the channel for giveaway and game start notifications")
@app_commands.describe(channel="Channel to post notifications in")
@command_enabled()
async def setnotifychannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO giveaway_game_notify_config(guild_id,channel_id) VALUES(?,?)",
                             (interaction.guild.id, channel.id))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Giveaway/game notifications → {channel.mention}\n"
        f"ℹ️ Only fully-open (auto-enterable) giveaways trigger a notification; games always do.")

@bot.command(name="setnotifychannel")
async def pfx_setnotifychannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setnotifychannel._callback(FakeInteraction(ctx), channel)


@bot.tree.command(name="removenotifychannel", description="Disable giveaway/game start notifications")
@command_enabled()
async def removenotifychannel(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM giveaway_game_notify_config WHERE guild_id=?", (interaction.guild.id,))
            await db.commit()
    await interaction.response.send_message("🔒 Giveaway/game notifications disabled.")

@bot.command(name="removenotifychannel")
async def pfx_removenotifychannel(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await removenotifychannel._callback(FakeInteraction(ctx))

# ═══════════════════════════════════════════════════════
# GAME PRESET DATA
# ═══════════════════════════════════════════════════════

def _ch(continent: str, cap_letter: str, extra: str = None) -> list[str]:
    h = [f"This country is in {continent}", f"Its capital city starts with the letter '{cap_letter}'"]
    if extra: h.append(extra)
    return h

_PRESET_DATA: dict[str, dict] = {
    "colors": {
        "description": "Common colors",
        "answers_hints": {
            "Red":      ["It is a primary color", "It is a warm color", "Associated with fire and blood"],
            "Orange":   ["It is a warm color", "Secondary color (red + yellow)", "Color of pumpkins"],
            "Yellow":   ["It is a primary color", "It is a warm color", "Color of the sun and bananas"],
            "Green":    ["It is a secondary color", "It is a cool color", "Color of grass and leaves"],
            "Blue":     ["It is a primary color", "It is a cool color", "Color of the sky and oceans"],
            "Purple":   ["It is a secondary color", "Historically associated with royalty", "Made by mixing red and blue"],
            "Pink":     ["It is a warm color", "Made by mixing red and white", "Color of flamingos"],
            "Brown":    ["Color of wood and chocolate", "Made by mixing all three primary colors"],
            "Black":    ["Absorbs all visible light", "The darkest possible color"],
            "White":    ["Reflects all visible light", "The lightest possible color"],
            "Gray":     ["Between black and white", "Color of storm clouds"],
            "Cyan":     ["Made by mixing blue and green", "Used in CMYK printing"],
            "Magenta":  ["Used in CMYK printing", "Mix of red and violet"],
            "Turquoise":["Mix of blue and green", "Color of the gemstone turquoise"],
            "Violet":   ["Shortest visible wavelength", "Part of the rainbow (ROY G BIV)"],
            "Indigo":   ["Between blue and violet", "One of Newton's seven rainbow colors"],
            "Maroon":   ["Dark brownish-red", "Named after the French word for chestnut"],
            "Navy":     ["Very dark blue", "Named after naval uniform color"],
            "Teal":     ["Combination of blue and green", "Named after the common teal duck"],
            "Coral":    ["Mix of orange, pink, and red", "Named after coral reef organisms"],
            "Gold":     ["Shiny metallic yellow", "Color of the precious metal gold"],
            "Silver":   ["Shiny metallic gray", "Color of the precious metal silver"],
            "Lavender": ["Light purple/violet", "Named after the lavender flower"],
            "Crimson":  ["Strong, deep red", "Associated with passion and urgency"],
            "Azure":    ["Bright cerulean blue", "Color of a clear midday sky"],
        }
    },
    "food": {
        "description": "Common foods",
        "answers_hints": {
            "Pizza":        ["Italian dish", "Baked in an oven", "Can be topped with pepperoni or vegetables"],
            "Sushi":        ["Japanese dish", "Usually involves rice and seafood", "Often served with wasabi"],
            "Burger":       ["American fast food", "Usually beef patty between two buns", "Often served with fries"],
            "Pasta":        ["Italian dish", "Made from flour and water", "Comes in shapes like spaghetti and penne"],
            "Tacos":        ["Mexican dish", "Served in a folded tortilla", "Filled with meat or vegetables"],
            "Ramen":        ["Japanese noodle soup", "Often topped with pork and egg", "Has a rich broth"],
            "Curry":        ["Common in South and Southeast Asia", "Made with spices", "Often served with rice"],
            "Steak":        ["Cut of beef", "Grilled or pan-fried", "Can be ordered rare or well-done"],
            "Fried rice":   ["Asian dish", "Made with leftover rice and egg", "Often cooked in a wok"],
            "Sandwich":     ["Food between two slices of bread", "Named after the Earl of Sandwich"],
            "Soup":         ["Liquid dish", "Can be hot or cold", "Made by cooking ingredients in broth"],
            "Salad":        ["Dish of raw vegetables", "Often dressed with oil and vinegar"],
            "Bread":        ["Baked food made from flour and water", "One of the oldest prepared foods"],
            "Chocolate":    ["Sweet food made from cacao beans", "Can be dark, milk, or white"],
            "Ice cream":    ["Frozen dessert", "Made with cream and sugar", "Comes in many flavors"],
            "Pancakes":     ["Flat cake cooked on a griddle", "Often served with syrup or fruit"],
            "Dumplings":    ["Dough filled with meat or vegetables", "Common in many Asian cuisines"],
            "Falafel":      ["Middle Eastern dish", "Deep-fried chickpea balls", "Often served in pita bread"],
            "Hummus":       ["Middle Eastern dip", "Made from chickpeas and tahini"],
            "Croissant":    ["French pastry", "Flaky and buttery", "Crescent-shaped baked good"],
            "Paella":       ["Spanish rice dish", "Often made with seafood or chicken"],
            "Hot dog":      ["American fast food", "Sausage in a long bun", "Common at sporting events"],
            "Pho":          ["Vietnamese noodle soup", "Made with broth and rice noodles"],
            "Churros":      ["Spanish dessert", "Fried dough pastry", "Often dipped in chocolate sauce"],
            "Biryani":      ["South Asian dish", "Fragrant rice cooked with spices and meat"],
            "Gyoza":        ["Japanese dumplings", "Usually filled with pork and cabbage"],
        }
    },
    "countries_africa": {
        "description": "Countries in Africa",
        "answers_hints": {
            "Algeria":          _ch("Africa","A","Largest country in Africa by area"),
            "Angola":           _ch("Africa","L"),
            "Cameroon":         _ch("Africa","Y","Called 'Africa in miniature' for its diversity"),
            "Egypt":            _ch("Africa","C","Home to the ancient pyramids and Sphinx"),
            "Ethiopia":         _ch("Africa","A","Never colonized; one of the oldest civilizations"),
            "Ghana":            _ch("Africa","A","First sub-Saharan country to gain independence"),
            "Kenya":            _ch("Africa","N","Famous for the Maasai Mara wildlife reserve"),
            "Libya":            _ch("Africa","T"),
            "Madagascar":       _ch("Africa","A","Fourth largest island in the world"),
            "Morocco":          _ch("Africa","R","Northernmost country in Africa"),
            "Mozambique":       _ch("Africa","M"),
            "Nigeria":          _ch("Africa","A","Most populous country in Africa"),
            "Rwanda":           _ch("Africa","K","Known as the 'Land of a Thousand Hills'"),
            "Senegal":          _ch("Africa","D"),
            "Somalia":          _ch("Africa","M","Easternmost country in Africa"),
            "South Africa":     _ch("Africa","P","Has three capital cities"),
            "South Sudan":      _ch("Africa","J","World's youngest country, independent since 2011"),
            "Sudan":            _ch("Africa","K"),
            "Tanzania":         _ch("Africa","D","Home to Mount Kilimanjaro, Africa's highest peak"),
            "Uganda":           _ch("Africa","K","Home to mountain gorillas"),
            "Zambia":           _ch("Africa","L","Home to Victoria Falls"),
            "Zimbabwe":         _ch("Africa","H","Shares Victoria Falls with Zambia"),
        }
    },
    "countries_europe": {
        "description": "Countries in Europe",
        "answers_hints": {
            "Austria":          _ch("Europe","V","Home of Mozart; former center of the Habsburg Empire"),
            "Belgium":          _ch("Europe","B","Home of the European Union headquarters"),
            "Croatia":          _ch("Europe","Z","Famous for Dubrovnik and Plitvice Lakes"),
            "Czech Republic":   _ch("Europe","P","Also called Czechia; home to medieval Prague"),
            "Denmark":          _ch("Europe","C","Birthplace of Hans Christian Andersen"),
            "Finland":          _ch("Europe","H","Home of Santa Claus (Rovaniemi)"),
            "France":           _ch("Europe","P","Home to the Eiffel Tower; most visited country"),
            "Germany":          _ch("Europe","B","Most populous country in the European Union"),
            "Greece":           _ch("Europe","A","Birthplace of democracy and the Olympic Games"),
            "Hungary":          _ch("Europe","B","Known for thermal baths and paprika"),
            "Iceland":          _ch("Europe","R","Most sparsely populated country in Europe"),
            "Ireland":          _ch("Europe","D","Known as the 'Emerald Isle'"),
            "Italy":            _ch("Europe","R","Home to the Roman Colosseum and pizza"),
            "Netherlands":      _ch("Europe","A","Famous for windmills, tulips, and canals"),
            "Norway":           _ch("Europe","O","Famous for fjords and the Northern Lights"),
            "Poland":           _ch("Europe","W"),
            "Portugal":         _ch("Europe","L","Westernmost country in continental Europe"),
            "Romania":          _ch("Europe","B","Home to Transylvania and the Dracula legend"),
            "Russia":           _ch("Europe","M","Largest country in the world by area"),
            "Spain":            _ch("Europe","M","Famous for flamenco and paella"),
            "Sweden":           _ch("Europe","S","Home of IKEA, ABBA, and Volvo"),
            "Switzerland":      _ch("Europe","B","Famous for chocolate, cheese, and watches"),
            "Ukraine":          _ch("Europe","K","Largest country lying entirely within Europe"),
            "United Kingdom":   _ch("Europe","L","Made up of England, Scotland, Wales, and Northern Ireland"),
        }
    },
    "countries_asia": {
        "description": "Countries in Asia",
        "answers_hints": {
            "China":            _ch("Asia","B","Most populous country in the world"),
            "India":            _ch("Asia","N","Second most populous country in the world"),
            "Japan":            _ch("Asia","T","Island nation known for Mount Fuji"),
            "South Korea":      _ch("Asia","S"),
            "Indonesia":        _ch("Asia","J","Largest archipelago nation in the world"),
            "Saudi Arabia":     _ch("Asia","R","Largest country in the Middle East"),
            "Turkey":           _ch("Asia","A","Bridges Europe and Asia; formerly the Ottoman Empire"),
            "Iran":             _ch("Asia","T","Formerly known as Persia"),
            "Iraq":             _ch("Asia","B","Location of ancient Mesopotamia"),
            "Thailand":         _ch("Asia","B","Known as the 'Land of Smiles'"),
            "Vietnam":          _ch("Asia","H","S-shaped country in Southeast Asia"),
            "Malaysia":         _ch("Asia","K","Home to the Petronas Twin Towers"),
            "Philippines":      _ch("Asia","M","Archipelago of over 7,600 islands"),
            "Pakistan":         _ch("Asia","I"),
            "Bangladesh":       _ch("Asia","D","One of the most densely populated countries"),
            "Nepal":            _ch("Asia","K","Home to Mount Everest, the world's highest peak"),
            "Israel":           _ch("Asia","J","Country in the Middle East"),
            "United Arab Emirates": _ch("Asia","A","Home to the Burj Khalifa, world's tallest building"),
            "Singapore":        _ch("Asia","S","City-state and one of the world's leading financial hubs"),
            "Kazakhstan":       _ch("Asia","N","Largest landlocked country in the world"),
        }
    },
    "countries_americas": {
        "description": "Countries in the Americas",
        "answers_hints": {
            "United States":    _ch("North America","W","Third largest country by area"),
            "Canada":           _ch("North America","O","Second largest country in the world by area"),
            "Mexico":           _ch("North America","M","Largest Spanish-speaking country in the world"),
            "Brazil":           _ch("South America","B","Largest country in South America"),
            "Argentina":        _ch("South America","B","Second largest country in South America"),
            "Colombia":         _ch("South America","B","Only South American country with coastlines on both oceans"),
            "Chile":            _ch("South America","S","Longest country in the world from north to south"),
            "Peru":             _ch("South America","L","Home to Machu Picchu and the Amazon River source"),
            "Venezuela":        _ch("South America","C","Home to Angel Falls, world's highest waterfall"),
            "Cuba":             _ch("the Caribbean","H","Largest island in the Caribbean"),
            "Jamaica":          _ch("the Caribbean","K","Birthplace of reggae music and Bob Marley"),
            "Panama":           _ch("Central America","P","Home to the famous canal connecting two oceans"),
            "Costa Rica":       _ch("Central America","S","Has no standing army; known for biodiversity"),
            "Guatemala":        _ch("Central America","G","Most populous country in Central America"),
        }
    },
    "fruits": {
        "description": "Common fruits",
        "answers_hints": {
            "Apple":        ["Common red or green fruit", "Grows on trees", "'An apple a day keeps the doctor away'"],
            "Banana":       ["Yellow tropical fruit", "High in potassium", "Grows in large bunches"],
            "Orange":       ["Citrus fruit", "High in vitamin C", "Named after its color"],
            "Grape":        ["Grows in clusters on vines", "Used to make wine and raisins"],
            "Strawberry":   ["Red fruit with seeds on the outside", "Heart-shaped", "Popular in desserts and jam"],
            "Watermelon":   ["Large green fruit, red inside", "About 92% water", "Very popular in summer"],
            "Mango":        ["Tropical fruit", "Yellow or orange flesh", "Called the 'king of fruits'"],
            "Pineapple":    ["Tropical fruit with spiky exterior", "Yellow sweet flesh inside"],
            "Peach":        ["Fuzzy skin, orange-yellow flesh", "Related to plums and cherries"],
            "Cherry":       ["Small round red fruit", "Grows on trees", "Has a hard stone inside"],
            "Kiwi":         ["Brown fuzzy exterior, bright green inside", "Named after the New Zealand bird"],
            "Lemon":        ["Yellow citrus fruit", "Very sour taste", "Used in cooking and cleaning"],
            "Coconut":      ["Large brown tropical fruit", "White flesh and coconut water inside"],
            "Avocado":      ["Technically a fruit (berry)", "Green creamy flesh", "Used to make guacamole"],
            "Blueberry":    ["Small blue/purple berry", "Very high in antioxidants"],
            "Mango":        ["Tropical fruit", "Yellow or orange flesh", "National fruit of India"],
            "Pomegranate":  ["Red fruit full of seeds", "High in antioxidants", "Ancient fruit from the Middle East"],
            "Dragon fruit": ["Cactus fruit", "Pink or yellow exterior", "White or red flesh with black seeds"],
        }
    },
    "vegetables": {
        "description": "Common vegetables",
        "answers_hints": {
            "Carrot":       ["Orange root vegetable", "Rich in vitamin A", "Rabbits famously love this vegetable"],
            "Broccoli":     ["Green vegetable that looks like a tiny tree", "Member of the cabbage family"],
            "Potato":       ["Starchy root vegetable", "Used for chips, fries, and mash"],
            "Tomato":       ["Technically a fruit but used as a vegetable", "Key ingredient in pizza sauce"],
            "Onion":        ["Makes your eyes water when cutting it", "Used as a base in almost every cuisine"],
            "Garlic":       ["Related to the onion family", "Strong flavor and smell", "Folklore says it repels vampires"],
            "Spinach":      ["Dark green leafy vegetable", "High in iron", "Popeye the Sailor Man eats this"],
            "Cucumber":     ["Long green vegetable", "Very high water content", "Often pickled to make gherkins"],
            "Bell pepper":  ["Can be red, green, or yellow", "Sweet and crunchy texture"],
            "Corn":         ["Also called maize", "Yellow kernels on a cob", "Can be popped into popcorn"],
            "Cauliflower":  ["White vegetable", "Related to broccoli and cabbage"],
            "Eggplant":     ["Purple vegetable", "Also called aubergine in British English"],
            "Pumpkin":      ["Large orange squash", "Used in pies and carved for Halloween"],
            "Asparagus":    ["Long green stalks", "A spring vegetable"],
            "Mushroom":     ["Technically a fungus, not a plant", "Has a savory umami flavor"],
            "Sweet potato": ["Orange root vegetable", "Sweeter than a regular potato"],
        }
    },
}

# Build world preset from all continents
_PRESET_DATA["countries_world"] = {
    "description": "Countries from all continents",
    "answers_hints": {
        **_PRESET_DATA["countries_africa"]["answers_hints"],
        **_PRESET_DATA["countries_europe"]["answers_hints"],
        **_PRESET_DATA["countries_asia"]["answers_hints"],
        **_PRESET_DATA["countries_americas"]["answers_hints"],
    },
}

_PRESET_CHOICES = [
    app_commands.Choice(name=f"{k} ({len(v['answers_hints'])} entries)", value=k)
    for k, v in _PRESET_DATA.items()
]

# ═══════════════════════════════════════════════════════
# GAME COMMANDS
# ═══════════════════════════════════════════════════════

# ── reroll ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="reroll", description="Reroll a giveaway winner")
@app_commands.describe(message_id="Message ID of the giveaway to reroll")
@command_enabled()
async def slash_reroll(interaction: discord.Interaction, message_id: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    try: mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True); return
    await interaction.response.defer()
    async with get_db() as db:
        async with db.execute("SELECT * FROM giveaways WHERE message_id=?", (mid,)) as cur:
            data = await cur.fetchone()
    if not data:
        await interaction.followup.send("❌ Giveaway not found."); return
    (_mid, channel_id, prize_raw, winner_count, legacy_reward,
     end_time, required_role, template, ended) = data
    channel = bot.get_channel(channel_id)
    if not channel:
        await interaction.followup.send("❌ Channel not found."); return
    try: message = await channel.fetch_message(mid)
    except discord.NotFound:
        await interaction.followup.send("❌ Message not found."); return
    reaction = discord.utils.get(message.reactions, emoji="🎉")
    if not reaction:
        await interaction.followup.send("❌ Reaction not found."); return
    try:
        meta = json.loads(prize_raw)
        if not isinstance(meta, dict): raise TypeError
        prize_label = meta.get("label", prize_raw)
    except Exception:
        meta = {"label": str(prize_raw), "balance": legacy_reward}
        prize_label = str(prize_raw)
    users = await _get_eligible_giveaway_participants(channel, reaction, required_role, meta)
    if not users:
        await interaction.followup.send("❌ No participants."); return
    weighted = []
    for user in users:
        lvl = await get_level(interaction.guild.id, user.id)
        weighted.extend([user] * random.randint(1, max(1, lvl // 4)))
    new_winner = random.choice(weighted)
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO giveaway_winners VALUES(?,?,?)",
                             (mid, new_winner.id, int(meta.get("balance", 0))))
            await db.commit()
    await distribute_prizes(channel.guild, [new_winner], meta)
    embed = discord.Embed(title="🔄 Giveaway Rerolled", color=discord.Color.orange(),
        description=f"**Prize:** {prize_label}\n**Reward:** {build_reward_summary(meta, channel.guild)}\n**New Winner:** {new_winner.mention}")
    await channel.send(embed=embed)
    await interaction.followup.send("✅ Giveaway rerolled.")

# ── giveaway roles ────────────────────────────────────────────────────────────
@bot.tree.command(name="giveawayroles", description="List roles that can manage giveaways")
@command_enabled()
async def slash_giveawayroles(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute("SELECT role_id FROM giveaway_roles WHERE guild_id=?",
                              (interaction.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("❌ No giveaway roles configured.", ephemeral=True); return
    mentions = [r.mention for row in rows if (r := interaction.guild.get_role(row[0]))]
    embed = discord.Embed(title="🎉 Giveaway Roles",
                          description="\n".join(mentions) if mentions else "*(all deleted)*",
                          color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

# ── auto giveaway management ──────────────────────────────────────────────────
@bot.tree.command(name="removeautogiveaway", description="Remove an entry from the auto giveaway pool")
@app_commands.describe(entry_id="ID shown in /listautogiveaways")
@command_enabled()
async def slash_removeautogiveaway(interaction: discord.Interaction, entry_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT prize FROM auto_giveaway_pool WHERE id=? AND guild_id=?",
                                  (entry_id, interaction.guild.id)) as cur:
                row = await cur.fetchone()
            if not row:
                await interaction.response.send_message(f"❌ No auto giveaway with ID `#{entry_id}`.", ephemeral=True); return
            await db.execute("DELETE FROM auto_giveaway_pool WHERE id=?", (entry_id,))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed **{row[0]}** (`#{entry_id}`) from the auto pool.")

@bot.tree.command(name="listautogiveaways", description="List all entries in the auto giveaway pool")
@command_enabled()
async def slash_listautogiveaways(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute(
            "SELECT id,prize,winners,chance,reward_balance,reward_exp FROM auto_giveaway_pool "
            "WHERE guild_id=? ORDER BY id", (interaction.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("❌ Auto giveaway pool is empty.", ephemeral=True); return
    total_weight = sum(r[3] for r in rows)
    embed = discord.Embed(title="🎉 Auto Giveaway Pool", color=discord.Color.gold())
    for row_id, prize, winners, chance, rb, re in rows:
        pct = (chance / total_weight * 100) if total_weight > 0 else 0
        parts = []
        if rb: parts.append(f"💰{rb:,}")
        if re: parts.append(f"⭐{re:,}")
        embed.add_field(name=f"`#{row_id}` {prize}",
                        value=f"Winners: {winners} | **{pct:.1f}%** | {' + '.join(parts) or 'No reward'}",
                        inline=False)
    embed.set_footer(text=f"{len(rows)} item(s) | total weight: {total_weight}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="stopgiveaways", description="Stop automatic giveaways")
@command_enabled()
async def slash_stopgiveaways(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    gid = interaction.guild.id
    task = auto_giveaway_tasks.pop(gid, None)
    if task: task.cancel()
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE auto_giveaway_config SET running=0 WHERE guild_id=?", (gid,))
            await db.commit()
    await interaction.response.send_message("🛑 Automatic giveaways stopped.")

# ── game management ───────────────────────────────────────────────────────────
@bot.tree.command(name="removegame", description="Remove a game and all its answers/hints")
@app_commands.describe(name="Game name")
@command_enabled()
async def slash_removegame(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                                  (interaction.guild.id, name)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Game **{name}** not found.", ephemeral=True); return
            await db.execute("DELETE FROM games WHERE guild_id=? AND game_name=?", (interaction.guild.id, name))
            await db.execute("DELETE FROM game_answers WHERE guild_id=? AND game_name=?", (interaction.guild.id, name))
            await db.execute("DELETE FROM game_hints WHERE guild_id=? AND game_name=?", (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed game **{name}** and all its answers and hints.")

@bot.tree.command(name="enablegame", description="Enable a game")
@app_commands.describe(name="Game name")
@command_enabled()
async def slash_enablegame(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                                  (interaction.guild.id, name)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Game **{name}** not found.", ephemeral=True); return
            await db.execute("UPDATE games SET enabled=1 WHERE guild_id=? AND game_name=?",
                             (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"✅ Game **{name}** enabled.")

@bot.tree.command(name="disablegame", description="Disable a game without deleting it")
@app_commands.describe(name="Game name")
@command_enabled()
async def slash_disablegame(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                                  (interaction.guild.id, name)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Game **{name}** not found.", ephemeral=True); return
            await db.execute("UPDATE games SET enabled=0 WHERE guild_id=? AND game_name=?",
                             (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"🔒 Game **{name}** disabled.")

@bot.tree.command(name="editgame", description="Edit a game's rewards, chance, or answer time")
@app_commands.describe(
    name="Game name", reward_balance="New coin reward (blank = keep current)",
    reward_exp="New EXP reward", reward_tickets="New ticket reward",
    reward_gamble_tokens="New gamble token reward", reward_vip_keys="New VIP key reward",
    reward_item="New item reward", reward_item_qty="New item quantity",
    reward_role="New role reward", chance="New selection weight",
    answer_time="New answer time in seconds")
@command_enabled()
async def slash_editgame(interaction: discord.Interaction, name: str,
    reward_balance: int = None, reward_exp: int = None, reward_tickets: int = None,
    reward_gamble_tokens: int = None, reward_vip_keys: int = None,
    reward_item: str = None, reward_item_qty: int = None,
    reward_role: discord.Role = None, chance: float = None, answer_time: int = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute(
            "SELECT reward_balance,reward_exp,reward_tickets,reward_gamble_tokens,reward_vip_keys,"
            "reward_item,reward_item_qty,reward_role_id,chance,answer_time FROM games "
            "WHERE guild_id=? AND game_name=?", (interaction.guild.id, name)) as cur:
            row = await cur.fetchone()
    if not row:
        await interaction.response.send_message(f"❌ Game **{name}** not found.", ephemeral=True); return
    (cur_rb, cur_re, cur_rt, cur_rgt, cur_rvk, cur_ri, cur_riq,
     cur_rr, cur_chance, cur_atime) = row
    new_rb    = reward_balance        if reward_balance      is not None else cur_rb
    new_re    = reward_exp            if reward_exp          is not None else cur_re
    new_rt    = reward_tickets        if reward_tickets      is not None else cur_rt
    new_rgt   = reward_gamble_tokens  if reward_gamble_tokens is not None else cur_rgt
    new_rvk   = reward_vip_keys       if reward_vip_keys     is not None else cur_rvk
    new_ri    = reward_item           if reward_item         is not None else cur_ri
    new_riq   = reward_item_qty       if reward_item_qty     is not None else cur_riq
    new_rr    = reward_role.id        if reward_role         is not None else cur_rr
    new_ch    = chance                if chance              is not None else cur_chance
    new_at    = answer_time           if answer_time         is not None else cur_atime
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "UPDATE games SET reward_balance=?,reward_exp=?,reward_tickets=?,"
                "reward_gamble_tokens=?,reward_vip_keys=?,reward_item=?,reward_item_qty=?,"
                "reward_role_id=?,chance=?,answer_time=? WHERE guild_id=? AND game_name=?",
                (new_rb, new_re, new_rt, new_rgt, new_rvk, new_ri, new_riq,
                 new_rr, new_ch, new_at, interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Game **{name}** updated.\n"
        f"Rewards: 💰{new_rb:,} ⭐{new_re:,} 🎟{new_rt} 🎲{new_rgt} 🔑{new_rvk}"
        + (f" 🎒{new_riq}x {new_ri}" if new_ri else "")
        + f" | Weight: {new_ch} | Time: {new_at}s")

@bot.command(name="editgame")
async def pfx_editgame(ctx, name: str, field: str, *, value: str):
    """Edit one field at a time: !editgame <name> <field> <value>
    Fields: balance, exp, tickets, gamble_tokens, vip_keys, item, item_qty, chance, answer_time"""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    field_map = {
        "balance": "reward_balance", "exp": "reward_exp", "tickets": "reward_tickets",
        "gamble_tokens": "reward_gamble_tokens", "vip_keys": "reward_vip_keys",
        "item": "reward_item", "item_qty": "reward_item_qty",
        "chance": "chance", "answer_time": "answer_time",
    }
    col = field_map.get(field.lower())
    if not col:
        await ctx.send(f"❌ Valid fields: {', '.join(field_map)}"); return
    async with get_db() as db:
        async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                              (ctx.guild.id, name)) as cur:
            if not await cur.fetchone():
                await ctx.send(f"❌ Game **{name}** not found."); return
    try:
        typed_val = float(value) if col in ("chance",) else (
            int(value) if col not in ("reward_item",) else value)
    except ValueError:
        typed_val = value
    async with db_lock:
        async with get_db() as db:
            await db.execute(f"UPDATE games SET {col}=? WHERE guild_id=? AND game_name=?",
                             (typed_val, ctx.guild.id, name))
            await db.commit()
    await ctx.send(f"✅ Game **{name}** — **{field}** set to `{typed_val}`.")

# ── game answers & hints ──────────────────────────────────────────────────────
@bot.tree.command(name="addgameanswer", description="Add an answer to a game")
@app_commands.describe(game_name="Game name (spaces allowed)", answer="The correct answer")
@command_enabled()
async def slash_addgameanswer(interaction: discord.Interaction, game_name: str, answer: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                              (interaction.guild.id, game_name)) as cur:
            if not await cur.fetchone():
                await interaction.response.send_message(
                    f"❌ Game **{game_name}** not found.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            cur = await db.execute("INSERT INTO game_answers(guild_id,game_name,answer) VALUES(?,?,?)",
                                   (interaction.guild.id, game_name, answer))
            new_id = cur.lastrowid
            await db.commit()
    await interaction.response.send_message(
        f"✅ Added answer `{answer}` to **{game_name}** (ID: #{new_id}).\n"
        f"Use `/addhint {game_name} {new_id} <order 1-5> <hint text>` to add hints.")

# Updated prefix version — game names with spaces need quotes: !addgameanswer "My Game" answer
@bot.command(name="addgameanswer")
async def pfx_addgameanswer(ctx, *, args: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    game_name, answer = _parse_game_name_and_rest(args)
    if not game_name or not answer:
        await ctx.send(f'❌ Usage: `{common._BOT_PREFIX}addgameanswer "Game Name" answer text`\n'
                       'Game names with spaces must be in quotes.'); return
    await slash_addgameanswer._callback(FakeInteraction(ctx), game_name, answer)


@bot.tree.command(name="removegameanswer", description="Remove an answer from a game by its ID")
@app_commands.describe(game_name="Game name", answer_id="Answer ID from /listgames <name>")
@command_enabled()
async def slash_removegameanswer(interaction: discord.Interaction, game_name: str, answer_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT id FROM game_answers WHERE id=? AND guild_id=? AND game_name=?",
                                  (answer_id, interaction.guild.id, game_name)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(
                        f"❌ Answer #{answer_id} not found in **{game_name}**.", ephemeral=True); return
            await db.execute("DELETE FROM game_answers WHERE id=?", (answer_id,))
            await db.execute("DELETE FROM game_hints WHERE guild_id=? AND game_name=? AND answer_id=?",
                             (interaction.guild.id, game_name, answer_id))
            await db.commit()
    await interaction.response.send_message(
        f"🗑 Removed answer #{answer_id} and its hints from **{game_name}**.")

@bot.command(name="removegameanswer")
async def pfx_removegameanswer(ctx, *, args: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    game_name, rest = _parse_game_name_and_rest(args)
    try: answer_id = int(rest)
    except ValueError:
        await ctx.send(f'❌ Usage: `{common._BOT_PREFIX}removegameanswer "Game Name" <answer_id>`'); return
    await slash_removegameanswer._callback(FakeInteraction(ctx), game_name, answer_id)


@bot.tree.command(name="listgames", description="List all games, or answers for a specific game")
@app_commands.describe(game_name="Leave blank to list all games, or enter a name to see its answers")
@command_enabled()
async def slash_listgames(interaction: discord.Interaction, game_name: str = None):
    await interaction.response.defer()
    gid = interaction.guild.id
    if game_name is None:
        async with get_db() as db:
            async with db.execute(
                "SELECT game_name,enabled,reward_balance,reward_exp,chance,answer_time "
                "FROM games WHERE guild_id=?", (gid,)) as cur:
                games = await cur.fetchall()
        if not games:
            await interaction.followup.send("❌ No games configured."); return
        lines = []
        for gname, enabled, rb, re, chance, atime in games:
            status = "✅" if enabled else "🔒"
            lines.append(f"{status} **{gname}** | 💰{rb:,} ⭐{re:,} | ⚖️{chance} ⏱{atime}s")
        embed = discord.Embed(title="🎮 Random Games", description="\n".join(lines), color=discord.Color.teal())
        await interaction.followup.send(embed=embed)
    else:
        async with get_db() as db:
            async with db.execute(
                "SELECT a.id,a.answer,COUNT(h.id) FROM game_answers a "
                "LEFT JOIN game_hints h ON h.answer_id=a.id AND h.guild_id=a.guild_id "
                "WHERE a.guild_id=? AND a.game_name=? GROUP BY a.id ORDER BY a.id",
                (gid, game_name)) as cur:
                answers = await cur.fetchall()
        if not answers:
            await interaction.followup.send(f"❌ No answers for **{game_name}** (or game not found)."); return
        lines = [f"`#{aid}` {'🔔'*hc if hc else '·'} {ans}" for aid, ans, hc in answers]
        text = "\n".join(lines)
        if len(text) > 1900: text = text[:1900] + "..."
        embed = discord.Embed(title=f"🎯 {game_name}", description=text, color=discord.Color.teal())
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="addhint", description="Add or replace a hint for a specific answer")
@app_commands.describe(game_name="Game name", answer_id="Answer ID from /listgames <name>",
                       order="Hint order 1-5 (revealed progressively)", hint="Hint text")
@command_enabled()
async def slash_addhint(interaction: discord.Interaction, game_name: str,
                        answer_id: int, order: int, hint: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if not (1 <= order <= 5):
        await interaction.response.send_message("❌ Order must be 1–5.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT answer FROM game_answers WHERE id=? AND guild_id=? AND game_name=?",
                              (answer_id, interaction.guild.id, game_name)) as cur:
            ans_row = await cur.fetchone()
    if not ans_row:
        await interaction.response.send_message(
            f"❌ Answer #{answer_id} not found in **{game_name}**.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM game_hints WHERE guild_id=? AND game_name=? AND answer_id=? AND hint_order=?",
                             (interaction.guild.id, game_name, answer_id, order))
            await db.execute("INSERT INTO game_hints(guild_id,game_name,answer_id,hint_text,hint_order) VALUES(?,?,?,?,?)",
                             (interaction.guild.id, game_name, answer_id, hint, order))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Hint #{order} set for answer **{ans_row[0]}** (#{answer_id}) in **{game_name}**.")

@bot.command(name="addhint")
async def pfx_addhint(ctx, *, args: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    # Format: "Game Name" answer_id order hint text
    game_name, rest = _parse_game_name_and_rest(args)
    parts = rest.split(None, 2)
    if len(parts) < 3:
        await ctx.send(f'❌ Usage: `{common._BOT_PREFIX}addhint "Game Name" <answer_id> <order 1-5> <hint>`'); return
    try: answer_id, order = int(parts[0]), int(parts[1])
    except ValueError:
        await ctx.send("❌ answer_id and order must be numbers."); return
    hint = parts[2]
    await slash_addhint._callback(FakeInteraction(ctx), game_name, answer_id, order, hint)


@bot.tree.command(name="removehint", description="Remove a hint by its ID")
@app_commands.describe(hint_id="Hint ID from /listhints")
@command_enabled()
async def slash_removehint(interaction: discord.Interaction, hint_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT hint_text FROM game_hints WHERE id=? AND guild_id=?",
                                  (hint_id, interaction.guild.id)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Hint #{hint_id} not found.", ephemeral=True); return
            await db.execute("DELETE FROM game_hints WHERE id=?", (hint_id,))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed hint #{hint_id}.")


@bot.tree.command(name="listhints", description="List hints for a game (all answers, or one answer)")
@app_commands.describe(game_name="Game name", answer_id="Filter to a specific answer ID (optional)")
@command_enabled()
async def slash_listhints(interaction: discord.Interaction, game_name: str, answer_id: int = None):
    await interaction.response.defer()
    async with get_db() as db:
        if answer_id is not None:
            async with db.execute(
                "SELECT h.id,a.id,a.answer,h.hint_order,h.hint_text "
                "FROM game_hints h JOIN game_answers a ON h.answer_id=a.id "
                "WHERE h.guild_id=? AND h.game_name=? AND h.answer_id=? ORDER BY h.hint_order",
                (interaction.guild.id, game_name, answer_id)) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT h.id,a.id,a.answer,h.hint_order,h.hint_text "
                "FROM game_hints h JOIN game_answers a ON h.answer_id=a.id "
                "WHERE h.guild_id=? AND h.game_name=? ORDER BY a.id,h.hint_order",
                (interaction.guild.id, game_name)) as cur:
                rows = await cur.fetchall()
    if not rows:
        await interaction.followup.send(f"❌ No hints found for **{game_name}**."); return
    lines = []; last_aid = None
    for h_id, a_id, answer, h_order, h_text in rows:
        if a_id != last_aid: lines.append(f"**`#{a_id}` {answer}**"); last_aid = a_id
        lines.append(f"  `[#{h_id}]` Hint {h_order}: {h_text}")
    text = "\n".join(lines)
    if len(text) > 1900: text = text[:1900] + "..."
    embed = discord.Embed(title=f"💡 Hints — {game_name}", description=text, color=discord.Color.teal())
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="stopgames", description="Stop the random games loop")
@command_enabled()
async def slash_stopgames(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    gid = interaction.guild.id
    task = game_tasks.pop(gid, None)
    if task: task.cancel()
    active_game_sessions.pop(gid, None)
    await interaction.response.send_message("🛑 Random games stopped.")


# ── Prefix wrappers for single-name commands (use * to capture full name) ─────
# Replace the existing prefix versions of these commands with these:

@bot.command(name="removegame")
async def pfx_removegame_new(ctx, *, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await slash_removegame._callback(FakeInteraction(ctx), name)

@bot.command(name="enablegame")
async def pfx_enablegame_new(ctx, *, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await slash_enablegame._callback(FakeInteraction(ctx), name)

@bot.command(name="disablegame")
async def pfx_disablegame_new(ctx, *, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await slash_disablegame._callback(FakeInteraction(ctx), name)

@bot.command(name="listgames")
async def pfx_listgames_new(ctx, *, game_name: str = None):
    await slash_listgames._callback(FakeInteraction(ctx), game_name)

@bot.command(name="listhints")
async def pfx_listhints_new(ctx, *, game_name: str):
    await slash_listhints._callback(FakeInteraction(ctx), game_name, None)

@bot.command(name="stopgiveaways")
async def pfx_stopgiveaways_new(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await slash_stopgiveaways._callback(FakeInteraction(ctx))

@bot.command(name="stopgames")
async def pfx_stopgames_new(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await slash_stopgames._callback(FakeInteraction(ctx))

@bot.command(name="giveawayroles")
async def pfx_giveawayroles_new(ctx):
    await slash_giveawayroles._callback(FakeInteraction(ctx))

@bot.command(name="removeautogiveaway")
async def pfx_removeautogiveaway_new(ctx, entry_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await slash_removeautogiveaway._callback(FakeInteraction(ctx), entry_id)

@bot.command(name="listautogiveaways")
async def pfx_listautogiveaways_new(ctx):
    await slash_listautogiveaways._callback(FakeInteraction(ctx))

# =================================================================================

@bot.tree.command(name="addgame", description="Add a random game to the pool")
@app_commands.describe(
    name="The question/prompt shown to players",
    reward_balance="Coin reward for winner", reward_exp="EXP reward for winner",
    reward_tickets="Mega ticket reward", reward_gamble_tokens="Gamble token reward",
    reward_vip_keys="VIP Chest Key reward", reward_item="Item or box name reward",
    reward_item_qty="Quantity of item reward (default 1)", reward_role="Role to give the winner",
    chance="Selection weight — higher = chosen more often (default 1.0)",
    answer_time="Seconds players have to answer this game (default 30)")
@command_enabled()
async def addgame(interaction: discord.Interaction, name: str,
                  reward_balance: int = 0, reward_exp: int = 0,
                  reward_tickets: int = 0, reward_gamble_tokens: int = 0,
                  reward_vip_keys: int = 0, reward_item: str = None,
                  reward_item_qty: int = 1, reward_role: discord.Role = None,
                  chance: float = 1.0, answer_time: int = 30):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if chance <= 0:
        await interaction.response.send_message("❌ Chance must be > 0.", ephemeral=True); return
    if answer_time < 5:
        await interaction.response.send_message("❌ Answer time must be ≥ 5 seconds.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute(
                    "INSERT INTO games(guild_id,game_name,reward_balance,reward_exp,reward_tickets,"
                    "reward_gamble_tokens,reward_vip_keys,reward_item,reward_item_qty,reward_role_id,chance,answer_time) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (interaction.guild.id, name, reward_balance, reward_exp, reward_tickets,
                     reward_gamble_tokens, reward_vip_keys, reward_item, reward_item_qty,
                     reward_role.id if reward_role else 0, chance, answer_time))
                await db.commit()
            except Exception:
                await interaction.response.send_message(f"❌ Game **{name}** already exists.", ephemeral=True); return
    parts = []
    if reward_balance > 0:        parts.append(f"💰 {reward_balance:,}")
    if reward_exp > 0:            parts.append(f"⭐ {reward_exp:,} EXP")
    if reward_tickets > 0:        parts.append(f"🎟 {reward_tickets}")
    if reward_gamble_tokens > 0:  parts.append(f"🎲 {reward_gamble_tokens}")
    if reward_vip_keys > 0:       parts.append(f"🔑 {reward_vip_keys}")
    if reward_item:               parts.append(f"🎒 {reward_item_qty}x {reward_item}")
    if reward_role:               parts.append(f"👑 {reward_role.mention}")
    await interaction.response.send_message(
        f"✅ Added game **{name}**\n"
        f"Reward: {' + '.join(parts) or 'None'} | Chance weight: {chance} | Answer time: {answer_time}s\n"
        f"Use `/addgameanswer` or `/addgamepreset` to add answers.")

@bot.command(name="addgame")
async def pfx_addgame(ctx, name: str, reward_balance: int = 0, reward_exp: int = 0,
                       chance: float = 1.0, answer_time: int = 30):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await addgame._callback(FakeInteraction(ctx), name, reward_balance, reward_exp,
                            0, 0, 0, None, 1, None, chance, answer_time)

@bot.command(name="addgameanswer")
async def cmd_addgameanswer(ctx, game_name: str, *, answer: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with get_db() as db:
        async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                              (ctx.guild.id, game_name)) as cur:
            if not await cur.fetchone(): await ctx.send(f"❌ Game **{game_name}** not found."); return
    async with db_lock:
        async with get_db() as db:
            cur = await db.execute("INSERT INTO game_answers(guild_id,game_name,answer) VALUES(?,?,?)",
                                   (ctx.guild.id, game_name, answer))
            new_id = cur.lastrowid
            await db.commit()
    await ctx.send(f"✅ Added answer `{answer}` to **{game_name}** (ID: #{new_id}).\n"
                   f"Use `!addhint {game_name} {new_id} <order 1-5> <hint text>` to add hints.")

@bot.command(name="removegameanswer")
async def cmd_removegameanswer(ctx, game_name: str, answer_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT id FROM game_answers WHERE id=? AND guild_id=? AND game_name=?",
                                  (answer_id, ctx.guild.id, game_name)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Answer #{answer_id} not found."); return
            await db.execute("DELETE FROM game_answers WHERE id=?", (answer_id,))
            await db.execute("DELETE FROM game_hints WHERE guild_id=? AND game_name=? AND answer_id=?",
                             (ctx.guild.id, game_name, answer_id))
            await db.commit()
    await ctx.send(f"🗑 Removed answer #{answer_id} and its hints from **{game_name}**.")

@bot.command(name="addhint")
async def cmd_addhint(ctx, game_name: str, answer_id: int, order: int, *, hint: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if not (1 <= order <= 5): await ctx.send("❌ Order must be 1–5."); return
    async with get_db() as db:
        async with db.execute("SELECT answer FROM game_answers WHERE id=? AND guild_id=? AND game_name=?",
                              (answer_id, ctx.guild.id, game_name)) as cur:
            ans_row = await cur.fetchone()
    if not ans_row: await ctx.send(f"❌ Answer #{answer_id} not found in **{game_name}**."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM game_hints WHERE guild_id=? AND game_name=? AND answer_id=? AND hint_order=?",
                             (ctx.guild.id, game_name, answer_id, order))
            await db.execute("INSERT INTO game_hints(guild_id,game_name,answer_id,hint_text,hint_order) VALUES(?,?,?,?,?)",
                             (ctx.guild.id, game_name, answer_id, hint, order))
            await db.commit()
    await ctx.send(f"✅ Hint #{order} set for answer **{ans_row[0]}** (#{answer_id}) in **{game_name}**.")

@bot.command(name="removehint")
async def cmd_removehint(ctx, hint_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT hint_text FROM game_hints WHERE id=? AND guild_id=?",
                                  (hint_id, ctx.guild.id)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Hint #{hint_id} not found."); return
            await db.execute("DELETE FROM game_hints WHERE id=?", (hint_id,))
            await db.commit()
    await ctx.send(f"🗑 Removed hint #{hint_id}.")

@bot.tree.command(name="addgamepreset", description="Bulk-add a preset of answers (and hints) to a game")
@app_commands.describe(game_name="Game to add answers to", preset="Which preset to load")
@app_commands.choices(preset=_PRESET_CHOICES)
@command_enabled()
async def addgamepreset(interaction: discord.Interaction, game_name: str, preset: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await interaction.response.defer()
    async with get_db() as db:
        async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                              (interaction.guild.id, game_name)) as cur:
            if not await cur.fetchone():
                await interaction.followup.send(f"❌ Game **{game_name}** not found."); return

    preset_data = _PRESET_DATA[preset]
    added = skipped = hints_added = 0
    async with db_lock:
        async with get_db() as db:
            for answer, hints in preset_data["answers_hints"].items():
                async with db.execute(
                    "SELECT id FROM game_answers WHERE guild_id=? AND game_name=? AND answer=?",
                    (interaction.guild.id, game_name, answer)) as cur:
                    existing = await cur.fetchone()
                if existing:
                    ans_id = existing[0]; skipped += 1
                else:
                    cur = await db.execute("INSERT INTO game_answers(guild_id,game_name,answer) VALUES(?,?,?)",
                                           (interaction.guild.id, game_name, answer))
                    ans_id = cur.lastrowid; added += 1
                await db.execute("DELETE FROM game_hints WHERE guild_id=? AND game_name=? AND answer_id=?",
                                 (interaction.guild.id, game_name, ans_id))
                for order, hint_text in enumerate(hints[:5], 1):
                    await db.execute(
                        "INSERT INTO game_hints(guild_id,game_name,answer_id,hint_text,hint_order) VALUES(?,?,?,?,?)",
                        (interaction.guild.id, game_name, ans_id, hint_text, order))
                    hints_added += 1
            await db.commit()

    total = len(preset_data["answers_hints"])
    await interaction.followup.send(
        f"✅ Preset **{preset}** loaded into **{game_name}**!\n"
        f"Added: **{added}** answers | Skipped (already existed): **{skipped}** | "
        f"Hints written: **{hints_added}** (across {total} total entries)")

@bot.command(name="addgamepreset")
async def pfx_addgamepreset(ctx, game_name: str, preset: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if preset not in _PRESET_DATA:
        await ctx.send(f"❌ Valid presets: {', '.join(_PRESET_DATA.keys())}"); return
    await addgamepreset._callback(FakeInteraction(ctx), game_name, preset)


@bot.tree.command(name="setgamechannel",
                  description="Set the channel for random games, interval, and hint timing")
@app_commands.describe(
    channel="Channel for games", interval_seconds="Seconds between one game ending and the next starting",
    hint1_delay="Seconds after question to reveal hint 1",
    hint2_delay="Seconds after question to reveal hint 2",
    hint3_delay="Seconds after question to reveal hint 3")
@command_enabled()
async def setgamechannel(interaction: discord.Interaction, channel: discord.TextChannel,
                         interval_seconds: int = 60, hint1_delay: Optional[int] = None,
                         hint2_delay: Optional[int] = None, hint3_delay: Optional[int] = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if interval_seconds < 5:
        await interaction.response.send_message("❌ Interval must be ≥ 5 seconds.", ephemeral=True); return
    delays = [d for d in [hint1_delay, hint2_delay, hint3_delay] if d is not None]
    hint_delays_json = json.dumps(delays) if delays else None
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO game_config(guild_id,channel_id,interval_seconds,hint_delays) VALUES(?,?,?,?)",
                (interaction.guild.id, channel.id, interval_seconds, hint_delays_json))
            await db.commit()
    hint_info = f" | Hints at: {', '.join(str(d)+'s' for d in delays)}" if delays else " | No hints configured"
    await interaction.response.send_message(
        f"✅ Game channel: {channel.mention} | Interval: **{interval_seconds}s** (after game ends){hint_info}")

@bot.command(name="setgamechannel")
async def pfx_setgamechannel(ctx, channel: discord.TextChannel, interval_seconds: int = 60):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setgamechannel._callback(FakeInteraction(ctx), channel, interval_seconds)


@bot.tree.command(name="startgames", description="Start automatic random games")
@command_enabled()
async def startgames(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    gid = interaction.guild.id
    if gid in game_tasks and not game_tasks[gid].done():
        await interaction.response.send_message("❌ Games already running.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT channel_id FROM game_config WHERE guild_id=?", (gid,)) as cur:
            if not await cur.fetchone():
                await interaction.response.send_message("❌ Use `/setgamechannel` first.", ephemeral=True); return
        async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND enabled=1", (gid,)) as cur:
            if not await cur.fetchall():
                await interaction.response.send_message("❌ No enabled games. Use `/addgame`.", ephemeral=True); return
    game_tasks[gid] = asyncio.create_task(guild_game_loop(gid))
    await interaction.response.send_message("🎮 Random games started!")

@bot.command(name="startgames")
async def pfx_startgames(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await startgames._callback(FakeInteraction(ctx))
    
# ═══════════════════════════════════════════════════════
# GAME LOOP
# ═══════════════════════════════════════════════════════

async def _send_hint_at(channel: discord.TextChannel, hint_text: str,
                         delay_secs: float, stop_event: asyncio.Event):
    """Wait delay_secs, then post a hint — unless the game was already answered."""
    try:
        await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=delay_secs)
    except asyncio.TimeoutError:
        if not stop_event.is_set():
            await channel.send(f"💡 **Hint:** {hint_text}")
    except (asyncio.CancelledError, Exception):
        pass


async def guild_game_loop(guild_id: int):
    await bot.wait_until_ready()
    while not bot.is_closed():
        async with get_db() as db:
            async with db.execute(
                "SELECT channel_id,interval_seconds,hint_delays FROM game_config WHERE guild_id=?",
                (guild_id,)) as cur:
                config = await cur.fetchone()
        if not config: break
        channel_id, interval_seconds, hint_delays_json = config
        channel = bot.get_channel(channel_id)
        if not channel: await asyncio.sleep(30); continue

        hint_delays: list[int] = []
        if hint_delays_json:
            try: hint_delays = json.loads(hint_delays_json)
            except Exception: hint_delays = []

        async with get_db() as db:
            async with db.execute(
                "SELECT game_name,reward_balance,reward_exp,reward_tickets,reward_gamble_tokens,"
                "reward_vip_keys,reward_item,reward_item_qty,reward_role_id,chance,answer_time "
                "FROM games WHERE guild_id=? AND enabled=1", (guild_id,)) as cur:
                game_rows = await cur.fetchall()

        eligible: list[dict] = []
        for row in game_rows:
            (gname, rb, re, rt, rgt, rvk, ri, riq, rrole, chance, atime) = row
            async with get_db() as db:
                async with db.execute("SELECT id,answer FROM game_answers WHERE guild_id=? AND game_name=?",
                                      (guild_id, gname)) as cur:
                    answers = await cur.fetchall()
            if answers:
                eligible.append({
                    "name": gname, "reward_balance": rb or 0, "reward_exp": re or 0,
                    "reward_tickets": rt or 0, "reward_gamble_tokens": rgt or 0,
                    "reward_vip_keys": rvk or 0, "reward_item": ri,
                    "reward_item_qty": riq or 1, "reward_role_id": rrole or 0,
                    "chance": chance or 1.0, "answer_time": atime or 30, "answers": answers,
                })

        if not eligible: await asyncio.sleep(interval_seconds); continue

        game = random.choices(eligible, weights=[g["chance"] for g in eligible], k=1)[0]
        correct_id, correct_ans = random.choice(game["answers"])
        answer_time = game["answer_time"]

        guild_obj = bot.get_guild(guild_id)
        reward_parts = []
        if game["reward_balance"] > 0:       reward_parts.append(f"💰 {game['reward_balance']:,} coins")
        if game["reward_exp"] > 0:           reward_parts.append(f"⭐ {game['reward_exp']:,} EXP")
        if game["reward_tickets"] > 0:       reward_parts.append(f"🎟 {game['reward_tickets']} ticket(s)")
        if game["reward_gamble_tokens"] > 0: reward_parts.append(f"🎲 {game['reward_gamble_tokens']} token(s)")
        if game["reward_vip_keys"] > 0:      reward_parts.append(f"🔑 {game['reward_vip_keys']} key(s)")
        if game["reward_item"]:              reward_parts.append(f"🎒 {game['reward_item_qty']}x {game['reward_item']}")
        if game["reward_role_id"] and guild_obj:
            role = guild_obj.get_role(game["reward_role_id"])
            if role: reward_parts.append(f"👑 {role.mention}")

        embed = discord.Embed(title="🎮 Random Game!", color=discord.Color.teal(),
            description=f"**{game['name']}**\n\nType your answer in chat!\n⏰ You have **{answer_time} seconds**.")
        if reward_parts:
            embed.add_field(name="🏆 Winner gets", value=" + ".join(reward_parts), inline=False)
        embed.set_footer(text=f"Answer within {answer_time} seconds!")
        await channel.send(embed=embed)

        notify_prize = " + ".join(reward_parts) if reward_parts else "No reward"
        await _send_giveaway_game_notify(guild_id, notify_prize, channel,
                                          extra_line=f"🎮 Game: {game['name']}")

        answered_event = asyncio.Event()
        active_game_sessions[guild_id] = {
            "game_name": game["name"], "answer": correct_ans,
            "channel_id": channel_id, "answered": False, "winner": None, "event": answered_event,
        }

        hints: list[str] = []
        if hint_delays:
            async with get_db() as db:
                async with db.execute(
                    "SELECT hint_text FROM game_hints "
                    "WHERE guild_id=? AND game_name=? AND answer_id=? ORDER BY hint_order",
                    (guild_id, game["name"], correct_id)) as cur:
                    hints = [r[0] for r in await cur.fetchall()]

        hint_tasks = []
        for i, delay in enumerate(hint_delays):
            if i < len(hints) and 0 < delay < answer_time:
                task = asyncio.create_task(
                    _send_hint_at(channel, f"{i+1}: {hints[i]}", delay, answered_event))
                hint_tasks.append(task)

        try:
            await asyncio.wait_for(answered_event.wait(), timeout=answer_time)
        except asyncio.TimeoutError:
            pass

        for task in hint_tasks:
            task.cancel()

        session = active_game_sessions.pop(guild_id, None)
        if not session: await asyncio.sleep(interval_seconds); continue

        if session.get("answered") and session.get("winner"):
            winner = session["winner"]
            if game["reward_balance"] > 0:
                await add_balance(guild_id, winner.id, game["reward_balance"], bot=bot)
            if game["reward_exp"] > 0:
                await add_exp(guild_id, winner.id, game["reward_exp"])
            if game["reward_tickets"] > 0:
                await add_tickets(guild_id, winner.id, game["reward_tickets"])
            if game["reward_gamble_tokens"] > 0:
                await inventory_add(guild_id, winner.id, GAMBLE_TOKEN, game["reward_gamble_tokens"])
            if game["reward_vip_keys"] > 0:
                await inventory_add(guild_id, winner.id, VIP_CHEST_KEY, game["reward_vip_keys"])
            if game["reward_item"]:
                await inventory_add(guild_id, winner.id, game["reward_item"], game["reward_item_qty"])
            if game["reward_role_id"] and guild_obj:
                role = guild_obj.get_role(game["reward_role_id"])
                member = guild_obj.get_member(winner.id)
                if role and member:
                    try: await member.add_roles(role)
                    except Exception: pass
            result_embed = discord.Embed(title="🎉 Correct!", color=discord.Color.green(),
                description=f"{winner.mention} got it! The answer was **{correct_ans}**.")
            if reward_parts:
                result_embed.add_field(name="Reward given", value=" + ".join(reward_parts), inline=False)
        else:
            result_embed = discord.Embed(title="⏰ Time's Up!", color=discord.Color.red(),
                description=f"Nobody got it. The answer was **{correct_ans}**.")

        await channel.send(embed=result_embed)
        await asyncio.sleep(interval_seconds)


async def game_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        for gid, task in list(game_tasks.items()):
            if task.done():
                try:
                    if exc := task.exception():
                        print(f"[GameLoop] Guild {gid} crashed: {exc}")
                except Exception:
                    pass
                game_tasks[gid] = asyncio.create_task(guild_game_loop(gid))
        await asyncio.sleep(30)

# ═══════════════════════════════════════════════════════
# CORE EVENTS
# ═══════════════════════════════════════════════════════

@bot.event
async def on_message(message):
    if message.author.bot: return
    if message.guild:
        session = active_game_sessions.get(message.guild.id)
        if session and not session.get("answered") and message.channel.id == session.get("channel_id"):
            if message.content.strip().lower() == session["answer"].lower():
                session["answered"] = True
                session["winner"] = message.author
                if "event" in session: session["event"].set()
    if message.content.startswith(common._BOT_PREFIX) and not _prefix_channel_allowed(message): return
    await bot.process_commands(message)


@bot.event
async def on_ready():
    await setup_database()
    await common._load_prefix()
    await load_disabled_commands()
    await load_prefix_restrictions()

    async with get_db() as db:
        async with db.execute("SELECT guild_id FROM auto_giveaway_config WHERE running=1") as cur:
            ag_guilds = [r[0] for r in await cur.fetchall()]
    for gid in ag_guilds:
        auto_giveaway_tasks[gid] = asyncio.create_task(auto_giveaway_loop(gid))
        print(f"[AutoGiveaway] Resumed for guild {gid}")

    _guild = discord.Object(id=_GUILD_ID)
    bot.tree.copy_global_to(guild=_guild)
    try:
        synced = await bot.tree.sync(guild=_guild)
        print(f"[Games Bot] Synced {len(synced)} commands to guild. Logged in as {bot.user}")
    except Exception as e:
        print(f"[Games Bot] Guild sync failed: {e}")
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()

    for task_fn in [giveaway_watcher, game_loop]:
        bot.loop.create_task(task_fn())

@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except discord.HTTPException as e:
        print(f"[Games Sync] Failed on join: {e}")
        

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure): return
    raise error


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Invalid argument: {error}")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        raise error


@bot.before_invoke
async def _log_prefix_command(ctx: commands.Context):
    if not ctx.guild: return
    embed = discord.Embed(
        description=f"{ctx.author.mention} used **`{common._BOT_PREFIX}{ctx.command.qualified_name}`**",
        color=discord.Color.light_grey(), timestamp=datetime.now(UTC))
    embed.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
    embed.set_footer(text=f"#{getattr(ctx.channel,'name','DM')} | UID: {ctx.author.id}")
    await log_event(ctx.guild.id, "command", embed)


if __name__ == "__main__":
    bot.run(TOKEN)
