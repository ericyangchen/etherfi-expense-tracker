import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://etherfi:etherfi_local@localhost:5432/etherfi",
)
AUTH_STATE_PATH = os.path.join(os.path.dirname(__file__), "data", "auth_state.json")

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
