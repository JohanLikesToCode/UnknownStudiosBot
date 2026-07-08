import os
import sys
import discord
from discord.ext import commands
from discord import app_commands
import asyncio

print("Starting bot initialization...")

# ─── Configuration ────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = os.getenv('GUILD_ID')

if not DISCORD_TOKEN:
    print("ERROR: DISCORD_TOKEN not set!")
    sys.exit(1)

# ─── Discord Bot Setup ────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

class BlankBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        try:
            if GUILD_ID:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                print(f"Synced commands to guild {GUILD_ID}")
            else:
                await self.tree.sync()
                print("Synced commands globally")
        except Exception as e:
            print(f"Error syncing commands: {e}")

    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')
        print("Bot is ready - use /blank to create blank data messages")

bot = BlankBot()

# ─── Slash Command ────────────────────────────────────────────────────────────
@bot.tree.command(name="blank", description="Create a blank data message for hardcoding message IDs")
async def blank(interaction: discord.Interaction):
    """Creates a blank message that can be used as a data message for hardcoding IDs."""
    await interaction.response.defer(ephemeral=False)
    
    # Send the blank message in the channel where command was called
    blank_message = await interaction.channel.send("📋 **Data Message**\n*This message is used for data storage and will be referenced by message ID.*")
    
    # Respond to the user with the message ID
    await interaction.followup.send(
        f"✅ Blank data message created!\n"
        f"**Channel:** {interaction.channel.mention}\n"
        f"**Message ID:** `{blank_message.id}`\n"
        f"**Jump Link:** {blank_message.jump_url}\n\n"
        f"Use this ID in your code for hardcoded references.",
        ephemeral=True
    )

# ─── Keep-alive ───────────────────────────────────────────────────────────────
from flask import Flask
from threading import Thread

app_flask = Flask('')

@app_flask.route('/')
def home():
    return "Bot is online!"

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app_flask.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()

if __name__ == "__main__":
    keep_alive()
    print("Starting Discord bot...")
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print(f"Fatal error: {e}")
