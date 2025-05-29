import random

import discord
from discord.ext import commands
from discord import app_commands
import os
from gtts import gTTS
#from pvrecorder import PVRecorder
from datetime import datetime
from dotenv import load_dotenv
import general

import time
from io import BytesIO

from pydantic.v1 import PathNotExistsError
from pydub import AudioSegment
from pydub.playback import play

import yt_dlp # NEW
from collections import deque # NEW
import asyncio # NEW

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.typing = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
now = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
channel_names_by_guild = {}
messages_buffer = {}
BUFFERS_LENGTH = 15

async def messages_handle_gejek(message):
    await message.channel.send("ty jesteś gejek")

command_map = {
    "gejek": messages_handle_gejek,
    "Gejek": messages_handle_gejek,
    "gejki": messages_handle_gejek,
    "gejuś": messages_handle_gejek,
}

async def handle_special_user(message):
    await message.channel.send("giga chad sie wypowiedział")

@bot.event
async def on_connect():
    print("Bot successfully connected!")

@bot.event
async def on_ready():
    print(f"Zalogowano jako: {bot.user}")
    await bot.tree.sync()

    for guild in bot.guilds:
        guild_buffers = general.initialize_guild_buffers(guild, channel_names_by_guild, BUFFERS_LENGTH)
        messages_buffer[guild.id] = guild_buffers
        await general.welcomeback_message(guild, bot)

    await bot.wait_until_ready()
    general.test_buffers(messages_buffer)

@bot.event
async def on_guild_join(guild):
    guild_buffers = general.initialize_guild_buffers(guild, channel_names_by_guild, BUFFERS_LENGTH)
    messages_buffer[guild.id] = guild_buffers

    await general.greeting_message(guild, bot)

    await bot.wait_until_ready()
    general.test_buffers(messages_buffer)

@bot.event
async def on_typing(channel, user, when):
    if channel.id == 279581584821190665 and not user.bot:
        await channel.send(f"{user.mention} właśnie wypisuje głupoty...", delete_after=3)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    log_path=f"G:\\DiscordBot\\servers\\{message.guild.name}\\{message.channel.name}.txt"
    log_message = f"{now} [{message.author}] {message.content}"

    if message.attachments:
        for attachment in message.attachments:
            general.append_to_file(log_path, (log_message + f"{attachment.url}"))
            print((log_message + f"{attachment.url}"))
    else:
        general.append_to_message_buffer(messages_buffer[message.guild.id], message.channel.name,
            log_path, log_message)

        print(log_message)
        print(
            f"\n - Channel: {message.channel.name}, Buffer length: {len(messages_buffer[message.guild.id][message.channel.name])}, "
            f"Buffer contents: {list(messages_buffer[message.guild.id][message.channel.name])}\n")

    # else:
    #     general.append_to_file(f"G:\\DiscordBot\\servers\\{message.guild.name}\\{message.channel.name}.txt\\",
    #                    f"{now} [{message.author}] {message.content}")
    #     print(f"[{now} [{message.author}] {message.content}")

   # if message.author.id == 120243473663262720:
   #     await handle_special_user(message)

    lowered = message.content.lower()
    for trigger, handler in command_map.items():
        if trigger in lowered:
            await handler(message)
            break

    await bot.process_commands(message)


@bot.command()
async def test(ctx, arg):
    if arg == "hello":
        await ctx.send("spierdalaj")
    else:
        await ctx.send("nie rozumie, to po polsku?")

@bot.tree.command(name="roll", description="Roll the dice!")
async def roll(interaction: discord.Interaction, start:int, stop:int):
    print("roll")
    result = random.randrange(start, stop)
    print("roll")
    await interaction.response.send_message(f"You have rolled {result}!")


bot.run(TOKEN)


