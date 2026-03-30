import random

import discord
from discord import app_commands
from discord.ext import commands


class FunCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- 8-BALL: FRAGE DAS SCHICKSAL ---
    @app_commands.command(name="8ball", description="Frage die magische Kugel eine Ja/Nein Frage.")
    @app_commands.describe(frage="Was möchtest du wissen?")
    async def eightball(self, interaction: discord.Interaction, frage: str):
        antworten = [
            "Ja, auf jeden Fall! ✅",
            "Es sieht sehr gut aus. ✨",
            "Frag mich später nochmal... 💤",
            "Darauf kannst du dich verlassen! 👍",
            "Vielleicht. 🤔",
            "Konzentrier dich und frag nochmal. 🔮",
            "Meine Quellen sagen: Nein. ❌",
            "Eher nicht... 📉",
            "Vergiss es. 🛑",
            "Niemals! 💀",
        ]
        embed = discord.Embed(title="🔮 Die Magische Kugel spricht...", color=discord.Color.purple())
        embed.add_field(name="Deine Frage:", value=frage, inline=False)
        embed.add_field(name="Antwort:", value=random.choice(antworten), inline=False)
        await interaction.response.send_message(embed=embed)

    # --- USERINFO: WER IST DAS EIGENTLICH? ---
    @app_commands.command(name="userinfo", description="Zeigt Infos über einen User an.")
    @app_commands.describe(member="Wähle einen User (leer lassen für dich selbst)")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user

        embed = discord.Embed(title=f"👤 Infos über {member.display_name}", color=member.color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=member.id, inline=True)
        embed.add_field(name="Server beigetreten", value=member.joined_at.strftime("%d.%m.%Y"), inline=True)
        embed.add_field(name="Account erstellt", value=member.created_at.strftime("%d.%m.%Y"), inline=True)

        rollen = [role.mention for role in member.roles if role != interaction.guild.default_role]
        embed.add_field(
            name=f"Rollen ({len(rollen)})",
            value=" ".join(rollen) if rollen else "Keine",
            inline=False,
        )

        await interaction.response.send_message(embed=embed)

    # --- MÜNZWURF ---
    @app_commands.command(name="münze", description="Wirf eine Münze (Kopf oder Zahl).")
    async def coinflip(self, interaction: discord.Interaction):
        ergebnis = random.choice(["Kopf 🪙", "Zahl 🪙"])
        await interaction.response.send_message(
            f"Die Münze wirbelt durch die Luft und landet auf... **{ergebnis}**!"
        )

    # --- AVATAR: ZEIG MIR DEIN BILD ---
    @app_commands.command(name="avatar", description="Zeigt das Profilbild eines Users groß an.")
    async def avatar(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        embed = discord.Embed(title=f"Bild von {member.display_name}", color=discord.Color.blue())
        embed.set_image(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    # --- SLAP: JEMANDEN KLATSCHEN ---
    @app_commands.command(name="slap", description="Verpasse jemandem eine virtuelle Backpfeife.")
    @app_commands.describe(member="Wen möchtest du slappen?")
    async def slap(self, interaction: discord.Interaction, member: discord.Member):
        if member == interaction.user:
            return await interaction.response.send_message(
                "Warum willst du dich selbst schlagen? 🥺", ephemeral=True
            )

        antworten = [
            f"{interaction.user.mention} verpasst {member.mention} eine saftige Backpfeife! 🖐️💥",
            f"{interaction.user.mention} schlägt {member.mention} mit einem nassen Fisch ins Gesicht! 🐟",
            f"{interaction.user.mention} slappt {member.mention} so fest, dass er sich einmal im Kreis dreht! 💫",
        ]
        await interaction.response.send_message(random.choice(antworten))

    # --- LIEBESTEST ---
    @app_commands.command(name="lovecheck", description="Testet die Liebe zwischen zwei Usern.")
    async def love(self, interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
        prozent = random.randint(0, 100)

        if prozent > 85:
            emoji = "💖 😍 💖"
        elif prozent > 50:
            emoji = "❤️"
        elif prozent > 20:
            emoji = "⚖️"
        else:
            emoji = "💔"

        embed = discord.Embed(title="💘 Liebes-Barometer 💘", color=discord.Color.red())
        embed.add_field(name="Partner 1", value=user1.mention, inline=True)
        embed.add_field(name="Partner 2", value=user2.mention, inline=True)
        embed.add_field(name="Ergebnis", value=f"**{prozent}%**\n{emoji}", inline=False)
        await interaction.response.send_message(embed=embed)

    # --- WÜRFELN ---
    @app_commands.command(name="würfel", description="Wirf einen Würfel (1-6 oder eigene Zahl).")
    @app_commands.describe(seiten="Wie viele Seiten soll der Würfel haben? (Standard 6)")
    async def roll(self, interaction: discord.Interaction, seiten: int = 6):
        if seiten < 2:
            return await interaction.response.send_message(
                "Ein Würfel braucht mindestens 2 Seiten! 🎲", ephemeral=True
            )
        ergebnis = random.randint(1, seiten)
        await interaction.response.send_message(f"🎲 Du hast eine **{ergebnis}** gewürfelt (W{seiten}).")


async def setup(bot):
    await bot.add_cog(FunCog(bot))
