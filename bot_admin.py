import os, json, random, asyncio, aiosqlite, discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta, UTC
from typing import Optional

import common
from common import (
    get_db, db_lock, setup_database, log_event, _log_embed, command_enabled,
    is_allowed_to_giveaway, _is_allowed_ctx, is_system_enabled, set_system_flag,
    get_balance, add_balance, get_exp, add_exp, get_level,
    inventory_add, inventory_remove, inventory_get,
    get_tickets, add_tickets,
    add_stat, ensure_stats, _do_reset,
    bump_msg_count, msg_count_flush_loop,
    FakeInteraction, _MC,
    GAMBLE_TOKEN, VIP_CHEST_KEY, BOT_OWNER_ID, COUNTING_BOT_ID, _COUNTING_FAIL_EMOJI,
    _SYSTEM_LABELS, _SYSTEM_CHOICES,
    disabled_commands, global_disabled_commands, load_disabled_commands,
    prefix_channel_rules, _prefix_channel_allowed, load_prefix_restrictions, set_prefix,
    register_bot_instance,
)

TOKEN = os.getenv("TOKEN_ADMIN")
_GUILD_ID = int(os.getenv("GUILD_ID", "0"))
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix=common.get_prefix, intents=intents, help_command=None)
register_bot_instance(bot)

# ═══════════════════════════════════════════════════════
# COUNTING SYSTEM
# ═══════════════════════════════════════════════════════

def _eval_weight(formula: str, count: int) -> float:
    try:
        if formula.replace(".","").replace("-","").isdigit():
            return max(0.0, float(formula))
        safe = {"count": count, "__builtins__": {}}
        result = eval(formula.replace("^","**"), safe)
        return max(0.0, float(result))
    except Exception:
        return 1.0


async def _process_counting(message: discord.Message):
    if not message.guild: return
    gid = message.guild.id
    async with get_db() as db:
        async with db.execute(
            "SELECT enabled,channel_id,announce_channel_id FROM counting_config WHERE guild_id=?", (gid,)) as cur:
            cfg = await cur.fetchone()
    if not cfg or not cfg[0] or message.channel.id != cfg[1]: return

    ban_check = int(datetime.now(UTC).timestamp())
    async with get_db() as db:
        async with db.execute("SELECT unban_time FROM counting_bans WHERE guild_id=? AND user_id=? AND unban_time>?",
                              (gid, message.author.id, ban_check)) as cur:
            if await cur.fetchone(): return

    content = message.content.strip().replace(",","")
    try:
        num = int(float(content))
        if str(num) != content and f"{num}" != content.split(".")[0]: raise ValueError
    except ValueError:
        return

    async with get_db() as db:
        async with db.execute("SELECT current_count,last_user_id,record FROM counting_state WHERE guild_id=?", (gid,)) as cur:
            state = await cur.fetchone()
    current = state[0] if state else 0
    last_uid = state[1] if state else 0
    record   = state[2] if state else 0

    if message.author.id == last_uid:
        try: await message.add_reaction("⛔")
        except Exception: pass
        unban_until = int(datetime.now(UTC).timestamp()) + 300
        async with db_lock:
            async with get_db() as db:
                await db.execute("INSERT OR REPLACE INTO counting_bans VALUES(?,?,?)", (gid, message.author.id, unban_until))
                await db.execute("UPDATE counting_state SET current_count=0,last_user_id=0 WHERE guild_id=?", (gid,))
                await db.commit()
        await message.channel.send(f"❌ {message.author.mention} counted twice in a row! Count reset to **0**. 5 min ban.")
        return

    if num != current + 1:
        try: await message.add_reaction("❌")
        except Exception: pass
        unban_until = int(datetime.now(UTC).timestamp()) + 60
        async with db_lock:
            async with get_db() as db:
                await db.execute("INSERT OR REPLACE INTO counting_bans VALUES(?,?,?)", (gid, message.author.id, unban_until))
                await db.execute("UPDATE counting_state SET current_count=0,last_user_id=0 WHERE guild_id=?", (gid,))
                await db.commit()
        await message.channel.send(f"❌ {message.author.mention} broke the count! Next was **{current+1}**, got **{num}**. Count reset. 1 min ban.")
        return

    new_count = num
    new_record = max(record, new_count)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO counting_state(guild_id,current_count,last_user_id,record) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id) DO UPDATE SET current_count=excluded.current_count,"
                "last_user_id=excluded.last_user_id,record=MAX(record,excluded.record)",
                (gid, new_count, message.author.id, new_record))
            await db.commit()

    try: await message.add_reaction("✅")
    except Exception: pass

    # Check special prizes
    async with get_db() as db:
        async with db.execute("SELECT prize_type,prize_value,prize_amount,label FROM counting_special_prizes "
                              "WHERE guild_id=? AND number=?", (gid, new_count)) as cur:
            specials = await cur.fetchall()
    for p_type, p_value, p_amount, label in specials:
        await _award_counting_prize(gid, message.author, p_type, p_value, p_amount)
        ann_ch_id = cfg[2]
        ann_ch = bot.get_channel(ann_ch_id) if ann_ch_id else message.channel
        if ann_ch:
            try: await ann_ch.send(f"🎉 {message.author.mention} reached **{new_count}** and won **{label or p_value}**!")
            except Exception: pass

    # Regular prizes (random draw)
    async with get_db() as db:
        async with db.execute("SELECT prize_type,prize_value,prize_amount,weight_formula FROM counting_prizes WHERE guild_id=?",
                              (gid,)) as cur:
            prize_pool = await cur.fetchall()
    if prize_pool:
        weights = [_eval_weight(p[3], new_count) for p in prize_pool]
        total_w = sum(weights)
        if total_w > 0:
            roll = random.uniform(0, total_w)
            upto = 0.0
            for (p_type, p_value, p_amount, _), w in zip(prize_pool, weights):
                upto += w
                if upto >= roll:
                    if random.random() < (w / total_w):
                        await _award_counting_prize(gid, message.author, p_type, p_value, p_amount)
                    break

    if new_record > record:
        try: await message.add_reaction("🏆")
        except Exception: pass


async def _award_counting_prize(guild_id, member, prize_type, prize_value, prize_amount):
    if prize_type == "balance":
        await add_balance(guild_id, member.id, prize_amount, bot=bot)
    elif prize_type == "exp":
        await add_exp(guild_id, member.id, prize_amount)
    elif prize_type == "tickets":
        await add_tickets(guild_id, member.id, prize_amount)
    elif prize_type == "item":
        await inventory_add(guild_id, member.id, prize_value, prize_amount)
    elif prize_type == "role":
        guild = bot.get_guild(guild_id)
        if guild:
            role = guild.get_role(int(prize_value))
            if role:
                try: await member.add_roles(role)
                except Exception: pass


@bot.tree.command(name="setcounting", description="Configure the counting system")
@app_commands.describe(channel="Counting channel", announce_channel="Where to announce milestones")
@command_enabled()
async def setcounting(interaction: discord.Interaction, channel: discord.TextChannel,
                      announce_channel: discord.TextChannel = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    ann_id = announce_channel.id if announce_channel else channel.id
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO counting_config VALUES(?,?,?,?)",
                             (interaction.guild.id, 1, channel.id, ann_id))
            await db.execute("INSERT OR IGNORE INTO counting_state VALUES(?,0,0,0,0)", (interaction.guild.id,))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Counting enabled in {channel.mention}. Milestones → {(announce_channel or channel).mention}")

@bot.command(name="setcounting")
async def pfx_setcounting(ctx, channel: discord.TextChannel, announce_channel: discord.TextChannel = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setcounting._callback(FakeInteraction(ctx), channel, announce_channel)


@bot.command(name="disablecounting")
async def cmd_disablecounting(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE counting_config SET enabled=0 WHERE guild_id=?", (ctx.guild.id,))
            await db.commit()
    await ctx.send("🔒 Counting disabled.")


@bot.command(name="resetcount")
async def cmd_resetcount(ctx, value: int = 0):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE counting_state SET current_count=?,last_user_id=0 WHERE guild_id=?",
                             (value, ctx.guild.id))
            await db.commit()
    await ctx.send(f"✅ Count reset to **{value}**.")


@bot.command(name="countingstats")
async def cmd_countingstats(ctx):
    async with get_db() as db:
        async with db.execute("SELECT current_count,last_user_id,record FROM counting_state WHERE guild_id=?",
                              (ctx.guild.id,)) as cur:
            state = await cur.fetchone()
    if not state: await ctx.send("❌ Counting not configured."); return
    count, last_uid, record = state
    last_member = ctx.guild.get_member(last_uid) if last_uid else None
    embed = discord.Embed(title="🔢 Counting Stats", color=discord.Color.blue())
    embed.add_field(name="Current Count", value=str(count))
    embed.add_field(name="Last Counter", value=last_member.mention if last_member else "Nobody yet")
    embed.add_field(name="Record", value=str(record))
    await ctx.send(embed=embed)


@bot.command(name="unbancounter")
async def cmd_unbancounter(ctx, user: discord.Member):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM counting_bans WHERE guild_id=? AND user_id=?",
                             (ctx.guild.id, user.id))
            await db.commit()
    await ctx.send(f"✅ Unbanned {user.mention} from counting.")


@bot.command(name="addcountingprize")
async def cmd_addcountingprize(ctx, prize_type: str, prize_value: str,
                                prize_amount: int = 0, *, weight_formula: str = "1"):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if prize_type not in ("balance","exp","tickets","item","role"):
        await ctx.send("❌ Valid types: balance, exp, tickets, item, role"); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO counting_prizes(guild_id,prize_type,prize_value,prize_amount,weight_formula) VALUES(?,?,?,?,?)",
                (ctx.guild.id, prize_type, prize_value, prize_amount, weight_formula))
            await db.commit()
    await ctx.send(f"✅ Added counting prize: `{prize_type}` — **{prize_value}** (×{prize_amount}) weight: `{weight_formula}`")


@bot.command(name="removecountingprize")
async def cmd_removecountingprize(ctx, prize_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT id FROM counting_prizes WHERE id=? AND guild_id=?",
                                  (prize_id, ctx.guild.id)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Prize #{prize_id} not found."); return
            await db.execute("DELETE FROM counting_prizes WHERE id=?", (prize_id,))
            await db.commit()
    await ctx.send(f"🗑 Removed counting prize #{prize_id}.")


@bot.command(name="addspecialprize")
async def cmd_addspecialprize(ctx, number: int, prize_type: str, prize_value: str,
                               prize_amount: int = 0, *, label: str = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if prize_type not in ("balance","exp","tickets","item","role"):
        await ctx.send("❌ Valid types: balance, exp, tickets, item, role"); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO counting_special_prizes(guild_id,number,prize_type,prize_value,prize_amount,label) VALUES(?,?,?,?,?,?)",
                (ctx.guild.id, number, prize_type, prize_value, prize_amount, label))
            await db.commit()
    await ctx.send(f"✅ Special prize at **{number}**: `{prize_type}` — **{label or prize_value}**")


@bot.command(name="removespecialprize")
async def cmd_removespecialprize(ctx, prize_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT id FROM counting_special_prizes WHERE id=? AND guild_id=?",
                                  (prize_id, ctx.guild.id)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Prize #{prize_id} not found."); return
            await db.execute("DELETE FROM counting_special_prizes WHERE id=?", (prize_id,))
            await db.commit()
    await ctx.send(f"🗑 Removed special prize #{prize_id}.")


@bot.command(name="listcountingprizes")
async def cmd_listcountingprizes(ctx):
    async with get_db() as db:
        async with db.execute("SELECT id,prize_type,prize_value,prize_amount,weight_formula FROM counting_prizes WHERE guild_id=?",
                              (ctx.guild.id,)) as cur:
            regular = await cur.fetchall()
        async with db.execute("SELECT id,number,prize_type,prize_value,prize_amount,label FROM counting_special_prizes "
                              "WHERE guild_id=? ORDER BY number", (ctx.guild.id,)) as cur:
            specials = await cur.fetchall()
    embed = discord.Embed(title="🎁 Counting Prizes", color=discord.Color.blue())
    if regular:
        lines = [f"`#{pid}` {pt}: **{pv}** ×{pa} | formula: `{wf}`" for pid,pt,pv,pa,wf in regular]
        embed.add_field(name="Regular Prizes", value="\n".join(lines), inline=False)
    if specials:
        lines = [f"`#{pid}` At **{n}**: {pt}: **{label or pv}** ×{pa}" for pid,n,pt,pv,pa,label in specials]
        embed.add_field(name="Special Prizes (at milestone)", value="\n".join(lines), inline=False)
    if not regular and not specials:
        embed.description = "No prizes configured."
    await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════
# VERIFICATION
# ═══════════════════════════════════════════════════════

class VerificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Verify", style=discord.ButtonStyle.success, custom_id="verify_panel:verify")
    async def verify_btn(self, interaction: discord.Interaction, btn):
        gid = interaction.guild.id
        async with get_db() as db:
            async with db.execute("SELECT verified_role_id,unverified_role_id FROM verification_config WHERE guild_id=?",
                                  (gid,)) as cur:
                cfg = await cur.fetchone()
        if not cfg:
            await interaction.response.send_message("❌ Verification not configured.", ephemeral=True); return
        verified_rid, unverified_rid = cfg
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            await interaction.response.send_message("❌ Could not find your member object.", ephemeral=True); return
        verified_role = interaction.guild.get_role(verified_rid)
        if verified_role and verified_role in member.roles:
            await interaction.response.send_message("✅ You are already verified!", ephemeral=True); return
        try:
            if verified_rid:
                role = interaction.guild.get_role(verified_rid)
                if role: await member.add_roles(role)
            if unverified_rid:
                role = interaction.guild.get_role(unverified_rid)
                if role and role in member.roles: await member.remove_roles(role)
            await interaction.response.send_message("✅ You have been verified! Welcome!", ephemeral=True)
            await log_event(gid, "admin", _log_embed("✅ Member Verified", discord.Color.green(),
                User=member.mention))
        except discord.Forbidden:
            await interaction.response.send_message("❌ Bot lacks permission to assign roles.", ephemeral=True)


@bot.tree.command(name="setverification", description="Post a verification panel")
@app_commands.describe(channel="Channel to post in", message="Text shown above the verify button",
                       verified_role="Role to give on verify", unverified_role="Role to remove on verify")
@command_enabled()
async def setverification(interaction: discord.Interaction, channel: discord.TextChannel,
                          message: str = "Click the button below to verify yourself.",
                          verified_role: discord.Role = None, unverified_role: discord.Role = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    gid = interaction.guild.id
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO verification_config VALUES(?,?,?,?,?,?)",
                (gid, channel.id, 0,
                 verified_role.id if verified_role else 0,
                 unverified_role.id if unverified_role else 0, message))
            await db.commit()
    embed = discord.Embed(title="🔐 Verification", description=message, color=discord.Color.blue())
    view = VerificationView()
    msg = await channel.send(embed=embed, view=view)
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE verification_config SET message_id=? WHERE guild_id=?", (msg.id, gid))
            await db.commit()
    await interaction.response.send_message(f"✅ Verification panel posted in {channel.mention}.")

@bot.command(name="setverification")
async def pfx_setverification(ctx, channel: discord.TextChannel, verified_role: discord.Role = None,
                               unverified_role: discord.Role = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setverification._callback(FakeInteraction(ctx), channel, "Click below to verify.", verified_role, unverified_role)


@bot.command(name="disableverification")
async def cmd_disableverification(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM verification_config WHERE guild_id=?", (ctx.guild.id,))
            await db.commit()
    await ctx.send("🔒 Verification disabled and config cleared.")

# ═══════════════════════════════════════════════════════
# WELCOME
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="setwelcome", description="Set a DM welcome message for new members")
@app_commands.describe(message="Welcome message ({user} = mention, {server} = server name)")
@command_enabled()
async def setwelcome(interaction: discord.Interaction, message: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO welcome_config(guild_id,enabled,message) VALUES(?,?,?) "
                "ON CONFLICT(guild_id) DO UPDATE SET enabled=1,message=excluded.message",
                (interaction.guild.id, 1, message))
            await db.commit()
    await interaction.response.send_message(f"✅ Welcome DM set. Preview:\n{message[:200]}")

@bot.command(name="setwelcome")
async def pfx_setwelcome(ctx, *, message: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setwelcome._callback(FakeInteraction(ctx), message)


@bot.command(name="disablewelcome")
async def cmd_disablewelcome(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE welcome_config SET enabled=0 WHERE guild_id=?", (ctx.guild.id,))
            await db.commit()
    await ctx.send("🔒 Welcome DM disabled.")


@bot.tree.command(name="setwelcomechannel", description="Set a channel welcome message for new members")
@app_commands.describe(channel="Channel to post in",
                       message="Message ({user} = mention, {server} = server name)")
@command_enabled()
async def setwelcomechannel(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO welcome_config(guild_id,enabled,message,channel_id,channel_enabled,channel_message) "
                "VALUES(?,0,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET "
                "channel_id=excluded.channel_id,channel_enabled=1,channel_message=excluded.channel_message",
                (interaction.guild.id, "", channel.id, 1, message))
            await db.commit()
    await interaction.response.send_message(f"✅ Welcome channel message set in {channel.mention}.")

@bot.command(name="setwelcomechannel")
async def pfx_setwelcomechannel(ctx, channel: discord.TextChannel, *, message: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setwelcomechannel._callback(FakeInteraction(ctx), channel, message)


@bot.command(name="disablewelcomechannel")
async def cmd_disablewelcomechannel(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE welcome_config SET channel_enabled=0 WHERE guild_id=?", (ctx.guild.id,))
            await db.commit()
    await ctx.send("🔒 Welcome channel message disabled.")


# ═══════════════════════════════════════════════════════
# GAMBLING — BLACKJACK + ROULETTE
# ═══════════════════════════════════════════════════════

_SUITS  = ["♠","♥","♦","♣"]
_RANKS  = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
_VALUES = {"A":11,"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"10":10,"J":10,"Q":10,"K":10}

def _new_deck() -> list[tuple[str,str]]:
    deck = [(r, s) for s in _SUITS for r in _RANKS]
    random.shuffle(deck)
    return deck

def _hand_value(hand: list[tuple[str,str]]) -> int:
    total = sum(_VALUES[r] for r, _ in hand)
    aces  = sum(1 for r, _ in hand if r == "A")
    while total > 21 and aces:
        total -= 10; aces -= 1
    return total

def _fmt_hand(hand: list[tuple[str,str]], hide_second: bool = False) -> str:
    if hide_second and len(hand) > 1:
        return f"{hand[0][0]}{hand[0][1]}  🂠"
    return "  ".join(f"{r}{s}" for r, s in hand)

def _bj_embed(state: dict, reveal_dealer: bool = False) -> discord.Embed:
    p_val = _hand_value(state["player"])
    d_val = _hand_value(state["dealer"]) if reveal_dealer else _hand_value([state["dealer"][0]])
    color = discord.Color.green() if reveal_dealer and p_val <= 21 else discord.Color.gold()
    embed = discord.Embed(title="🃏 Blackjack", color=color)
    embed.add_field(name=f"Dealer {'('+str(d_val)+')' if reveal_dealer else '(?)'}", 
                    value=_fmt_hand(state["dealer"], hide_second=not reveal_dealer), inline=False)
    embed.add_field(name=f"Your Hand ({p_val})", value=_fmt_hand(state["player"]), inline=False)
    embed.add_field(name="Bet", value=f"{state['bet']:,} coins", inline=True)
    return embed


class _BJView(discord.ui.View):
    def __init__(self, state: dict, guild_id: int, user_id: int):
        super().__init__(timeout=60)
        self.state = state; self.guild_id = guild_id; self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your game.", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, result: str):
        for item in self.children: item.disabled = True
        s = self.state
        p_val = _hand_value(s["player"]); d_val = _hand_value(s["dealer"])
        embed = _bj_embed(s, reveal_dealer=True)
        bet = s["bet"]
        if result == "win":
            winnings = bet * 2
            await add_balance(self.guild_id, self.user_id, winnings, bot=bot)
            desc = f"🎉 **You Win!** +{winnings:,} coins"
        elif result == "blackjack":
            winnings = int(bet * 2.5)
            await add_balance(self.guild_id, self.user_id, winnings, bot=bot)
            desc = f"🃏 **Blackjack!** +{winnings:,} coins (2.5×)"
        elif result == "push":
            await add_balance(self.guild_id, self.user_id, bet, bot=bot)
            desc = f"🤝 **Push!** Bet returned ({bet:,} coins)"
        else:
            desc = f"💸 **You Lose!** -{bet:,} coins"
        embed.description = desc
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="👊")
    async def hit(self, interaction: discord.Interaction, btn):
        s = self.state
        s["player"].append(s["deck"].pop())
        p_val = _hand_value(s["player"])
        if p_val > 21:
            await self._finish(interaction, "lose")
        elif p_val == 21:
            await self._stand_logic(interaction)
        else:
            await interaction.response.edit_message(embed=_bj_embed(s), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, emoji="✋")
    async def stand(self, interaction: discord.Interaction, btn):
        await self._stand_logic(interaction)

    async def _stand_logic(self, interaction: discord.Interaction):
        s = self.state
        while _hand_value(s["dealer"]) < 17:
            s["dealer"].append(s["deck"].pop())
        p_val = _hand_value(s["player"]); d_val = _hand_value(s["dealer"])
        if d_val > 21 or p_val > d_val:
            result = "win"
        elif p_val == d_val:
            result = "push"
        else:
            result = "lose"
        await self._finish(interaction, result)

    @discord.ui.button(label="Double Down", style=discord.ButtonStyle.danger, emoji="💰")
    async def double_down(self, interaction: discord.Interaction, btn):
        s = self.state
        extra = s["bet"]
        bal = await get_balance(self.guild_id, self.user_id)
        if bal < extra:
            await interaction.response.send_message("❌ Not enough balance to double down.", ephemeral=True); return
        await add_balance(self.guild_id, self.user_id, -extra, bot=bot)
        s["bet"] *= 2
        s["player"].append(s["deck"].pop())
        p_val = _hand_value(s["player"])
        if p_val > 21:
            await self._finish(interaction, "lose")
        else:
            await self._stand_logic(interaction)

    async def on_timeout(self):
        for item in self.children: item.disabled = True
        s = self.state
        await add_balance(self.guild_id, self.user_id, s["bet"], bot=bot)


@bot.tree.command(name="blackjack", description="Play blackjack against the dealer")
@app_commands.describe(bet="Coins to bet (uses one gamble token)")
@command_enabled()
async def blackjack(interaction: discord.Interaction, bet: int):
    if not await is_system_enabled(interaction.guild.id, "gamble"):
        await interaction.response.send_message("❌ Gambling is disabled.", ephemeral=True); return
    gid, uid = interaction.guild.id, interaction.user.id
    if bet <= 0:
        await interaction.response.send_message("❌ Bet must be > 0.", ephemeral=True); return

    inv = await inventory_get(gid, uid)
    tokens = next((q for n, q in inv if n.lower() == GAMBLE_TOKEN.lower()), 0)
    if tokens < 1:
        await interaction.response.send_message(
            f"❌ You need a **{GAMBLE_TOKEN}** to gamble. Ask an admin for one!", ephemeral=True); return

    bal = await get_balance(gid, uid)
    if bal < bet:
        await interaction.response.send_message("❌ Not enough balance.", ephemeral=True); return

    await inventory_remove(gid, uid, GAMBLE_TOKEN, 1)
    await add_balance(gid, uid, -bet, bot=bot)

    async with get_db() as db:
        async with db.execute("SELECT date FROM daily_gamble_log WHERE guild_id=? AND user_id=?",
                              (gid, uid)) as cur:
            log_row = await cur.fetchone()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO daily_gamble_log VALUES(?,?,?)", (gid, uid, today))
            await db.commit()

    deck = _new_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    state = {"deck": deck, "player": player, "dealer": dealer, "bet": bet}

    p_val = _hand_value(player)
    if p_val == 21:
        while _hand_value(dealer) < 17:
            dealer.append(deck.pop())
        d_val = _hand_value(dealer)
        embed = _bj_embed(state, reveal_dealer=True)
        if d_val == 21:
            embed.description = "🤝 **Push! Both got Blackjack.** Bet returned."
            await add_balance(gid, uid, bet, bot=bot)
        else:
            winnings = int(bet * 2.5)
            await add_balance(gid, uid, winnings, bot=bot)
            embed.description = f"🃏 **Blackjack!** +{winnings:,} coins"
        await interaction.response.send_message(embed=embed)
        return

    view = _BJView(state, gid, uid)
    await interaction.response.send_message(embed=_bj_embed(state), view=view)

@bot.command(name="blackjack")
async def pfx_blackjack(ctx, bet: int):
    await blackjack._callback(FakeInteraction(ctx), bet)


_ROULETTE_RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

@bot.tree.command(name="roulette", description="Bet on a roulette number (0-36) or color (red/black)")
@app_commands.describe(bet="Coins to bet", choice="Number 0-36, 'red', or 'black'")
@command_enabled()
async def roulette_cmd(interaction: discord.Interaction, bet: int, choice: str):
    if not await is_system_enabled(interaction.guild.id, "gamble"):
        await interaction.response.send_message("❌ Gambling is disabled.", ephemeral=True); return
    gid, uid = interaction.guild.id, interaction.user.id
    if bet <= 0:
        await interaction.response.send_message("❌ Bet must be > 0.", ephemeral=True); return

    inv = await inventory_get(gid, uid)
    tokens = next((q for n, q in inv if n.lower() == GAMBLE_TOKEN.lower()), 0)
    if tokens < 1:
        await interaction.response.send_message(
            f"❌ You need a **{GAMBLE_TOKEN}** to gamble.", ephemeral=True); return

    bal = await get_balance(gid, uid)
    if bal < bet:
        await interaction.response.send_message("❌ Not enough balance.", ephemeral=True); return

    choice = choice.strip().lower()
    number_bet = None
    if choice in ("red","black"):
        color_bet = choice
    else:
        try:
            number_bet = int(choice)
            if not (0 <= number_bet <= 36):
                await interaction.response.send_message("❌ Number must be 0–36.", ephemeral=True); return
            color_bet = None
        except ValueError:
            await interaction.response.send_message("❌ Choose a number (0-36), 'red', or 'black'.", ephemeral=True); return

    await inventory_remove(gid, uid, GAMBLE_TOKEN, 1)
    await add_balance(gid, uid, -bet, bot=bot)

    result = random.randint(0, 36)
    result_color = "🟥 Red" if result in _ROULETTE_RED else ("⬛ Black" if result != 0 else "🟩 Green")

    won = False; payout = 0
    if number_bet is not None and result == number_bet:
        won = True; payout = bet * 35
    elif color_bet == "red" and result in _ROULETTE_RED:
        won = True; payout = bet
    elif color_bet == "black" and result != 0 and result not in _ROULETTE_RED:
        won = True; payout = bet

    embed = discord.Embed(title="🎡 Roulette", color=discord.Color.green() if won else discord.Color.red())
    embed.add_field(name="Ball landed on", value=f"**{result}** — {result_color}", inline=False)
    embed.add_field(name="Your bet", value=f"{bet:,} coins on **{choice}**", inline=False)
    if won:
        total_return = bet + payout
        await add_balance(gid, uid, total_return, bot=bot)
        embed.description = f"🎉 **You Win!** +{payout:,} coins"
    else:
        embed.description = f"💸 **You Lose!** -{bet:,} coins"

    await interaction.response.send_message(embed=embed)

@bot.command(name="roulette")
async def pfx_roulette(ctx, bet: int, choice: str):
    await roulette_cmd._callback(FakeInteraction(ctx), bet, choice)


@bot.command(name="givegambletoken")
async def cmd_givegambletoken(ctx, user: discord.Member, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    await inventory_add(ctx.guild.id, user.id, GAMBLE_TOKEN, amount)
    await ctx.send(f"🎲 Gave **{amount}x {GAMBLE_TOKEN}** to {user.mention}.")
    await log_event(ctx.guild.id, "item", _log_embed("🎲 Gamble Token Given", discord.Color.green(),
        Admin=ctx.author.mention, User=user.mention, Tokens=str(amount)))

@bot.command(name="takegambletoken")
async def cmd_takegambletoken(ctx, user: discord.Member, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    if not await inventory_remove(ctx.guild.id, user.id, GAMBLE_TOKEN, amount):
        await ctx.send(f"❌ {user.mention} doesn't have {amount}x {GAMBLE_TOKEN}."); return
    await ctx.send(f"🗑 Took **{amount}x {GAMBLE_TOKEN}** from {user.mention}.")

@bot.command(name="givegambletokenrole")
async def cmd_givegambletokenrole(ctx, role: discord.Role, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    members = [m for m in ctx.guild.members if role in m.roles and not m.bot]
    if not members: await ctx.send(f"❌ No non-bot members with {role.mention}."); return
    async with ctx.typing():
        for m in members:
            await inventory_add(ctx.guild.id, m.id, GAMBLE_TOKEN, amount)
    await ctx.send(f"🎲 Gave **{amount}x {GAMBLE_TOKEN}** to **{len(members)}** member(s).")


async def daily_gamble_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(UTC)
        target = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now >= target: target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        for guild in bot.guilds:
            if not await is_system_enabled(guild.id, "gamble"): continue
            for member in guild.members:
                if member.bot: continue
                inv = await inventory_get(guild.id, member.id)
                tokens = next((q for n, q in inv if n.lower() == GAMBLE_TOKEN.lower()), 0)
                if tokens < 1:
                    await inventory_add(guild.id, member.id, GAMBLE_TOKEN, 1)

# ═══════════════════════════════════════════════════════
# REDEEM CODES
# ═══════════════════════════════════════════════════════

def _parse_prize_json(prize_str: str) -> dict | None:
    """Parse prize JSON or shorthand like 'balance:1000' or 'exp:500'."""
    try:
        data = json.loads(prize_str)
        if isinstance(data, dict): return data
    except (json.JSONDecodeError, ValueError):
        pass
    if ":" in prize_str:
        parts = prize_str.strip().split(":")
        if len(parts) == 2:
            key, val = parts[0].strip().lower(), parts[1].strip()
            try: return {key: int(val)}
            except ValueError: return {"label": val}
    return None


async def _award_code_prize(gid: int, uid: int, prize: dict, guild: discord.Guild = None):
    if "balance" in prize and prize["balance"]: await add_balance(gid, uid, int(prize["balance"]), bot=bot)
    if "exp" in prize and prize["exp"]: await add_exp(gid, uid, int(prize["exp"]))
    if "tickets" in prize and prize["tickets"]: await add_tickets(gid, uid, int(prize["tickets"]))
    if "gamble_tokens" in prize and prize["gamble_tokens"]:
        await inventory_add(gid, uid, GAMBLE_TOKEN, int(prize["gamble_tokens"]))
    if "vip_keys" in prize and prize["vip_keys"]:
        await inventory_add(gid, uid, VIP_CHEST_KEY, int(prize["vip_keys"]))
    if "item" in prize and prize["item"]:
        qty = int(prize.get("item_qty", 1))
        await inventory_add(gid, uid, prize["item"], qty)
    if "role_id" in prize and prize["role_id"] and guild:
        role = guild.get_role(int(prize["role_id"]))
        member = guild.get_member(uid)
        if role and member:
            try: await member.add_roles(role)
            except Exception: pass


def _prize_summary(prize: dict, guild: discord.Guild = None) -> str:
    parts = []
    if prize.get("balance"):       parts.append(f"💰 {int(prize['balance']):,} coins")
    if prize.get("exp"):           parts.append(f"⭐ {int(prize['exp']):,} EXP")
    if prize.get("tickets"):       parts.append(f"🎟 {prize['tickets']} ticket(s)")
    if prize.get("gamble_tokens"): parts.append(f"🎲 {prize['gamble_tokens']} gamble token(s)")
    if prize.get("vip_keys"):      parts.append(f"🔑 {prize['vip_keys']} VIP key(s)")
    if prize.get("item"):          parts.append(f"🎒 {prize.get('item_qty',1)}x {prize['item']}")
    if prize.get("role_id") and guild:
        r = guild.get_role(int(prize["role_id"]))
        if r: parts.append(f"👑 {r.mention}")
    if prize.get("label"):         parts.append(str(prize["label"]))
    return " + ".join(parts) if parts else "Unknown reward"


@bot.tree.command(name="createcode", description="Create a redeem code")
@app_commands.describe(
    code="The code users type to redeem",
    prize_json='Prize as JSON {"balance":1000,"exp":500} or shorthand "balance:1000"',
    uses="Max uses (-1 = unlimited, default 1)",
    min_level="Minimum Activity Rank required (default 0 = any)",
    min_balance="Minimum balance required (default 0 = any)",
    required_role="Role required to redeem this code")
@command_enabled()
async def createcode(interaction: discord.Interaction, code: str, prize_json: str,
                     uses: int = 1, min_level: int = 0, min_balance: int = 0,
                     required_role: discord.Role = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    code = code.strip().upper()
    if not code:
        await interaction.response.send_message("❌ Code cannot be empty.", ephemeral=True); return
    prize = _parse_prize_json(prize_json)
    if not prize:
        await interaction.response.send_message(
            '❌ Invalid prize format. Use JSON like `{"balance":1000}` or shorthand `balance:1000`.',
            ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute(
                    "INSERT INTO redeem_codes(guild_id,code,prize_json,uses_left,min_level,min_balance,required_role_id) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (interaction.guild.id, code, json.dumps(prize), uses,
                     min_level, min_balance, required_role.id if required_role else 0))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(f"❌ Code **{code}** already exists.", ephemeral=True); return
    uses_str = "unlimited" if uses == -1 else str(uses)
    reqs = []
    if min_level: reqs.append(f"Level {min_level}+")
    if min_balance: reqs.append(f"{min_balance:,}+ balance")
    if required_role: reqs.append(required_role.mention)
    req_str = " | Requirements: " + ", ".join(reqs) if reqs else ""
    await interaction.response.send_message(
        f"✅ Code **{code}** created!\n"
        f"Prize: {_prize_summary(prize, interaction.guild)} | Uses: {uses_str}{req_str}", ephemeral=True)
    await log_event(interaction.guild.id, "admin", _log_embed("🎟 Code Created", discord.Color.green(),
        Admin=interaction.user.mention, Code=code, Uses=uses_str))

@bot.command(name="createcode")
async def pfx_createcode(ctx, code: str, prize_json: str, uses: int = 1,
                          min_level: int = 0, min_balance: int = 0):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await createcode._callback(FakeInteraction(ctx), code, prize_json, uses, min_level, min_balance, None)


@bot.command(name="deletecode")
async def cmd_deletecode(ctx, code: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    code = code.strip().upper()
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT code FROM redeem_codes WHERE guild_id=? AND code=?",
                                  (ctx.guild.id, code)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Code **{code}** not found."); return
            await db.execute("DELETE FROM redeem_codes WHERE guild_id=? AND code=?", (ctx.guild.id, code))
            await db.execute("DELETE FROM code_uses WHERE guild_id=? AND code=?", (ctx.guild.id, code))
            await db.commit()
    await ctx.send(f"🗑 Code **{code}** deleted.")


@bot.command(name="listcodes")
async def cmd_listcodes(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with get_db() as db:
        async with db.execute(
            "SELECT code,prize_json,uses_left,min_level,min_balance FROM redeem_codes WHERE guild_id=? ORDER BY code",
            (ctx.guild.id,)) as cur:
            codes = await cur.fetchall()
    if not codes: await ctx.send("❌ No codes configured."); return
    lines = []
    for code, pj, uses, ml, mb in codes:
        try: prize = json.loads(pj); ps = _prize_summary(prize)
        except Exception: ps = str(pj)
        uses_str = "∞" if uses == -1 else str(uses)
        reqs = []
        if ml: reqs.append(f"Lvl{ml}+")
        if mb: reqs.append(f"{mb:,}+ bal")
        req_str = f" [{', '.join(reqs)}]" if reqs else ""
        lines.append(f"**{code}** ({uses_str} use{'s' if uses!=1 else ''}){req_str} — {ps[:60]}")
    embed = discord.Embed(title="🎟 Redeem Codes", description="\n".join(lines[:20]), color=discord.Color.green())
    if len(codes) > 20: embed.set_footer(text=f"+{len(codes)-20} more codes not shown")
    await ctx.send(embed=embed)


@bot.tree.command(name="redeem", description="Redeem a code for a reward")
@app_commands.describe(code="The code to redeem")
@command_enabled()
async def redeem(interaction: discord.Interaction, code: str):
    code = code.strip().upper()
    gid, uid = interaction.guild.id, interaction.user.id
    await interaction.response.defer(ephemeral=True)

    # Check guild code
    async with get_db() as db:
        async with db.execute(
            "SELECT prize_json,uses_left,min_level,min_balance,required_role_id FROM redeem_codes "
            "WHERE guild_id=? AND code=?", (gid, code)) as cur:
            guild_code = await cur.fetchone()
        # Check global code
        async with db.execute("SELECT prize_json,uses_left,min_level,min_balance FROM global_redeem_codes WHERE code=?",
                              (code,)) as cur:
            global_code = await cur.fetchone()

    if not guild_code and not global_code:
        await interaction.followup.send("❌ Invalid code.", ephemeral=True); return

    if guild_code:
        pj, uses_left, min_level, min_balance, req_role_id = guild_code
        async with get_db() as db:
            async with db.execute("SELECT user_id FROM code_uses WHERE guild_id=? AND code=? AND user_id=?",
                                  (gid, code, uid)) as cur:
                if await cur.fetchone():
                    await interaction.followup.send("❌ You've already redeemed this code.", ephemeral=True); return
        if uses_left == 0:
            await interaction.followup.send("❌ This code has no uses remaining.", ephemeral=True); return
        if min_level:
            lvl = await get_level(gid, uid)
            if lvl < min_level:
                await interaction.followup.send(f"❌ You need Activity Rank **{min_level}+** (you are {lvl}).", ephemeral=True); return
        if min_balance:
            bal = await get_balance(gid, uid)
            if bal < min_balance:
                await interaction.followup.send(f"❌ You need **{min_balance:,}+** coins (you have {bal:,}).", ephemeral=True); return
        if req_role_id:
            member = interaction.guild.get_member(uid)
            if not member or req_role_id not in {r.id for r in member.roles}:
                role = interaction.guild.get_role(req_role_id)
                await interaction.followup.send(
                    f"❌ You need the {role.mention if role else 'required'} role to redeem this code.", ephemeral=True); return
        try: prize = json.loads(pj)
        except Exception: await interaction.followup.send("❌ Corrupt prize data.", ephemeral=True); return
        async with db_lock:
            async with get_db() as db:
                await db.execute("INSERT INTO code_uses VALUES(?,?,?)", (gid, code, uid))
                if uses_left != -1:
                    await db.execute("UPDATE redeem_codes SET uses_left=uses_left-1 WHERE guild_id=? AND code=?",
                                     (gid, code))
                await db.commit()
        await _award_code_prize(gid, uid, prize, interaction.guild)
        await interaction.followup.send(
            f"✅ Code **{code}** redeemed!\nReward: {_prize_summary(prize, interaction.guild)}", ephemeral=True)
        await log_event(gid, "admin", _log_embed("🎟 Code Redeemed", discord.Color.green(),
            User=interaction.user.mention, Code=code, Reward=_prize_summary(prize)[:100]))
        return

    # Global code fallback
    pj, uses_left, min_level, min_balance = global_code
    async with get_db() as db:
        async with db.execute("SELECT user_id FROM global_code_uses WHERE code=? AND user_id=?", (code, uid)) as cur:
            if await cur.fetchone():
                await interaction.followup.send("❌ You've already redeemed this global code.", ephemeral=True); return
    if uses_left == 0:
        await interaction.followup.send("❌ This code has no uses remaining.", ephemeral=True); return
    if min_level:
        lvl = await get_level(gid, uid)
        if lvl < min_level:
            await interaction.followup.send(f"❌ You need Activity Rank **{min_level}+**.", ephemeral=True); return
    if min_balance:
        bal = await get_balance(gid, uid)
        if bal < min_balance:
            await interaction.followup.send(f"❌ You need **{min_balance:,}+** coins.", ephemeral=True); return
    try: prize = json.loads(pj)
    except Exception: await interaction.followup.send("❌ Corrupt prize data.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT INTO global_code_uses VALUES(?,?)", (code, uid))
            if uses_left != -1:
                await db.execute("UPDATE global_redeem_codes SET uses_left=uses_left-1 WHERE code=?", (code,))
            await db.commit()
    await _award_code_prize(gid, uid, prize, interaction.guild)
    await interaction.followup.send(
        f"✅ Global code **{code}** redeemed!\nReward: {_prize_summary(prize, interaction.guild)}", ephemeral=True)

@bot.command(name="redeem")
async def pfx_redeem(ctx, code: str):
    await redeem._callback(FakeInteraction(ctx), code)


# ═══════════════════════════════════════════════════════
# ADMIN PANEL
# ═══════════════════════════════════════════════════════

class _APModal(discord.ui.Modal):
    user_input    = discord.ui.TextInput(label="User ID or @mention", max_length=30)
    amount_input  = discord.ui.TextInput(label="Amount (leave blank where not needed)", required=False, max_length=20)

    def __init__(self, action: str):
        super().__init__(title=f"Admin Panel — {action.replace('_',' ').title()}")
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        uid_raw = self.user_input.value.strip().lstrip("<@!").rstrip(">")
        try: uid = int(uid_raw)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True); return

        member = interaction.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Member not found in this server.", ephemeral=True); return

        amount = 0
        if self.amount_input.value.strip():
            try: amount = int(self.amount_input.value.strip())
            except ValueError:
                await interaction.response.send_message("❌ Invalid amount.", ephemeral=True); return

        gid = interaction.guild.id
        action = self.action
        if action == "add_balance":
            if amount <= 0: await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
            await add_balance(gid, uid, amount, bot=bot)
            msg = f"✅ Added {amount:,} coins to {member.mention}."
        elif action == "remove_balance":
            if amount <= 0: await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
            await add_balance(gid, uid, -amount, bot=bot)
            msg = f"❌ Removed {amount:,} coins from {member.mention}."
        elif action == "add_exp":
            if amount <= 0: await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
            await add_exp(gid, uid, amount, is_bonus=True)
            msg = f"⭐ Added {amount:,} EXP to {member.mention}."
        elif action == "reset_balance":
            await _do_reset(gid, uid, "balance")
            msg = f"🔄 Reset balance for {member.mention}."
        elif action == "reset_exp":
            await _do_reset(gid, uid, "exp")
            msg = f"🔄 Reset EXP for {member.mention}."
        elif action == "reset_inventory":
            await _do_reset(gid, uid, "inventory")
            msg = f"🔄 Reset inventory for {member.mention}."
        elif action == "reset_stats":
            await _do_reset(gid, uid, "stats")
            msg = f"🔄 Reset stats for {member.mention}."
        elif action == "reset_all":
            await _do_reset(gid, uid, "all")
            msg = f"🔄 Reset everything for {member.mention}."
        else:
            msg = "❌ Unknown action."

        await interaction.response.send_message(msg, ephemeral=True)
        await log_event(gid, "admin", _log_embed(f"⚙️ Admin Panel: {action}", discord.Color.orange(),
            Admin=interaction.user.mention, Target=member.mention,
            Amount=str(amount) if amount else "N/A"))


class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _check(self, interaction: discord.Interaction) -> bool:
        if not await is_allowed_to_giveaway(interaction):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="💰 Add Balance", style=discord.ButtonStyle.success, custom_id="ap:add_balance", row=0)
    async def add_bal(self, i, b):
        if await self._check(i): await i.response.send_modal(_APModal("add_balance"))

    @discord.ui.button(label="💸 Remove Balance", style=discord.ButtonStyle.danger, custom_id="ap:remove_balance", row=0)
    async def rem_bal(self, i, b):
        if await self._check(i): await i.response.send_modal(_APModal("remove_balance"))

    @discord.ui.button(label="⭐ Add EXP", style=discord.ButtonStyle.success, custom_id="ap:add_exp", row=0)
    async def add_exp_btn(self, i, b):
        if await self._check(i): await i.response.send_modal(_APModal("add_exp"))

    @discord.ui.button(label="🔄 Reset Balance", style=discord.ButtonStyle.secondary, custom_id="ap:reset_balance", row=1)
    async def rst_bal(self, i, b):
        if await self._check(i): await i.response.send_modal(_APModal("reset_balance"))

    @discord.ui.button(label="🔄 Reset EXP", style=discord.ButtonStyle.secondary, custom_id="ap:reset_exp", row=1)
    async def rst_exp(self, i, b):
        if await self._check(i): await i.response.send_modal(_APModal("reset_exp"))

    @discord.ui.button(label="🔄 Reset Inventory", style=discord.ButtonStyle.secondary, custom_id="ap:reset_inventory", row=1)
    async def rst_inv(self, i, b):
        if await self._check(i): await i.response.send_modal(_APModal("reset_inventory"))

    @discord.ui.button(label="🔄 Reset Stats", style=discord.ButtonStyle.secondary, custom_id="ap:reset_stats", row=2)
    async def rst_stats(self, i, b):
        if await self._check(i): await i.response.send_modal(_APModal("reset_stats"))

    @discord.ui.button(label="💥 Reset Everything", style=discord.ButtonStyle.danger, custom_id="ap:reset_all", row=2)
    async def rst_all(self, i, b):
        if await self._check(i): await i.response.send_modal(_APModal("reset_all"))


@bot.tree.command(name="setadminpanel", description="Post the admin panel in a channel")
@app_commands.describe(channel="Channel to post the admin panel in")
@command_enabled()
async def setadminpanel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await interaction.response.defer()
    gid = interaction.guild.id
    async with get_db() as db:
        async with db.execute("SELECT channel_id,message_id FROM admin_panel_config WHERE guild_id=?", (gid,)) as cur:
            old = await cur.fetchone()
    if old and old[0] and old[1]:
        old_ch = bot.get_channel(old[0])
        if old_ch:
            try: await (await old_ch.fetch_message(old[1])).delete()
            except Exception: pass
    embed = discord.Embed(title="⚙️ Admin Panel",
        description="Use the buttons below to manage player balances, EXP, and data.",
        color=discord.Color.blurple())
    embed.set_footer(text="All actions are logged. Only authorised roles can use these buttons.")
    msg = await channel.send(embed=embed, view=AdminPanelView())
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO admin_panel_config(guild_id,channel_id,message_id) VALUES(?,?,?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,message_id=excluded.message_id",
                (gid, channel.id, msg.id))
            await db.commit()
    await interaction.followup.send(f"✅ Admin panel posted in {channel.mention}.")

@bot.command(name="setadminpanel")
async def pfx_setadminpanel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setadminpanel._callback(FakeInteraction(ctx), channel)

# ═══════════════════════════════════════════════════════
# DISABLED COMMANDS
# ═══════════════════════════════════════════════════════

@bot.command(name="disablecmd")
async def cmd_disablecmd(ctx, *, cmd_name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    cmd_name = cmd_name.strip().lower()
    gid = ctx.guild.id
    disabled_commands.setdefault(gid, set()).add(cmd_name)
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute("INSERT INTO disabled_commands_persist VALUES(?,?)", (gid, cmd_name))
                await db.commit()
            except aiosqlite.IntegrityError:
                pass
    await ctx.send(f"🔒 Command **{cmd_name}** disabled in this server.")

@bot.command(name="enablecmd")
async def cmd_enablecmd(ctx, *, cmd_name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    cmd_name = cmd_name.strip().lower()
    gid = ctx.guild.id
    disabled_commands.get(gid, set()).discard(cmd_name)
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM disabled_commands_persist WHERE guild_id=? AND command_name=?",
                             (gid, cmd_name))
            await db.commit()
    await ctx.send(f"✅ Command **{cmd_name}** re-enabled.")

@bot.command(name="listcmds")
async def cmd_listcmds(ctx):
    gid = ctx.guild.id
    gcmds = sorted(global_disabled_commands)
    lcmds = sorted(disabled_commands.get(gid, set()))
    embed = discord.Embed(title="🔒 Disabled Commands", color=discord.Color.red())
    embed.add_field(name="Globally Disabled", value="\n".join(f"• {c}" for c in gcmds) or "None", inline=False)
    embed.add_field(name="Disabled in this Server", value="\n".join(f"• {c}" for c in lcmds) or "None", inline=False)
    await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════
# SYSTEM TOGGLES
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="enablesystem", description="Enable a system in this server")
@app_commands.choices(system=_SYSTEM_CHOICES)
@command_enabled()
async def enablesystem(interaction: discord.Interaction, system: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await set_system_flag(interaction.guild.id, system, True)
    label = _SYSTEM_LABELS.get(system, system)
    await interaction.response.send_message(f"✅ {label} **enabled** in this server.")

@bot.command(name="enablesystem")
async def pfx_enablesystem(ctx, system: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if system not in _SYSTEM_LABELS: await ctx.send(f"❌ Valid systems: {', '.join(_SYSTEM_LABELS)}"); return
    await enablesystem._callback(FakeInteraction(ctx), system)


@bot.tree.command(name="disablesystem", description="Disable a system in this server")
@app_commands.choices(system=_SYSTEM_CHOICES)
@command_enabled()
async def disablesystem(interaction: discord.Interaction, system: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await set_system_flag(interaction.guild.id, system, False)
    label = _SYSTEM_LABELS.get(system, system)
    await interaction.response.send_message(f"🔒 {label} **disabled** in this server.")

@bot.command(name="disablesystem")
async def pfx_disablesystem(ctx, system: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if system not in _SYSTEM_LABELS: await ctx.send(f"❌ Valid systems: {', '.join(_SYSTEM_LABELS)}"); return
    await disablesystem._callback(FakeInteraction(ctx), system)


@bot.command(name="systemstatus")
async def cmd_systemstatus(ctx):
    gid = ctx.guild.id
    embed = discord.Embed(title="⚙️ System Status", color=discord.Color.blurple())
    for key, label in _SYSTEM_LABELS.items():
        enabled = await is_system_enabled(gid, key)
        embed.add_field(name=label, value="✅ Enabled" if enabled else "🔒 Disabled", inline=True)
    await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════
# LOG CHANNELS
# ═══════════════════════════════════════════════════════

_LOG_TYPES = ["balance","exp","giveaway","mega","chest","box","item","trade","admin","command","error"]
_LOG_CHOICES = [app_commands.Choice(name=t.title(), value=t) for t in _LOG_TYPES]

@bot.tree.command(name="setlogchannel", description="Set a log channel for a specific event type")
@app_commands.describe(log_type="Type of events to log", channel="Channel to log to")
@app_commands.choices(log_type=_LOG_CHOICES)
@command_enabled()
async def setlogchannel(interaction: discord.Interaction, log_type: str, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO log_channels VALUES(?,?,?)",
                             (interaction.guild.id, log_type, channel.id))
            await db.commit()
    await interaction.response.send_message(f"✅ **{log_type.title()}** events → {channel.mention}")

@bot.command(name="setlogchannel")
async def pfx_setlogchannel(ctx, log_type: str, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if log_type not in _LOG_TYPES: await ctx.send(f"❌ Valid types: {', '.join(_LOG_TYPES)}"); return
    await setlogchannel._callback(FakeInteraction(ctx), log_type, channel)


@bot.command(name="removelogchannel")
async def cmd_removelogchannel(ctx, log_type: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if log_type not in _LOG_TYPES: await ctx.send(f"❌ Valid types: {', '.join(_LOG_TYPES)}"); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM log_channels WHERE guild_id=? AND log_type=?", (ctx.guild.id, log_type))
            await db.commit()
    await ctx.send(f"🗑 Removed log channel for **{log_type}**.")


@bot.command(name="listlogchannels")
async def cmd_listlogchannels(ctx):
    async with get_db() as db:
        async with db.execute("SELECT log_type,channel_id FROM log_channels WHERE guild_id=? ORDER BY log_type",
                              (ctx.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows: await ctx.send("❌ No log channels configured."); return
    lines = []
    for lt, cid in rows:
        ch = ctx.guild.get_channel(cid)
        lines.append(f"• **{lt.title()}** → {ch.mention if ch else f'<deleted {cid}>'}")
    await ctx.send(embed=discord.Embed(title="📋 Log Channels", description="\n".join(lines),
                                       color=discord.Color.blurple()))

# ═══════════════════════════════════════════════════════
# PREFIX CHANNEL RESTRICTIONS
# ═══════════════════════════════════════════════════════

@bot.command(name="disableprefixchannel")
async def cmd_disableprefixchannel(ctx, channel: discord.TextChannel, role: discord.Role = None):
    """Disable prefix commands in a channel globally, or for a specific role."""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    rid = role.id if role else 0
    key = (ctx.guild.id, channel.id)
    prefix_channel_rules.setdefault(key, {})[rid] = False
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO prefix_restrictions VALUES(?,?,?,?)",
                             (ctx.guild.id, channel.id, rid, 0))
            await db.commit()
    scope = f" for {role.mention}" if role else " for everyone"
    await ctx.send(f"🔒 Prefix commands disabled in {channel.mention}{scope}.")

@bot.command(name="enableprefixchannel")
async def cmd_enableprefixchannel(ctx, channel: discord.TextChannel, role: discord.Role = None):
    """Allow a specific role to use prefix commands in a channel where they're otherwise disabled."""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    rid = role.id if role else 0
    key = (ctx.guild.id, channel.id)
    prefix_channel_rules.setdefault(key, {})[rid] = True
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO prefix_restrictions VALUES(?,?,?,?)",
                             (ctx.guild.id, channel.id, rid, 1))
            await db.commit()
    scope = f" for {role.mention}" if role else " for everyone"
    await ctx.send(f"✅ Prefix commands enabled in {channel.mention}{scope}.")

@bot.command(name="resetprefixchannel")
async def cmd_resetprefixchannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    key = (ctx.guild.id, channel.id)
    prefix_channel_rules.pop(key, None)
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM prefix_restrictions WHERE guild_id=? AND channel_id=?",
                             (ctx.guild.id, channel.id))
            await db.commit()
    await ctx.send(f"✅ Prefix restrictions cleared for {channel.mention}.")

@bot.command(name="listprefixchannels")
async def cmd_listprefixchannels(ctx):
    async with get_db() as db:
        async with db.execute("SELECT channel_id,role_id,allowed FROM prefix_restrictions WHERE guild_id=?",
                              (ctx.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows: await ctx.send("❌ No prefix restrictions set."); return
    lines = []
    for cid, rid, allowed in rows:
        ch = ctx.guild.get_channel(cid)
        ch_str = ch.mention if ch else f"<#{cid}>"
        role_str = f"@{ctx.guild.get_role(rid).name}" if rid and ctx.guild.get_role(rid) else "everyone"
        status = "✅ Allowed" if allowed else "🔒 Blocked"
        lines.append(f"• {ch_str} | {role_str} → {status}")
    await ctx.send(embed=discord.Embed(title="🔒 Prefix Restrictions", description="\n".join(lines),
                                       color=discord.Color.orange()))


# ═══════════════════════════════════════════════════════
# AUTO-RESET ON LEAVE
# ═══════════════════════════════════════════════════════

@bot.command(name="enableautoreset")
async def cmd_enableautoreset(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO auto_reset_config VALUES(?,1)", (ctx.guild.id,))
            await db.commit()
    await ctx.send("✅ Auto-reset on leave enabled.")

@bot.command(name="disableautoreset")
async def cmd_disableautoreset(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE auto_reset_config SET enabled=0 WHERE guild_id=?", (ctx.guild.id,))
            await db.commit()
    await ctx.send("🔒 Auto-reset on leave disabled.")


@bot.command(name="setautoresetrule")
async def cmd_setautoresetrule(ctx, reset_type: str, delay_seconds: int = 0):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if reset_type not in ("balance","exp","inventory","tickets","stats","all"):
        await ctx.send("❌ Valid types: balance, exp, inventory, tickets, stats, all"); return
    if delay_seconds < 0: await ctx.send("❌ Delay must be ≥ 0."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO auto_reset_rules VALUES(?,?,?)",
                             (ctx.guild.id, reset_type, delay_seconds))
            await db.commit()
    delay_str = f"after {delay_seconds}s" if delay_seconds else "immediately"
    await ctx.send(f"✅ Auto-reset rule: **{reset_type}** will be reset {delay_str} when a member leaves.")


@bot.command(name="removeautoresetrule")
async def cmd_removeautoresetrule(ctx, reset_type: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM auto_reset_rules WHERE guild_id=? AND reset_type=?",
                             (ctx.guild.id, reset_type))
            await db.commit()
    await ctx.send(f"🗑 Removed auto-reset rule for **{reset_type}**.")


@bot.command(name="listautoresetrules")
async def cmd_listautoresetrules(ctx):
    async with get_db() as db:
        async with db.execute("SELECT enabled FROM auto_reset_config WHERE guild_id=?", (ctx.guild.id,)) as cur:
            cfg = await cur.fetchone()
        async with db.execute("SELECT reset_type,delay_seconds FROM auto_reset_rules WHERE guild_id=? ORDER BY reset_type",
                              (ctx.guild.id,)) as cur:
            rules = await cur.fetchall()
    status = "✅ Enabled" if (cfg and cfg[0]) else "🔒 Disabled"
    embed = discord.Embed(title="🔄 Auto-Reset on Leave", color=discord.Color.orange())
    embed.add_field(name="Status", value=status, inline=False)
    if rules:
        lines = [f"• **{rt}** — {'immediately' if ds==0 else f'after {ds}s'}" for rt, ds in rules]
        embed.add_field(name="Rules", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Rules", value="No rules set.", inline=False)
    await ctx.send(embed=embed)


async def auto_reset_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = int(datetime.now(UTC).timestamp())
        async with get_db() as db:
            async with db.execute(
                "SELECT guild_id,user_id,reset_type FROM auto_reset_pending WHERE reset_after<=?", (now,)) as cur:
                pending = await cur.fetchall()
        for gid, uid, reset_type in pending:
            try:
                await _do_reset(gid, uid, reset_type)
                await log_event(gid, "admin", _log_embed("🔄 Auto-Reset Executed", discord.Color.orange(),
                    User=f"<@{uid}>", Type=reset_type))
            except Exception as e:
                print(f"[AutoReset] execute error gid={gid} uid={uid} type={reset_type}: {e}")
                await log_event(gid, "error", _log_embed("❌ AutoReset Error", discord.Color.red(),
                    User=f"<@{uid}>", Type=reset_type, Error=str(e)[:200]))
        if pending:
            async with db_lock:
                async with get_db() as db:
                    await db.execute("DELETE FROM auto_reset_pending WHERE reset_after<=?", (now,))
                    await db.commit()
        await asyncio.sleep(30)

# ═══════════════════════════════════════════════════════
# EXP FROM CHAT
# ═══════════════════════════════════════════════════════

async def _calc_exp_gain(member: discord.Member, channel: discord.TextChannel) -> int:
    base = 1
    gid = member.guild.id
    async with get_db() as db:
        async with db.execute(
            "SELECT role_id,boost_percent,channel_id,category_id FROM exp_boosts WHERE guild_id=?", (gid,)) as cur:
            boosts = await cur.fetchall()
    role_ids   = {r.id for r in member.roles}
    cat_id     = getattr(channel, "category_id", None)
    total_boost = 0.0
    for role_id, boost_pct, ch_id, cat_id_rule in boosts:
        if role_id not in role_ids: continue
        if ch_id and ch_id != channel.id: continue
        if cat_id_rule and cat_id_rule != cat_id: continue
        total_boost += boost_pct
    return max(1, int(base + (base * total_boost / 100)))

# ═══════════════════════════════════════════════════════
# RESET COMMANDS
# ═══════════════════════════════════════════════════════

_RESET_TYPES = ("balance","exp","inventory","tickets","stats","all")

@bot.command(name="resetuser")
async def cmd_resetuser(ctx, user: discord.Member, reset_type: str = "all"):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if reset_type not in _RESET_TYPES:
        await ctx.send(f"❌ Valid types: {', '.join(_RESET_TYPES)}"); return
    await _do_reset(ctx.guild.id, user.id, reset_type)
    await ctx.send(f"🔄 Reset **{reset_type}** for {user.mention}.")
    await log_event(ctx.guild.id, "admin", _log_embed("🔄 User Reset", discord.Color.orange(),
        Admin=ctx.author.mention, User=user.mention, Type=reset_type))

@bot.command(name="resetrole")
async def cmd_resetrole(ctx, role: discord.Role, reset_type: str = "all"):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if reset_type not in _RESET_TYPES:
        await ctx.send(f"❌ Valid types: {', '.join(_RESET_TYPES)}"); return
    members = [m for m in ctx.guild.members if role in m.roles and not m.bot]
    if not members: await ctx.send(f"❌ No non-bot members with {role.mention}."); return
    async with ctx.typing():
        for m in members:
            await _do_reset(ctx.guild.id, m.id, reset_type)
    await ctx.send(f"🔄 Reset **{reset_type}** for **{len(members)}** member(s) with {role.mention}.")
    await log_event(ctx.guild.id, "admin", _log_embed("🔄 Role Reset", discord.Color.orange(),
        Admin=ctx.author.mention, Role=role.name, Type=reset_type, Count=str(len(members))))

# ═══════════════════════════════════════════════════════
# CLEANUP TRANSFER
# ═══════════════════════════════════════════════════════

@bot.command(name="cleanuptransfer")
async def cmd_cleanuptransfer(ctx):
    if ctx.author.id != BOT_OWNER_ID: await ctx.send("❌ Owner only."); return
    gid = ctx.guild.id
    _CLEANUP_TABLES = [
        "balances","exp_history","user_stats","inventory",
        "mega_tickets","mega_bought","giveaway_roles","log_channels",
        "disabled_commands_persist","system_flags","prefix_restrictions",
        "auto_reset_config","auto_reset_rules","auto_reset_pending",
        "verification_config","welcome_config","counting_config","counting_state",
        "counting_bans","counting_prizes","counting_special_prizes",
        "auto_giveaway_config","auto_giveaway_pool","giveaway_game_notify_config",
        "auto_entry_roles","auto_entry_users","auto_entry_threshold",
        "mega_announce_config","mega_info_config","mega_payout_config",
        "chest_channel_config","chest_prizes","rare_chest_config",
        "rare_drop_config","abuse_boxes","abuse_box_prizes","rare_box_config",
        "game_config","stats_channel_config","admin_panel_config",
        "balance_ranks","item_store","daily_key_log",
        "daily_gamble_log","redeem_codes","code_uses",
    ]
    async with ctx.typing():
        async with db_lock:
            async with get_db() as db:
                for tbl in _CLEANUP_TABLES:
                    try:
                        await db.execute(f"DELETE FROM {tbl} WHERE guild_id=?", (gid,))
                    except Exception as e:
                        print(f"[Cleanup] {tbl}: {e}")
                # Power giveaways + games + named tables
                for tbl in ("power_giveaway_config","power_giveaway_role_entries","power_giveaway_channel_rates",
                            "power_giveaway_role_boosts","power_giveaway_user_entries",
                            "games","game_config","game_answers","game_hints"):
                    try:
                        await db.execute(f"DELETE FROM {tbl} WHERE guild_id=?", (gid,))
                    except Exception as e:
                        print(f"[Cleanup] {tbl}: {e}")
                await db.commit()
    await ctx.send(f"🗑 Cleaned up all data for **{ctx.guild.name}** (`{gid}`).")

# ═══════════════════════════════════════════════════════
# TRANSFER (owner-only, migrate guild data)
# ═══════════════════════════════════════════════════════

@bot.command(name="transfer")
async def cmd_transfer(ctx, guild_id_from: int, guild_id_to: int):
    if ctx.author.id != BOT_OWNER_ID: await ctx.send("❌ Owner only."); return
    guild_from = bot.get_guild(guild_id_from)
    guild_to   = bot.get_guild(guild_id_to)
    if not guild_from: await ctx.send(f"❌ Source guild {guild_id_from} not found."); return
    if not guild_to:   await ctx.send(f"❌ Target guild {guild_id_to} not found."); return

    async with ctx.typing():
        _SIMPLE = [
            "balances","exp_history","user_stats","inventory",
            "mega_tickets","mega_bought","giveaway_roles","log_channels",
            "disabled_commands_persist","system_flags","prefix_restrictions",
            "auto_reset_config","auto_reset_rules","auto_reset_pending",
            "verification_config","welcome_config","counting_config","counting_state",
            "counting_bans","mega_announce_config","mega_info_config","mega_payout_config",
            "chest_channel_config","rare_chest_config","rare_drop_config",
            "game_config","stats_channel_config","admin_panel_config","balance_ranks",
            "item_store","daily_key_log","daily_gamble_log","redeem_codes","code_uses",
            "giveaway_game_notify_config","auto_entry_roles","auto_entry_users",
            "auto_entry_threshold","mega_payout_config","power_giveaway_config",
            "power_giveaway_role_entries","power_giveaway_channel_rates",
            "power_giveaway_role_boosts","power_giveaway_user_entries",
        ]

        # Tables with auto-increment IDs that are referenced by other tables
        _AUTO_INC = {
            "auto_giveaway_pool":   ("auto_giveaway_pool",   None, None),
            "chest_prizes":         ("chest_prizes",          None, None),
            "counting_prizes":      ("counting_prizes",       None, None),
            "counting_special_prizes": ("counting_special_prizes", None, None),
            "abuse_boxes":          None,
            "games":                ("games", None, None),
        }

        async with db_lock:
            async with get_db() as db:
                await db.execute("DELETE FROM auto_giveaway_config WHERE guild_id=?", (guild_id_to,))
                async with db.execute("SELECT channel_id,interval_seconds,duration_seconds,running FROM auto_giveaway_config WHERE guild_id=?",
                                       (guild_id_from,)) as cur:
                    row = await cur.fetchone()
                if row:
                    await db.execute("INSERT OR REPLACE INTO auto_giveaway_config VALUES(?,?,?,?,?)",
                                     (guild_id_to, *row))

                for tbl in _SIMPLE:
                    try:
                        async with db.execute(f"PRAGMA table_info({tbl})") as cur:
                            cols = [r[1] for r in await cur.fetchall()]
                        if "guild_id" not in cols: continue
                        await db.execute(f"DELETE FROM {tbl} WHERE guild_id=?", (guild_id_to,))
                        non_gid = [c for c in cols if c != "guild_id"]
                        col_str = "guild_id," + ",".join(non_gid)
                        sel_str = str(guild_id_to) + "," + ",".join(non_gid)
                        await db.execute(
                            f"INSERT OR IGNORE INTO {tbl}({col_str}) "
                            f"SELECT {sel_str} FROM {tbl} WHERE guild_id=?", (guild_id_from,))
                    except Exception as e:
                        print(f"[Transfer] {tbl}: {e}")

                # Handle games + game_answers + game_hints with ID remapping
                await db.execute("DELETE FROM games WHERE guild_id=?", (guild_id_to,))
                await db.execute("DELETE FROM game_answers WHERE guild_id=?", (guild_id_to,))
                await db.execute("DELETE FROM game_hints WHERE guild_id=?", (guild_id_to,))
                async with db.execute("SELECT game_name,enabled,reward_balance,reward_exp,reward_tickets,"
                                       "reward_gamble_tokens,reward_vip_keys,reward_item,reward_item_qty,"
                                       "reward_role_id,chance,answer_time FROM games WHERE guild_id=?",
                                       (guild_id_from,)) as cur:
                    games_rows = await cur.fetchall()
                for row in games_rows:
                    await db.execute("INSERT OR IGNORE INTO games(guild_id,game_name,enabled,reward_balance,"
                                     "reward_exp,reward_tickets,reward_gamble_tokens,reward_vip_keys,"
                                     "reward_item,reward_item_qty,reward_role_id,chance,answer_time) "
                                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (guild_id_to, *row))
                    gname = row[0]
                    async with db.execute("SELECT id,answer FROM game_answers WHERE guild_id=? AND game_name=?",
                                           (guild_id_from, gname)) as cur:
                        ans_rows = await cur.fetchall()
                    for old_aid, answer in ans_rows:
                        cur2 = await db.execute("INSERT INTO game_answers(guild_id,game_name,answer) VALUES(?,?,?)",
                                                (guild_id_to, gname, answer))
                        new_aid = cur2.lastrowid
                        async with db.execute("SELECT hint_text,hint_order FROM game_hints "
                                               "WHERE guild_id=? AND game_name=? AND answer_id=?",
                                               (guild_id_from, gname, old_aid)) as cur:
                            hint_rows = await cur.fetchall()
                        for ht, ho in hint_rows:
                            await db.execute("INSERT INTO game_hints(guild_id,game_name,answer_id,hint_text,hint_order) "
                                             "VALUES(?,?,?,?,?)", (guild_id_to, gname, new_aid, ht, ho))

                # Abuse boxes
                await db.execute("DELETE FROM abuse_boxes WHERE guild_id=?", (guild_id_to,))
                await db.execute("DELETE FROM abuse_box_prizes WHERE guild_id=?", (guild_id_to,))
                await db.execute("DELETE FROM rare_box_config WHERE guild_id=?", (guild_id_to,))
                async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=?", (guild_id_from,)) as cur:
                    box_rows = await cur.fetchall()
                for (box_name,) in box_rows:
                    await db.execute("INSERT OR IGNORE INTO abuse_boxes VALUES(?,?)", (guild_id_to, box_name))
                    async with db.execute("SELECT id,prize_type,prize_value,prize_amount,chance FROM abuse_box_prizes "
                                           "WHERE guild_id=? AND box_name=?", (guild_id_from, box_name)) as cur:
                        prize_rows = await cur.fetchall()
                    for old_pid, pt, pv, pa, pc in prize_rows:
                        cur2 = await db.execute("INSERT INTO abuse_box_prizes(guild_id,box_name,prize_type,prize_value,prize_amount,chance) "
                                                "VALUES(?,?,?,?,?,?)", (guild_id_to, box_name, pt, pv, pa, pc))
                        new_pid = cur2.lastrowid
                        async with db.execute("SELECT 1 FROM rare_box_config WHERE guild_id=? AND box_name=? AND prize_id=?",
                                               (guild_id_from, box_name, old_pid)) as cur:
                            if await cur.fetchone():
                                await db.execute("INSERT OR IGNORE INTO rare_box_config VALUES(?,?,?)",
                                                 (guild_id_to, box_name, new_pid))

                await db.commit()

    await ctx.send(f"✅ Transferred data from **{guild_from.name}** → **{guild_to.name}**.")

# ═══════════════════════════════════════════════════════
# HELP COMMAND
# ═══════════════════════════════════════════════════════

_HELP_CATS = {
    "economy": ("💰","Economy",[
        ("balance","Check your or another user's balance","/balance [@user]"),
        ("gift","Give your own coins to another user","!gift @user <amount>"),
        ("addbalance / removebalance","Admin: add/remove coins","!addbalance @user <amount>"),
        ("activityrank","Check Activity Rank and EXP","/activityrank [@user]"),
        ("addexp / removeexp","Admin: add/remove usable EXP","!addexp @user <amount>"),
        ("addtotalexp / removetotalexp","Admin: add/remove Total EXP (affects leaderboard rank)","!addtotalexp @user <amount>"),
        ("expboost","Admin: set EXP boost for a role","/expboost <role> <boost%> [channel]"),
        ("removeexpboost","Admin: remove an EXP boost","/removeexpboost <role> [channel]"),
        ("listexpboosts","List all active EXP boosts","/listexpboosts"),
        ("leaderboard","View leaderboards (balance, EXP, tickets, hosted balance…)","/leaderboard <category>"),
        ("addbalancerank","Admin: add a role granted at a balance threshold","/addbalancerank <threshold> @role"),
        ("removebalancerank / listbalanceranks / refreshbalanceranks / checkbalancerank","Manage balance ranks","!listbalanceranks"),
        ("setstatchannel","Admin: post the stats panel","/setstatchannel #channel"),
        ("trade","Open a trade offer with another user","/trade @user"),
        ("item store/buy/use/inv/info/give/take/add/remove","Item store and inventory","!item store"),
    ]),
    "giveaways": ("🎉","Giveaways",[
        ("giveaway","Admin: create a timed giveaway","/giveaway <prize> <seconds> <winners> [rewards…]"),
        ("host","Any user: host a giveaway from your own balance","/host <amount> [winners] [seconds] [prize]"),
        ("reroll","Admin: reroll a giveaway winner","!reroll <message_id>"),
        ("mywinnings","View your giveaway win history","/mywinnings [@user]"),
        ("addgiveawayrole / removegiveawayrole / giveawayroles","Manage who can run giveaways","!addgiveawayrole @role"),
        ("addautogiveaway","Add to the auto giveaway pool","/addautogiveaway <prize> [winners] [chance] [rewards…]"),
        ("removeautogiveaway / listautogiveaways","Manage the auto pool","!listautogiveaways"),
        ("startgiveaways","Start automatic giveaways","/startgiveaways <interval> <duration> [#channel]"),
        ("stopgiveaways","Stop automatic giveaways","!stopgiveaways"),
        ("addautoentryrole","Admin: allow a role to use auto-entry","/addautoentryrole @role [message_req]"),
        ("autoentry","Toggle automatic entry into all giveaways","/autoentry"),
        ("setautoentrythreshold","Admin: set min prize for auto-entry","/setautoentrythreshold <min_bal> <recent_msgs>"),
        ("setnotifychannel","Admin: set notification channel for giveaway/game starts","/setnotifychannel #channel"),
    ]),
    "drops": ("📦","Drops & Raffle",[
        ("chest","Open EXP chest(s)","/chest [amount]  or  !chest [amount]"),
        ("vipchest","Open VIP Chest(s) — needs VIP Key","/vipchest [amount]"),
        ("setchestchannel","Admin: post the chest panel","/setchestchannel #channel"),
        ("addchestprize / removechestprize / listchestprizes","Admin: configure chest prizes","!listchestprizes [chest|vipchest]"),
        ("setraredropchannel","Admin: set rare drop announcement channel","!setraredropchannel #channel"),
        ("buytickets","Buy mega raffle tickets (capped; chat 3+ words to earn unlimited)","/buytickets <amount>"),
        ("megachance","Check your mega raffle win chance","/megachance [@user]"),
        ("setmegachannel / setmegainfochannel","Admin: configure mega raffle channels","!setmegachannel #channel"),
        ("setmegapayout","Admin: configure payout formula (total vs bought)","/setmegapayout <mode> <multiplier> [winners]"),
        ("checkmegahistory","View recent mega raffle draw history","!checkmegahistory"),
        ("openbox","Open an abuse box from your inventory","/openbox <box> [amount]"),
        ("addbox / removebox / addboxprize / removeboxprize / listboxes / givebox","Admin: manage abuse boxes","!listboxes"),
        ("addrarechestdrop / removerarechestdrop","Admin: mark a prize as rare","!addrarechestdrop chest <name>"),
    ]),
    "powergiveaway": ("🔥","Power Giveaways",[
        ("powergiveaway setup","Create/update a named recurring giveaway (run multiple at once)","/powergiveaway setup <name> <prize> <winners> <interval> <embed_ch> <winners_ch>"),
        ("powergiveaway setrole/removerole","Set fixed entries for a role per named giveaway","/powergiveaway setrole <name> @role <entries>"),
        ("powergiveaway setchannel/removechannel","Set chat entries per message per named giveaway","/powergiveaway setchannel <name> #channel <entries>"),
        ("powergiveaway setboost/removeboost","Set a chat-entry multiplier for a role","/powergiveaway setboost <name> @role <multiplier>"),
        ("powergiveaway start/stop","Start or stop a named giveaway","/powergiveaway start <name>"),
        ("powergiveaway status/list/delete","View or manage named giveaways","/powergiveaway list"),
    ]),
    "games": ("🎮","Random Games",[
        ("addgame","Add a game/question to the pool","/addgame <name> [rewards…] [chance] [answer_time]"),
        ("removegame / enablegame / disablegame","Manage games","!removegame <name>"),
        ("addgameanswer / removegameanswer","Add/remove answers for a game","!addgameanswer <game> <answer>"),
        ("addgamepreset","Bulk-load a preset (colors, food, countries…)","/addgamepreset <game> <preset>"),
        ("listgames","List all games or answers for a specific game","!listgames [name]"),
        ("addhint / removehint / listhints","Manage answer hints","!addhint <game> <answer_id> <order 1-5> <hint>"),
        ("setgamechannel","Set the channel and interval for games","/setgamechannel #channel [interval] [hint1] [hint2] [hint3]"),
        ("startgames / stopgames","Start/stop the random games loop","!startgames"),
    ]),
    "gambling": ("🎲","Gambling",[
        ("blackjack","Play blackjack (costs 1 Gamble Token)","/blackjack <bet>"),
        ("roulette","Bet on a number (0-36) or color (red/black)","/roulette <bet> <choice>"),
        ("givegambletoken / takegambletoken","Admin: give/take gamble tokens","!givegambletoken @user [amount]"),
        ("givegambletokenrole","Admin: give a token to all members with a role","!givegambletokenrole @role [amount]"),
    ]),
    "codes": ("🎟","Redeem Codes",[
        ("createcode","Admin: create a redeem code","/createcode <code> <prize> [uses] [min_level] [min_balance] [required_role]"),
        ("deletecode / listcodes","Admin: delete or list codes","!listcodes"),
        ("redeem","Redeem a code for a reward","/redeem <code>"),
    ]),
    "counting": ("🔢","Counting",[
        ("setcounting","Admin: enable counting in a channel","/setcounting #channel [#announce_channel]"),
        ("disablecounting / resetcount / countingstats / unbancounter","Manage the counting system","!countingstats"),
        ("addcountingprize / removecountingprize / listcountingprizes","Configure prizes","!addcountingprize balance 0 1000 count/10"),
        ("addspecialprize / removespecialprize","Add a prize at a specific count milestone","!addspecialprize 100 balance 0 50000 Centennial"),
    ]),
    "admin": ("⚙️","Admin & Systems",[
        ("setadminpanel","Post the admin panel (balance/EXP/reset buttons)","/setadminpanel #channel"),
        ("disablecmd / enablecmd / listcmds","Disable or re-enable commands in this server","!disablecmd <name>"),
        ("enablesystem / disablesystem / systemstatus","Toggle systems (mega, vipkey, gamble)","/enablesystem <system>"),
        ("setlogchannel / removelogchannel / listlogchannels","Configure log channels","/setlogchannel <type> #channel"),
        ("disableprefixchannel / enableprefixchannel / resetprefixchannel / listprefixchannels","Control prefix usage per channel","!disableprefixchannel #channel [@role]"),
        ("enableautoreset / disableautoreset","Toggle auto-reset when a member leaves","!enableautoreset"),
        ("setautoresetrule / removeautoresetrule / listautoresetrules","Configure what resets and when","!setautoresetrule balance 0"),
        ("resetuser / resetrole","Manually reset a user's or role's data","!resetuser @user [type]"),
        ("cleanuptransfer","Owner: wipe all data for this server","!cleanuptransfer"),
        ("transfer","Owner: migrate all server data to another server","!transfer <from_id> <to_id>"),
        ("setverification","Post a verification button panel","/setverification #channel [message] [@verified_role] [@unverified_role]"),
        ("setwelcome / disablewelcome","Set/disable DM welcome message","!setwelcome <message>"),
        ("setwelcomechannel / disablewelcomechannel","Set/disable channel welcome message","!setwelcomechannel #channel <message>"),
    ]),
}

@bot.tree.command(name="help", description="Browse all available commands")
@app_commands.describe(category="Filter by category")
@app_commands.choices(category=[
    app_commands.Choice(name=f"{v[0]} {v[1]}", value=k) for k, v in _HELP_CATS.items()
])
async def help_cmd(interaction: discord.Interaction, category: str = None):
    p = common._BOT_PREFIX
    if category and category in _HELP_CATS:
        emoji, title, cmds = _HELP_CATS[category]
        embed = discord.Embed(title=f"{emoji} {title} Commands", color=discord.Color.blurple())
        for name, desc, usage in cmds:
            embed.add_field(name=f"`{name}`", value=f"{desc}\n`{usage}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        embed = discord.Embed(title="📋 Help — All Categories",
            description=f"Use `/help <category>` or `{p}help <category>` to view commands.\nPrefix: `{p}`",
            color=discord.Color.blurple())
        for key, (emoji, title, cmds) in _HELP_CATS.items():
            embed.add_field(name=f"{emoji} **{title}**", value=f"`{p}help {key}` — {len(cmds)} command(s)", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.command(name="help")
async def pfx_help(ctx, category: str = None):
    p = common._BOT_PREFIX
    if category and category.lower() in _HELP_CATS:
        emoji, title, cmds = _HELP_CATS[category.lower()]
        embed = discord.Embed(title=f"{emoji} {title} Commands", color=discord.Color.blurple())
        for name, desc, usage in cmds:
            embed.add_field(name=f"`{name}`", value=f"{desc}\n`{usage}`", inline=False)
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(title="📋 Help — All Categories",
            description=f"Use `{p}help <category>` to view specific commands.\nPrefix: `{p}`",
            color=discord.Color.blurple())
        for key, (emoji, title, cmds) in _HELP_CATS.items():
            embed.add_field(name=f"{emoji} **{title}**", value=f"`{p}help {key}` — {len(cmds)} command(s)", inline=True)
        await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════
# OWNER COMMANDS
# ═══════════════════════════════════════════════════════

@bot.command(name="setprefix")
async def cmd_setprefix(ctx, new_prefix: str):
    if ctx.author.id != BOT_OWNER_ID: await ctx.send("❌ Owner only."); return
    await set_prefix(new_prefix)
    await ctx.send(f"✅ Prefix updated to `{new_prefix}`.")

@bot.command(name="gstatus")
async def cmd_gstatus(ctx):
    if ctx.author.id != BOT_OWNER_ID: await ctx.send("❌ Owner only."); return
    lines = [f"• **{g.name}** (`{g.id}`) — {g.member_count} members" for g in bot.guilds]
    embed = discord.Embed(title=f"🌐 Guilds ({len(bot.guilds)})",
        description="\n".join(lines) or "None", color=discord.Color.blurple())
    await ctx.send(embed=embed)

@bot.command(name="gdisablecmd")
async def cmd_gdisablecmd(ctx, *, cmd_name: str):
    if ctx.author.id != BOT_OWNER_ID: await ctx.send("❌ Owner only."); return
    cmd_name = cmd_name.strip().lower()
    global_disabled_commands.add(cmd_name)
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute("INSERT INTO global_disabled_commands VALUES(?)", (cmd_name,))
                await db.commit()
            except aiosqlite.IntegrityError:
                pass
    await ctx.send(f"🔒 **{cmd_name}** disabled globally.")

@bot.command(name="genablecmd")
async def cmd_genablecmd(ctx, *, cmd_name: str):
    if ctx.author.id != BOT_OWNER_ID: await ctx.send("❌ Owner only."); return
    cmd_name = cmd_name.strip().lower()
    global_disabled_commands.discard(cmd_name)
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM global_disabled_commands WHERE command_name=?", (cmd_name,))
            await db.commit()
    await ctx.send(f"✅ **{cmd_name}** re-enabled globally.")

@bot.command(name="gdisablesystem")
async def cmd_gdisablesystem(ctx, system: str):
    if ctx.author.id != BOT_OWNER_ID: await ctx.send("❌ Owner only."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO global_system_flags VALUES(?,0)", (system,))
            await db.commit()
    await ctx.send(f"🔒 **{system}** disabled globally.")

@bot.command(name="genablesystem")
async def cmd_genablesystem(ctx, system: str):
    if ctx.author.id != BOT_OWNER_ID: await ctx.send("❌ Owner only."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO global_system_flags VALUES(?,1)", (system,))
            await db.commit()
    await ctx.send(f"✅ **{system}** enabled globally.")

@bot.command(name="gcreate")
async def cmd_gcreate(ctx, code: str, prize_json: str, uses: int = -1, min_level: int = 0):
    if ctx.author.id != BOT_OWNER_ID: await ctx.send("❌ Owner only."); return
    code = code.strip().upper()
    prize = _parse_prize_json(prize_json)
    if not prize: await ctx.send("❌ Invalid prize format."); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute("INSERT INTO global_redeem_codes VALUES(?,?,?,?,0)",
                                 (code, json.dumps(prize), uses, min_level))
                await db.commit()
            except aiosqlite.IntegrityError:
                await ctx.send(f"❌ Global code **{code}** already exists."); return
    uses_str = "unlimited" if uses == -1 else str(uses)
    await ctx.send(f"✅ Global code **{code}** created ({uses_str} uses). Prize: {_prize_summary(prize)}")

@bot.command(name="gdelete")
async def cmd_gdelete(ctx, code: str):
    if ctx.author.id != BOT_OWNER_ID: await ctx.send("❌ Owner only."); return
    code = code.strip().upper()
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM global_redeem_codes WHERE code=?", (code,))
            await db.execute("DELETE FROM global_code_uses WHERE code=?", (code,))
            await db.commit()
    await ctx.send(f"🗑 Global code **{code}** deleted.")

@bot.command(name="gcodes")
async def cmd_gcodes(ctx):
    if ctx.author.id != BOT_OWNER_ID: await ctx.send("❌ Owner only."); return
    async with get_db() as db:
        async with db.execute("SELECT code,prize_json,uses_left,min_level FROM global_redeem_codes ORDER BY code") as cur:
            codes = await cur.fetchall()
    if not codes: await ctx.send("❌ No global codes."); return
    lines = []
    for code, pj, uses, ml in codes:
        try: prize = json.loads(pj); ps = _prize_summary(prize)
        except Exception: ps = str(pj)
        uses_str = "∞" if uses == -1 else str(uses)
        req = f" [Lvl{ml}+]" if ml else ""
        lines.append(f"**{code}** ({uses_str}){req} — {ps[:60]}")
    embed = discord.Embed(title="🎟 Global Codes", description="\n".join(lines), color=discord.Color.green())
    await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════
# CORE EVENTS
# ═══════════════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot: return
    if not message.guild: return

    await _process_counting(message)

    if isinstance(message.author, discord.Member) and not message.author.bot:
        gid, uid = message.guild.id, message.author.id
        try:
            exp_gain = await _calc_exp_gain(message.author, message.channel)
            await add_exp(gid, uid, exp_gain)
        except Exception as e:
            print(f"[EXP] {e}")
        bump_msg_count(gid, uid)

    if message.content.startswith(common._BOT_PREFIX) and not _prefix_channel_allowed(message):
        return
    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if not payload.guild_id: return
    guild = bot.get_guild(payload.guild_id)
    if not guild: return

    # Counting bot reaction check
    if payload.user_id == COUNTING_BOT_ID and str(payload.emoji) in _COUNTING_FAIL_EMOJI:
        async with get_db() as db:
            async with db.execute("SELECT channel_id FROM counting_config WHERE guild_id=? AND enabled=1",
                                  (payload.guild_id,)) as cur:
                cfg = await cur.fetchone()
        if cfg and cfg[0] == payload.channel_id:
            async with db_lock:
                async with get_db() as db:
                    await db.execute("UPDATE counting_state SET current_count=0,last_user_id=0 WHERE guild_id=?",
                                     (payload.guild_id,))
                    await db.commit()
            ch = guild.get_channel(payload.channel_id)
            if ch:
                try: await ch.send("🔄 Counting bot marked that as wrong — count reset to **0**.")
                except Exception: pass


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot: return
    gid = member.guild.id

    # Cancel any pending auto-reset for this user
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM auto_reset_pending WHERE guild_id=? AND user_id=?",
                             (gid, member.id))
            await db.commit()

    # Assign unverified role
    async with get_db() as db:
        async with db.execute("SELECT unverified_role_id FROM verification_config WHERE guild_id=?", (gid,)) as cur:
            vcfg = await cur.fetchone()
    if vcfg and vcfg[0]:
        role = member.guild.get_role(vcfg[0])
        if role:
            try: await member.add_roles(role)
            except Exception: pass

    # Welcome DM
    async with get_db() as db:
        async with db.execute("SELECT enabled,message,channel_id,channel_enabled,channel_message FROM welcome_config WHERE guild_id=?",
                              (gid,)) as cur:
            wcfg = await cur.fetchone()
    if wcfg:
        enabled, dm_msg, ch_id, ch_enabled, ch_msg = wcfg
        if enabled and dm_msg:
            text = dm_msg.replace("{user}", member.mention).replace("{server}", member.guild.name)
            try: await member.send(text)
            except Exception: pass
        if ch_enabled and ch_id and ch_msg:
            ch = member.guild.get_channel(ch_id)
            if ch:
                text = ch_msg.replace("{user}", member.mention).replace("{server}", member.guild.name)
                try: await ch.send(text)
                except Exception: pass


@bot.event
async def on_member_remove(member: discord.Member):
    if member.bot: return
    gid = member.guild.id
    async with get_db() as db:
        async with db.execute("SELECT enabled FROM auto_reset_config WHERE guild_id=?", (gid,)) as cur:
            cfg = await cur.fetchone()
    if not cfg or not cfg[0]: return
    async with get_db() as db:
        async with db.execute("SELECT reset_type,delay_seconds FROM auto_reset_rules WHERE guild_id=?", (gid,)) as cur:
            rules = await cur.fetchall()
    if not rules: return
    now = int(datetime.now(UTC).timestamp())
    async with db_lock:
        async with get_db() as db:
            for reset_type, delay_seconds in rules:
                reset_after = now + delay_seconds
                await db.execute(
                    "INSERT OR REPLACE INTO auto_reset_pending(guild_id,user_id,reset_type,reset_after) VALUES(?,?,?,?)",
                    (gid, member.id, reset_type, reset_after))
            await db.commit()


@bot.event
async def on_ready():
    await setup_database()
    await common._load_prefix()
    await load_disabled_commands()
    await load_prefix_restrictions()
    bot.add_view(VerificationView())
    bot.add_view(AdminPanelView())

    ok, fail = 0, 0
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            ok += 1
        except discord.HTTPException as e:
            print(f"[Admin Sync] ❌ {guild.name}: {e}")
            fail += 1
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync(guild=None)
    print(f"[Admin Bot] Synced {ok} ok / {fail} failed. Logged in as {bot.user}")

    for task_fn in [auto_reset_loop, daily_gamble_loop,
                    lambda: msg_count_flush_loop(bot)]:
        bot.loop.create_task(task_fn())


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except discord.HTTPException as e:
        print(f"[Admin Sync] Failed on join: {e}")


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
