import asyncio
import random

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands


# --- UI KOMPONENTEN FÜR DIE SUCHE ---
class SearchDropdown(discord.ui.Select):
    def __init__(self, entries, cog, voice_client):
        self.entries = entries
        self.cog = cog
        self.voice_client = voice_client

        options = []
        for i, entry in enumerate(entries):
            # Maximal 100 Zeichen für den Titel, sonst meckert Discord
            title = entry.get("title", "Unbekanntes Video")[:90]
            uploader = entry.get("uploader", "")[:30]

            options.append(
                discord.SelectOption(
                    label=f"{i + 1}. {title}",
                    description=f"Kanal: {uploader}" if uploader else None,
                    value=str(i),
                    emoji="🎵",
                )
            )

        super().__init__(
            placeholder="Wähle das gewünschte Lied aus...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        # Wenn der User etwas auswählt:
        await interaction.response.defer()
        selected_index = int(self.values[0])
        selected_song = self.entries[selected_index]

        # Lied über unsere Hilfsfunktion zur Warteschlange hinzufügen
        await self.cog.process_songs(interaction, [selected_song], self.voice_client, is_dropdown=True)


class SearchView(discord.ui.View):
    def __init__(self, entries, cog, voice_client):
        super().__init__(timeout=60)  # Nach 60 Sekunden verschwindet das Menü
        self.add_item(SearchDropdown(entries, cog, voice_client))


# --- HAUPT-COG ---
class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues = {}
        self.current_song = {}
        self.skip_votes = {}
        self.repeat_mode = {}

        # --- EINSTELLUNGEN ---
        self.search_limit = 5  # 🟢 CONFIG: Wie viele Suchergebnisse im Dropdown angezeigt werden (Max. 25)
        self.max_playlist_items = 50  # harte Begrenzung für Playlist-Importe

        self.ytdl_options = {
            "format": "bestaudio/best",
            "noplaylist": False,
            "playlistend": 50,
            "quiet": True,
            "default_search": "auto",
            "source_address": "0.0.0.0",
            # Verhindert JS-Runtime-Warnungen bei Umgebungen ohne node/deno.
            "extractor_args": {"youtube": {"player_skip": ["js"]}},
        }
        self.ytdl = yt_dlp.YoutubeDL(self.ytdl_options)

        self.ffmpeg_options = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            "options": "-vn",
        }

    def play_next(self, guild, channel):
        guild_id = guild.id
        current = self.current_song.get(guild_id)

        if guild_id in self.skip_votes:
            self.skip_votes[guild_id].clear()

        if guild_id not in self.repeat_mode:
            self.repeat_mode[guild_id] = "off"

        if current and self.repeat_mode[guild_id] == "queue":
            self.queues.setdefault(guild_id, []).append(current)

        if current and self.repeat_mode[guild_id] == "song":
            next_song = current
            self.current_song[guild_id] = next_song
            voice_client = guild.voice_client
            if voice_client:
                player = discord.FFmpegPCMAudio(next_song["url"], **self.ffmpeg_options)
                voice_client.play(player, after=lambda e: self.play_next(guild, channel))
                embed = discord.Embed(
                    description=f"🔁 **Wiederholt:** `{next_song['title']}`", color=discord.Color.blurple()
                )
                asyncio.run_coroutine_threadsafe(channel.send(embed=embed), self.bot.loop)
            return

        if guild_id in self.queues and len(self.queues[guild_id]) > 0:
            next_song = self.queues[guild_id].pop(0)
            self.current_song[guild_id] = next_song

            voice_client = guild.voice_client
            if voice_client:
                player = discord.FFmpegPCMAudio(next_song["url"], **self.ffmpeg_options)
                voice_client.play(player, after=lambda e: self.play_next(guild, channel))

                embed = discord.Embed(
                    description=f"🎶 **Spielt jetzt:** `{next_song['title']}`", color=discord.Color.green()
                )
                asyncio.run_coroutine_threadsafe(channel.send(embed=embed), self.bot.loop)
        else:
            self.current_song[guild_id] = None

    async def process_songs(
        self,
        interaction: discord.Interaction,
        songs_to_add: list,
        voice_client: discord.VoiceClient,
        is_dropdown=False,
    ):
        """Hilfsfunktion: Fügt gefundene Lieder der Warteschlange hinzu und startet sie, falls nötig."""
        guild_id = interaction.guild.id

        if guild_id not in self.queues:
            self.queues[guild_id] = []
        if guild_id not in self.skip_votes:
            self.skip_votes[guild_id] = set()
        if guild_id not in self.repeat_mode:
            self.repeat_mode[guild_id] = "off"

        processed_songs = []
        for s in songs_to_add:
            processed_songs.append(
                {
                    "url": s["url"],
                    "title": s.get("title", "Unbekanntes Lied"),
                    "requester": interaction.user.mention,
                }
            )

        was_playing = voice_client.is_playing() or voice_client.is_paused()

        # Bot ist still? -> Sofort starten
        if not was_playing and processed_songs:
            first_song = processed_songs.pop(0)
            self.current_song[guild_id] = first_song
            player = discord.FFmpegPCMAudio(first_song["url"], **self.ffmpeg_options)
            voice_client.play(player, after=lambda e: self.play_next(interaction.guild, interaction.channel))

            embed = discord.Embed(
                description=f"🎶 **Spielt jetzt:** `{first_song['title']}`", color=discord.Color.green()
            )

        # Rest in die Queue
        if processed_songs:
            self.queues[guild_id].extend(processed_songs)

        # Die richtige Bestätigungsnachricht bauen
        if len(songs_to_add) > 1:
            embed = discord.Embed(
                description=(
                    f"📑 **Playlist geladen!** `{len(songs_to_add)}` Lieder wurden hinzugefügt.\n"
                    f"*Gesamt in der Queue: {len(self.queues[guild_id])}*"
                ),
                color=discord.Color.blue(),
            )
        elif was_playing:
            embed = discord.Embed(
                description=(
                    f"📝 **Zur Warteschlange hinzugefügt:** `{songs_to_add[0]['title']}`\n"
                    f"*Position: {len(self.queues[guild_id])}*"
                ),
                color=discord.Color.blue(),
            )

        # Nachricht senden
        if is_dropdown:
            await interaction.edit_original_response(content=None, embed=embed, view=None)
        else:
            await interaction.followup.send(embed=embed)

    @app_commands.command(name="play", description="Spielt ein Lied ab oder fügt eine Playlist hinzu.")
    @app_commands.describe(suche="YouTube Link, Suchbegriff oder Playlist-Link")
    async def play_cmd(self, interaction: discord.Interaction, suche: str):
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message(
                "❌ Du musst zuerst einem Voice Channel beitreten!", ephemeral=True
            )

        await interaction.response.defer()
        voice_channel = interaction.user.voice.channel
        voice_client = interaction.guild.voice_client

        if not voice_client:
            voice_client = await voice_channel.connect()
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)

        is_url = suche.startswith("http://") or suche.startswith("https://")

        if is_url:
            extract_query = suche
        else:
            # 🟢 Nutzt jetzt deine dynamische Config! (Gedeckelt auf maximal 25 wegen Discord)
            safe_limit = min(self.search_limit, 25)
            extract_query = f"ytsearch{safe_limit}:{suche}"

        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None, lambda: self.ytdl.extract_info(extract_query, download=False)
            )

            if not data:
                return await interaction.followup.send("❌ Nichts gefunden.")

            entries = [e for e in data.get("entries", []) if e]
            if not entries and data.get("url"):
                entries = [data]

            if is_url:
                if len(entries) > self.max_playlist_items:
                    entries = entries[: self.max_playlist_items]
                await self.process_songs(interaction, entries, voice_client)
            else:
                if not entries:
                    return await interaction.followup.send("❌ Keine passenden Lieder gefunden.")

                view = SearchView(entries, self, voice_client)
                await interaction.followup.send(
                    f"🔍 **Top {len(entries)} Ergebnisse gefunden!** Bitte wähle ein Lied aus:", view=view
                )

        except Exception as e:
            return await interaction.followup.send(f"❌ Fehler beim Laden: {e}")

    @app_commands.command(name="playlist", description="Lädt eine Playlist-URL in die Warteschlange.")
    @app_commands.describe(url="YouTube Playlist-Link")
    async def playlist_cmd(self, interaction: discord.Interaction, url: str):
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message(
                "❌ Du musst zuerst einem Voice Channel beitreten!", ephemeral=True
            )
        if "http" not in url:
            return await interaction.response.send_message("❌ Bitte eine gültige URL angeben.", ephemeral=True)

        await interaction.response.defer()
        voice_channel = interaction.user.voice.channel
        voice_client = interaction.guild.voice_client
        if not voice_client:
            voice_client = await voice_channel.connect()
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)

        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, lambda: self.ytdl.extract_info(url, download=False))
            entries = [e for e in data.get("entries", []) if e]
            if not entries:
                return await interaction.followup.send("❌ Keine Playlist-Einträge gefunden.")
            if len(entries) > self.max_playlist_items:
                entries = entries[: self.max_playlist_items]
                await interaction.followup.send(
                    f"ℹ️ Playlist wurde auf die ersten {self.max_playlist_items} Einträge begrenzt."
                )
            await self.process_songs(interaction, entries, voice_client)
        except Exception as e:
            await interaction.followup.send(f"❌ Playlist konnte nicht geladen werden: {e}")

    @app_commands.command(
        name="skip", description="Überspringt das aktuelle Lied (Admins sofort, User brauchen 3 Votes)."
    )
    async def skip_cmd(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_playing():
            return await interaction.response.send_message(
                "❌ Es läuft gerade gar keine Musik!", ephemeral=True
            )

        guild_id = interaction.guild.id
        is_admin = interaction.user.guild_permissions.administrator
        self.skip_votes.setdefault(guild_id, set())

        if is_admin:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="⏭️ **Admin-Skip!** Das Lied wurde sofort übersprungen.",
                    color=discord.Color.gold(),
                )
            )
            voice_client.stop()
            return

        if interaction.user.id in self.skip_votes[guild_id]:
            return await interaction.response.send_message(
                "❌ Du hast bereits für einen Skip abgestimmt!", ephemeral=True
            )

        self.skip_votes[guild_id].add(interaction.user.id)
        current_votes = len(self.skip_votes[guild_id])
        votes_needed = 3

        if current_votes >= votes_needed:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="⏭️ **Skip erfolgreich!** 3 User haben abgestimmt. Lied wird übersprungen.",
                    color=discord.Color.green(),
                )
            )
            voice_client.stop()
        else:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=f"🗳️ **Skip-Vote registriert!** ({current_votes}/{votes_needed} benötigten Stimmen)",
                    color=discord.Color.orange(),
                )
            )

    @app_commands.command(name="pause", description="Pausiert die aktuelle Musik.")
    async def pause_cmd(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            return await interaction.response.send_message("❌ Es läuft aktuell nichts.", ephemeral=True)
        vc.pause()
        await interaction.response.send_message("⏸️ Musik pausiert.")

    @app_commands.command(name="resume", description="Setzt pausierte Musik fort.")
    async def resume_cmd(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_paused():
            return await interaction.response.send_message("❌ Es ist nichts pausiert.", ephemeral=True)
        vc.resume()
        await interaction.response.send_message("▶️ Musik läuft weiter.")

    @app_commands.command(name="stop", description="Stoppt die Wiedergabe und leert die Queue.")
    async def stop_cmd(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        guild_id = interaction.guild.id
        if guild_id in self.queues:
            self.queues[guild_id].clear()
        self.current_song[guild_id] = None
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await interaction.response.send_message("⏹️ Wiedergabe gestoppt, Queue geleert.")

    @app_commands.command(name="shuffle", description="Mischt die aktuelle Warteschlange.")
    async def shuffle_cmd(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        queue = self.queues.get(guild_id, [])
        if len(queue) < 2:
            return await interaction.response.send_message("❌ Zu wenig Songs zum Mischen.", ephemeral=True)

        random.shuffle(queue)
        await interaction.response.send_message(f"🔀 Queue gemischt ({len(queue)} Songs).")

    @app_commands.command(name="repeat", description="Setzt den Repeat-Modus.")
    @app_commands.choices(
        modus=[
            app_commands.Choice(name="Aus", value="off"),
            app_commands.Choice(name="Song", value="song"),
            app_commands.Choice(name="Queue", value="queue"),
        ]
    )
    async def repeat_cmd(self, interaction: discord.Interaction, modus: app_commands.Choice[str]):
        guild_id = interaction.guild.id
        self.repeat_mode[guild_id] = modus.value
        labels = {"off": "Aus", "song": "Song", "queue": "Queue"}
        await interaction.response.send_message(f"🔁 Repeat-Modus: **{labels.get(modus.value, modus.value)}**")

    @app_commands.command(name="nowplaying", description="Zeigt den aktuell gespielten Song.")
    async def nowplaying_cmd(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        song = self.current_song.get(guild_id)
        if not song:
            return await interaction.response.send_message("❌ Aktuell läuft kein Song.", ephemeral=True)
        mode = self.repeat_mode.get(guild_id, "off")
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🎵 Jetzt läuft",
                description=f"`{song['title']}`\nAngefordert von: {song['requester']}\nRepeat: **{mode}**",
                color=discord.Color.green(),
            )
        )

    @app_commands.command(name="clearqueue", description="Leert die gesamte Warteschlange.")
    async def clearqueue_cmd(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        self.queues[guild_id] = []
        await interaction.response.send_message("🧹 Queue wurde geleert.")

    @app_commands.command(name="remove", description="Entfernt einen Song aus der Warteschlange per Position.")
    @app_commands.describe(position="Position aus /queue (beginnend bei 1)")
    async def remove_cmd(self, interaction: discord.Interaction, position: int):
        guild_id = interaction.guild.id
        queue = self.queues.get(guild_id, [])
        if position < 1 or position > len(queue):
            return await interaction.response.send_message("❌ Ungültige Position.", ephemeral=True)
        removed = queue.pop(position - 1)
        await interaction.response.send_message(f"🗑️ Entfernt: `{removed['title']}`")

    @app_commands.command(name="queue", description="Zeigt die aktuelle Warteschlange an.")
    async def queue_cmd(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        current = self.current_song.get(guild_id)

        if guild_id not in self.queues or len(self.queues[guild_id]) == 0:
            if current:
                return await interaction.response.send_message(
                    embed=discord.Embed(
                        description=(
                            f"🎶 **Aktuell läuft:** `{current['title']}`\n\n"
                            "*Die Warteschlange ist danach leer.*"
                        ),
                        color=discord.Color.blue(),
                    )
                )
            else:
                return await interaction.response.send_message(
                    "📭 Die Warteschlange ist komplett leer.", ephemeral=True
                )

        queue_list = ""
        for i, song in enumerate(self.queues[guild_id][:10]):
            queue_list += f"**{i + 1}.** `{song['title']}` (von {song['requester']})\n"

        if len(self.queues[guild_id]) > 10:
            queue_list += f"\n*...und {len(self.queues[guild_id]) - 10} weitere.*"

        embed = discord.Embed(
            title="📜 Musik Warteschlange",
            description=(
                f"🎶 **Aktuell:** `{current['title'] if current else 'Nichts'}`\n\n"
                f"**Als Nächstes:**\n{queue_list}"
            ),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leave", description="Stoppt die Musik und verlässt den Channel.")
    async def leave_cmd(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_connected():
            guild_id = interaction.guild.id
            if guild_id in self.queues:
                self.queues[guild_id].clear()
            self.current_song[guild_id] = None
            if guild_id in self.skip_votes:
                self.skip_votes[guild_id].clear()

            await voice_client.disconnect()
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="👋 **Tschüss!** Warteschlange geleert und Channel verlassen.",
                    color=discord.Color.red(),
                )
            )
        else:
            await interaction.response.send_message(
                "🤔 Ich bin doch gar nicht in einem Voice Channel?", ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(MusicCog(bot))
