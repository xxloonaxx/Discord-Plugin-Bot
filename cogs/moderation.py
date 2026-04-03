import discord
from datetime import timedelta
from discord import app_commands
from discord.ext import commands

from config import MOD_LOG_CHANNEL_NSFW, MOD_LOG_CHANNEL_SFW, NSFW_SERVER_ID


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _log_channel_id_for_guild(self, guild_id: int) -> int:
        return MOD_LOG_CHANNEL_NSFW if guild_id == NSFW_SERVER_ID else MOD_LOG_CHANNEL_SFW

    async def _send_mod_log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        channel = self.bot.get_channel(self._log_channel_id_for_guild(guild.id))
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

    async def _check_perms(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if isinstance(user, discord.Member) and (user.guild_permissions.administrator or user.guild_permissions.ban_members):
            return True
        if interaction.response.is_done():
            await interaction.followup.send("❌ Keine Berechtigung.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        return False

    @app_commands.command(name="mod_purge", description="Löscht Nachrichten im aktuellen Channel.")
    async def mod_purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 200]) -> None:
        if not await self._check_perms(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"✅ {len(deleted)} Nachrichten gelöscht.", ephemeral=True)

    @app_commands.command(name="mod_timeout", description="Timeout für einen User (Minuten).")
    async def mod_timeout(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason") -> None:
        if not await self._check_perms(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        until = discord.utils.utcnow() + timedelta(minutes=minutes)
        await member.edit(timed_out_until=until, reason=reason)
        await interaction.followup.send(f"✅ {member.mention} hat Timeout für {minutes} Minuten.", ephemeral=True)

        embed = discord.Embed(title="🔨 Timeout", color=discord.Color.orange())
        embed.add_field(name="User", value=f"{member} ({member.id})", inline=False)
        embed.add_field(name="Mod", value=f"{interaction.user} ({interaction.user.id})", inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        await self._send_mod_log(interaction.guild, embed)

    @app_commands.command(name="mod_untimeout", description="Entfernt Timeout von einem User.")
    async def mod_untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason") -> None:
        if not await self._check_perms(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await member.edit(timed_out_until=None, reason=reason)
        await interaction.followup.send(f"✅ Timeout entfernt für {member.mention}.", ephemeral=True)

    @app_commands.command(name="mod_kick", description="Kickt einen User.")
    async def mod_kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason") -> None:
        if not await self._check_perms(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await member.kick(reason=reason)
        await interaction.followup.send(f"✅ {member} wurde gekickt.", ephemeral=True)

    @app_commands.command(name="mod_ban", description="Bannt einen User.")
    async def mod_ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason") -> None:
        if not await self._check_perms(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.guild.ban(member, reason=reason, delete_message_days=0)
        await interaction.followup.send(f"✅ {member} wurde gebannt.", ephemeral=True)

    @app_commands.command(name="mod_unban", description="Entbannt einen User per User-ID.")
    async def mod_unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason") -> None:
        if not await self._check_perms(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        user = await self.bot.fetch_user(int(user_id))
        await interaction.guild.unban(user, reason=reason)
        await interaction.followup.send(f"✅ {user} wurde entbannt.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))
