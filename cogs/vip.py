import json
import re
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from config import (
    ALL_VIP_ROLES,
    BAN_LOG_CHANNEL_ID,
    DISCORD_GUILD_ID,
    GIST_BLACKLIST_ID,
    GIST_LOG_ID,
    GIST_VIP_ID,
    GITHUB_TOKEN,
    PANEL_CHANNEL_ID,
    ROLE_18_PLUS,
    ROLES_STAFF,
    ROLES_VIP,
    ROLES_VIP_PLUS,
    MOD_LOG_CHANNEL_NSFW,
    MOD_LOG_CHANNEL_SFW,
    NSFW_SERVER_ID,
)

GUILD_ID = DISCORD_GUILD_ID
VIP_CATEGORIES = ["staffplus", "staff", "vipplus", "vip"]
VIPLIST_FILENAME = "viplist.json"
VIPLIST_LEGACY_FILENAME = "viplist"


# --- GITHUB HELPER ---
async def get_gist(gist_id, filename):
    if not GITHUB_TOKEN:
        return None
    url = f"https://api.github.com/gists/{gist_id}?t={int(time.time())}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Cache-Control": "no-cache"}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(url) as r:
            if r.status == 200:
                data = await r.json()
                if filename in data["files"]:
                    return data["files"][filename]["content"]
    return None


async def update_gist(gist_id, filename, content):
    if not GITHUB_TOKEN:
        return
    url = f"https://api.github.com/gists/{gist_id}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    payload = {"files": {filename: {"content": content}}}
    async with aiohttp.ClientSession(headers=headers) as s:
        await s.patch(url, json=payload)


def _default_vip_data() -> dict:
    return {category: [] for category in VIP_CATEGORIES}


def parse_vip_json(raw: str | None) -> tuple[dict, bool]:
    if not raw:
        return _default_vip_data(), True
    candidate = raw.strip().lstrip("\ufeff")
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            candidate = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # tolerate common formatting issues from manual gist edits
        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return _default_vip_data(), False
    if not isinstance(parsed, dict):
        return _default_vip_data(), False

    data = _default_vip_data()
    for category in VIP_CATEGORIES:
        values = parsed.get(category, [])
        if isinstance(values, list):
            data[category] = [str(v).strip() for v in values if str(v).strip()]
    return data, True


# --- REFRESH BAN PANEL ---
async def refresh_ban_panel(channel):
    if not channel:
        return
    try:
        async for m in channel.history(limit=50):
            if m.author.bot and m.embeds and m.embeds[0].title == "🚨 Ban Management":
                try:
                    await m.delete()
                except Exception:
                    pass
    except Exception:
        pass

    embed = discord.Embed(
        title="🚨 Ban Management",
        description="👉 Type `/ban` in the chat to submit a new ban request.",
        color=discord.Color.dark_grey(),
    )
    await channel.send(embed=embed, view=BanPanelButtonView())


# --- MAIN VERIFICATION LOGIC ---
async def process_verification(interaction: discord.Interaction, vrc_name: str):
    if not any(r.id == ROLE_18_PLUS for r in interaction.user.roles):
        return await interaction.followup.send(
            embed=discord.Embed(
                description="🔞 **Access Denied:** You must be 18+ verified.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )

    bl_raw = await get_gist(GIST_BLACKLIST_ID, "blacklistet") or ""
    bl_names = [line.strip().lower() for line in bl_raw.split("\n") if line.strip()]
    tr_raw = await get_gist(GIST_LOG_ID, "ban_tracker") or ""

    if vrc_name.lower() in bl_names or str(interaction.user.id) in tr_raw:
        return await interaction.followup.send(
            embed=discord.Embed(
                description="🚫 **Access Denied:** You are blacklisted.",
                color=discord.Color.dark_red(),
            ),
            ephemeral=True,
        )

    allowed = []
    if any(r.id in ROLES_STAFF for r in interaction.user.roles):
        allowed.append("staff")
    if any(r.id in ROLES_VIP_PLUS for r in interaction.user.roles):
        allowed.append("vipplus")
    if any(r.id in ROLES_VIP for r in interaction.user.roles):
        allowed.append("vip")

    if not allowed:
        return await interaction.followup.send(
            embed=discord.Embed(
                description="❌ **Access Denied:** No valid VIP/Staff role.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )

    json_raw = await get_gist(GIST_VIP_ID, VIPLIST_FILENAME)
    data, valid_vip_json = parse_vip_json(json_raw)
    if not valid_vip_json:
        return await interaction.followup.send(
            embed=discord.Embed(
                description="❌ VIP-Datenbank ist beschädigt (ungültiges JSON).",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )

    found_cat, exact_vrc_name = None, vrc_name
    for cat in VIP_CATEGORIES:
        for db_name in data.get(cat, []):
            if db_name.lower() == vrc_name.lower():
                found_cat, exact_vrc_name = cat, db_name
                break
        if found_cat:
            break

    if not found_cat:
        return await interaction.followup.send(
            embed=discord.Embed(
                description=f"❌ `{vrc_name}` not found in VIP database.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )

    if found_cat not in allowed:
        return await interaction.followup.send(
            embed=discord.Embed(
                description="❌ **Access Denied:** Tier mismatch.", color=discord.Color.red()
            ),
            ephemeral=True,
        )

    log_raw = await get_gist(GIST_LOG_ID, "log") or ""
    if f"({interaction.user.id})" in log_raw:
        return await interaction.followup.send(
            embed=discord.Embed(description="❌ You are already verified!", color=discord.Color.red()),
            ephemeral=True,
        )
    if f"VRC: {exact_vrc_name}" in log_raw:
        return await interaction.followup.send(
            embed=discord.Embed(description="❌ Name already claimed!", color=discord.Color.red()),
            ephemeral=True,
        )

    try:
        await interaction.user.edit(nick=exact_vrc_name)
    except Exception:
        pass

    await interaction.followup.send(
        embed=discord.Embed(
            description=f"✅ Verified as **{exact_vrc_name}**!\n*Updates in VRChat take ~5-10 mins.*",
            color=discord.Color.green(),
        ),
        ephemeral=True,
    )
    await update_gist(
        GIST_LOG_ID,
        "log",
        log_raw + f"\n{interaction.user.name} ({interaction.user.id}) - VRC: {exact_vrc_name}",
    )


# --- UI CLASSES ---
class VerifyModal(discord.ui.Modal, title="VRChat Verification"):
    vrc_name = discord.ui.TextInput(
        label="VRChat Username",
        style=discord.TextStyle.short,
        placeholder="Exact VRChat name...",
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await process_verification(interaction, self.vrc_name.value)


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify Now",
        style=discord.ButtonStyle.green,
        emoji="✅",
        custom_id="persistent_verify_button",
    )
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerifyModal())


class BanPanelButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Submit New Ban",
        style=discord.ButtonStyle.blurple,
        emoji="🚨",
        custom_id="persistent_new_ban_btn",
    )
    async def btn_new_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "👉 **Please type `/ban` in the chat to submit a ban request.**", ephemeral=True
        )


class ActiveBanView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Unban User", style=discord.ButtonStyle.green, emoji="🔓", custom_id="active_unban_btn"
    )
    async def btn_unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ **Access Denied.**", ephemeral=True)

        await interaction.response.defer()
        msg = interaction.message
        embed = msg.embeds[0]

        vrc_name = embed.fields[0].value.strip("`")
        discord_id = None
        for f in embed.fields:
            if "Discord" in f.name:
                try:
                    discord_id = int(f.value.split("\n")[1].strip("()"))
                except Exception:
                    pass

        cur_bl = await get_gist(GIST_BLACKLIST_ID, "blacklistet") or ""
        new_bl = "\n".join([l for l in cur_bl.split("\n") if l.strip() and l.lower() != vrc_name.lower()])
        await update_gist(GIST_BLACKLIST_ID, "blacklistet", new_bl.strip())

        tr_raw = await get_gist(GIST_LOG_ID, "ban_tracker") or ""
        new_tr = "\n".join(
            [l for l in tr_raw.split("\n") if l.strip() and not l.lower().startswith(f"{vrc_name.lower()}|")]
        )
        await update_gist(GIST_LOG_ID, "ban_tracker", new_tr.strip())

        if discord_id:
            try:
                user = await interaction.client.fetch_user(discord_id)
                await interaction.guild.unban(user, reason="Admin unbanned")
            except Exception:
                pass

        embed.title = "✅ UNBANNED"
        embed.color = discord.Color.green()
        for i, f in enumerate(embed.fields):
            if "Duration" in f.name:
                embed.set_field_at(i, name=f.name, value="Unbanned manually", inline=f.inline)

        embed.set_footer(text=f"Unbanned by: {interaction.user.display_name}")
        self.clear_items()
        await msg.edit(embed=embed, view=self)
        await refresh_ban_panel(msg.channel)


class PendingBanView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def process_approval(self, interaction: discord.Interaction, action: str):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ **Access Denied.**", ephemeral=True)

        await interaction.response.defer()
        msg = interaction.message
        embed = msg.embeds[0]

        if "PENDING" not in embed.title:
            return

        if action == "deny":
            embed.title = "❌ BAN REQUEST DENIED"
            embed.color = discord.Color.dark_grey()
            embed.set_footer(text=f"Denied by: {interaction.user.display_name}")
            self.clear_items()
            await msg.edit(embed=embed, view=self)
            return await refresh_ban_panel(msg.channel)

        vrc_name = embed.fields[0].value.strip("`")
        discord_id = None
        req_dur, rule_reason = "", ""

        for f in embed.fields:
            if "Discord" in f.name:
                try:
                    discord_id = int(f.value.split("\n")[1].strip("()"))
                except Exception:
                    pass
            if "Duration" in f.name:
                req_dur = f.value
            if "Reason" in f.name:
                rule_reason = f.value.split("\n")[0].strip("**")

        dur_map = {
            "Permanent": 0,
            "1 Hour": 3600,
            "1 Day": 86400,
            "1 Week": 604800,
            "1 Month": 2592000,
        }
        dur_secs = dur_map.get(req_dur, 0)
        if action in ["underage", "perma"]:
            dur_secs = 0

        cur_bl = await get_gist(GIST_BLACKLIST_ID, "blacklistet") or ""
        bl_names = [l.strip().lower() for l in cur_bl.split("\n") if l.strip()]
        if vrc_name.lower() not in bl_names:
            await update_gist(GIST_BLACKLIST_ID, "blacklistet", f"{cur_bl}\n{vrc_name}".strip())

        tr_raw = await get_gist(GIST_LOG_ID, "ban_tracker") or ""
        expiry = int(time.time()) + dur_secs if dur_secs > 0 else 0
        dur_text = "Permanent" if expiry == 0 else f"<t:{expiry}:F> (<t:{expiry}:R>)"
        did_str = str(discord_id) if discord_id else "None"

        tr_lines = [l for l in tr_raw.split("\n") if l.strip() and not l.lower().startswith(f"{vrc_name.lower()}|")]
        tr_lines.append(f"{vrc_name}|{did_str}|{expiry}")
        await update_gist(GIST_LOG_ID, "ban_tracker", "\n".join(tr_lines).strip())

        if discord_id:
            try:
                user = await interaction.client.fetch_user(discord_id)
                await interaction.guild.ban(user, reason=f"VRC: {vrc_name} | {rule_reason}")
            except Exception:
                pass

        if action == "approve":
            embed.title = "🚫 APPROVED & BANNED"
            embed.color = discord.Color.dark_red()
        elif action == "underage":
            embed.title = "🔞 APPROVED: UNDER AGE"
            embed.color = discord.Color.orange()
            for i, f in enumerate(embed.fields):
                if "Reason" in f.name:
                    embed.set_field_at(
                        i,
                        name=f.name,
                        value=f.value + "\n\n**[STATUS: UNDERAGE BAN]**",
                        inline=f.inline,
                    )
        elif action == "perma":
            embed.title = "⛔ APPROVED: PERMANENT BAN"
            embed.color = discord.Color.from_rgb(0, 0, 0)
            for i, f in enumerate(embed.fields):
                if "Reason" in f.name:
                    embed.set_field_at(
                        i,
                        name=f.name,
                        value=f.value + "\n\n**[STATUS: PERMANENT BAN]**",
                        inline=f.inline,
                    )

        for i, f in enumerate(embed.fields):
            if "Duration" in f.name:
                embed.set_field_at(i, name=f.name, value=dur_text, inline=f.inline)

        embed.set_footer(text=f"Approved by: {interaction.user.display_name}")
        await msg.edit(embed=embed, view=ActiveBanView())
        await refresh_ban_panel(msg.channel)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="✅", custom_id="pend_app")
    async def btn_approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_approval(interaction, "approve")

    @discord.ui.button(label="Under Age", style=discord.ButtonStyle.blurple, emoji="🔞", custom_id="pend_ua")
    async def btn_underage(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_approval(interaction, "underage")

    @discord.ui.button(label="Perma Ban", style=discord.ButtonStyle.gray, emoji="⛔", custom_id="pend_perma")
    async def btn_perma(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_approval(interaction, "perma")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, emoji="❌", custom_id="pend_deny")
    async def btn_deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_approval(interaction, "deny")


class BanRequestModal(discord.ui.Modal, title="Submit Ban Request"):
    vrc_name = discord.ui.TextInput(
        label="VRChat Username", style=discord.TextStyle.short, required=True, max_length=100
    )
    extra_details = discord.ui.TextInput(
        label="Evidence / Details", style=discord.TextStyle.long, required=False, max_length=500
    )

    def __init__(self, rule_reason, duration_name, target_member=None):
        super().__init__()
        self.rule_reason = rule_reason
        self.duration_name = duration_name
        self.target_member = target_member

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        scope = "🌐 VRChat & Discord" if self.target_member else "🎮 VRChat Only"

        ban_room = interaction.guild.get_channel(BAN_LOG_CHANNEL_ID)
        if ban_room:
            embed = discord.Embed(
                title="🟡 PENDING BAN APPROVAL",
                color=discord.Color.yellow(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="👤 Name (VRChat)", value=f"`{self.vrc_name.value}`", inline=True)
            if self.target_member:
                embed.add_field(
                    name="🆔 Discord",
                    value=f"{self.target_member.mention}\n({self.target_member.id})",
                    inline=True,
                )
            embed.add_field(
                name="⚖️ Reason",
                value=f"**{self.rule_reason}**\n{self.extra_details.value or 'No details.'}",
                inline=False,
            )
            embed.add_field(name="⏱️ Requested Duration", value=self.duration_name, inline=True)
            embed.add_field(name="📍 Scope", value=scope, inline=True)
            embed.add_field(name="📝 Requested By", value=interaction.user.mention, inline=False)

            await ban_room.send(embed=embed, view=PendingBanView())
            await refresh_ban_panel(ban_room)

        await interaction.followup.send(
            embed=discord.Embed(description="✅ Ban Request submitted!", color=discord.Color.green()),
            ephemeral=True,
        )


# --- VIP COG CLASS ---
class VIPCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def ensure_gist_files(self):
        print("🔍 Checking Gist database...")
        for gid, f, d in [
            (GIST_BLACKLIST_ID, "blacklistet", ""),
            (GIST_LOG_ID, "ban_tracker", ""),
            (GIST_LOG_ID, "log", "--- LOG: VERIFIED VIPs ---"),
        ]:
            if await get_gist(gid, f) is None:
                await update_gist(gid, f, d)

        # Handle viplist file separately to avoid creating a second/empty list.
        current_viplist = await get_gist(GIST_VIP_ID, VIPLIST_FILENAME)
        if current_viplist is None:
            legacy = await get_gist(GIST_VIP_ID, VIPLIST_LEGACY_FILENAME)
            if legacy:
                await update_gist(GIST_VIP_ID, VIPLIST_FILENAME, legacy)
            else:
                await update_gist(GIST_VIP_ID, VIPLIST_FILENAME, json.dumps(_default_vip_data(), indent=4))
        print("✅ Gist ready.")

    async def cog_load(self):
        self.bot.add_view(VerifyView())
        self.bot.add_view(PendingBanView())
        self.bot.add_view(BanPanelButtonView())
        self.bot.add_view(ActiveBanView())

        self.bot.loop.create_task(self.ensure_gist_files())

        ban_room = self.bot.get_channel(BAN_LOG_CHANNEL_ID)
        if ban_room:
            self.bot.loop.create_task(refresh_ban_panel(ban_room))

        if not self.auto_audit.is_running():
            self.auto_audit.start()

    async def cog_unload(self):
        self.auto_audit.cancel()

    async def _mirror_ban_action(self, source_guild: discord.Guild, user: discord.User | discord.Member, *, unban: bool) -> None:
        if source_guild.id == NSFW_SERVER_ID:
            target_guild_id = GUILD_ID
            target_log_channel = MOD_LOG_CHANNEL_SFW
        elif source_guild.id == GUILD_ID:
            target_guild_id = NSFW_SERVER_ID
            target_log_channel = MOD_LOG_CHANNEL_NSFW
        else:
            return

        target_guild = self.bot.get_guild(target_guild_id)
        if not target_guild:
            return

        action = "unbanned" if unban else "banned"
        try:
            if unban:
                await target_guild.unban(user, reason=f"Mirror-unban from {source_guild.id}")
            else:
                await target_guild.ban(user, reason=f"Mirror-ban from {source_guild.id}", delete_message_days=0)
        except Exception:
            return

        log_channel = self.bot.get_channel(target_log_channel)
        if log_channel:
            try:
                await log_channel.send(
                    f"🔁 Cross-Server Sync: `{user}` (`{user.id}`) wurde im Zielserver {action} "
                    f"(Quelle: `{source_guild.name}` / `{source_guild.id}`)."
                )
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User | discord.Member):
        await self._mirror_ban_action(guild, user, unban=False)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User | discord.Member):
        await self._mirror_ban_action(guild, user, unban=True)

    @tasks.loop(minutes=30)
    async def auto_audit(self):
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        log_raw = await get_gist(GIST_LOG_ID, "log") or ""
        json_raw = await get_gist(GIST_VIP_ID, VIPLIST_FILENAME)
        if not log_raw:
            return

        data, valid_vip_json = parse_vip_json(json_raw)
        if not valid_vip_json:
            return

        json_changed = False
        new_log = ["--- LOG: VERIFIED VIPs ---"]

        for line in log_raw.split("\n"):
            if " - VRC: " in line and "(" in line and ")" in line:
                try:
                    user_id = int(line.split("(")[1].split(")")[0])
                    vrc_name = line.split("VRC: ")[1].strip()
                    member = guild.get_member(user_id)

                    if member and any(r.id in ALL_VIP_ROLES for r in member.roles) and any(
                        r.id == ROLE_18_PLUS for r in member.roles
                    ):
                        new_log.append(line)
                    else:
                        for cat in data:
                            if vrc_name in data[cat]:
                                data[cat].remove(vrc_name)
                                json_changed = True
                        if member:
                            try:
                                await member.edit(nick=None)
                            except Exception:
                                pass
                except Exception:
                    pass

        if json_changed:
            await update_gist(GIST_VIP_ID, VIPLIST_FILENAME, json.dumps(data, indent=4))
        await update_gist(GIST_LOG_ID, "log", "\n".join(new_log))

    # --- COMMANDS ---
    @app_commands.command(name="verify", description="Link VRChat name to Discord.")
    async def verify_cmd(self, interaction: discord.Interaction, vrc_name: str):
        await interaction.response.defer(ephemeral=True)
        await process_verification(interaction, vrc_name)

    @app_commands.command(name="unverify", description="Unlink your Discord account.")
    async def unverify(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        log_raw = await get_gist(GIST_LOG_ID, "log") or ""
        new_log = []
        found = False
        for line in log_raw.split("\n"):
            if f"({interaction.user.id})" in line:
                found = True
            else:
                new_log.append(line)
        if not found:
            return await interaction.followup.send("❌ Not verified.", ephemeral=True)
        await update_gist(GIST_LOG_ID, "log", "\n".join(new_log))
        try:
            await interaction.user.edit(nick=None)
        except Exception:
            pass
        await interaction.followup.send("⚠️ Unlinked.", ephemeral=True)

    @app_commands.command(name="setup_panel", description="Spawns Verify Button.")
    @app_commands.default_permissions(administrator=True)
    async def setup_panel(self, interaction: discord.Interaction):
        channel = self.bot.get_channel(PANEL_CHANNEL_ID)
        if channel is None:
            return await interaction.response.send_message(
                "❌ Panel-Channel nicht gefunden. Prüfe `PANEL_CHANNEL_ID`.",
                ephemeral=True,
            )
        embed = discord.Embed(
            title="🎮 VIP Verification",
            description="Click the **Verify Now** button below.",
            color=discord.Color.gold(),
        )
        await channel.send(embed=embed, view=VerifyView())
        await interaction.response.send_message("✅ Panel deployed!", ephemeral=True)

    @app_commands.command(name="audit", description="Manual audit.")
    @app_commands.default_permissions(administrator=True)
    async def audit(self, interaction: discord.Interaction):
        await interaction.response.send_message("🔄 Auditing...", ephemeral=True)
        await self.auto_audit()
        await interaction.edit_original_response(content="✅ Audit complete.")

    @app_commands.command(name="adminverify", description="Force-link user.")
    @app_commands.default_permissions(administrator=True)
    async def adminverify(self, interaction: discord.Interaction, member: discord.Member, vrc_name: str):
        await interaction.response.defer(ephemeral=True)
        json_raw = await get_gist(GIST_VIP_ID, VIPLIST_FILENAME)
        data, valid_vip_json = parse_vip_json(json_raw)
        if not valid_vip_json:
            return await interaction.followup.send("❌ VIP-JSON ist ungültig. Bitte zuerst reparieren.", ephemeral=True)
        found = False
        for cat in VIP_CATEGORIES:
            for n in data.get(cat, []):
                if n.lower() == vrc_name.lower():
                    found = True
                    vrc_name = n
                    break
            if found:
                break

        log_raw = await get_gist(GIST_LOG_ID, "log") or ""
        if f"({member.id})" in log_raw or f"VRC: {vrc_name}" in log_raw:
            return await interaction.followup.send("❌ Already in log.", ephemeral=True)

        await update_gist(GIST_LOG_ID, "log", log_raw + f"\n{member.name} ({member.id}) - VRC: {vrc_name}")
        try:
            await member.edit(nick=vrc_name)
        except Exception:
            pass
        await interaction.followup.send(f"✅ Force-verified {member.mention}!", ephemeral=True)

    @app_commands.command(name="add_vip", description="Add name to JSON.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(
        category=[
            app_commands.Choice(name=c, value=c.lower().replace("+", "plus"))
            for c in ["Staff+", "Staff", "VIP+", "VIP"]
        ]
    )
    async def add_vip(self, interaction: discord.Interaction, category: app_commands.Choice[str], vrc_name: str):
        await interaction.response.defer(ephemeral=True)
        vrc_name = vrc_name.strip()
        if not vrc_name:
            return await interaction.followup.send("❌ Name darf nicht leer sein.", ephemeral=True)
        raw = await get_gist(GIST_VIP_ID, VIPLIST_FILENAME)
        data, valid_vip_json = parse_vip_json(raw)
        if not valid_vip_json:
            return await interaction.followup.send(
                "❌ VIP-JSON ist ungültig. Bitte erst das Gist-Format korrigieren.", ephemeral=True
            )
        cv = category.value
        if cv not in data:
            data[cv] = []
        if any(name.lower() == vrc_name.lower() for name in data[cv]):
            return await interaction.followup.send(f"ℹ️ `{vrc_name}` ist bereits in `{cv}`.", ephemeral=True)
        data[cv].append(vrc_name)
        await update_gist(GIST_VIP_ID, VIPLIST_FILENAME, json.dumps(data, indent=4))
        await interaction.followup.send(f"✅ Added `{vrc_name}`.", ephemeral=True)

    @app_commands.command(name="remove_vip", description="Remove name from JSON.")
    @app_commands.default_permissions(administrator=True)
    async def remove_vip(self, interaction: discord.Interaction, vrc_name: str):
        await interaction.response.defer(ephemeral=True)
        data, valid_vip_json = parse_vip_json(await get_gist(GIST_VIP_ID, VIPLIST_FILENAME))
        if not valid_vip_json:
            return await interaction.followup.send(
                "❌ VIP-JSON ist ungültig. Entfernen aktuell blockiert.", ephemeral=True
            )
        for cat in data:
            for n in data[cat]:
                if n.lower() == vrc_name.lower():
                    data[cat].remove(n)
                    await update_gist(GIST_VIP_ID, VIPLIST_FILENAME, json.dumps(data, indent=4))
                    return await interaction.followup.send(f"✅ Removed `{n}`.", ephemeral=True)
        await interaction.followup.send("❌ Not found.", ephemeral=True)

    @app_commands.command(name="list_vips", description="Show JSON.")
    @app_commands.default_permissions(administrator=True)
    async def list_vips(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data, valid_vip_json = parse_vip_json(await get_gist(GIST_VIP_ID, VIPLIST_FILENAME))
        if not valid_vip_json:
            return await interaction.followup.send(
                "❌ VIP-JSON ist ungültig und kann nicht angezeigt werden.", ephemeral=True
            )
        embed = discord.Embed(title="📋 VIPs", color=discord.Color.blue())
        for c in VIP_CATEGORIES:
            embed.add_field(name=c.upper(), value="\n".join(data.get(c, [])[:20]) or "None", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="ban", description="Submit ban request.")
    @app_commands.choices(
        rule=[
            app_commands.Choice(name=n, value=n)
            for n in [
                "§1 Respect & Consent",
                "§2 Underage",
                "§3 No NSFW/ERP",
                "§4 Avatar Limits",
                "§5 Stage Etiquette",
                "§6 Disruptive",
                "§7 Malicious",
                "§8 Staff Evasion",
                "Other",
            ]
        ]
    )
    @app_commands.choices(
        duration=[
            app_commands.Choice(name=n, value=n)
            for n in ["Permanent", "1 Hour", "1 Day", "1 Week", "1 Month"]
        ]
    )
    async def ban_cmd(
        self,
        interaction: discord.Interaction,
        rule: app_commands.Choice[str],
        duration: app_commands.Choice[str],
        member: discord.Member = None,
    ):
        if not any(r.id in ALL_VIP_ROLES for r in interaction.user.roles) and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Denied.", ephemeral=True)
        try:
            await interaction.response.send_modal(BanRequestModal(rule.value, duration.value, member))
        except Exception:
            pass

    @app_commands.command(name="blacklist_add", description="Silent VRC ban.")
    @app_commands.default_permissions(administrator=True)
    async def blacklist_add(self, interaction: discord.Interaction, entry: str):
        await interaction.response.defer(ephemeral=True)
        bl = await get_gist(GIST_BLACKLIST_ID, "blacklistet") or ""
        await update_gist(GIST_BLACKLIST_ID, "blacklistet", (bl + f"\n{entry}").strip())
        await interaction.followup.send(f"🤫 `{entry}` blacklisted.", ephemeral=True)

    @app_commands.command(name="blacklist_remove", description="Silent VRC unban.")
    @app_commands.default_permissions(administrator=True)
    async def blacklist_remove(self, interaction: discord.Interaction, entry: str):
        await interaction.response.defer(ephemeral=True)
        bl = await get_gist(GIST_BLACKLIST_ID, "blacklistet") or ""
        await update_gist(
            GIST_BLACKLIST_ID,
            "blacklistet",
            "\n".join([l for l in bl.split("\n") if l.lower() != entry.lower()]),
        )
        await interaction.followup.send(f"✅ `{entry}` unbanned.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(VIPCog(bot))
