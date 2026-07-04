import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta, UTC
 
import common
from common import (
    get_db, db_lock, setup_database, log_event, _log_embed, command_enabled,
    is_allowed_to_giveaway, _is_allowed_ctx,
    get_balance, add_balance, get_exp, add_exp, get_level, get_level_exp,
    ensure_stats, add_stat,
    inventory_add, inventory_remove, inventory_get, get_item, get_all_items, add_item, remove_item,
    get_tickets, add_tickets, get_gamble_tokens,
    _update_balance_rank,
    FakeInteraction, _MC,
    VIP_CHEST_KEY, GAMBLE_TOKEN,
    disabled_commands, global_disabled_commands, load_disabled_commands,
    prefix_channel_rules, _prefix_channel_allowed, load_prefix_restrictions,
    register_bot_instance,
)
 
TOKEN = os.getenv("TOKEN_ECONOMY")
 
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True
 
bot = commands.Bot(command_prefix=common.get_prefix, intents=intents, help_command=None)
register_bot_instance(bot)
 
# ═══════════════════════════════════════════════════════
# BALANCE
# ═══════════════════════════════════════════════════════
 
@bot.tree.command(name="balance", description="Check a balance")
@command_enabled()
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    bal = await get_balance(interaction.guild.id, user.id)
    embed = discord.Embed(title=f"💰 {user.display_name}'s Balance",
                          description=f"{bal:,} coins", color=discord.Color.green())
    await interaction.response.send_message(embed=embed)
 
@bot.command(name="balance")
async def pfx_balance(ctx, user: discord.Member = None):
    await balance._callback(FakeInteraction(ctx), user)
 
 
@bot.command(name="gift")
async def cmd_gift(ctx, user: discord.Member, amount: int):
    if amount <= 0: await ctx.send("❌ Amount must be > 0."); return
    if user.id == ctx.author.id: await ctx.send("❌ You cannot gift yourself."); return
    gid = ctx.guild.id
    bal = await get_balance(gid, ctx.author.id)
    if bal < amount: await ctx.send("❌ Not enough balance."); return
    await add_balance(gid, ctx.author.id, -amount, bot=bot)
    await add_balance(gid, user.id, amount, bot=bot)
    await add_stat(gid, ctx.author.id, "gifted_balance", amount)
    await ctx.send(f"💸 You gifted **{amount:,}** coins to {user.mention}!")
    await log_event(gid, "balance", _log_embed("🎁 Gift Sent", discord.Color.green(),
        From=ctx.author.mention, To=user.mention, Amount=f"{amount:,}"))
 
@bot.command(name="addbalance")
async def cmd_addbalance(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await add_balance(ctx.guild.id, user.id, amount, bot=bot)
    await ctx.send(f"✅ Added {amount:,} coins to {user.mention}.")
    await log_event(ctx.guild.id, "balance", _log_embed("💰 Balance Added", discord.Color.green(),
        Admin=ctx.author.mention, User=user.mention, Amount=f"+{amount:,}"))
    await log_event(ctx.guild.id, "admin", _log_embed("⚙️ addbalance", discord.Color.orange(),
        By=ctx.author.mention, User=user.mention, Amount=f"+{amount:,}"))
 
@bot.command(name="removebalance")
async def cmd_removebalance(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await add_balance(ctx.guild.id, user.id, -amount, bot=bot)
    await ctx.send(f"❌ Removed {amount:,} coins from {user.mention}.")
    await log_event(ctx.guild.id, "balance", _log_embed("💸 Balance Removed", discord.Color.red(),
        Admin=ctx.author.mention, User=user.mention, Amount=f"-{amount:,}"))
    await log_event(ctx.guild.id, "admin", _log_embed("⚙️ removebalance", discord.Color.orange(),
        By=ctx.author.mention, User=user.mention, Amount=f"-{amount:,}"))
 
# ═══════════════════════════════════════════════════════
# EXP / ACTIVITY RANK
# ═══════════════════════════════════════════════════════
 
@bot.tree.command(name="activityrank", description="Check a user's Activity Rank and EXP")
@command_enabled()
async def level(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    gid = interaction.guild.id
    exp    = await get_level_exp(gid, user.id)
    usable = await get_exp(gid, user.id)
    lvl    = await get_level(gid, user.id)
    embed = discord.Embed(title=f"⭐ {user.display_name}'s Activity Rank", color=discord.Color.gold())
    embed.add_field(name="Activity Rank", value=str(lvl), inline=False)
    embed.add_field(name="Total EXP (7d)", value=f"{exp:,}", inline=False)
    embed.add_field(name="Usable EXP", value=f"{usable:,}", inline=False)
    await interaction.response.send_message(embed=embed)
 
@bot.command(name="activityrank")
async def pfx_activityrank(ctx, user: discord.Member = None):
    await level._callback(FakeInteraction(ctx), user)
 
 
@bot.command(name="addexp")
async def cmd_addexp(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be > 0."); return
    await add_exp(ctx.guild.id, user.id, amount, is_bonus=True)
    await ctx.send(f"✅ Added **{amount:,}** usable EXP to {user.mention}.")
    await log_event(ctx.guild.id, "exp", _log_embed("⭐ Usable EXP Added", discord.Color.green(),
        Admin=ctx.author.mention, User=user.mention, Amount=f"+{amount:,}"))
 
@bot.command(name="removeexp")
async def cmd_removeexp(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await add_exp(ctx.guild.id, user.id, -amount)
    await ctx.send(f"❌ Removed {amount:,} EXP from {user.mention}.")
    await log_event(ctx.guild.id, "exp", _log_embed("📉 EXP Removed", discord.Color.red(),
        Admin=ctx.author.mention, User=user.mention, Amount=f"-{amount:,}"))
 
@bot.command(name="addtotalexp")
async def cmd_addtotalexp(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be > 0."); return
    now = int(datetime.now(UTC).timestamp())
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                             (ctx.guild.id, user.id, amount, now, 0))
            await db.execute("INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                             (ctx.guild.id, user.id, -amount, now, 0))
            await db.commit()
    await ctx.send(f"✅ Added **{amount:,}** to {user.mention}'s Total EXP (7d) / Activity Rank. Usable EXP unchanged.")
 
@bot.command(name="removetotalexp")
async def cmd_removetotalexp(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be > 0."); return
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    remaining = amount; actually_removed = 0
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT rowid, amount FROM exp_history "
                "WHERE guild_id=? AND user_id=? AND timestamp>=? AND amount>0 AND is_bonus=0 "
                "ORDER BY timestamp ASC", (ctx.guild.id, user.id, week_ago)) as cur:
                entries = await cur.fetchall()
            for rowid, entry_amount in entries:
                if remaining <= 0: break
                if entry_amount <= remaining:
                    await db.execute("DELETE FROM exp_history WHERE rowid=?", (rowid,))
                    remaining -= entry_amount
                else:
                    await db.execute("UPDATE exp_history SET amount=? WHERE rowid=?",
                                     (entry_amount - remaining, rowid)); remaining = 0
            actually_removed = amount - remaining
            if actually_removed > 0:
                await db.execute(
                    "INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                    (ctx.guild.id, user.id, actually_removed, int(datetime.now(UTC).timestamp()), 1))
            await db.commit()
    if actually_removed == 0:
        await ctx.send(f"❌ {user.mention} has no Total EXP (7d) to remove.")
    else:
        await ctx.send(f"✅ Removed **{actually_removed:,}** from {user.mention}'s Total EXP (7d). Usable EXP unchanged.")
    await log_event(ctx.guild.id, "exp", _log_embed("📉 Total EXP Removed", discord.Color.orange(),
        Admin=ctx.author.mention, User=user.mention,
        Removed=f"-{actually_removed:,}", Requested=f"-{amount:,}"))
 
# ═══════════════════════════════════════════════════════
# EXP BOOSTS
# ═══════════════════════════════════════════════════════
 
@bot.tree.command(name="expboost",
                  description="Set an EXP boost for a role — optionally limit it to a channel or category")
@app_commands.describe(
    role="Role to boost", boost="e.g. 1.5 = +1.5%, -25 = penalty. All matching boosts are summed.",
    channel="Only apply in this channel", category="Only apply in this category")
@command_enabled()
async def expboost(interaction: discord.Interaction, role: discord.Role, boost: float,
                   channel: discord.TextChannel = None, category: discord.CategoryChannel = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if boost == 0:
        await interaction.response.send_message("❌ Boost cannot be 0%.", ephemeral=True); return
    if channel and category:
        await interaction.response.send_message("❌ Specify a channel OR a category, not both.", ephemeral=True); return
    channel_id  = channel.id  if channel  else 0
    category_id = category.id if category else 0
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO exp_boosts VALUES(?,?,?,?,?)",
                             (interaction.guild.id, role.id, boost, channel_id, category_id))
            await db.commit()
    sign  = "+" if boost > 0 else ""
    scope = "globally"
    if channel:   scope = f"in {channel.mention} only"
    elif category: scope = f"in the **{category.name}** category only"
    await interaction.response.send_message(
        f"✅ {role.mention} now earns **{sign}{boost}% EXP** per message {scope}.")
 
@bot.command(name="expboost")
async def pfx_expboost(ctx, role: discord.Role, boost: float, channel: discord.TextChannel = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await expboost._callback(FakeInteraction(ctx), role, boost, channel, None)
 
 
@bot.tree.command(name="removeexpboost",
                  description="Remove an EXP boost — specify the same scope used when it was set")
@app_commands.describe(role="Role to remove boost from", channel="Channel-specific boost", category="Category-specific boost")
@command_enabled()
async def removeexpboost(interaction: discord.Interaction, role: discord.Role,
                          channel: discord.TextChannel = None, category: discord.CategoryChannel = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if channel and category:
        await interaction.response.send_message("❌ Specify a channel OR a category, not both.", ephemeral=True); return
    channel_id  = channel.id  if channel  else 0
    category_id = category.id if category else 0
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM exp_boosts WHERE guild_id=? AND role_id=? AND channel_id=? AND category_id=?",
                (interaction.guild.id, role.id, channel_id, category_id))
            await db.commit()
    scope = "global"
    if channel:   scope = f"channel {channel.mention}"
    elif category: scope = f"category **{category.name}**"
    await interaction.response.send_message(f"🗑 Removed {scope} EXP boost from {role.mention}.")
 
@bot.command(name="removeexpboost")
async def pfx_removeexpboost(ctx, role: discord.Role, channel: discord.TextChannel = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await removeexpboost._callback(FakeInteraction(ctx), role, channel, None)
 
 
_EXP_BOOSTS_PER_EMBED = 20
 
def _build_exp_boost_embeds(guild, rows: list) -> list:
    lines = []
    for role_id, boost, channel_id, category_id in rows:
        role = guild.get_role(role_id)
        name = role.mention if role else f"<deleted role {role_id}>"
        sign = "+" if boost > 0 else ""
        if channel_id:
            ch = guild.get_channel(channel_id)
            scope = ch.mention if ch else "🗑 deleted channel"
        elif category_id:
            cat = guild.get_channel(category_id)
            scope = f"📁 {cat.name}" if cat else "🗑 deleted category"
        else:
            scope = "🌐 Global"
        lines.append(f"• {name} — **{sign}{boost}%** | {scope}")
    chunks = [lines[i:i+_EXP_BOOSTS_PER_EMBED] for i in range(0, len(lines), _EXP_BOOSTS_PER_EMBED)]
    total_pages = len(chunks)
    embeds = []
    for i, chunk in enumerate(chunks):
        title = "⚡ Active EXP Boosts" + (f"  ({i+1}/{total_pages})" if total_pages > 1 else "")
        embed = discord.Embed(title=title, description="\n".join(chunk), color=discord.Color.blurple())
        if i == total_pages - 1:
            embed.set_footer(text=f"{len(rows)} boost(s) total")
        embeds.append(embed)
    return embeds
 
@bot.tree.command(name="listexpboosts", description="List all active EXP boosts for this server")
@command_enabled()
async def slash_listexpboosts(interaction: discord.Interaction):
    try:
        async with get_db() as db:
            async with db.execute(
                "SELECT role_id, boost_percent, channel_id, category_id FROM exp_boosts "
                "WHERE guild_id=? ORDER BY boost_percent DESC", (interaction.guild.id,)) as cur:
                rows = await cur.fetchall()
        if not rows:
            await interaction.response.send_message("❌ No EXP boosts configured.", ephemeral=True); return
        embeds = _build_exp_boost_embeds(interaction.guild, rows)
        await interaction.response.send_message(embeds=embeds[:10])
        for extra in embeds[10:]:
            await interaction.followup.send(embed=extra)
    except Exception as e:
        try: await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
        except Exception: await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
 
@bot.command(name="listexpboosts")
async def cmd_listexpboosts(ctx):
    try:
        async with get_db() as db:
            async with db.execute(
                "SELECT role_id, boost_percent, channel_id, category_id FROM exp_boosts "
                "WHERE guild_id=? ORDER BY boost_percent DESC", (ctx.guild.id,)) as cur:
                rows = await cur.fetchall()
        if not rows:
            await ctx.send("❌ No EXP boosts configured."); return
        embeds = _build_exp_boost_embeds(ctx.guild, rows)
        await ctx.send(embeds=embeds[:10])
        for extra in embeds[10:]:
            await ctx.send(embed=extra)
    except Exception as e:
        await ctx.send(f"❌ Error fetching EXP boosts: {e}")
 
# ═══════════════════════════════════════════════════════
# LEADERBOARD
# ═══════════════════════════════════════════════════════
 
_LB_PER_PAGE = 10
 
class LeaderboardView(discord.ui.View):
    def __init__(self, all_data, guild, caller_id, caller_rank, caller_amt, title, current_page, total_pages):
        super().__init__(timeout=120)
        self.all_data, self.guild, self.caller_id = all_data, guild, caller_id
        self.caller_rank, self.caller_amt = caller_rank, caller_amt
        self.title, self.current, self.total = title, current_page, total_pages
        self._sync()
 
    def _sync(self):
        for btn in self.children:
            if isinstance(btn, discord.ui.Button):
                if btn.label == "◀": btn.disabled = self.current <= 1
                elif btn.label == "▶": btn.disabled = self.current >= self.total
 
    def build_embed(self, page: int) -> discord.Embed:
        medals = ["🥇", "🥈", "🥉"]
        start  = (page - 1) * _LB_PER_PAGE
        chunk  = self.all_data[start:start + _LB_PER_PAGE]
        embed  = discord.Embed(title=self.title, color=discord.Color.gold())
        lines  = []
        for i, (uid, amt) in enumerate(chunk):
            rank = start + i + 1
            m = self.guild.get_member(uid)
            name = m.display_name if m else "*[Left Server]*"
            star = " ★" if uid == self.caller_id else ""
            prefix = medals[rank-1] if rank <= 3 else f"**#{rank}**"
            lines.append(f"{prefix} {name}{star} — {amt:,}")
        embed.description = "\n".join(lines) if lines else "*No entries on this page.*"
        page_info = f"Page {page}/{self.total} · {len(self.all_data)} entries"
        if self.caller_rank is not None:
            embed.set_footer(text=f"{page_info} · Your rank: #{self.caller_rank} ({self.caller_amt:,})")
        else:
            embed.set_footer(text=f"{page_info} · You have no entry yet")
        return embed
 
    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, disabled=True)
    async def prev_page(self, interaction: discord.Interaction, btn):
        if interaction.user.id != self.caller_id:
            await interaction.response.send_message("❌ Not your leaderboard.", ephemeral=True); return
        self.current -= 1; self._sync()
        await interaction.response.edit_message(embed=self.build_embed(self.current), view=self)
 
    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, btn):
        if interaction.user.id != self.caller_id:
            await interaction.response.send_message("❌ Not your leaderboard.", ephemeral=True); return
        self.current += 1; self._sync()
        await interaction.response.edit_message(embed=self.build_embed(self.current), view=self)
 
    async def on_timeout(self):
        for item in self.children: item.disabled = True
 
 
@bot.tree.command(name="leaderboard", description="View leaderboards")
@app_commands.choices(category=[
    app_commands.Choice(name="Total EXP", value="total_exp"),
    app_commands.Choice(name="Usable EXP", value="current_exp"),
    app_commands.Choice(name="Balance", value="balance"),
    app_commands.Choice(name="Lifetime Mega Tickets", value="mega_tickets_bought"),
    app_commands.Choice(name="Current Mega Tickets", value="current_tickets"),
    app_commands.Choice(name="Chests Opened", value="chests_opened"),
    app_commands.Choice(name="Gifted Balance", value="gifted_balance"),
    app_commands.Choice(name="Hosted Balance (given away via /host)", value="hosted_balance"),
])
@app_commands.describe(category="Which leaderboard to view", page="Jump directly to this page number (default: 1)")
@command_enabled()
async def leaderboard(interaction: discord.Interaction, category: app_commands.Choice[str], page: int = 1):
    value = category.value
    gid   = interaction.guild.id
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    await interaction.response.defer()
 
    all_data = []
    async with get_db() as db:
        if value == "current_exp":
            async with db.execute(
                "SELECT user_id, SUM(amount) FROM exp_history "
                "WHERE guild_id=? AND timestamp>=? GROUP BY user_id "
                "HAVING SUM(amount)>0 ORDER BY SUM(amount) DESC", (gid, week_ago)) as cur:
                all_data = [(uid, int(amt)) for uid, amt in await cur.fetchall()]
        elif value == "current_tickets":
            async with db.execute(
                "SELECT user_id, tickets FROM mega_tickets WHERE guild_id=? AND tickets>0 ORDER BY tickets DESC",
                (gid,)) as cur:
                all_data = list(await cur.fetchall())
        elif value == "balance":
            async with db.execute(
                "SELECT user_id, balance FROM balances WHERE guild_id=? AND balance>0 ORDER BY balance DESC",
                (gid,)) as cur:
                all_data = list(await cur.fetchall())
        else:
            async with db.execute(
                f"SELECT user_id, {value} FROM user_stats WHERE guild_id=? AND {value}>0 ORDER BY {value} DESC",
                (gid,)) as cur:
                all_data = list(await cur.fetchall())
 
    if not all_data:
        await interaction.followup.send("❌ No data found."); return
 
    caller_rank = caller_amt = None
    for rank, (uid, amt) in enumerate(all_data, 1):
        if uid == interaction.user.id:
            caller_rank, caller_amt = rank, amt
            break
 
    title_map = {
        "total_exp": "🏆 Total EXP", "current_exp": "⭐ Usable EXP", "balance": "💰 Balance",
        "mega_tickets_bought": "🎟 Lifetime Mega Tickets", "current_tickets": "🎫 Current Mega Tickets",
        "chests_opened": "📦 Chests Opened", "gifted_balance": "💸 Gifted Balance",
        "hosted_balance": "🎁 Hosted Balance Given Away",
    }
    title = title_map[value] + " Leaderboard"
    total_pages = max(1, (len(all_data) + _LB_PER_PAGE - 1) // _LB_PER_PAGE)
    page = max(1, min(page, total_pages))
    view = LeaderboardView(all_data, interaction.guild, interaction.user.id, caller_rank, caller_amt, title, page, total_pages)
    await interaction.followup.send(embed=view.build_embed(page), view=view if total_pages > 1 else None)
 
@bot.command(name="leaderboard")
async def pfx_leaderboard(ctx, category: str = "balance", page: int = 1):
    _valid = {"total_exp","current_exp","balance","mega_tickets_bought",
              "current_tickets","chests_opened","gifted_balance","hosted_balance"}
    if category not in _valid:
        await ctx.send(f"❌ Valid categories: {', '.join(sorted(_valid))}"); return
    await leaderboard._callback(FakeInteraction(ctx), _MC(category), page)
 
 
_VALID_STATS = {"total_exp", "gifted_balance", "chests_opened", "mega_tickets_bought", "hosted_balance"}
 
@bot.command(name="addleaderboardstat")
async def cmd_addleaderboardstat(ctx, user: discord.Member, stat: str, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if stat not in _VALID_STATS: await ctx.send(f"❌ Valid stats: {', '.join(_VALID_STATS)}"); return
    if amount <= 0: await ctx.send("❌ Amount must be > 0."); return
    await ensure_stats(ctx.guild.id, user.id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(f"UPDATE user_stats SET {stat}={stat}+? WHERE guild_id=? AND user_id=?",
                             (amount, ctx.guild.id, user.id))
            await db.commit()
    await ctx.send(f"✅ Added **{amount:,}** to {user.mention}'s **{stat}**.")
 
@bot.command(name="removeleaderboardstat")
async def cmd_removeleaderboardstat(ctx, user: discord.Member, stat: str, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if stat not in _VALID_STATS: await ctx.send(f"❌ Valid stats: {', '.join(_VALID_STATS)}"); return
    if amount <= 0: await ctx.send("❌ Amount must be > 0."); return
    await ensure_stats(ctx.guild.id, user.id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(f"UPDATE user_stats SET {stat}=MAX(0,{stat}-?) WHERE guild_id=? AND user_id=?",
                             (amount, ctx.guild.id, user.id))
            await db.commit()
    await ctx.send(f"❌ Removed **{amount:,}** from {user.mention}'s **{stat}**.")
 
# ═══════════════════════════════════════════════════════
# BALANCE RANKS
# ═══════════════════════════════════════════════════════
 
@bot.tree.command(name="addbalancerank", description="Add or update a balance rank role")
@app_commands.describe(threshold="Balance required to receive this role", role="Role granted at this balance")
@command_enabled()
async def addbalancerank(interaction: discord.Interaction, threshold: int, role: discord.Role):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if threshold < 0:
        await interaction.response.send_message("❌ Threshold must be ≥ 0.", ephemeral=True); return
 
    async with get_db() as db:
        async with db.execute(
            "SELECT role_id FROM balance_ranks WHERE guild_id=? AND threshold=? AND role_id!=?",
            (interaction.guild.id, threshold, role.id)) as cur:
            dup = await cur.fetchone()
 
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO balance_ranks(guild_id,role_id,threshold) VALUES(?,?,?) "
                "ON CONFLICT(guild_id,role_id) DO UPDATE SET threshold=excluded.threshold",
                (interaction.guild.id, role.id, threshold))
            await db.commit()
 
    warnings = []
    if dup:
        other = interaction.guild.get_role(dup[0])
        warnings.append(f"⚠️ {other.mention if other else dup[0]} already uses threshold {threshold:,} — ties broken arbitrarily.")
    bot_member = interaction.guild.me
    if not bot_member.guild_permissions.manage_roles:
        warnings.append("⚠️ I don't have the **Manage Roles** permission — I won't be able to assign this rank.")
    elif bot_member.top_role <= role:
        warnings.append(
            f"⚠️ My highest role ({bot_member.top_role.mention}) is below or equal to {role.mention} — "
            f"move my role ABOVE it in Server Settings → Roles, or I can't assign it.")
 
    msg = (f"✅ {role.mention} is now the balance rank for **{threshold:,}+** coins.\n"
           f"ℹ️ Run `/refreshbalanceranks` to apply this to members with existing balances.")
    if warnings:
        msg += "\n" + "\n".join(warnings)
    await interaction.response.send_message(msg)
 
@bot.command(name="addbalancerank")
async def pfx_addbalancerank(ctx, threshold: int, role: discord.Role):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await addbalancerank._callback(FakeInteraction(ctx), threshold, role)
 
 
@bot.tree.command(name="removebalancerank", description="Remove a balance rank role")
@command_enabled()
async def removebalancerank(interaction: discord.Interaction, role: discord.Role):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM balance_ranks WHERE guild_id=? AND role_id=?",
                             (interaction.guild.id, role.id))
            await db.commit()
    await interaction.response.send_message(
        f"🗑 {role.mention} removed from balance ranks. Members who already have it keep it until manually removed.")
 
@bot.command(name="removebalancerank")
async def pfx_removebalancerank(ctx, role: discord.Role):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await removebalancerank._callback(FakeInteraction(ctx), role)
 
 
@bot.tree.command(name="listbalanceranks", description="List all balance rank thresholds")
@command_enabled()
async def listbalanceranks(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute(
            "SELECT role_id, threshold FROM balance_ranks WHERE guild_id=? ORDER BY threshold ASC",
            (interaction.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("❌ No balance ranks configured."); return
    embed = discord.Embed(title="📈 Balance Ranks", color=discord.Color.gold())
    lines = []
    for rid, threshold in rows:
        r = interaction.guild.get_role(rid)
        lines.append(f"**{threshold:,}+** coins → {r.mention if r else f'<deleted role {rid}>'}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="A member only ever holds ONE balance rank role — the highest they qualify for.")
    await interaction.response.send_message(embed=embed)
 
@bot.command(name="listbalanceranks")
async def pfx_listbalanceranks(ctx):
    await listbalanceranks._callback(FakeInteraction(ctx))
 
 
@bot.tree.command(name="refreshbalanceranks",
                  description="Re-evaluate balance ranks for every member with a balance")
@command_enabled()
async def refreshbalanceranks(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await interaction.response.defer()
    gid = interaction.guild.id
    async with get_db() as db:
        async with db.execute("SELECT DISTINCT user_id FROM balances WHERE guild_id=?", (gid,)) as cur:
            user_ids = [r[0] for r in await cur.fetchall()]
    updated = errors = 0
    for uid in user_ids:
        try:
            member = interaction.guild.get_member(uid)
            if not member: continue
            before_roles = {r.id for r in member.roles}
            await _update_balance_rank(bot, gid, uid)
            member2 = interaction.guild.get_member(uid)
            if member2 and {r.id for r in member2.roles} != before_roles:
                updated += 1
        except Exception as e:
            errors += 1
            print(f"[RefreshBalanceRanks] {uid}: {e}")
    msg = f"✅ Refreshed balance ranks for **{len(user_ids)}** member(s) — **{updated}** role change(s) made."
    if errors:
        msg += f"\n⚠️ **{errors}** error(s) occurred — check your admin log channel."
    await interaction.followup.send(msg)
 
@bot.command(name="refreshbalanceranks")
async def pfx_refreshbalanceranks(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await refreshbalanceranks._callback(FakeInteraction(ctx))
 
 
@bot.tree.command(name="checkbalancerank",
                  description="Diagnose why a user might not have their expected balance rank")
@app_commands.describe(user="User to check (defaults to yourself)")
@command_enabled()
async def checkbalancerank(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    gid = interaction.guild.id
    bal = await get_balance(gid, user.id)
    async with get_db() as db:
        async with db.execute(
            "SELECT role_id, threshold FROM balance_ranks WHERE guild_id=? ORDER BY threshold DESC", (gid,)) as cur:
            ranks = await cur.fetchall()
    if not ranks:
        await interaction.response.send_message("❌ No balance ranks configured on this server."); return
 
    target_role_id = None
    for rid, threshold in ranks:
        if bal >= threshold:
            target_role_id = rid; break
    target_role = interaction.guild.get_role(target_role_id) if target_role_id else None
    rank_role_ids = {r[0] for r in ranks}
    current_rank_roles = [r for r in user.roles if r.id in rank_role_ids]
 
    bot_member = interaction.guild.me
    lines = [
        f"**Balance:** {bal:,} coins",
        f"**Should have:** {target_role.mention if target_role else '*(none — below lowest threshold)*'}",
        f"**Currently has:** {', '.join(r.mention for r in current_rank_roles) if current_rank_roles else '*(none)*'}",
    ]
    if target_role:
        if not bot_member.guild_permissions.manage_roles:
            lines.append("⚠️ I'm missing the **Manage Roles** permission.")
        elif bot_member.top_role <= target_role:
            lines.append(f"⚠️ My top role is below or equal to {target_role.mention} — move my role higher.")
        elif target_role not in current_rank_roles:
            lines.append("⚠️ Nothing obviously wrong with permissions — try `/refreshbalanceranks`.")
    embed = discord.Embed(title=f"🔍 Balance Rank Check — {user.display_name}",
                          description="\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)
 
@bot.command(name="checkbalancerank")
async def pfx_checkbalancerank(ctx, user: discord.Member = None):
    await checkbalancerank._callback(FakeInteraction(ctx), user)
 
# ═══════════════════════════════════════════════════════
# STATS PANEL
# ═══════════════════════════════════════════════════════
 
async def _build_stats_embed(guild: discord.Guild) -> discord.Embed:
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(tickets),0) FROM mega_tickets WHERE guild_id=? AND tickets>0",
            (guild.id,)) as cur:
            members, pool = await cur.fetchone()
    embed = discord.Embed(
        title="📊 Stats",
        description="Click a button below to check your personal stats.\nAll responses are **private**.",
        color=discord.Color.blurple())
    embed.add_field(name="🎟 Current Mega Raffle Pool", value=f"{pool:,} tickets across {members:,} member(s)", inline=False)
    embed.set_footer(text="Results are only visible to you")
    return embed
 
async def _refresh_stats_channel(guild: discord.Guild):
    async with get_db() as db:
        async with db.execute(
            "SELECT channel_id, message_id FROM stats_channel_config WHERE guild_id=?", (guild.id,)) as cur:
            row = await cur.fetchone()
    if not row or not row[0]: return
    ch = bot.get_channel(row[0])
    if not ch: return
    embed = await _build_stats_embed(guild)
    view = StatsChannelView()
    if row[1]:
        try:
            msg = await ch.fetch_message(row[1])
            await msg.edit(embed=embed, view=view)
            return
        except discord.NotFound:
            pass
    new_msg = await ch.send(embed=embed, view=view)
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE stats_channel_config SET message_id=? WHERE guild_id=?", (new_msg.id, guild.id))
            await db.commit()
 
 
class StatsChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
 
    @discord.ui.button(label="💰 Balance", style=discord.ButtonStyle.secondary, custom_id="stats_panel:balance", row=0)
    async def check_balance(self, interaction: discord.Interaction, btn):
        bal = await get_balance(interaction.guild.id, interaction.user.id)
        embed = discord.Embed(title=f"💰 {interaction.user.display_name}'s Balance",
                              description=f"**{bal:,}** coins", color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)
 
    @discord.ui.button(label="⭐ Activity Rank", style=discord.ButtonStyle.secondary, custom_id="stats_panel:rank", row=0)
    async def check_rank(self, interaction: discord.Interaction, btn):
        gid, uid = interaction.guild.id, interaction.user.id
        exp = await get_level_exp(gid, uid); usable = await get_exp(gid, uid); lvl = await get_level(gid, uid)
        embed = discord.Embed(title=f"⭐ {interaction.user.display_name}'s Activity Rank", color=discord.Color.gold())
        embed.add_field(name="Activity Rank", value=str(lvl), inline=True)
        embed.add_field(name="Total EXP (7d)", value=f"{exp:,}", inline=True)
        embed.add_field(name="Usable EXP", value=f"{usable:,}", inline=True)
        embed.add_field(name="Chests Available", value=f"{usable // common.CHEST_COST}", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
 
    @discord.ui.button(label="🎟 Mega Tickets", style=discord.ButtonStyle.secondary, custom_id="stats_panel:tickets", row=0)
    async def check_tickets(self, interaction: discord.Interaction, btn):
        gid, uid = interaction.guild.id, interaction.user.id
        tickets = await get_tickets(gid, uid)
        async with get_db() as db:
            async with db.execute("SELECT COALESCE(SUM(tickets),0) FROM mega_tickets WHERE guild_id=?", (gid,)) as cur:
                total = (await cur.fetchone())[0]
        chance = (tickets / total * 100) if total else 0
        embed = discord.Embed(title="🎟 Your Mega Raffle Stats", color=discord.Color.gold())
        embed.add_field(name="Your Tickets", value=f"{tickets:,}", inline=True)
        embed.add_field(name="Total Pool", value=f"{total:,}", inline=True)
        embed.add_field(name="Win Chance", value=f"{chance:.2f}%", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
 
    @discord.ui.button(label="🎒 Inventory", style=discord.ButtonStyle.secondary, custom_id="stats_panel:inventory", row=0)
    async def check_inventory(self, interaction: discord.Interaction, btn):
        gid, uid = interaction.guild.id, interaction.user.id
        inv = await inventory_get(gid, uid)
        embed = discord.Embed(title=f"🎒 {interaction.user.display_name}'s Inventory", color=discord.Color.blurple())
        if not inv:
            embed.description = "Inventory is empty."
        else:
            lines = []
            for item_name, qty in inv:
                if item_name == VIP_CHEST_KEY: lines.append(f"• 🔑 **{item_name}** ×{qty}")
                elif item_name == GAMBLE_TOKEN: lines.append(f"• 🎲 **{item_name}** ×{qty}")
                else:
                    si = await get_item(gid, item_name)
                    if si:
                        r = interaction.guild.get_role(si[3])
                        lines.append(f"• **{item_name}** ×{qty}" + (f" → {r.mention}" if r else ""))
                    else:
                        lines.append(f"• 📦 **{item_name}** ×{qty}")
            embed.description = "\n".join(lines[:30])
            if len(inv) > 30: embed.set_footer(text=f"Showing 30 of {len(inv)} items")
        await interaction.response.send_message(embed=embed, ephemeral=True)
 
 
@bot.tree.command(name="setstatchannel", description="Post the stats panel embed in a channel")
@app_commands.describe(channel="Channel to post the panel in")
@command_enabled()
async def setstatchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await interaction.response.defer()
    gid = interaction.guild.id
    async with get_db() as db:
        async with db.execute("SELECT channel_id, message_id FROM stats_channel_config WHERE guild_id=?", (gid,)) as cur:
            old = await cur.fetchone()
    if old and old[0] and old[0] != channel.id and old[1]:
        old_ch = bot.get_channel(old[0])
        if old_ch:
            try: await (await old_ch.fetch_message(old[1])).delete()
            except Exception: pass
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO stats_channel_config(guild_id, channel_id, message_id) VALUES(?,?,0) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=0",
                (gid, channel.id))
            await db.commit()
    await _refresh_stats_channel(interaction.guild)
    await interaction.followup.send(f"✅ Stats panel posted in {channel.mention}.")
 
@bot.command(name="setstatchannel")
async def pfx_setstatchannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setstatchannel._callback(FakeInteraction(ctx), channel)
 
# ═══════════════════════════════════════════════════════
# TRADE SYSTEM
# ═══════════════════════════════════════════════════════
 
trade_sessions: dict = {}
 
class TradeOffer:
    def __init__(self):
        self.balance = 0; self.exp = 0; self.tickets = 0
        self.items: list[tuple[str, int]] = []
 
    def display(self) -> str:
        lines = []
        if self.balance > 0: lines.append(f"💰 {self.balance:,} coins")
        if self.exp > 0:     lines.append(f"⭐ {self.exp:,} EXP")
        if self.tickets > 0: lines.append(f"🎟 {self.tickets:,} ticket(s)")
        for n, q in self.items: lines.append(f"🎒 {q}x {n}")
        return "\n".join(lines) if lines else "*Nothing*"
 
 
class TradeSession:
    def __init__(self, guild_id, initiator_id, target_id):
        self.guild_id, self.initiator_id, self.target_id = guild_id, initiator_id, target_id
        self.offers = {initiator_id: None, target_id: None}
        self.confirmed = {initiator_id: False, target_id: False}
        self.message = None; self.done = False; self.lock = asyncio.Lock()
 
    def session_key(self):
        return (self.guild_id, frozenset({self.initiator_id, self.target_id}))
 
    def build_embed(self, guild):
        init = guild.get_member(self.initiator_id); tgt = guild.get_member(self.target_id)
        embed = discord.Embed(title="🤝 Trade Offer", color=discord.Color.blurple())
        io, to = self.offers[self.initiator_id], self.offers[self.target_id]
        is_ = "✅" if self.confirmed[self.initiator_id] else ("📋" if io else "❓")
        ts_ = "✅" if self.confirmed[self.target_id] else ("📋" if to else "❓")
        embed.add_field(name=f"{init.display_name if init else 'User'}'s offer {is_}",
                        value=io.display() if io else "*Not set yet*", inline=True)
        embed.add_field(name=f"{tgt.display_name if tgt else 'User'}'s offer {ts_}",
                        value=to.display() if to else "*Not set yet*", inline=True)
        return embed
 
 
class TradeOfferModal(discord.ui.Modal, title="Set Your Trade Offer"):
    balance_input = discord.ui.TextInput(label="Balance to offer (0 for none)", default="0", max_length=20)
    exp_input     = discord.ui.TextInput(label="EXP to offer (0 for none)", default="0", max_length=20)
    tickets_input = discord.ui.TextInput(label="Mega tickets (0 for none)", default="0", max_length=20)
    items_input   = discord.ui.TextInput(label="Items/boxes (blank for none)",
                                         placeholder="Name:qty, Name2:qty2", required=False, max_length=300)
 
    def __init__(self, session):
        super().__init__()
        self.session = session
 
    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id; session = self.session
        try: balance = max(0, int(self.balance_input.value.strip()))
        except ValueError:
            await interaction.response.send_message("❌ Invalid balance.", ephemeral=True); return
        try: exp = max(0, int(self.exp_input.value.strip()))
        except ValueError:
            await interaction.response.send_message("❌ Invalid EXP.", ephemeral=True); return
        try: tickets = max(0, int(self.tickets_input.value.strip()))
        except ValueError:
            await interaction.response.send_message("❌ Invalid tickets.", ephemeral=True); return
        items = []
        for part in (self.items_input.value or "").split(","):
            part = part.strip()
            if not part: continue
            if ":" not in part:
                await interaction.response.send_message(f"❌ Bad format `{part}` — use Name:qty", ephemeral=True); return
            iname, qty_str = part.rsplit(":", 1)
            try:
                qty = int(qty_str.strip()); assert qty > 0
            except Exception:
                await interaction.response.send_message(f"❌ Invalid qty for {iname}", ephemeral=True); return
            items.append((iname.strip(), qty))
        if balance > 0 and await get_balance(interaction.guild.id, uid) < balance:
            await interaction.response.send_message("❌ Not enough coins.", ephemeral=True); return
        if exp > 0 and await get_exp(interaction.guild.id, uid) < exp:
            await interaction.response.send_message("❌ Not enough EXP.", ephemeral=True); return
        if tickets > 0 and await get_tickets(session.guild_id, uid) < tickets:
            await interaction.response.send_message("❌ Not enough tickets.", ephemeral=True); return
        if items:
            inv = {n.lower(): q for n, q in await inventory_get(self.session.guild_id, uid)}
            for n, q in items:
                if inv.get(n.lower(), 0) < q:
                    await interaction.response.send_message(f"❌ Not enough {n}.", ephemeral=True); return
        offer = TradeOffer()
        offer.balance, offer.exp, offer.tickets, offer.items = balance, exp, tickets, items
        session.offers[uid] = offer; session.confirmed[uid] = False
        await session.message.edit(embed=session.build_embed(interaction.guild), view=TradeView(session))
        await interaction.response.send_message("✅ Offer updated!", ephemeral=True)
 
 
class TradeView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=300)
        self.session = session
 
    async def interaction_check(self, interaction):
        if interaction.user.id not in (self.session.initiator_id, self.session.target_id):
            await interaction.response.send_message("❌ Not your trade.", ephemeral=True); return False
        if self.session.done:
            await interaction.response.send_message("❌ Trade already finished.", ephemeral=True); return False
        return True
 
    @discord.ui.button(label="Set Offer", style=discord.ButtonStyle.primary, emoji="📋")
    async def set_offer(self, interaction, button):
        await interaction.response.send_modal(TradeOfferModal(self.session))
 
    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction, button):
        session = self.session
        if session.offers[interaction.user.id] is None:
            await interaction.response.send_message("❌ Set your offer first.", ephemeral=True); return
        async with session.lock:
            session.confirmed[interaction.user.id] = True
            if not all(session.confirmed.values()):
                await session.message.edit(embed=session.build_embed(interaction.guild), view=self)
                await interaction.response.send_message("✅ Confirmed! Waiting for other party.", ephemeral=True); return
            session.done = True
            success, err = await execute_trade(session)
            trade_sessions.pop(session.session_key(), None)
        if success:
            await session.message.edit(embed=discord.Embed(title="✅ Trade Complete!", color=discord.Color.green()), view=None)
            await interaction.response.send_message("✅ Trade executed!", ephemeral=True)
        else:
            session.confirmed[session.initiator_id] = session.confirmed[session.target_id] = False
            session.done = False
            await session.message.edit(embed=session.build_embed(interaction.guild), view=TradeView(session))
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
 
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction, button):
        session = self.session; session.done = True
        trade_sessions.pop(session.session_key(), None)
        await session.message.edit(embed=discord.Embed(title="❌ Trade Cancelled", color=discord.Color.red()), view=None)
        await interaction.response.send_message("Trade cancelled.", ephemeral=True)
 
    async def on_timeout(self):
        if not self.session.done:
            self.session.done = True
            trade_sessions.pop(self.session.session_key(), None)
            if self.session.message:
                try:
                    await self.session.message.edit(
                        embed=discord.Embed(title="⏰ Trade Expired", color=discord.Color.light_grey()), view=None)
                except Exception: pass
 
 
async def execute_trade(session) -> tuple[bool, str]:
    iid, tid = session.initiator_id, session.target_id
    gid = session.guild_id
    for uid, offer in [(iid, session.offers[iid]), (tid, session.offers[tid])]:
        if offer.balance > 0 and await get_balance(gid, uid) < offer.balance:
            return False, f"<@{uid}> no longer has enough coins."
        if offer.exp > 0 and await get_exp(gid, uid) < offer.exp:
            return False, f"<@{uid}> no longer has enough EXP."
        if offer.tickets > 0 and await get_tickets(gid, uid) < offer.tickets:
            return False, f"<@{uid}> no longer has enough tickets."
        inv = {n.lower(): q for n, q in await inventory_get(gid, uid)}
        for n, q in offer.items:
            if inv.get(n.lower(), 0) < q:
                return False, f"<@{uid}> no longer has {q}x {n}."
    io, to = session.offers[iid], session.offers[tid]
    if io.balance > 0: await add_balance(gid, iid, -io.balance, bot=bot); await add_balance(gid, tid, io.balance, bot=bot)
    if to.balance > 0: await add_balance(gid, tid, -to.balance, bot=bot); await add_balance(gid, iid, to.balance, bot=bot)
    if io.exp > 0: await add_exp(gid, iid, -io.exp); await add_exp(gid, tid, io.exp)
    if to.exp > 0: await add_exp(gid, tid, -to.exp); await add_exp(gid, iid, to.exp)
    if io.tickets > 0: await add_tickets(gid, iid, -io.tickets); await add_tickets(gid, tid, io.tickets)
    if to.tickets > 0: await add_tickets(gid, tid, -to.tickets); await add_tickets(gid, iid, to.tickets)
    for n, q in io.items: await inventory_remove(gid, iid, n, q); await inventory_add(gid, tid, n, q)
    for n, q in to.items: await inventory_remove(gid, tid, n, q); await inventory_add(gid, iid, n, q)
 
    guild = bot.get_guild(gid)
    if guild:
        init = guild.get_member(iid); tgt = guild.get_member(tid)
        embed = discord.Embed(title="🤝 Trade Executed", color=discord.Color.blurple(), timestamp=datetime.now(UTC))
        embed.add_field(name=f"{init.display_name if init else '?'} gave", value=io.display(), inline=True)
        embed.add_field(name=f"{tgt.display_name if tgt else '?'} gave", value=to.display(), inline=True)
        await log_event(gid, "trade", embed)
    return True, ""
 
 
@bot.tree.command(name="trade", description="Initiate a trade with another user")
@command_enabled()
async def trade(interaction: discord.Interaction, user: discord.Member):
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ Can't trade with yourself.", ephemeral=True); return
    if user.bot:
        await interaction.response.send_message("❌ Can't trade with a bot.", ephemeral=True); return
    key = (interaction.guild.id, frozenset({interaction.user.id, user.id}))
    if key in trade_sessions:
        await interaction.response.send_message("❌ A trade is already in progress.", ephemeral=True); return
    session = TradeSession(interaction.guild.id, interaction.user.id, user.id)
    trade_sessions[key] = session
    await interaction.response.send_message(
        f"🤝 {interaction.user.mention} wants to trade with {user.mention}!\n"
        f"Click **Set Offer** to enter what you're offering, then **Confirm**.",
        embed=session.build_embed(interaction.guild), view=TradeView(session))
    session.message = await interaction.original_response()
 
@bot.command(name="trade")
async def pfx_trade(ctx, user: discord.Member):
    if user.id == ctx.author.id: await ctx.send("❌ Can't trade with yourself."); return
    if user.bot: await ctx.send("❌ Can't trade with a bot."); return
    key = (ctx.guild.id, frozenset({ctx.author.id, user.id}))
    if key in trade_sessions: await ctx.send("❌ A trade is already in progress."); return
    session = TradeSession(ctx.guild.id, ctx.author.id, user.id)
    trade_sessions[key] = session
    msg = await ctx.send(f"🤝 {ctx.author.mention} wants to trade with {user.mention}!\n"
                        "Click **Set Offer**, then **Confirm**.",
                        embed=session.build_embed(ctx.guild), view=TradeView(session))
    session.message = msg
 
# ═══════════════════════════════════════════════════════
# ITEM STORE & INVENTORY
# ═══════════════════════════════════════════════════════
 
item_group = app_commands.Group(name="item", description="Item store commands")
bot.tree.add_command(item_group)
 
@item_group.command(name="add", description="Add item to store")
@command_enabled()
async def item_add(interaction: discord.Interaction, name: str, price: int, role: discord.Role, description: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_item(interaction.guild.id, name, price, role.id, description)
    await interaction.response.send_message(f"✅ Added **{name}** to the store.")
 
@item_group.command(name="remove", description="Remove item from store")
@command_enabled()
async def item_remove(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if not await get_item(interaction.guild.id, name):
        await interaction.response.send_message("❌ Item not found."); return
    await remove_item(interaction.guild.id, name)
    await interaction.response.send_message(f"🗑 Removed **{name}** from the store.")
 
@item_group.command(name="info", description="View item info")
@command_enabled()
async def item_info(interaction: discord.Interaction, name: str):
    item = await get_item(interaction.guild.id, name)
    if item:
        _, item_name, price, role_id, description = item
        role = interaction.guild.get_role(role_id)
        embed = discord.Embed(title=f"🛒 {item_name}", color=discord.Color.blurple())
        embed.add_field(name="Price", value=f"{price:,} coins", inline=False)
        embed.add_field(name="Role", value=role.mention if role else "?", inline=False)
        embed.add_field(name="Description", value=description, inline=False)
        await interaction.response.send_message(embed=embed); return
    await interaction.response.send_message(
        "❌ Item not found. (Box info lives in the drops bot — use that bot's `/item info`.)")
 
@item_group.command(name="store", description="View item store")
@command_enabled()
async def item_store_cmd(interaction: discord.Interaction):
    items = await get_all_items(interaction.guild.id)
    if not items:
        await interaction.response.send_message("❌ Store is empty."); return
    embed = discord.Embed(title="🛒 Item Store", color=discord.Color.green())
    for _, item_name, price, role_id, description in items:
        role = interaction.guild.get_role(role_id)
        embed.add_field(name=item_name, value=f"💰 {price:,} coins\n🎭 {role.mention if role else '?'}", inline=False)
    await interaction.response.send_message(embed=embed)
 
@item_group.command(name="buy", description="Buy an item — goes to your inventory")
@command_enabled()
async def item_buy(interaction: discord.Interaction, name: str):
    item = await get_item(interaction.guild.id, name)
    if not item:
        await interaction.response.send_message("❌ Item not found."); return
    _, item_name, price, role_id, description = item
    bal = await get_balance(interaction.guild.id, interaction.user.id)
    if bal < price:
        await interaction.response.send_message("❌ Not enough balance."); return
    if not interaction.guild.get_role(role_id):
        await interaction.response.send_message("❌ Role no longer exists."); return
    await add_balance(interaction.guild.id, interaction.user.id, -price, bot=bot)
    await inventory_add(interaction.guild.id, interaction.user.id, item_name, 1)
    await interaction.response.send_message(
        f"✅ Bought **{item_name}** for {price:,} coins. Use `/item use {item_name}` to redeem!")
    await log_event(interaction.guild.id, "item", _log_embed(
        "🛒 Item Purchased", discord.Color.blue(), User=interaction.user.mention, Item=item_name))
 
@item_group.command(name="use", description="Use a store item to receive its role")
@command_enabled()
async def item_use(interaction: discord.Interaction, name: str):
    item = await get_item(interaction.guild.id, name)
    if not item:
        await interaction.response.send_message("❌ Item not found."); return
    _, item_name, price, role_id, description = item
    inv = await inventory_get(interaction.guild.id, interaction.user.id)
    owned = {n.lower(): q for n, q in inv}
    if owned.get(item_name.lower(), 0) < 1:
        await interaction.response.send_message(f"❌ You don't have **{item_name}** in your inventory."); return
    role = interaction.guild.get_role(role_id)
    if not role:
        await interaction.response.send_message("❌ Role no longer exists."); return
    member = interaction.guild.get_member(interaction.user.id)
    if role in member.roles:
        await interaction.response.send_message(f"❌ You already have **{role.name}**."); return
    if not await inventory_remove(interaction.guild.id, interaction.user.id, item_name, 1):
        await interaction.response.send_message("❌ Failed to remove item."); return
    await member.add_roles(role)
    await interaction.response.send_message(f"✅ Used **{item_name}** — you now have {role.mention}!")
    await log_event(interaction.guild.id, "item", _log_embed(
        "✅ Item Used (Role Claimed)", discord.Color.blue(), User=interaction.user.mention, Item=item_name))
 
@item_group.command(name="give", description="Give an item to a user (admin only)")
@app_commands.describe(user="Target user", name="Item name", quantity="How many (default 1)")
@command_enabled()
async def item_give(interaction: discord.Interaction, user: discord.Member, name: str, quantity: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if quantity <= 0:
        await interaction.response.send_message("❌ Quantity must be ≥ 1.", ephemeral=True); return
    store_item = await get_item(interaction.guild.id, name)
    canonical = store_item[1] if store_item else name.strip()
    if not store_item and name.strip() not in (VIP_CHEST_KEY, GAMBLE_TOKEN):
        await interaction.response.send_message(
            f"ℹ️ **{name}** isn't a known store item — giving it as a raw inventory entry anyway.",
            ephemeral=True)
    await inventory_add(interaction.guild.id, user.id, canonical, quantity)
    await interaction.response.send_message(f"✅ Gave **{quantity}x {canonical}** to {user.mention}.")
    await log_event(interaction.guild.id, "item", _log_embed(
        "🎒 Item Given", discord.Color.green(),
        Admin=interaction.user.mention, User=user.mention, Item=canonical, Qty=str(quantity)))
 
@item_group.command(name="take", description="Take an item from a user (admin only)")
@app_commands.describe(user="Target user", name="Item name", quantity="How many (default 1)")
@command_enabled()
async def item_take(interaction: discord.Interaction, user: discord.Member, name: str, quantity: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if quantity <= 0:
        await interaction.response.send_message("❌ Quantity must be ≥ 1.", ephemeral=True); return
    store_item = await get_item(interaction.guild.id, name)
    canonical = store_item[1] if store_item else name.strip()
    if not await inventory_remove(interaction.guild.id, user.id, canonical, quantity):
        await interaction.response.send_message(f"❌ {user.mention} doesn't have {quantity}x **{canonical}**."); return
    await interaction.response.send_message(f"🗑 Took **{quantity}x {canonical}** from {user.mention}.")
    await log_event(interaction.guild.id, "item", _log_embed(
        "🎒 Item Taken", discord.Color.red(),
        Admin=interaction.user.mention, User=user.mention, Item=canonical, Qty=str(quantity)))
 
@item_group.command(name="inv", description="Check a user's inventory")
@app_commands.describe(user="User to check (defaults to yourself)")
@command_enabled()
async def item_inv(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    inv = await inventory_get(interaction.guild.id, user.id)
    embed = discord.Embed(title=f"🎒 {user.display_name}'s Inventory", color=discord.Color.blurple())
    if not inv:
        embed.description = "Inventory is empty."
    else:
        lines = []
        for item_name, quantity in inv:
            si = await get_item(interaction.guild.id, item_name)
            if si:
                role = interaction.guild.get_role(si[3])
                lines.append(f"• **{item_name}** x{quantity}" + (f" → {role.mention}" if role else ""))
            elif item_name == VIP_CHEST_KEY:
                lines.append(f"• 🔑 **{item_name}** x{quantity}")
            elif item_name == GAMBLE_TOKEN:
                lines.append(f"• 🎲 **{item_name}** x{quantity}")
            else:
                lines.append(f"• 📦 **{item_name}** x{quantity}")
        embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)
 
 
@bot.group(name="item", invoke_without_command=True)
async def pfx_item(ctx):
    p = common._BOT_PREFIX
    await ctx.send(
        f"**Item commands:** `{p}item store` · `{p}item buy <name>` · `{p}item use <name>` · "
        f"`{p}item inv [@user]` · `{p}item info <name>` · `{p}item give @user <name> [qty]` · "
        f"`{p}item take @user <name> [qty]` · `{p}item add <name> <price> @role <desc>` · `{p}item remove <name>`")
 
@pfx_item.command(name="store")
async def pfx_item_store(ctx):
    await item_store_cmd._callback(FakeInteraction(ctx))
 
@pfx_item.command(name="buy")
async def pfx_item_buy_cmd(ctx, *, name: str):
    await item_buy._callback(FakeInteraction(ctx), name)
 
@pfx_item.command(name="use")
async def pfx_item_use_cmd(ctx, *, name: str):
    await item_use._callback(FakeInteraction(ctx), name)
 
@pfx_item.command(name="inv")
async def pfx_item_inv_cmd(ctx, user: discord.Member = None):
    await item_inv._callback(FakeInteraction(ctx), user)
 
@pfx_item.command(name="info")
async def pfx_item_info_cmd(ctx, *, name: str):
    await item_info._callback(FakeInteraction(ctx), name)
 
@pfx_item.command(name="give")
async def pfx_item_give_cmd(ctx, user: discord.Member, name: str, quantity: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await item_give._callback(FakeInteraction(ctx), user, name, quantity)
 
@pfx_item.command(name="take")
async def pfx_item_take_cmd(ctx, user: discord.Member, name: str, quantity: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await item_take._callback(FakeInteraction(ctx), user, name, quantity)
 
@pfx_item.command(name="add")
async def pfx_item_add_cmd(ctx, name: str, price: int, role: discord.Role, *, description: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await item_add._callback(FakeInteraction(ctx), name, price, role, description)
 
@pfx_item.command(name="remove")
async def pfx_item_remove_cmd(ctx, *, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await item_remove._callback(FakeInteraction(ctx), name)
 
# ═══════════════════════════════════════════════════════
# CORE EVENTS
# ═══════════════════════════════════════════════════════
 
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.content.startswith(common._BOT_PREFIX) and not _prefix_channel_allowed(message):
        return
    await bot.process_commands(message)
 
@bot.event
async def on_ready():
    await setup_database()
    await common._load_prefix()
    await load_disabled_commands()
    await load_prefix_restrictions()
    bot.add_view(StatsChannelView())

    try:
        synced = await bot.tree.sync()
        print(f"[Economy Bot] ✅ Synced {len(synced)} global slash command(s). Logged in as {bot.user}")
    except Exception as e:
        print(f"[Economy Bot] ❌ Sync failed: {e}")

    for guild in bot.guilds:
        try:
            await _refresh_stats_channel(guild)
        except Exception as e:
            print(f"[StatsPanel restore] {guild.name}: {e}")
 
@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except discord.HTTPException as e:
        print(f"[Economy Sync] Failed on join: {e}")
 
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return
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
    if not ctx.guild:
        return
    embed = discord.Embed(
        description=f"{ctx.author.mention} used **`{common._BOT_PREFIX}{ctx.command.qualified_name}`**",
        color=discord.Color.light_grey(), timestamp=datetime.now(UTC))
    embed.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
    embed.set_footer(text=f"#{getattr(ctx.channel, 'name', 'DM')} | UID: {ctx.author.id}")
    await log_event(ctx.guild.id, "command", embed)

# ── gift ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="gift", description="Give your own coins to another user")
@app_commands.describe(user="Who to gift to", amount="How many coins")
@command_enabled()
async def slash_gift(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ You cannot gift yourself.", ephemeral=True); return
    gid = interaction.guild.id
    bal = await get_balance(gid, interaction.user.id)
    if bal < amount:
        await interaction.response.send_message("❌ Not enough balance.", ephemeral=True); return
    await add_balance(gid, interaction.user.id, -amount, bot=bot)
    await add_balance(gid, user.id, amount, bot=bot)
    await add_stat(gid, interaction.user.id, "gifted_balance", amount)
    await interaction.response.send_message(f"💸 You gifted **{amount:,}** coins to {user.mention}!")
    await log_event(gid, "balance", _log_embed("🎁 Gift Sent", discord.Color.green(),
        From=interaction.user.mention, To=user.mention, Amount=f"{amount:,}"))

# ── addbalance / removebalance ───────────────────────────────────────────────
@bot.tree.command(name="addbalance", description="Admin: add coins to a user")
@app_commands.describe(user="Target user", amount="Amount to add")
@command_enabled()
async def slash_addbalance(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_balance(interaction.guild.id, user.id, amount, bot=bot)
    await interaction.response.send_message(f"✅ Added {amount:,} coins to {user.mention}.")
    await log_event(interaction.guild.id, "balance", _log_embed("💰 Balance Added", discord.Color.green(),
        Admin=interaction.user.mention, User=user.mention, Amount=f"+{amount:,}"))
    await log_event(interaction.guild.id, "admin", _log_embed("⚙️ addbalance", discord.Color.orange(),
        By=interaction.user.mention, User=user.mention, Amount=f"+{amount:,}"))

@bot.tree.command(name="removebalance", description="Admin: remove coins from a user")
@app_commands.describe(user="Target user", amount="Amount to remove")
@command_enabled()
async def slash_removebalance(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_balance(interaction.guild.id, user.id, -amount, bot=bot)
    await interaction.response.send_message(f"❌ Removed {amount:,} coins from {user.mention}.")
    await log_event(interaction.guild.id, "balance", _log_embed("💸 Balance Removed", discord.Color.red(),
        Admin=interaction.user.mention, User=user.mention, Amount=f"-{amount:,}"))

# ── EXP admin ────────────────────────────────────────────────────────────────
@bot.tree.command(name="addexp", description="Admin: add usable EXP to a user")
@app_commands.describe(user="Target user", amount="EXP to add")
@command_enabled()
async def slash_addexp(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    await add_exp(interaction.guild.id, user.id, amount, is_bonus=True)
    await interaction.response.send_message(f"✅ Added **{amount:,}** usable EXP to {user.mention}.")

@bot.tree.command(name="removeexp", description="Admin: remove usable EXP from a user")
@app_commands.describe(user="Target user", amount="EXP to remove")
@command_enabled()
async def slash_removeexp(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_exp(interaction.guild.id, user.id, -amount)
    await interaction.response.send_message(f"❌ Removed {amount:,} EXP from {user.mention}.")

@bot.tree.command(name="addtotalexp", description="Admin: add Total EXP (Activity Rank only, usable unchanged)")
@app_commands.describe(user="Target user", amount="EXP to add to rank")
@command_enabled()
async def slash_addtotalexp(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    now = int(datetime.now(UTC).timestamp())
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                             (interaction.guild.id, user.id, amount, now, 0))
            await db.execute("INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                             (interaction.guild.id, user.id, -amount, now, 0))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Added **{amount:,}** to {user.mention}'s Total EXP (7d). Usable EXP unchanged.")

@bot.tree.command(name="removetotalexp", description="Admin: remove Total EXP (Activity Rank only)")
@app_commands.describe(user="Target user", amount="EXP to remove from rank")
@command_enabled()
async def slash_removetotalexp(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await interaction.response.defer()
    ctx_like = FakeInteraction(None)  # use the prefix logic by calling cmd directly
    # Inline the logic rather than calling the prefix command
    if amount <= 0:
        await interaction.followup.send("❌ Amount must be > 0."); return
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    remaining = amount; actually_removed = 0
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT rowid, amount FROM exp_history "
                "WHERE guild_id=? AND user_id=? AND timestamp>=? AND amount>0 AND is_bonus=0 "
                "ORDER BY timestamp ASC", (interaction.guild.id, user.id, week_ago)) as cur:
                entries = await cur.fetchall()
            for rowid, entry_amount in entries:
                if remaining <= 0: break
                if entry_amount <= remaining:
                    await db.execute("DELETE FROM exp_history WHERE rowid=?", (rowid,))
                    remaining -= entry_amount
                else:
                    await db.execute("UPDATE exp_history SET amount=? WHERE rowid=?",
                                     (entry_amount - remaining, rowid)); remaining = 0
            actually_removed = amount - remaining
            if actually_removed > 0:
                await db.execute("INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                                 (interaction.guild.id, user.id, actually_removed, int(datetime.now(UTC).timestamp()), 1))
            await db.commit()
    if actually_removed == 0:
        await interaction.followup.send(f"❌ {user.mention} has no Total EXP (7d) to remove.")
    else:
        await interaction.followup.send(
            f"✅ Removed **{actually_removed:,}** from {user.mention}'s Total EXP (7d). Usable EXP unchanged.")

# ── leaderboard stat admin ───────────────────────────────────────────────────
@bot.tree.command(name="addleaderboardstat", description="Admin: manually add to a user's leaderboard stat")
@app_commands.describe(user="Target user", stat="Which stat to add to", amount="Amount to add")
@app_commands.choices(stat=[app_commands.Choice(name=s.replace("_"," ").title(), value=s) for s in
                             ("total_exp","gifted_balance","chests_opened","mega_tickets_bought","hosted_balance")])
@command_enabled()
async def slash_addleaderboardstat(interaction: discord.Interaction, user: discord.Member,
                                   stat: str, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    await ensure_stats(interaction.guild.id, user.id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(f"UPDATE user_stats SET {stat}={stat}+? WHERE guild_id=? AND user_id=?",
                             (amount, interaction.guild.id, user.id))
            await db.commit()
    await interaction.response.send_message(f"✅ Added **{amount:,}** to {user.mention}'s **{stat}**.")

@bot.tree.command(name="removeleaderboardstat", description="Admin: remove from a user's leaderboard stat")
@app_commands.describe(user="Target user", stat="Which stat to remove from", amount="Amount to remove")
@app_commands.choices(stat=[app_commands.Choice(name=s.replace("_"," ").title(), value=s) for s in
                             ("total_exp","gifted_balance","chests_opened","mega_tickets_bought","hosted_balance")])
@command_enabled()
async def slash_removeleaderboardstat(interaction: discord.Interaction, user: discord.Member,
                                      stat: str, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    await ensure_stats(interaction.guild.id, user.id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(f"UPDATE user_stats SET {stat}=MAX(0,{stat}-?) WHERE guild_id=? AND user_id=?",
                             (amount, interaction.guild.id, user.id))
            await db.commit()
    await interaction.response.send_message(f"❌ Removed **{amount:,}** from {user.mention}'s **{stat}**.")
 
if __name__ == "__main__":
    bot.run(TOKEN)
