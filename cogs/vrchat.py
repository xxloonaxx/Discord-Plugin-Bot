import base64
import json
import time
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    ROLES_STAFF,
    VRCHAT_API_BASE_URL,
    VRCHAT_AUDIT_LOG_CHANNEL_ID,
    VRCHAT_GROUP_ID,
    VRCHAT_INVITE_CHANNEL_ID,
    VRCHAT_STAFF_ROLE_IDS,
    VRCHAT_USER_AGENT,
)

POLL_SECONDS = 10


class VRChatInviteActionView(discord.ui.View):
    def __init__(self, cog: "VRChatCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    def _extract_meta_from_embed(self, interaction: discord.Interaction) -> tuple[str | None, str | None]:
        message = interaction.message
        if not message or not message.embeds:
            return None, None

        embed = message.embeds[0]
        group_id = None
        user_id = None

        for field in embed.fields:
            if field.name == "Group ID":
                group_id = field.value.strip()
            elif field.name == "UID":
                user_id = field.value.strip()

        return group_id, user_id

    async def _finalize_message(
        self,
        interaction: discord.Interaction,
        *,
        status_text: str,
        color: discord.Color,
    ) -> None:
        if not interaction.message or not interaction.message.embeds:
            return

        old = interaction.message.embeds[0]
        embed = discord.Embed(title=old.title, description=old.description, color=color)
        if old.thumbnail and old.thumbnail.url:
            embed.set_thumbnail(url=old.thumbnail.url)

        replaced = False
        for field in old.fields:
            if field.name == "Status":
                embed.add_field(name="Status", value=status_text, inline=False)
                replaced = True
            else:
                embed.add_field(name=field.name, value=field.value, inline=field.inline)

        if not replaced:
            embed.add_field(name="Status", value=status_text, inline=False)

        self.disable_all_items()
        await interaction.message.edit(embed=embed, view=self)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not self.cog._has_mod_permission(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return False

        await interaction.response.defer(ephemeral=True)
        if not self.cog._is_authenticated():
            await interaction.followup.send("❌ VRChat-Login fehlt.", ephemeral=True)
            return False

        return True

    @discord.ui.button(label="Akzeptieren", style=discord.ButtonStyle.success, emoji="✅", custom_id="vrchat_invite_accept")
    async def accept_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return

        group_id, user_id = self._extract_meta_from_embed(interaction)
        if not group_id or not user_id:
            return await interaction.followup.send("❌ Konnte Request-Daten nicht lesen.", ephemeral=True)

        ok, data = await self.cog._respond_group_join_request(group_id, user_id, action="accept", block=False)
        if not ok:
            return await interaction.followup.send(f"❌ Akzeptieren fehlgeschlagen: {data}", ephemeral=True)

        await self._finalize_message(interaction, status_text=f"✅ Akzeptiert von {interaction.user.mention}", color=discord.Color.green())
        await interaction.followup.send("✅ Anfrage akzeptiert.", ephemeral=True)

    @discord.ui.button(label="Ablehnen", style=discord.ButtonStyle.secondary, emoji="❌", custom_id="vrchat_invite_reject")
    async def reject_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return

        group_id, user_id = self._extract_meta_from_embed(interaction)
        if not group_id or not user_id:
            return await interaction.followup.send("❌ Konnte Request-Daten nicht lesen.", ephemeral=True)

        ok, data = await self.cog._respond_group_join_request(group_id, user_id, action="reject", block=False)
        if not ok:
            return await interaction.followup.send(f"❌ Ablehnen fehlgeschlagen: {data}", ephemeral=True)

        await self._finalize_message(interaction, status_text=f"❌ Abgelehnt von {interaction.user.mention}", color=discord.Color.light_grey())
        await interaction.followup.send("✅ Anfrage abgelehnt.", ephemeral=True)

    @discord.ui.button(label="Blocken", style=discord.ButtonStyle.danger, emoji="⛔", custom_id="vrchat_invite_block")
    async def block_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return

        group_id, user_id = self._extract_meta_from_embed(interaction)
        if not group_id or not user_id:
            return await interaction.followup.send("❌ Konnte Request-Daten nicht lesen.", ephemeral=True)

        ok, data = await self.cog._respond_group_join_request(group_id, user_id, action="reject", block=True)
        if not ok:
            await self.cog._respond_group_join_request(group_id, user_id, action="reject", block=False)
            ban_ok, ban_data = await self.cog._ban_group_user(group_id, user_id, reason=f"Blocked via Discord by {interaction.user}")
            if not ban_ok:
                return await interaction.followup.send(
                    f"❌ Blocken fehlgeschlagen: reject={data} / ban={ban_data}",
                    ephemeral=True,
                )

        await self._finalize_message(interaction, status_text=f"⛔ Geblockt von {interaction.user.mention}", color=discord.Color.red())
        await interaction.followup.send("✅ Anfrage geblockt.", ephemeral=True)


class VRChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.api_base = VRCHAT_API_BASE_URL.rstrip("/")
        self.user_agent = VRCHAT_USER_AGENT
        self.group_id = VRCHAT_GROUP_ID

        self.session: aiohttp.ClientSession | None = None
        self.auth_header: str | None = None
        self.logged_in_user: str | None = None
        self.login_pending_2fa = False
        self.pending_2fa_method = "app"

        self.temp_bans_file = Path("data/vrchat_temp_bans.json")
        self.temp_bans: dict[str, dict[str, Any]] = {}

        self.mod_notes_file = Path("data/vrchat_mod_notes.json")
        self.mod_notes: dict[str, list[str]] = {}

        self.audit_log_channel_id = VRCHAT_AUDIT_LOG_CHANNEL_ID
        self.invite_channel_id = VRCHAT_INVITE_CHANNEL_ID

        self.last_group_audit_id: str | None = None
        self.last_group_request_id: str | None = None
        self.seen_audit_event_ids: set[str] = set()
        self.seen_join_request_ids: set[str] = set()

        configured_staff = [x for x in VRCHAT_STAFF_ROLE_IDS if x > 0]
        self.staff_role_ids = configured_staff if configured_staff else [x for x in ROLES_STAFF if x > 0]

        self.audit_ready_announced = False
        self.invite_ready_announced = False

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
        self._load_temp_bans()
        self._load_mod_notes()

        if not self.temp_unban_worker.is_running():
            self.temp_unban_worker.start()
        if not self.group_audit_log_worker.is_running():
            self.group_audit_log_worker.start()
        if not self.group_join_request_worker.is_running():
            self.group_join_request_worker.start()

        self.bot.add_view(VRChatInviteActionView(self))

    async def cog_unload(self) -> None:
        if self.temp_unban_worker.is_running():
            self.temp_unban_worker.cancel()
        if self.group_audit_log_worker.is_running():
            self.group_audit_log_worker.cancel()
        if self.group_join_request_worker.is_running():
            self.group_join_request_worker.cancel()

        if self.session and not self.session.closed:
            await self.session.close()

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ Fehler: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Fehler: {error}", ephemeral=True)

    def _has_mod_permission(self, user: discord.abc.User | discord.Member) -> bool:
        if not isinstance(user, discord.Member):
            return False
        if user.guild_permissions.administrator or user.guild_permissions.ban_members:
            return True
        return any(role.id in self.staff_role_ids for role in user.roles)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._has_mod_permission(interaction.user):
            return True
        await interaction.response.send_message(
            "❌ Nur Staff/Ban-Berechtigung darf VRChat-Commands nutzen.",
            ephemeral=True,
        )
        return False

    def _load_temp_bans(self) -> None:
        self.temp_bans_file.parent.mkdir(parents=True, exist_ok=True)
        if self.temp_bans_file.exists():
            try:
                self.temp_bans = json.loads(self.temp_bans_file.read_text(encoding="utf-8"))
            except Exception:
                self.temp_bans = {}

    def _save_temp_bans(self) -> None:
        self.temp_bans_file.parent.mkdir(parents=True, exist_ok=True)
        self.temp_bans_file.write_text(json.dumps(self.temp_bans, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_mod_notes(self) -> None:
        self.mod_notes_file.parent.mkdir(parents=True, exist_ok=True)
        if self.mod_notes_file.exists():
            try:
                self.mod_notes = json.loads(self.mod_notes_file.read_text(encoding="utf-8"))
            except Exception:
                self.mod_notes = {}

    def _save_mod_notes(self) -> None:
        self.mod_notes_file.parent.mkdir(parents=True, exist_ok=True)
        self.mod_notes_file.write_text(json.dumps(self.mod_notes, indent=2, ensure_ascii=False), encoding="utf-8")

    def _encode_basic_auth(self, username: str, password: str) -> str:
        raw = f"{username}:{password}".encode("utf-8")
        return f"Basic {base64.b64encode(raw).decode('utf-8')}"

    def _extract_user_id_from_target(self, target: str) -> str:
        value = target.strip()
        if "/home/user/" in value:
            value = value.split("/home/user/")[-1].split("/")[0]
        return value

    def _is_vrc_18_verified(self, user_payload: dict[str, Any]) -> bool:
        if bool(user_payload.get("ageVerified")):
            return True
        status = str(user_payload.get("ageVerificationStatus", "")).lower()
        if status in {"verified", "18+", "age_verified"}:
            return True
        tags = user_payload.get("tags", [])
        if not isinstance(tags, list):
            return False
        lookup = {str(tag).lower() for tag in tags}
        return "age_verified" in lookup

    def _has_session_auth_cookie(self) -> bool:
        if not self.session:
            return False
        for cookie in self.session.cookie_jar:
            if cookie.key.lower() == "auth":
                return True
        return False

    def _is_authenticated(self) -> bool:
        return self._has_session_auth_cookie() or self.login_pending_2fa

    async def _ensure_authenticated(self, interaction: discord.Interaction) -> bool:
        if self._is_authenticated():
            return True
        await interaction.followup.send("❌ Nicht eingeloggt. Nutze zuerst `/vrc_login`.", ephemeral=True)
        return False

    async def _resolve_user(self, query: str) -> tuple[int, dict[str, Any] | None]:
        value = self._extract_user_id_from_target(query)

        if value.startswith("usr_"):
            status, data = await self._request("GET", f"/users/{value}")
            if status >= 400 or not isinstance(data, dict):
                return status, None
            return status, data

        status, data = await self._request("GET", "/users", params={"search": value, "n": 1})
        if status >= 400 or not isinstance(data, list) or not data:
            return status, None

        first = data[0]
        return status, first if isinstance(first, dict) else None

    def _trim_seen_set(self, seen: set[str], max_size: int = 2000) -> None:
        if len(seen) > max_size:
            for item in list(seen)[: len(seen) - max_size]:
                seen.discard(item)

    async def _send_channel_message(
        self,
        channel_id: int,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        if channel_id <= 0:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return
        try:
            await channel.send(content=content, embed=embed, view=view)
        except Exception:
            return

    async def _request(self, method: str, endpoint: str, **kwargs) -> tuple[int, Any]:
        if not self.session:
            raise RuntimeError("VRChat session nicht initialisiert.")

        headers = kwargs.pop("headers", {})
        headers["User-Agent"] = self.user_agent

        normalized_endpoint = endpoint.split("?", 1)[0]
        if self.auth_header and normalized_endpoint == "/auth/user":
            headers["Authorization"] = self.auth_header

        url = f"{self.api_base}{endpoint}"

        try:
            async with self.session.request(method, url, headers=headers, **kwargs) as response:
                text = await response.text()
                try:
                    data = json.loads(text) if text else {}
                except Exception:
                    data = {"raw": text}
                return response.status, data
        except aiohttp.ClientError as exc:
            return 599, {"error": {"message": str(exc), "status_code": 599}}

    async def _unban_vrc_user(self, group_id: str, user_id: str) -> tuple[bool, str]:
        status, data = await self._request("DELETE", f"/groups/{group_id}/bans/{user_id}")
        if 200 <= status < 300:
            return True, "Entbannt"
        return False, f"HTTP {status}: {data}"

    async def _fetch_group_audit_logs(self, limit: int = 20) -> list[dict[str, Any]]:
        status, data = await self._request(
            "GET",
            f"/groups/{self.group_id}/auditLogs",
            params={"n": max(1, min(limit, 100))},
        )
        if status >= 400 or not isinstance(data, list):
            return []
        return [x for x in data if isinstance(x, dict)]

    async def _fetch_group_join_requests(self, limit: int = 20) -> list[dict[str, Any]]:
        status, data = await self._request(
            "GET",
            f"/groups/{self.group_id}/requests",
            params={"n": max(1, min(limit, 100))},
        )
        if status >= 400 or not isinstance(data, list):
            return []
        return [x for x in data if isinstance(x, dict)]

    async def _respond_group_join_request(self, group_id: str, user_id: str, *, action: str, block: bool = False) -> tuple[bool, Any]:
        payload: dict[str, Any] = {"action": action}
        if block:
            payload["block"] = True

        status, data = await self._request("PUT", f"/groups/{group_id}/requests/{user_id}", json=payload)
        return (200 <= status < 300), data

    async def _ban_group_user(self, group_id: str, user_id: str, reason: str = "Blocked via Discord invite moderation") -> tuple[bool, Any]:
        payload = {"userId": user_id, "reason": reason}
        status, data = await self._request("POST", f"/groups/{group_id}/bans", json=payload)
        return (200 <= status < 300), data

    async def _make_join_request_embed(self, request_data: dict[str, Any]) -> tuple[discord.Embed, str | None]:
        user_id = (
            request_data.get("userId")
            or request_data.get("targetUserId")
            or request_data.get("sourceUserId")
            or request_data.get("user", {}).get("id")
        )

        request_id = request_data.get("id")
        unique_id = str(request_id or user_id or f"unknown:{time.time()}")

        if not user_id:
            embed = discord.Embed(
                title="📩 Neue Gruppenanfrage",
                color=discord.Color.blurple(),
                description="Request gefunden, aber ohne userId.",
            )
            embed.add_field(name="Group ID", value=self.group_id, inline=False)
            embed.add_field(name="Request ID", value=unique_id, inline=False)
            embed.add_field(name="Status", value="⏳ Offen", inline=False)
            return embed, unique_id

        request_user = request_data.get("user", {}) if isinstance(request_data.get("user"), dict) else {}
        user_payload: dict[str, Any] | None = None

        if isinstance(user_id, str) and user_id.startswith("usr_"):
            _, user_payload = await self._resolve_user(user_id)

        display_name = (user_payload or {}).get("displayName") or request_user.get("displayName") or user_id
        age_status = "Ja" if (user_payload and self._is_vrc_18_verified(user_payload)) else "Nein"

        image_url = (
            (user_payload or {}).get("userIcon")
            or request_user.get("thumbnailUrl")
            or request_user.get("iconUrl")
            or request_user.get("profilePicOverride")
            or request_user.get("currentAvatarThumbnailImageUrl")
            or (user_payload or {}).get("currentAvatarThumbnailImageUrl")
        )

        created_at = request_data.get("createdAt", "unknown")

        embed = discord.Embed(
            title="📩 Neue Gruppenanfrage",
            color=discord.Color.blurple(),
            description="Neue Beitrittsanfrage für deine konfigurierte Gruppe",
        )
        embed.add_field(name="DisplayName", value=str(display_name), inline=True)
        embed.add_field(name="UID", value=str(user_id), inline=True)
        embed.add_field(name="Age Verified", value=age_status, inline=True)
        embed.add_field(name="Group ID", value=self.group_id, inline=False)
        embed.add_field(name="Request ID", value=str(unique_id), inline=False)
        embed.add_field(name="Zeit", value=str(created_at), inline=False)
        embed.add_field(name="Status", value="⏳ Offen", inline=False)

        if image_url:
            embed.set_thumbnail(url=image_url)

        return embed, unique_id

    @tasks.loop(minutes=1)
    async def temp_unban_worker(self) -> None:
        if not self._is_authenticated():
            return

        now_ts = int(time.time())
        expired = [ban_key for ban_key, item in self.temp_bans.items() if int(item.get("expires_at", 0)) <= now_ts]
        if not expired:
            return

        changed = False
        for ban_key in expired:
            item = self.temp_bans.get(ban_key, {})
            group_id = item.get("group_id")
            user_id = item.get("user_id")
            if group_id and user_id:
                await self._unban_vrc_user(group_id, user_id)
            self.temp_bans.pop(ban_key, None)
            changed = True

        if changed:
            self._save_temp_bans()

    @temp_unban_worker.before_loop
    async def before_temp_unban_worker(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=POLL_SECONDS)
    async def group_audit_log_worker(self) -> None:
        if not self._is_authenticated():
            return

        if not self.audit_ready_announced:
            self.audit_ready_announced = True
            await self._send_channel_message(
                self.audit_log_channel_id,
                content=f"🟢 VRChat Audit-Watcher aktiv für `{self.group_id}`. Abfrage alle {POLL_SECONDS} Sekunden.",
            )

        logs = await self._fetch_group_audit_logs(limit=30)
        if not logs:
            return

        newest_seen = str(logs[0].get("id") or logs[0].get("createdAt") or "")
        if self.last_group_audit_id is None:
            for entry in logs:
                seed_id = str(entry.get("id") or entry.get("createdAt") or "")
                if seed_id:
                    self.seen_audit_event_ids.add(seed_id)
            self.last_group_audit_id = newest_seen
            self._trim_seen_set(self.seen_audit_event_ids)
            return

        new_entries: list[dict[str, Any]] = []
        for entry in reversed(logs):
            entry_id = str(entry.get("id") or entry.get("createdAt") or "")
            if not entry_id or entry_id in self.seen_audit_event_ids:
                continue
            new_entries.append(entry)
            self.seen_audit_event_ids.add(entry_id)

        for entry in new_entries:
            actor = entry.get("actorDisplayName") or entry.get("actorId") or "unknown"
            target = entry.get("targetDisplayName") or entry.get("targetId") or entry.get("objectDisplayName") or entry.get("objectId") or "unknown"
            event_type = entry.get("eventType", "unknown")
            created_at = entry.get("createdAt", "unknown")
            description = entry.get("description") or entry.get("details") or "-"

            embed = discord.Embed(
                title="📜 Neuer VRChat Audit-Log",
                color=discord.Color.orange(),
                description=f"**Group ID:** {self.group_id}",
            )
            embed.add_field(name="Event", value=str(event_type), inline=True)
            embed.add_field(name="Actor", value=str(actor), inline=True)
            embed.add_field(name="Target", value=str(target), inline=True)
            embed.add_field(name="Zeit", value=str(created_at), inline=False)
            embed.add_field(name="Info", value=str(description)[:1000], inline=False)

            await self._send_channel_message(self.audit_log_channel_id, embed=embed)

        self.last_group_audit_id = newest_seen
        self._trim_seen_set(self.seen_audit_event_ids)

    @group_audit_log_worker.before_loop
    async def before_group_audit_log_worker(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=POLL_SECONDS)
    async def group_join_request_worker(self) -> None:
        if not self._is_authenticated():
            return

        if not self.invite_ready_announced:
            self.invite_ready_announced = True
            await self._send_channel_message(
                self.invite_channel_id,
                content=f"🟢 VRChat Invite-Watcher aktiv für `{self.group_id}`. Abfrage alle {POLL_SECONDS} Sekunden.",
            )

        requests = await self._fetch_group_join_requests(limit=30)
        if not requests:
            return

        newest_seen = str(requests[0].get("id") or requests[0].get("createdAt") or "")
        if self.last_group_request_id is None:
            for entry in requests:
                seed_id = str(entry.get("id") or entry.get("createdAt") or "")
                if seed_id:
                    self.seen_join_request_ids.add(seed_id)
            self.last_group_request_id = newest_seen
            self._trim_seen_set(self.seen_join_request_ids)
            return

        for entry in reversed(requests):
            embed, unique_id = await self._make_join_request_embed(entry)
            if not unique_id or unique_id in self.seen_join_request_ids:
                continue
            await self._send_channel_message(self.invite_channel_id, embed=embed, view=VRChatInviteActionView(self))
            self.seen_join_request_ids.add(unique_id)

        self.last_group_request_id = newest_seen
        self._trim_seen_set(self.seen_join_request_ids)

    @group_join_request_worker.before_loop
    async def before_group_join_request_worker(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="vrc_login", description="Login bei VRChat mit Username + Passwort + 2FA Methode.")
    @app_commands.choices(
        two_factor_method=[
            app_commands.Choice(name="App (TOTP)", value="app"),
            app_commands.Choice(name="Email OTP", value="email"),
        ]
    )
    async def vrc_login(
        self,
        interaction: discord.Interaction,
        username: str,
        password: str,
        two_factor_method: app_commands.Choice[str],
        two_factor_code: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        self.auth_header = None
        self.logged_in_user = None
        self.login_pending_2fa = False
        self.pending_2fa_method = two_factor_method.value

        if self.session:
            self.session.cookie_jar.clear()

        self.auth_header = self._encode_basic_auth(username, password)

        status, data = await self._request("GET", "/auth/user")
        if status >= 400 or not isinstance(data, dict):
            self.auth_header = None
            return await interaction.followup.send(f"❌ Login fehlgeschlagen: `{status}` {data}", ephemeral=True)

        if data.get("requiresTwoFactorAuth"):
            self.login_pending_2fa = True
            if not two_factor_code:
                return await interaction.followup.send(
                    f"🔐 Login ok, aber 2FA nötig ({two_factor_method.name}). Nutze nun `/vrc_2fa app_code:<code>`.",
                    ephemeral=True,
                )

            endpoint = "/auth/twofactorauth/totp/verify" if two_factor_method.value == "app" else "/auth/twofactorauth/emailotp/verify"
            self.auth_header = None
            two_status, two_data = await self._request("POST", endpoint, json={"code": two_factor_code})
            if two_status >= 400:
                self.login_pending_2fa = True
                return await interaction.followup.send(f"❌ 2FA fehlgeschlagen: `{two_status}` {two_data}", ephemeral=True)

            self.login_pending_2fa = False
            me_status, me = await self._request("GET", "/auth/user")
            self.logged_in_user = me.get("displayName", username) if me_status < 400 and isinstance(me, dict) else username
            return await interaction.followup.send(f"✅ Eingeloggt als **{self.logged_in_user}**.", ephemeral=True)

        self.logged_in_user = data.get("displayName", username)
        self.login_pending_2fa = False
        self.auth_header = None
        await interaction.followup.send(f"✅ Eingeloggt als **{self.logged_in_user}**.", ephemeral=True)

    @app_commands.command(name="vrc_2fa", description="Bestätige VRChat 2FA (App/TOTP oder Email OTP).")
    async def vrc_2fa(self, interaction: discord.Interaction, app_code: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self.login_pending_2fa:
            return await interaction.followup.send("❌ Kein 2FA-Login ausstehend.", ephemeral=True)

        endpoint = "/auth/twofactorauth/totp/verify" if self.pending_2fa_method == "app" else "/auth/twofactorauth/emailotp/verify"
        self.auth_header = None
        status, data = await self._request("POST", endpoint, json={"code": app_code})
        if status >= 400:
            return await interaction.followup.send(f"❌ 2FA fehlgeschlagen: `{status}` {data}", ephemeral=True)

        self.login_pending_2fa = False
        me_status, me = await self._request("GET", "/auth/user")
        self.logged_in_user = me.get("displayName", "unknown") if me_status < 400 and isinstance(me, dict) else "unknown"
        await interaction.followup.send(f"✅ 2FA erfolgreich. Eingeloggt als **{self.logged_in_user}**.", ephemeral=True)

    @app_commands.command(name="vrc_logout", description="Logout / Session zurücksetzen.")
    async def vrc_logout(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        self.auth_header = None
        self.logged_in_user = None
        self.login_pending_2fa = False
        self.pending_2fa_method = "app"
        if self.session:
            self.session.cookie_jar.clear()
        await interaction.followup.send("✅ VRChat Session zurückgesetzt.", ephemeral=True)

    @app_commands.command(name="vrc_watcher_status", description="Zeigt Status der VRChat Watcher.")
    async def vrc_watcher_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        msg = (
            f"Auth: **{'Ja' if self._is_authenticated() else 'Nein'}**\n"
            f"Audit Worker: **{'running' if self.group_audit_log_worker.is_running() else 'stopped'}**\n"
            f"Invite Worker: **{'running' if self.group_join_request_worker.is_running() else 'stopped'}**\n"
            f"Temp-Unban Worker: **{'running' if self.temp_unban_worker.is_running() else 'stopped'}**\n"
            f"Group ID: `{self.group_id}`\n"
            f"Audit Channel: `{self.audit_log_channel_id}`\n"
            f"Invite Channel: `{self.invite_channel_id}`\n"
            f"Last audit ID: `{self.last_group_audit_id}`\n"
            f"Last request ID: `{self.last_group_request_id}`\n"
        )
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="vrc_force_poll", description="Erzwingt sofortige Log/Invite-Abfrage.")
    async def vrc_force_poll(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return
        await self.group_audit_log_worker()
        await self.group_join_request_worker()
        await interaction.followup.send("✅ Polling manuell ausgeführt.", ephemeral=True)

    @app_commands.command(name="vrc_me", description="Zeigt Informationen über den eingeloggten VRChat User.")
    async def vrc_me(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return
        status, data = await self._request("GET", "/auth/user")
        if status >= 400 or not isinstance(data, dict):
            return await interaction.followup.send(f"❌ Fehler: `{status}` {data}", ephemeral=True)

        embed = discord.Embed(title="👤 VRChat Me", color=discord.Color.blurple())
        embed.add_field(name="DisplayName", value=data.get("displayName", "?"), inline=True)
        embed.add_field(name="User ID", value=data.get("id", "?"), inline=True)
        embed.add_field(name="Location", value=data.get("location", "unknown"), inline=False)
        embed.add_field(name="18+ verified", value="Ja" if self._is_vrc_18_verified(data) else "Nein", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="vrc_close_instance", description="Schließt die aktuelle Instanz des eingeloggten Users.")
    async def vrc_close_instance(self, interaction: discord.Interaction, hard_close: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return

        status, me = await self._request("GET", "/auth/user")
        if status >= 400 or not isinstance(me, dict):
            return await interaction.followup.send(f"❌ Konnte User nicht laden: `{status}` {me}", ephemeral=True)

        location = str(me.get("location", "")).strip()
        if not location or ":" not in location or not location.startswith("wrld_"):
            return await interaction.followup.send(f"❌ Keine schließbare Instanz gefunden (`{location or 'unbekannt'}`).", ephemeral=True)

        world_id, instance_id = location.split(":", 1)
        close_status, close_data = await self._request(
            "DELETE",
            f"/instances/{world_id}:{instance_id}",
            params={"hardClose": str(hard_close).lower()},
        )
        if close_status >= 400:
            return await interaction.followup.send(f"❌ Schließen fehlgeschlagen: `{close_status}` {close_data}", ephemeral=True)

        await interaction.followup.send(f"✅ Instanz geschlossen: `{world_id}:{instance_id}`", ephemeral=True)

    @app_commands.command(name="vrc_user", description="Sucht einen VRChat User über userId oder displayName.")
    async def vrc_user(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return
        status, data = await self._resolve_user(query)
        if status >= 400 or not data:
            return await interaction.followup.send(f"❌ User nicht gefunden: `{status}`", ephemeral=True)

        embed = discord.Embed(title="🔎 VRChat User", color=discord.Color.green())
        embed.add_field(name="DisplayName", value=data.get("displayName", "?"), inline=True)
        embed.add_field(name="ID", value=data.get("id", "?"), inline=True)
        embed.add_field(name="18+ verified", value="Ja" if self._is_vrc_18_verified(data) else "Nein", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="vrc_group_info", description="Infos zur konfigurierten VRChat Gruppe.")
    async def vrc_group_info(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return

        status, data = await self._request("GET", f"/groups/{self.group_id}")
        if status >= 400 or not isinstance(data, dict):
            return await interaction.followup.send(f"❌ Fehler: `{status}` {data}", ephemeral=True)

        embed = discord.Embed(title="👥 VRChat Gruppe", color=discord.Color.orange())
        embed.add_field(name="Name", value=data.get("name", "?"), inline=True)
        embed.add_field(name="ID", value=data.get("id", self.group_id), inline=True)
        embed.add_field(name="Owner", value=data.get("ownerId", "?"), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="vrc_group_logs", description="Liest Audit-Logs der konfigurierten VRChat Gruppe.")
    async def vrc_group_logs(self, interaction: discord.Interaction, limit: int = 10) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return

        status, data = await self._request("GET", f"/groups/{self.group_id}/auditLogs", params={"n": max(1, min(limit, 100))})
        if status >= 400:
            return await interaction.followup.send(f"❌ Logs konnten nicht geladen werden: `{status}` {data}", ephemeral=True)
        if not isinstance(data, list) or not data:
            return await interaction.followup.send("ℹ️ Keine Logs gefunden.", ephemeral=True)

        lines = []
        for item in data[:25]:
            if not isinstance(item, dict):
                continue
            actor = item.get("actorDisplayName") or item.get("actorId", "unknown")
            event = item.get("eventType", "unknown")
            lines.append(f"• `{event}` von **{actor}**")

        await interaction.followup.send("\n".join(lines) or "ℹ️ Keine Logs gefunden.", ephemeral=True)

    @app_commands.command(name="vrc_invite", description="Sendet eine Invite in eine Instance.")
    async def vrc_invite(self, interaction: discord.Interaction, user_id: str, world_instance: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return

        clean_user_id = self._extract_user_id_from_target(user_id)
        payload = {"instanceId": world_instance}
        status, data = await self._request("POST", f"/invite/{clean_user_id}", json=payload)
        if status >= 400:
            return await interaction.followup.send(f"❌ Invite fehlgeschlagen: `{status}` {data}", ephemeral=True)

        await interaction.followup.send("✅ Invite gesendet.", ephemeral=True)

    @app_commands.command(name="vrcban", description="Bannt einen User in der konfigurierten Gruppe.")
    async def vrcban(self, interaction: discord.Interaction, target: str, reason: str = "No reason") -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return

        status, user_data = await self._resolve_user(target)
        if status >= 400 or not user_data:
            return await interaction.followup.send(f"❌ User lookup fehlgeschlagen: `{status}`", ephemeral=True)

        user_id = user_data.get("id")
        if not user_id:
            return await interaction.followup.send("❌ User-ID konnte nicht ermittelt werden.", ephemeral=True)

        payload = {"userId": user_id, "reason": reason}
        ban_status, ban_data = await self._request("POST", f"/groups/{self.group_id}/bans", json=payload)
        if ban_status >= 400:
            return await interaction.followup.send(f"❌ Ban fehlgeschlagen: `{ban_status}` {ban_data}", ephemeral=True)

        await interaction.followup.send(
            f"✅ Gebannt: **{user_data.get('displayName', user_id)}** | 18+ verified: **{'Ja' if self._is_vrc_18_verified(user_data) else 'Nein'}**",
            ephemeral=True,
        )

    @app_commands.command(name="vrc_tempban", description="Temporärer Ban (Minuten), mit Auto-Unban.")
    async def vrc_tempban(self, interaction: discord.Interaction, target: str, minutes: int, reason: str = "Temp Ban") -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return

        status, user_data = await self._resolve_user(target)
        if status >= 400 or not user_data:
            return await interaction.followup.send(f"❌ User lookup fehlgeschlagen: `{status}`", ephemeral=True)

        user_id = user_data.get("id")
        if not user_id:
            return await interaction.followup.send("❌ User-ID konnte nicht ermittelt werden.", ephemeral=True)

        payload = {"userId": user_id, "reason": reason}
        ban_status, ban_data = await self._request("POST", f"/groups/{self.group_id}/bans", json=payload)
        if ban_status >= 400:
            return await interaction.followup.send(f"❌ Temp-Ban fehlgeschlagen: `{ban_status}` {ban_data}", ephemeral=True)

        expires_at = int(time.time()) + max(1, minutes) * 60
        key = f"{self.group_id}:{user_id}"
        self.temp_bans[key] = {
            "group_id": self.group_id,
            "user_id": user_id,
            "expires_at": expires_at,
            "reason": reason,
            "moderator": str(interaction.user),
            "display_name": user_data.get("displayName", user_id),
        }
        self._save_temp_bans()

        await interaction.followup.send(
            f"✅ Temp-Ban gesetzt für **{user_data.get('displayName', user_id)}** bis <t:{expires_at}:F>.",
            ephemeral=True,
        )

    @app_commands.command(name="vrcunban", description="Entbannt einen User aus der konfigurierten Gruppe.")
    async def vrcunban(self, interaction: discord.Interaction, user_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return

        clean_user_id = self._extract_user_id_from_target(user_id)
        ok, message = await self._unban_vrc_user(self.group_id, clean_user_id)
        if not ok:
            return await interaction.followup.send(f"❌ Unban fehlgeschlagen: {message}", ephemeral=True)

        self.temp_bans.pop(f"{self.group_id}:{clean_user_id}", None)
        self._save_temp_bans()
        await interaction.followup.send("✅ User entbannt.", ephemeral=True)

    @app_commands.command(name="vrc_list_bans", description="Listet aktive Bans der konfigurierten Gruppe.")
    async def vrc_list_bans(self, interaction: discord.Interaction, limit: int = 20) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return

        status, data = await self._request("GET", f"/groups/{self.group_id}/bans", params={"n": max(1, min(limit, 100))})
        if status >= 400:
            return await interaction.followup.send(f"❌ Fehler: `{status}` {data}", ephemeral=True)
        if not isinstance(data, list) or not data:
            return await interaction.followup.send("ℹ️ Keine Bans gefunden.", ephemeral=True)

        lines = []
        for entry in data[:100]:
            if not isinstance(entry, dict):
                continue
            user_id = entry.get("userId", "?")
            display_name = entry.get("displayName") or entry.get("user", {}).get("displayName") or entry.get("targetDisplayName") or "Unknown"
            created_at = entry.get("createdAt") or entry.get("bannedAt") or "?"
            lines.append(f"• **{display_name}** (`{user_id}`) | {created_at}")

        await interaction.followup.send("\n".join(lines[:40]) or "ℹ️ Keine Bans gefunden.", ephemeral=True)

    @app_commands.command(name="vrc_modnote_add", description="Fügt Mod-Notiz zu VRChat User hinzu.")
    async def vrc_modnote_add(self, interaction: discord.Interaction, user_id: str, note: str) -> None:
        await interaction.response.defer(ephemeral=True)
        clean_user_id = self._extract_user_id_from_target(user_id)
        self.mod_notes.setdefault(clean_user_id, []).append(f"{int(time.time())}:{interaction.user}:{note}")
        self._save_mod_notes()
        await interaction.followup.send("✅ Mod-Notiz gespeichert.", ephemeral=True)

    @app_commands.command(name="vrc_modnote_list", description="Zeigt Mod-Notizen eines Users.")
    async def vrc_modnote_list(self, interaction: discord.Interaction, user_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        clean_user_id = self._extract_user_id_from_target(user_id)
        notes = self.mod_notes.get(clean_user_id, [])
        if not notes:
            return await interaction.followup.send("ℹ️ Keine Notizen.", ephemeral=True)
        await interaction.followup.send("\n".join([f"• {n}" for n in notes[-20:]]), ephemeral=True)

    @app_commands.command(name="vrc_modnote_clear", description="Löscht alle Mod-Notizen eines Users.")
    async def vrc_modnote_clear(self, interaction: discord.Interaction, user_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        clean_user_id = self._extract_user_id_from_target(user_id)
        self.mod_notes.pop(clean_user_id, None)
        self._save_mod_notes()
        await interaction.followup.send("✅ Mod-Notizen gelöscht.", ephemeral=True)

    @app_commands.command(name="vrc_remove_from_group", description="Entfernt User aus Gruppe.")
    async def vrc_remove_from_group(self, interaction: discord.Interaction, user_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return

        clean_user_id = self._extract_user_id_from_target(user_id)
        status, data = await self._request("DELETE", f"/groups/{self.group_id}/members/{clean_user_id}")
        if status >= 400:
            return await interaction.followup.send(f"❌ Entfernen fehlgeschlagen: `{status}` {data}", ephemeral=True)

        await interaction.followup.send("✅ User aus Gruppe entfernt.", ephemeral=True)

    @app_commands.command(name="vrc_group_member", description="Gruppenmitglied Infos holen.")
    async def vrc_group_member(self, interaction: discord.Interaction, user_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._ensure_authenticated(interaction):
            return

        clean_user_id = self._extract_user_id_from_target(user_id)
        status, data = await self._request("GET", f"/groups/{self.group_id}/members/{clean_user_id}")
        if status >= 400:
            return await interaction.followup.send(f"❌ Fehler: `{status}` {data}", ephemeral=True)

        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        await interaction.followup.send(f"```json\n{pretty[:1800]}\n```", ephemeral=True)

    @app_commands.command(name="vrc_pending_tempbans", description="Zeigt aktive Temp-Bans mit Auto-Unban Zeit.")
    async def vrc_pending_tempbans(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        active = [item for item in self.temp_bans.values() if item.get("group_id") == self.group_id]
        if not active:
            return await interaction.followup.send("ℹ️ Keine aktiven Temp-Bans.", ephemeral=True)

        lines = [
            f"• **{item.get('display_name', 'unknown')}** (`{item.get('user_id')}`) bis <t:{item.get('expires_at', 0)}:F>"
            for item in active
        ]
        await interaction.followup.send("\n".join(lines[:40]), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VRChatCog(bot))
