import discord
from discord.ext import commands
from discord import app_commands
import os
from gtts import gTTS
#from pvrecorder import PVRecorder
import datetime
from openai import OpenAI
from dotenv import load_dotenv

import time
from io import BytesIO

from pydantic.v1 import PathNotExistsError
from pydub import AudioSegment
from pydub.playback import play
from collections import deque
import re
import asyncio
import yt_dlp

BUFFERS_LENGTH = 15


async def greeting_message(guild, bot):
    bot_member = guild.get_member(bot.user.id)
    if bot_member is None:
        print("Bot member not found in guild!")
        return

    for channel in guild.text_channels:
        if channel.permissions_for(bot_member).send_messages:
            await channel.send("Thanks for adding me!")
            break

async def welcomeback_message(guild, bot):
    bot_member = guild.get_member(bot.user.id)
    if bot_member is None:
        print("Bot member not found in guild!")
        return

    for channel in guild.text_channels:
        if channel.permissions_for(bot_member).send_messages:
            await channel.send("And..... I'm back!", delete_after=7)
            break

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

def create_message_buffers(channel_names, buffers_length):
    buffers = {buffer_name: deque(maxlen=buffers_length) for buffer_name in channel_names}
    return buffers

def append_to_message_buffer(messages_buffer, guild_id, channel_name, message, maxlen=30):
    if guild_id not in messages_buffer:
        messages_buffer[guild_id] = {}

    if channel_name not in messages_buffer[guild_id]:
        messages_buffer[guild_id][channel_name] = deque(maxlen=maxlen)

    messages_buffer[guild_id][channel_name].append(message)

def append_buffer_to_file(buffers, channel_name, path):
    try:
        with open(path, "a", encoding="utf-8") as f:
            while True:
                f.write(buffers[channel_name].popleft() + "\n")
    except KeyError:
        print(f"Brak bufora dla pliku: {path}")
    except IndexError:
        pass

def test_buffers(messages_buffer):
    if not messages_buffer:
        print("No buffers found. messages_buffer is empty.")
        return

    for guild_id, channels in messages_buffer.items():
        print(f"\n Guild ID: {guild_id}")
        if not channels:
            print("No channels found in this guild.")
            continue

        for channel_name, buffer in channels.items():
            print(f"   - Channel: {channel_name}, Buffer length: {len(buffer)}, Buffer contents: {list(buffer)}")

def initialize_guild_buffers(guild, channel_names_by_guild, BUFFERS_LENGTH):
    create_folder(f"G:\\DiscordBot\\servers\\{safe_name(guild.name)}")
    print(f"Serwer: {guild.name}")
    print("Kanały tekstowe:")
    channel_names = []

    for channel in guild.text_channels:
        channel_names.append(channel.name)
        print(f" - {channel.name}")
        create_textfile(f"G:\\DiscordBot\\servers\\{safe_name(guild.name)}\\{safe_name(channel.name)}.txt")

    channel_names_by_guild[guild.id] = channel_names

    guild_buffers = create_message_buffers(channel_names_by_guild[guild.id], BUFFERS_LENGTH)
    return guild_buffers

def safe_name(name):
    return re.sub(r'[\\/*?:"<>|]', '_', name)

async def search_ytdlp_async(query, ydl_opts):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))

def _extract(query, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)

def test_dynamic_buffering():
    from collections import defaultdict

    # Simulate messages_buffer structure: guild_id -> channel_name -> deque
    messages_buffer = defaultdict(dict)
    guild_id = 1234567890
    BUFFERS_LENGTH = 5

    # Simulated message input (channel_name: [messages])
    test_data = {
        "general": ["Hello", "How are you?", "Test1", "Test2", "Test3"],
        "bot-commands": ["!play song", "!pause", "!resume"],
    }
def safe_append_to_buffer(messages_buffer, guild_id, channel_name, message, maxlen=30):
    if guild_id not in messages_buffer:
        messages_buffer[guild_id] = {}

    if channel_name not in messages_buffer[guild_id]:
        messages_buffer[guild_id][channel_name] = deque(maxlen=maxlen)

    messages_buffer[guild_id][channel_name].append(message)

