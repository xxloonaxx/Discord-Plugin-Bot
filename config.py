import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    # Optional: Bot funktioniert auch ohne python-dotenv, wenn ENV extern gesetzt ist.
    pass
# --- CORE BOT ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "1333922279498711060"))

# --- GITHUB / GIST ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_UPDATE_BASE_RAW_URL = os.getenv("GITHUB_UPDATE_BASE_RAW_URL", "")
GITHUB_UPDATE_REPO = os.getenv("GITHUB_UPDATE_REPO", "")
GITHUB_UPDATE_BRANCH = os.getenv("GITHUB_UPDATE_BRANCH", "main")
GIST_VIP_ID = "342809965bc1e29db5aabd1b95c804ef"
GIST_LOG_ID = "dcb2bc939f773205552c2d0d42a65c09"
GIST_BLACKLIST_ID = "d9ce07b1341d8968579e20645073ed62"

# --- CHANNELS ---
PANEL_CHANNEL_ID = 1484380899591065610
BAN_LOG_CHANNEL_ID = 1335195263912120402
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# --- VRCHAT ---
VRCHAT_API_BASE_URL = os.getenv("VRCHAT_API_BASE_URL", "https://api.vrchat.cloud/api/1")
VRCHAT_USER_AGENT = os.getenv(
    "VRCHAT_USER_AGENT",
    "DiscordPluginBot/1.0 (contact: discord.gg/yourserver | admin@example.com)",
)
VRCHAT_GROUP_ID = os.getenv("VRCHAT_GROUP_ID", "grp_998c5578-c65c-4d20-b1e5-70cf3a724f32")
VRCHAT_AUDIT_LOG_CHANNEL_ID = int(os.getenv("VRCHAT_AUDIT_LOG_CHANNEL_ID", "0"))
VRCHAT_INVITE_CHANNEL_ID = int(os.getenv("VRCHAT_INVITE_CHANNEL_ID", "0"))
VRCHAT_STAFF_ROLE_IDS: list[int] = []
for _role_token in os.getenv("VRCHAT_STAFF_ROLE_IDS", "1484342349633945711").split(","):
    _role_token = _role_token.strip()
    if not _role_token:
        continue
    try:
        VRCHAT_STAFF_ROLE_IDS.append(int(_role_token))
    except ValueError:
        continue

# --- ROLES ---
ROLE_18_PLUS = 1333922279532396567
ROLES_STAFF = [1484342349633945711]
ROLES_VIP_PLUS = [1479675805289414807, 1335235137239126016]
ROLES_VIP = [1333922279553499195, 1333922279553499196]
ALL_VIP_ROLES = ROLES_STAFF + ROLES_VIP_PLUS + ROLES_VIP
