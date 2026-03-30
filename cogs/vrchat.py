import asyncio
import base64
import json
import time
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import VRCHAT_API_BASE_URL, VRCHAT_GROUP_AFTERDARK_ID, VRCHAT_GROUP_MAIN_ID


class VRChatCog(commands.Cog):
    """VRChat utility + moderation cog.

    Hinweis: Nutzt direkte HTTP-Calls gegen die VRChat API.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.api_base = VRCHAT_API_BASE_URL.rstrip("/")
        self.session: aiohttp.ClientSession | None = None
        self.auth_header: str | None = None
        self.logged_in_user: str | None = None
        self.login_pending_2fa = False
        self.temp_bans_file = Path("data/vrchat_temp_bans.json")
        self.temp_bans: dict[str, dict] = {}
        self.mod_notes_file = Path("data/vrchat_mod_notes.json")
        self.mod_notes: dict[str, list[str]] = {}

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
        self._load_temp_bans()
        self._load_mod_notes()
        if not self.temp_unban_worker.is_running():
            self.temp_unban_worker.start()

    async def cog_unload(self) -> None:
        if self.temp_unban_worker.is_running():
            self.temp_unban_worker.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    def _load_temp_bans(self) -> None:
        self.temp_bans_file.parent.mkdir(parents=True, exist_ok=True)
        if self.temp_bans_file.exists():
            try:
                self.temp_bans = json.loads(self.temp_bans_file.read_text(encoding="utf-8"))
            except Exception:
                self.temp_bans = {}

    def _save_temp_bans(self) -> None:
        self.temp_bans_file.parent.mkdir(parents=True, exist_ok=True)
        self.temp_bans_file.write_text(json.dumps(self.temp_bans, indent=2), encoding="utf-8")

    def _load_mod_notes(self) -> None:
        self.mod_notes_file.parent.mkdir(parents=True, exist_ok=True)
        if self.mod_notes_file.exists():
            try:
                self.mod_notes = json.loads(self.mod_notes_file.read_text(encoding="utf-8"))
            except Exception:
                self.mod_notes = {}

    def _save_mod_notes(self) -> None:
        self.mod_notes_file.parent.mkdir(parents=True, exist_ok=True)
        self.mod_notes_file.write_text(json.dumps(self.mod_notes, indent=2), encoding="utf-8")

    async def _request(self, method: str, endpoint: str, **kwargs):
        if not self.session:
            raise RuntimeError("VRChat session nicht initialisiert.")

        headers = kwargs.pop("headers", {})
        if self.auth_header:
            headers["Authorization"] = self.auth_header
        url = f"{self.api_base}{endpoint}"
        async with self.session.request(method, url, headers=headers, **kwargs) as response:
            text = await response.text()
            try:
                data = json.loads(text) if text else {}
            except Exception:
                data = {"raw": text}
            return response.status, data

    def _encode_basic_auth(self, username: str, password: str) -> str:
        raw = f"{username}:{password}".encode("utf-8")
        return f"Basic {base64.b64encode(raw).decode('utf-8')}"

    def _is_vrc_18_verified(self, user_payload: dict) -> bool:
        tags = user_payload.get("tags", [])
        if not isinstance(tags, list):
            return False
        lookup = {str(tag).lower() for tag in tags}
        return "system_trust_veteran" in lookup or "age_verified" in lookup

    async def _unban_vrc_user(self, group_id: str, user_id: str) -> tuple[bool, str]:
        status, data = await self._request("DELETE", f"/groups/{group_id}/bans/{user_id}")
        if 200 <= status < 300:
            return True, "Entbannt"
        return False, f"HTTP {status}: {data}"

    @tasks.loop(minutes=1)
    async def temp_unban_worker(self) -> None:
        now_ts = int(time.time())
        expired = [ban_key for ban_key, item in self.temp_bans.items() if item.get("expires_at", 0) <= now_ts]
        if not expired:
            return

        for ban_key in expired:
            item = self.temp_bans.get(ban_key, {})
            group_id = item.get("group_id")
            user_id = item.get("user_id")
            if group_id and user_id:
                await self._unban_vrc_user(group_id, user_id)
            self.temp_bans.pop(ban_key, None)
        self._save_temp_bans()

    # -------------------- CORE / LOGIN (10 utility commands) --------------------
    @app_commands.command(name="vrc_login", description="Login bei VRChat mit Username + Passwort.")
    async def vrc_login(self, interaction: discord.Interaction, username: str, password: str):
        await interaction.response.defer(ephemeral=True)
        self.auth_header = self._encode_basic_auth(username, password)
        status, data = await self._request("GET", "/auth/user")
        if status >= 400:
            self.auth_header = None
            return await interaction.followup.send(f"❌ Login fehlgeschlagen: `{status}` {data}", ephemeral=True)

        two_fa = data.get("requiresTwoFactorAuth")
        if two_fa:
            self.login_pending_2fa = True
            return await interaction.followup.send(
                "🔐 Login ok, aber 2FA nötig. Nutze jetzt `/vrc_2fa app_code:<code>`.",
                ephemeral=True,
            )

        self.logged_in_user = data.get("displayName", username)
        self.login_pending_2fa = False
        await interaction.followup.send(f"✅ Eingeloggt als **{self.logged_in_user}**.", ephemeral=True)

    @app_commands.command(name="vrc_2fa", description="Bestätige VRChat 2FA (App/TOTP).")
    async def vrc_2fa(self, interaction: discord.Interaction, app_code: str):
        await interaction.response.defer(ephemeral=True)
        if not self.auth_header or not self.login_pending_2fa:
            return await interaction.followup.send("❌ Kein 2FA-Login ausstehend.", ephemeral=True)

        status, data = await self._request("POST", "/auth/twofactorauth/totp/verify", json={"code": app_code})
        if status >= 400:
            return await interaction.followup.send(f"❌ 2FA fehlgeschlagen: `{status}` {data}", ephemeral=True)

        self.login_pending_2fa = False
        me_status, me = await self._request("GET", "/auth/user")
        self.logged_in_user = me.get("displayName", "unknown") if me_status < 400 else "unknown"
        await interaction.followup.send(f"✅ 2FA erfolgreich. Eingeloggt als **{self.logged_in_user}**.", ephemeral=True)

    @app_commands.command(name="vrc_logout", description="Logout / Session zurücksetzen.")
    async def vrc_logout(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        self.auth_header = None
        self.logged_in_user = None
        self.login_pending_2fa = False
        if self.session:
            self.session.cookie_jar.clear()
        await interaction.followup.send("✅ VRChat Session zurückgesetzt.", ephemeral=True)

    @app_commands.command(name="vrc_me", description="Zeigt Informationen über den eingeloggten VRChat User.")
    async def vrc_me(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        status, data = await self._request("GET", "/auth/user")
        if status >= 400:
            return await interaction.followup.send(f"❌ Fehler: `{status}` {data}", ephemeral=True)

        embed = discord.Embed(title="👤 VRChat Me", color=discord.Color.blurple())
        embed.add_field(name="DisplayName", value=data.get("displayName", "?"), inline=True)
        embed.add_field(name="User ID", value=data.get("id", "?"), inline=True)
        embed.add_field(name="18+ verified", value="Ja" if self._is_vrc_18_verified(data) else "Nein", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="vrc_user", description="Sucht einen VRChat User über userId oder displayName.")
    async def vrc_user(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True)
        if query.startswith("usr_"):
            status, data = await self._request("GET", f"/users/{query}")
        else:
            status, data = await self._request("GET", "/users", params={"search": query, "n": 1})
            if status < 400 and isinstance(data, list) and data:
                data = data[0]

        if status >= 400 or not data:
            return await interaction.followup.send(f"❌ User nicht gefunden: `{status}`", ephemeral=True)

        embed = discord.Embed(title="🔎 VRChat User", color=discord.Color.green())
        embed.add_field(name="DisplayName", value=data.get("displayName", "?"), inline=True)
        embed.add_field(name="ID", value=data.get("id", "?"), inline=True)
        embed.add_field(name="18+ verified", value="Ja" if self._is_vrc_18_verified(data) else "Nein", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="vrc_search_user", description="Suche mehrere VRChat User.")
    async def vrc_search_user(self, interaction: discord.Interaction, search: str):
        await interaction.response.defer(ephemeral=True)
        status, data = await self._request("GET", "/users", params={"search": search, "n": 10})
        if status >= 400 or not isinstance(data, list):
            return await interaction.followup.send(f"❌ Suche fehlgeschlagen: `{status}` {data}", ephemeral=True)

        lines = [f"• **{u.get('displayName', '?')}** (`{u.get('id', '?')}`)" for u in data[:10]] or ["Keine Treffer"]
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="vrc_group_info", description="Infos zur VRChat Gruppe (main/afterdark).")
    @app_commands.choices(group=[app_commands.Choice(name="main", value="main"), app_commands.Choice(name="afterdark", value="afterdark")])
    async def vrc_group_info(self, interaction: discord.Interaction, group: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        group_id = VRCHAT_GROUP_MAIN_ID if group.value == "main" else VRCHAT_GROUP_AFTERDARK_ID
        status, data = await self._request("GET", f"/groups/{group_id}")
        if status >= 400:
            return await interaction.followup.send(f"❌ Fehler: `{status}` {data}", ephemeral=True)

        embed = discord.Embed(title=f"👥 Gruppe {group.value}", color=discord.Color.orange())
        embed.add_field(name="Name", value=data.get("name", "?"), inline=True)
        embed.add_field(name="ID", value=data.get("id", group_id), inline=True)
        embed.add_field(name="Owner", value=data.get("ownerId", "?"), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="vrc_group_logs", description="Liest Audit-Logs einer VRChat Gruppe.")
    @app_commands.choices(group=[app_commands.Choice(name="main", value="main"), app_commands.Choice(name="afterdark", value="afterdark")])
    async def vrc_group_logs(self, interaction: discord.Interaction, group: app_commands.Choice[str], limit: int = 10):
        await interaction.response.defer(ephemeral=True)
        group_id = VRCHAT_GROUP_MAIN_ID if group.value == "main" else VRCHAT_GROUP_AFTERDARK_ID
        status, data = await self._request("GET", f"/groups/{group_id}/auditLogs", params={"n": max(1, min(limit, 25))})
        if status >= 400:
            return await interaction.followup.send(f"❌ Logs konnten nicht geladen werden: `{status}` {data}", ephemeral=True)

        if not isinstance(data, list) or not data:
            return await interaction.followup.send("ℹ️ Keine Logs gefunden.", ephemeral=True)

        lines = []
        for item in data[:25]:
            actor = item.get("actorDisplayName") or item.get("actorId", "unknown")
            event = item.get("eventType", "unknown")
            lines.append(f"• `{event}` von **{actor}**")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="vrc_invite", description="Sendet eine Invite in eine Instance.")
    async def vrc_invite(self, interaction: discord.Interaction, user_id: str, world_instance: str):
        await interaction.response.defer(ephemeral=True)
        payload = {"instanceId": world_instance}
        status, data = await self._request("POST", f"/invite/{user_id}", json=payload)
        if status >= 400:
            return await interaction.followup.send(f"❌ Invite fehlgeschlagen: `{status}` {data}", ephemeral=True)
        await interaction.followup.send("✅ Invite gesendet.", ephemeral=True)

    @app_commands.command(name="vrc_instance_info", description="Zeigt Instanz Infos (worldId:instanceId).")
    async def vrc_instance_info(self, interaction: discord.Interaction, world_id: str, instance_id: str):
        await interaction.response.defer(ephemeral=True)
        status, data = await self._request("GET", f"/instances/{world_id}:{instance_id}")
        if status >= 400:
            return await interaction.followup.send(f"❌ Fehler: `{status}` {data}", ephemeral=True)
        await interaction.followup.send(f"```json\n{json.dumps(data, indent=2)[:1800]}\n```", ephemeral=True)

    # -------------------- MODERATION (10 commands) --------------------
    @app_commands.command(name="vrcban", description="Bannt einen User in einer Gruppe (displayName oder userId/link).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(group=[app_commands.Choice(name="main", value="main"), app_commands.Choice(name="afterdark", value="afterdark")])
    async def vrcban(self, interaction: discord.Interaction, group: app_commands.Choice[str], target: str, reason: str = "No reason"):
        await interaction.response.defer(ephemeral=True)
        group_id = VRCHAT_GROUP_MAIN_ID if group.value == "main" else VRCHAT_GROUP_AFTERDARK_ID
        user_id = target.strip()
        if "/home/user/" in user_id:
            user_id = user_id.split("/home/user/")[-1].split("/")[0]

        status, user_data = await self._request("GET", f"/users/{user_id}") if user_id.startswith("usr_") else await self._request("GET", "/users", params={"search": user_id, "n": 1})
        if status >= 400:
            return await interaction.followup.send(f"❌ User lookup fehlgeschlagen: `{status}`", ephemeral=True)

        if isinstance(user_data, list):
            if not user_data:
                return await interaction.followup.send("❌ User nicht gefunden.", ephemeral=True)
            user_data = user_data[0]

        user_id = user_data.get("id", user_id)
        verified_18 = self._is_vrc_18_verified(user_data)

        payload = {"userId": user_id, "reason": reason}
        ban_status, ban_data = await self._request("POST", f"/groups/{group_id}/bans", json=payload)
        if ban_status >= 400:
            return await interaction.followup.send(f"❌ Ban fehlgeschlagen: `{ban_status}` {ban_data}", ephemeral=True)

        await interaction.followup.send(
            f"✅ Gebannt: **{user_data.get('displayName', user_id)}** | 18+ verified: **{'Ja' if verified_18 else 'Nein'}**",
            ephemeral=True,
        )

    @app_commands.command(name="vrc_tempban", description="Temporärer Ban (Minuten), mit Auto-Unban.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(group=[app_commands.Choice(name="main", value="main"), app_commands.Choice(name="afterdark", value="afterdark")])
    async def vrc_tempban(self, interaction: discord.Interaction, group: app_commands.Choice[str], user_id: str, minutes: int, reason: str = "Temp Ban"):
        await interaction.response.defer(ephemeral=True)
        group_id = VRCHAT_GROUP_MAIN_ID if group.value == "main" else VRCHAT_GROUP_AFTERDARK_ID
        payload = {"userId": user_id, "reason": reason}
        status, data = await self._request("POST", f"/groups/{group_id}/bans", json=payload)
        if status >= 400:
            return await interaction.followup.send(f"❌ Temp-Ban fehlgeschlagen: `{status}` {data}", ephemeral=True)

        expires_at = int(time.time()) + max(1, minutes) * 60
        key = f"{group_id}:{user_id}"
        self.temp_bans[key] = {
            "group_id": group_id,
            "user_id": user_id,
            "expires_at": expires_at,
            "reason": reason,
            "moderator": str(interaction.user),
        }
        self._save_temp_bans()
        await interaction.followup.send(f"✅ Temp-Ban gesetzt bis <t:{expires_at}:F>.", ephemeral=True)

    @app_commands.command(name="vrcunban", description="Entbannt einen User aus einer Gruppe.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(group=[app_commands.Choice(name="main", value="main"), app_commands.Choice(name="afterdark", value="afterdark")])
    async def vrcunban(self, interaction: discord.Interaction, group: app_commands.Choice[str], user_id: str):
        await interaction.response.defer(ephemeral=True)
        group_id = VRCHAT_GROUP_MAIN_ID if group.value == "main" else VRCHAT_GROUP_AFTERDARK_ID
        ok, message = await self._unban_vrc_user(group_id, user_id)
        if not ok:
            return await interaction.followup.send(f"❌ Unban fehlgeschlagen: {message}", ephemeral=True)

        self.temp_bans.pop(f"{group_id}:{user_id}", None)
        self._save_temp_bans()
        await interaction.followup.send("✅ User entbannt.", ephemeral=True)

    @app_commands.command(name="vrc_list_bans", description="Listet aktive Bans einer Gruppe.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(group=[app_commands.Choice(name="main", value="main"), app_commands.Choice(name="afterdark", value="afterdark")])
    async def vrc_list_bans(self, interaction: discord.Interaction, group: app_commands.Choice[str], limit: int = 20):
        await interaction.response.defer(ephemeral=True)
        group_id = VRCHAT_GROUP_MAIN_ID if group.value == "main" else VRCHAT_GROUP_AFTERDARK_ID
        status, data = await self._request("GET", f"/groups/{group_id}/bans", params={"n": max(1, min(limit, 100))})
        if status >= 400:
            return await interaction.followup.send(f"❌ Fehler: `{status}` {data}", ephemeral=True)
        if not isinstance(data, list) or not data:
            return await interaction.followup.send("ℹ️ Keine Bans gefunden.", ephemeral=True)
        lines = [f"• `{x.get('userId', '?')}` | {x.get('createdAt', '?')}" for x in data[:100]]
        await interaction.followup.send("\n".join(lines[:40]), ephemeral=True)

    @app_commands.command(name="vrc_modnote_add", description="Fügt Mod-Notiz zu VRChat User hinzu.")
    @app_commands.default_permissions(administrator=True)
    async def vrc_modnote_add(self, interaction: discord.Interaction, user_id: str, note: str):
        await interaction.response.defer(ephemeral=True)
        self.mod_notes.setdefault(user_id, []).append(f"{int(time.time())}:{interaction.user}:{note}")
        self._save_mod_notes()
        await interaction.followup.send("✅ Mod-Notiz gespeichert.", ephemeral=True)

    @app_commands.command(name="vrc_modnote_list", description="Zeigt Mod-Notizen eines Users.")
    @app_commands.default_permissions(administrator=True)
    async def vrc_modnote_list(self, interaction: discord.Interaction, user_id: str):
        await interaction.response.defer(ephemeral=True)
        notes = self.mod_notes.get(user_id, [])
        if not notes:
            return await interaction.followup.send("ℹ️ Keine Notizen.", ephemeral=True)
        await interaction.followup.send("\n".join([f"• {n}" for n in notes[-20:]]), ephemeral=True)

    @app_commands.command(name="vrc_modnote_clear", description="Löscht alle Mod-Notizen eines Users.")
    @app_commands.default_permissions(administrator=True)
    async def vrc_modnote_clear(self, interaction: discord.Interaction, user_id: str):
        await interaction.response.defer(ephemeral=True)
        self.mod_notes.pop(user_id, None)
        self._save_mod_notes()
        await interaction.followup.send("✅ Mod-Notizen gelöscht.", ephemeral=True)

    @app_commands.command(name="vrc_remove_from_group", description="Entfernt User aus Gruppe.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(group=[app_commands.Choice(name="main", value="main"), app_commands.Choice(name="afterdark", value="afterdark")])
    async def vrc_remove_from_group(self, interaction: discord.Interaction, group: app_commands.Choice[str], user_id: str):
        await interaction.response.defer(ephemeral=True)
        group_id = VRCHAT_GROUP_MAIN_ID if group.value == "main" else VRCHAT_GROUP_AFTERDARK_ID
        status, data = await self._request("DELETE", f"/groups/{group_id}/members/{user_id}")
        if status >= 400:
            return await interaction.followup.send(f"❌ Entfernen fehlgeschlagen: `{status}` {data}", ephemeral=True)
        await interaction.followup.send("✅ User aus Gruppe entfernt.", ephemeral=True)

    @app_commands.command(name="vrc_group_member", description="Gruppenmitglied Infos holen.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(group=[app_commands.Choice(name="main", value="main"), app_commands.Choice(name="afterdark", value="afterdark")])
    async def vrc_group_member(self, interaction: discord.Interaction, group: app_commands.Choice[str], user_id: str):
        await interaction.response.defer(ephemeral=True)
        group_id = VRCHAT_GROUP_MAIN_ID if group.value == "main" else VRCHAT_GROUP_AFTERDARK_ID
        status, data = await self._request("GET", f"/groups/{group_id}/members/{user_id}")
        if status >= 400:
            return await interaction.followup.send(f"❌ Fehler: `{status}` {data}", ephemeral=True)
        await interaction.followup.send(f"```json\n{json.dumps(data, indent=2)[:1800]}\n```", ephemeral=True)

    @app_commands.command(name="vrc_pending_tempbans", description="Zeigt aktive Temp-Bans mit Auto-Unban Zeit.")
    @app_commands.default_permissions(administrator=True)
    async def vrc_pending_tempbans(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.temp_bans:
            return await interaction.followup.send("ℹ️ Keine aktiven Temp-Bans.", ephemeral=True)
        lines = []
        for item in self.temp_bans.values():
            lines.append(
                f"• `{item.get('user_id')}` in `{item.get('group_id')}` bis <t:{item.get('expires_at', 0)}:F>"
            )
        await interaction.followup.send("\n".join(lines[:40]), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VRChatCog(bot))
