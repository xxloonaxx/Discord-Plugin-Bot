import asyncio
import logging
import os
from pathlib import Path
import sys
from typing import Final
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import (
    DISCORD_GUILD_ID,
    DISCORD_TOKEN,
    GITHUB_UPDATE_BASE_RAW_URL,
    GITHUB_UPDATE_BRANCH,
    GITHUB_UPDATE_REPO,
    LOG_CHANNEL_ID,
)


# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("core-bot")
debug_enabled = False
runtime_log_channel_id = LOG_CHANNEL_ID

BASE_UPDATABLE_FILES: Final[dict[str, str]] = {
    "main.py": "main.py",
    "config.py": "config.py",
    "vip.py": "cogs/vip.py",
    "music.py": "cogs/music.py",
    "fun.py": "cogs/fun.py",
    "vrchat.py": "cogs/vrchat.py",
    "moderation.py": "cogs/moderation.py",
}
UPDATE_FILE_INDEX: dict[str, str] = dict(BASE_UPDATABLE_FILES)


def refresh_local_update_index() -> None:
    UPDATE_FILE_INDEX.clear()
    UPDATE_FILE_INDEX.update(BASE_UPDATABLE_FILES)
    for cog_file in Path("cogs").glob("*.py"):
        if cog_file.name.startswith("_"):
            continue
        UPDATE_FILE_INDEX[cog_file.name] = str(cog_file)


refresh_local_update_index()


def resolve_raw_base_url(branch_override: str | None = None) -> str:
    if GITHUB_UPDATE_BASE_RAW_URL:
        base = GITHUB_UPDATE_BASE_RAW_URL.rstrip("/")
        if "github.com" in base and "/tree/" in base:
            # allow normal GitHub branch links:
            # https://github.com/user/repo/tree/branch
            parsed = base.split("github.com/", 1)[1]
            repo_and_tree = parsed.split("/tree/", 1)
            if len(repo_and_tree) == 2:
                repo = repo_and_tree[0].strip("/")
                branch = branch_override or repo_and_tree[1].strip("/")
                return f"https://raw.githubusercontent.com/{repo}/{branch}"
        return base

    if not GITHUB_UPDATE_REPO:
        return ""
    branch = branch_override or GITHUB_UPDATE_BRANCH or "main"
    return f"https://raw.githubusercontent.com/{GITHUB_UPDATE_REPO.strip('/')}/{branch}"


def resolve_repo_and_branch(branch_override: str | None = None) -> tuple[str, str]:
    branch = branch_override or GITHUB_UPDATE_BRANCH or "main"
    if GITHUB_UPDATE_REPO:
        return GITHUB_UPDATE_REPO.strip("/"), branch

    base = GITHUB_UPDATE_BASE_RAW_URL.rstrip("/")
    if "github.com/" in base and "/tree/" in base:
        parsed = base.split("github.com/", 1)[1]
        repo_and_tree = parsed.split("/tree/", 1)
        if len(repo_and_tree) == 2:
            return repo_and_tree[0].strip("/"), branch
    if "raw.githubusercontent.com/" in base:
        parsed = base.split("raw.githubusercontent.com/", 1)[1].strip("/")
        parts = parsed.split("/")
        if len(parts) >= 3:
            return f"{parts[0]}/{parts[1]}", branch
    return "", branch


async def sync_remote_cog_index(branch_override: str | None = None) -> int:
    refresh_local_update_index()
    repo, branch = resolve_repo_and_branch(branch_override)
    if not repo:
        return 0

    api_url = f"https://api.github.com/repos/{repo}/git/trees/{quote(branch, safe='')}?recursive=1"
    added = 0
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(api_url) as response:
                if response.status != 200:
                    return 0
                payload = await response.json()
    except Exception:
        return 0

    for item in payload.get("tree", []):
        path = item.get("path", "")
        if item.get("type") == "blob" and path.startswith("cogs/") and path.endswith(".py"):
            file_name = Path(path).name
            if file_name not in UPDATE_FILE_INDEX:
                added += 1
            UPDATE_FILE_INDEX[file_name] = path
    return added


class DiscordLogHandler(logging.Handler):
    def __init__(self, queue: asyncio.Queue[str], loop_provider) -> None:
        super().__init__()
        self.queue = queue
        self.loop_provider = loop_provider

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            loop = self.loop_provider()
            if loop is None:
                return
            loop.call_soon_threadsafe(self.queue.put_nowait, message)
        except Exception:
            pass


# --- KONFIGURATION ---
if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN fehlt. Bitte als Umgebungsvariable setzen, z. B.\n"
        "export DISCORD_TOKEN='dein_token'"
    )
GUILD_ID_INT = DISCORD_GUILD_ID


# --- INTENTS ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class CoreBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="§", intents=intents, help_command=None)
        self.log_queue: asyncio.Queue[str] = asyncio.Queue()
        self.log_relay_task: asyncio.Task | None = None
        self.discord_log_handler = DiscordLogHandler(self.log_queue, lambda: self.loop)
        self.discord_log_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        logging.getLogger().addHandler(self.discord_log_handler)

    async def setup_hook(self) -> None:
        # --- AUTOMATISCHES LADEN ALLER COGS BEIM START ---
        cogs_dir = Path("./cogs")
        cogs_dir.mkdir(exist_ok=True)

        for cog_file in cogs_dir.glob("*.py"):
            if cog_file.name.startswith("_"):
                continue

            extension_name = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(extension_name)
                logger.info("Cog geladen: %s", cog_file.name)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Fehler beim Laden von %s: %s", cog_file.name, exc)

        # --- SLASH COMMANDS SYNCHRONISIEREN ---
        guild = discord.Object(id=GUILD_ID_INT)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        logger.info("%s Slash Commands für Guild %s synchronisiert.", len(synced), GUILD_ID_INT)
        if self.log_relay_task is None:
            self.log_relay_task = self.loop.create_task(self.log_relay_loop())

    async def log_relay_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            message = await self.log_queue.get()
            if not runtime_log_channel_id:
                continue
            channel = self.get_channel(runtime_log_channel_id)
            if channel is None:
                continue
            try:
                for chunk_start in range(0, len(message), 1800):
                    await channel.send(f"```log\n{message[chunk_start:chunk_start + 1800]}\n```")
            except Exception:
                continue


bot = CoreBot()


@bot.event
async def on_ready() -> None:
    if bot.user:
        logger.info("Bot ONLINE als: %s", bot.user)
    await bot.change_presence(activity=discord.Game(name="🟢 /help | System bereit"))


@bot.listen("on_app_command_completion")
async def on_app_command_completion(interaction: discord.Interaction, command) -> None:
    if debug_enabled:
        logger.info(
            "DEBUG command complete | guild=%s user=%s command=%s",
            interaction.guild_id,
            interaction.user,
            getattr(command, "name", "unknown"),
        )


# --- ADMIN VERWALTUNGS-BEFEHLE ---
@bot.tree.command(name="cog", description="Lade, entlade oder lade Cogs neu (Admin).")
@app_commands.default_permissions(administrator=True)
@app_commands.choices(
    action=[
        app_commands.Choice(name="Load (Neu laden)", value="load"),
        app_commands.Choice(name="Unload (Ausschalten)", value="unload"),
        app_commands.Choice(name="Reload (Aktualisieren)", value="reload"),
    ]
)
@app_commands.describe(action="Was möchtest du tun?", cog_name="Der Name der Datei (ohne .py)")
async def manage_cog(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    cog_name: str,
) -> None:
    await interaction.response.defer(ephemeral=True)

    try:
        extension_name = f"cogs.{cog_name}"
        if action.value == "load":
            await bot.load_extension(extension_name)
        elif action.value == "unload":
            await bot.unload_extension(extension_name)
        elif action.value == "reload":
            await bot.reload_extension(extension_name)

        guild = discord.Object(id=GUILD_ID_INT)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)

        await interaction.followup.send(
            embed=discord.Embed(
                description=(
                    f"✅ Datei `cogs/{cog_name}.py` wurde erfolgreich verarbeitet "
                    f"({action.value})."
                ),
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fehler bei Cog-Aktion %s für %s: %s", action.value, cog_name, exc)
        await interaction.followup.send(
            embed=discord.Embed(
                description=f"❌ **Fehler bei `cogs/{cog_name}.py`:**\n```{exc}```",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )


@bot.tree.command(name="list_cogs", description="Zeigt alle Cogs und ihren aktuellen Status an (Admin).")
@app_commands.default_permissions(administrator=True)
async def list_cogs_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    cogs_dir = Path("./cogs")
    cogs_dir.mkdir(exist_ok=True)

    available_cogs = [cog_file.stem for cog_file in cogs_dir.glob("*.py")]
    loaded_cogs = set(bot.extensions.keys())

    if not available_cogs:
        await interaction.followup.send("ℹ️ Keine Plugins im Ordner gefunden.", ephemeral=True)
        return

    lines = []
    for cog in sorted(available_cogs):
        extension_name = f"cogs.{cog}"
        is_loaded = extension_name in loaded_cogs
        emoji = "🟢" if is_loaded else "🔴"
        status = "Aktiv" if is_loaded else "Deaktiviert"
        lines.append(f"{emoji} **{cog}** *({status})*")

    embed = discord.Embed(
        title="⚙️ Plugin Übersicht",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="ping", description="Prüfe die Bot-Latenz.")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(f"🏓 Pong! `{round(bot.latency * 1000)}ms`", ephemeral=True)


@bot.tree.command(
    name="update_file",
    description="Lädt eine Datei von GitHub und aktualisiert sie lokal (Admin).",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    file_name="Datei aus Index, z. B. vip.py oder cogs/new_plugin.py",
    branch="Optionaler Branch-Name, z. B. codex/ubergeben-von-dateien-fur-bot-anc0ft",
)
async def update_file_cmd(
    interaction: discord.Interaction,
    file_name: str,
    branch: str | None = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    branch_name = branch.strip() if branch else None
    new_entries = await sync_remote_cog_index(branch_name)
    raw_base = resolve_raw_base_url(branch_override=branch_name)
    if not raw_base:
        await interaction.followup.send(
            "❌ Setze `GITHUB_UPDATE_BASE_RAW_URL` oder `GITHUB_UPDATE_REPO` in der .env.",
            ephemeral=True,
        )
        return

    requested = file_name.strip()
    target_rel_path = UPDATE_FILE_INDEX.get(requested, requested)
    if ".." in target_rel_path or target_rel_path.startswith("/"):
        await interaction.followup.send("❌ Ungültiger Dateipfad.", ephemeral=True)
        return

    raw_url = f"{raw_base.rstrip('/')}/{target_rel_path}"

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(raw_url) as response:
                if response.status != 200:
                    await interaction.followup.send(
                        f"❌ Download fehlgeschlagen ({response.status}) für:\n`{raw_url}`",
                        ephemeral=True,
                    )
                    return
                content = await response.text()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fehler beim Datei-Update von %s: %s", raw_url, exc)
        await interaction.followup.send(f"❌ Fehler beim Download: `{exc}`", ephemeral=True)
        return

    if not content.strip():
        await interaction.followup.send("❌ Dateiinhalt ist leer, Update abgebrochen.", ephemeral=True)
        return

    target_path = Path(target_rel_path)
    target_path.write_text(content, encoding="utf-8")

    if target_path.parts[0] == "cogs" and target_path.suffix == ".py":
        extension_name = f"cogs.{target_path.stem}"
        try:
            if extension_name in bot.extensions:
                await bot.reload_extension(extension_name)
            else:
                await bot.load_extension(extension_name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Datei aktualisiert, aber Cog-Reload fehlgeschlagen: %s", exc)
            await interaction.followup.send(
                (
                    f"⚠️ Datei `{target_rel_path}` wurde aktualisiert, "
                    f"aber Reload fehlgeschlagen:\n```{exc}```"
                ),
                ephemeral=True,
            )
            return

    await interaction.followup.send(
        (
            f"✅ `{target_rel_path}` wurde von GitHub aktualisiert.\n"
            f"Quelle: `{raw_url}`\n"
            f"Branch: `{branch_name or GITHUB_UPDATE_BRANCH or 'main'}`\n"
            f"Index-Update: `{new_entries}` neue Cog-Datei(en) erkannt.\n"
            "ℹ️ Bei `main.py`/`config.py` den Bot danach neu starten."
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="update_all",
    description="Aktualisiert alle indexierten Dateien von GitHub und startet den Bot neu.",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(branch="Optionaler Branch für den Komplett-Update-Lauf")
async def update_all_cmd(interaction: discord.Interaction, branch: str | None = None) -> None:
    await interaction.response.defer(ephemeral=True)
    branch_name = branch.strip() if branch else None
    new_entries = await sync_remote_cog_index(branch_name)
    raw_base = resolve_raw_base_url(branch_override=branch_name)
    if not raw_base:
        return await interaction.followup.send(
            "❌ Setze `GITHUB_UPDATE_BASE_RAW_URL` oder `GITHUB_UPDATE_REPO` in der .env.",
            ephemeral=True,
        )

    updated: list[str] = []
    failed: list[str] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        for key, target_rel_path in sorted(UPDATE_FILE_INDEX.items()):
            if ".." in target_rel_path or target_rel_path.startswith("/"):
                failed.append(f"{key} (invalid path)")
                continue
            raw_url = f"{raw_base.rstrip('/')}/{target_rel_path}"
            try:
                async with session.get(raw_url) as response:
                    if response.status != 200:
                        failed.append(f"{key} ({response.status})")
                        continue
                    content = await response.text()
            except Exception:
                failed.append(f"{key} (network)")
                continue

            if not content.strip():
                failed.append(f"{key} (empty)")
                continue

            Path(target_rel_path).write_text(content, encoding="utf-8")
            updated.append(target_rel_path)

    for rel_path in updated:
        target_path = Path(rel_path)
        if target_path.parts and target_path.parts[0] == "cogs" and target_path.suffix == ".py":
            extension_name = f"cogs.{target_path.stem}"
            try:
                if extension_name in bot.extensions:
                    await bot.reload_extension(extension_name)
                else:
                    await bot.load_extension(extension_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Reload fehlgeschlagen für %s: %s", extension_name, exc)

    await interaction.followup.send(
        (
            f"✅ Update-All abgeschlossen.\n"
            f"Branch: `{branch_name or GITHUB_UPDATE_BRANCH or 'main'}`\n"
            f"Neu erkannt: `{new_entries}`\n"
            f"Aktualisiert: `{len(updated)}`\n"
            f"Fehler: `{len(failed)}`\n"
            "🔁 Neustart wird jetzt ausgeführt..."
        ),
        ephemeral=True,
    )
    await asyncio.sleep(1.0)
    os.execv(sys.executable, [sys.executable, *sys.argv])


@bot.tree.command(name="list_update_files", description="Zeigt alle Dateien, die über /update_file gezogen werden können.")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(branch="Optionaler Branch für Remote-Cog-Scan")
async def list_update_files_cmd(interaction: discord.Interaction, branch: str | None = None) -> None:
    await interaction.response.defer(ephemeral=True)
    branch_name = branch.strip() if branch else None
    new_entries = await sync_remote_cog_index(branch_name)
    lines = [f"• `{name}` -> `{path}`" for name, path in sorted(UPDATE_FILE_INDEX.items())]
    await interaction.followup.send(
        (
            f"📦 **Update-Index** ({len(lines)} Dateien)\n"
            f"Neu erkannt: `{new_entries}`\n\n" + "\n".join(lines[:60])
        ),
        ephemeral=True,
    )


@bot.tree.command(name="set_status", description="Setzt den Bot-Status/Praesenz (Admin).")
@app_commands.default_permissions(administrator=True)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Playing", value="playing"),
        app_commands.Choice(name="Watching", value="watching"),
        app_commands.Choice(name="Listening", value="listening"),
        app_commands.Choice(name="Competing", value="competing"),
        app_commands.Choice(name="Clear", value="clear"),
    ]
)
@app_commands.describe(mode="Art des Status", text="Status-Text")
async def set_status_cmd(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    text: str | None = None,
) -> None:
    if mode.value == "clear":
        await bot.change_presence(activity=None)
        await interaction.response.send_message("✅ Status entfernt.", ephemeral=True)
        return

    if not text:
        await interaction.response.send_message("❌ Für diesen Modus ist ein Text nötig.", ephemeral=True)
        return

    activity: discord.BaseActivity
    if mode.value == "playing":
        activity = discord.Game(name=text)
    elif mode.value == "watching":
        activity = discord.Activity(type=discord.ActivityType.watching, name=text)
    elif mode.value == "listening":
        activity = discord.Activity(type=discord.ActivityType.listening, name=text)
    else:
        activity = discord.Activity(type=discord.ActivityType.competing, name=text)

    await bot.change_presence(activity=activity)
    await interaction.response.send_message(
        f"✅ Status gesetzt: **{mode.value}** → `{text}`",
        ephemeral=True,
    )


@bot.tree.command(name="debug_mode", description="Schaltet Debug-Logging an/aus (Terminal + Log-Channel).")
@app_commands.default_permissions(administrator=True)
@app_commands.choices(
    state=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off"),
    ]
)
async def debug_mode_cmd(interaction: discord.Interaction, state: app_commands.Choice[str]) -> None:
    global debug_enabled
    debug_enabled = state.value == "on"
    logging.getLogger().setLevel(logging.DEBUG if debug_enabled else logging.INFO)
    await interaction.response.send_message(
        f"✅ Debug-Modus ist jetzt **{state.value.upper()}**.",
        ephemeral=True,
    )


@bot.tree.command(name="set_log_channel", description="Setzt den dauerhaften Log-Channel.")
@app_commands.default_permissions(administrator=True)
async def set_log_channel_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
) -> None:
    global runtime_log_channel_id
    runtime_log_channel_id = channel.id
    await interaction.response.send_message(
        f"✅ Log-Channel gesetzt auf {channel.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="terminal_debug", description="Wichtige Terminal-Debug-Befehle ausführen (Admin).")
@app_commands.default_permissions(administrator=True)
@app_commands.choices(
    command=[
        app_commands.Choice(name="uptime", value="uptime"),
        app_commands.Choice(name="disk", value="disk"),
        app_commands.Choice(name="memory", value="memory"),
        app_commands.Choice(name="git_status", value="git_status"),
        app_commands.Choice(name="restart", value="restart"),
    ]
)
async def terminal_debug_cmd(interaction: discord.Interaction, command: app_commands.Choice[str]) -> None:
    await interaction.response.defer(ephemeral=True)
    if command.value == "restart":
        await interaction.followup.send("♻️ Restart über Terminal-Debug ausgelöst.", ephemeral=True)
        os.execv(sys.executable, [sys.executable, *sys.argv])
        return

    cmd_map = {
        "uptime": "uptime",
        "disk": "df -h",
        "memory": "free -h",
        "git_status": "git status --short",
    }
    shell_cmd = cmd_map[command.value]

    try:
        process = await asyncio.create_subprocess_shell(
            shell_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = (stdout + stderr).decode("utf-8", errors="replace").strip() or "(keine Ausgabe)"
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"❌ Fehler beim Ausführen: `{exc}`", ephemeral=True)
        return

    await interaction.followup.send(
        f"🖥️ `{shell_cmd}`\n```bash\n{output[:1800]}\n```",
        ephemeral=True,
    )


@bot.tree.command(name="restart_bot", description="Startet den Bot-Prozess neu (Admin).")
@app_commands.default_permissions(administrator=True)
async def restart_bot_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("♻️ Bot startet neu...", ephemeral=True)
    logger.warning("Restart wurde via Slash-Command ausgelöst von %s", interaction.user)
    os.execv(sys.executable, [sys.executable, *sys.argv])


def main() -> None:
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
