import os

BOT_TOKEN = os.environ["BOT_TOKEN"]  # обязательная env var
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
LINK_TTL_HOURS = int(os.environ.get("LINK_TTL_HOURS", "48"))
MAX_LINKS_PER_USER = int(os.environ.get("MAX_LINKS_PER_USER", "50"))
