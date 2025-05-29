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

def append_to_message_buffer(buffers, channel_name, path, message):
    try:
        if len(buffers[channel_name]) is buffers[channel_name].maxlen:
            append_buffer_to_file(buffers, channel_name, path)
            buffers[channel_name].clear()
        buffers[channel_name].append(message)

    except KeyError:
        print(f"Channel {channel_name} does not exist!")

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

