import random

import discord
from discord.ext import commands
from discord import app_commands
import os
from gtts import gTTS
#from pvrecorder import PVRecorder
from datetime import datetime
from dotenv import load_dotenv

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

async def messages_handle_gejek(message):
    await message.channel.send("jurando gej")

command_map = {
    "jurando1": messages_handle_gejek,
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

    for guild in bot.guilds:
        create_folder(f"G:\\DiscordBot\\servers\\{guild.name}")

        print(f"Serwer: {guild.name}")
        print("Kanały tekstowe:")

        for channel in guild.text_channels:
            create_textfile(f"G:\\DiscordBot\\servers\\{guild.name}\\{channel.name}.txt")
            channel_names = [channel.name]
            channel_names_by_guild[guild.id] = channel_names
            for name in channel_names:
                print(f" - {name}")

def create_folder(path):
    if not os.path.exists(path):
        os.makedirs(path)
    else:
        print(f"Directory {path} already exists!")

def create_textfile(path):
    try:
        with open(path, "x") as file:
            file.write("")
        print(f"Text file {path} successfully created!")
    except FileExistsError:
        #print(f"File {path} already exists!")
        pass

def append_to_file(path, message):
    try:
        with open(path, "a") as file:
            file.write(message + "\n")
    except FileExistsError:
        print(f"File {path} already exists!")
    except PathNotExistsError:
        print(f"Path {path} does not exist!")


@bot.event
async def on_typing(channel, user, when):
    if channel.id == 279581584821190665 and not user.bot:
        await channel.send(f"{user.name} właśnie wypisuje głupoty...")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.attachments:
        for attachment in message.attachments:
            append_to_file(f"G:\\DiscordBot\\servers\\{message.guild.name}\\{message.channel.name}.txt\\",
                           f"{now} [{message.author}] {message.content} {attachment.url}")
            print(f"{now} [{message.author}] {message.content} {attachment.url}")
    else:
        append_to_file(f"G:\\DiscordBot\\servers\\{message.guild.name}\\{message.channel.name}.txt\\",
                       f"{now} [{message.author}] {message.content}")
        print(f"[{now} [{message.author}] {message.content}")

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
async def roll(interaction: discord.Interaction, start: int, stop: int):
    result = random.randrange(start, stop)
    await interaction.response.send_message(f"You have rolled {result}!")



bot.run(TOKEN)


