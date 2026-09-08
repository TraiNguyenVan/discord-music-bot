import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "")
ACTIVITY = os.getenv("ACTIVITY_NAME", "")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.voice_states = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
    if ACTIVITY:
        bot.activity = discord.Activity(type=discord.ActivityType.listening, name=ACTIVITY)
    return bot


bot = build_bot()


@bot.event
async def on_interaction(inter: discord.Interaction):
    # Usage log: every slash command + button press, one line each.
    try:
        guild = inter.guild.id if inter.guild else "DM"
        user = f"{inter.user}({inter.user.id})"
        if inter.type == discord.InteractionType.application_command:
            data = inter.data or {}
            name = inter.command.name if inter.command else str(data.get("name", "?"))
            opts = data.get("options", []) if isinstance(data, dict) else []
            params = " ".join(f'{o.get("name")}={o.get("value")}' for o in opts) if opts else "-"
            print(f"[usage] /{name} {params} | guild={guild} user={user}", flush=True)
        elif inter.type == discord.InteractionType.component:
            data = inter.data or {}
            print(f"[usage] [button:{data.get('custom_id', '?')}] | guild={guild} user={user}", flush=True)
    except Exception:
        pass


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id if bot.user else '?'})")
    try:
        await bot.load_extension("cogs.music")
    except commands.ExtensionAlreadyLoaded:
        pass
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:  # noqa: BLE001
        print(f"Slash sync failed: {e}")
    print("Ready. Join a voice channel, then use /play.")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN. Copy .env.example to .env and set it.")
    import logging

    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
    bot.run(TOKEN)
