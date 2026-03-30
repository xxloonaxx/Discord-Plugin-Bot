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
)


# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("core-bot")

BASE_UPDATABLE_FILES: Final[dict[str, str]] = {
    "main.py": "main.py",
    "config.py": "config.py",
    "vip.py": "cogs/vip.py",
    "music.py": "cogs/music.py",
    "fun.py": "cogs/fun.py",
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


bot = CoreBot()


@bot.event
async def on_ready() -> None:
    if bot.user:
        logger.info("Bot ONLINE als: %s", bot.user)
    await bot.change_presence(activity=discord.Game(name="🟢 /help | System bereit"))


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
