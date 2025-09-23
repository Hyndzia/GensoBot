import random
from flask import Flask
import threading
import discord
from discord.ext import commands
from discord import app_commands
import os
from datetime import datetime
from dotenv import load_dotenv
import general
import nacl
import time
from io import BytesIO

import yt_dlp

from pydantic.v1 import PathNotExistsError
from pydub import AudioSegment
from pydub.playback import play

from collections import deque
import asyncio

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.typing = True
intents.guilds = True
intents.messages = True

SONG_QUEUES = {}

bot = commands.Bot(command_prefix="!", intents=intents)
now = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
channel_names_by_guild = {}
messages_buffer = {}
messages_buffer2 = {}
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
        #await general.welcomeback_message(guild, bot)

    await bot.wait_until_ready()
    general.test_buffers(messages_buffer)

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!", 200

def run_web():
    app.run(host="0.0.0.0", port=7777)

threading.Thread(target=run_web).start()

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
        await channel.send(f"{user.mention} właśnie wypisuje głupoty...", delete_after=4)

@bot.event
async def on_message(message):
    messages_buffer2[message.guild.id] = {}
    if message.author == bot.user:
        return

    #log_path=f"G:\\DiscordBot\\servers\\{message.guild.name}\\{message.channel.name}.txt"
    
    base_dir = os.path.dirname(os.path.abspath(__file__))  # folder where main.py lives
    log_path = os.path.join(base_dir, "servers", message.guild.name, f"{message.channel.name}.txt")

    # make sure the parent folders exist before writing
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    log_message = f"{now} [{message.author}] {message.content}"

    if message.attachments:
        for attachment in message.attachments:
            general.append_to_file(log_path, (log_message + f"{attachment.url}"))
            print((log_message + f"{attachment.url}"))
    else:
        general.append_to_message_buffer(messages_buffer2, message.guild.id, message.channel.name, log_message, 10)

        print(log_message)
        buffer = messages_buffer[message.guild.id][message.channel.name]
        print(
            f"\n - Channel: {message.channel.name}, Buffer length: {len(buffer)}, Buffer contents: {list(buffer)}"
        )

    lowered = message.content.lower()
    for trigger, handler in command_map.items():
        if trigger in lowered:
            await handler(message)
            break

    await bot.process_commands(message)
    # At the end of on_message:
    general.test_buffers({message.guild.id: messages_buffer2[message.guild.id]})


@bot.command()
async def test(ctx, arg):
    if arg == "hello":
        await ctx.send("")
    else:
        await ctx.send("nie rozumie, to po polsku?")

@bot.tree.command(name="roll", description="Roll the dice!")
async def roll(interaction: discord.Interaction, start:int, stop:int):
    print("roll")
    result = random.randrange(start, stop)
    print("roll")
    await interaction.response.send_message(f"{interaction.user.mention} has rolled {result}!")

@bot.tree.command(name="skip", description="Skips the current playing song")
async def skip(interaction: discord.Interaction):
    if interaction.guild.voice_client and (
            interaction.guild.voice_client.is_playing() or interaction.guild.voice_client.is_paused()):
        interaction.guild.voice_client.stop()
        await interaction.response.send_message("Skipped the current song.")
    else:
        await interaction.response.send_message("Not playing anything to skip.")

@bot.tree.command(name="pause", description="Pause the currently playing song.")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    # Check if the bot is in a voice channel
    if voice_client is None:
        return await interaction.response.send_message("I'm not in a voice channel.")

    # Check if something is actually playing
    if not voice_client.is_playing():
        return await interaction.response.send_message("Nothing is currently playing.")

    # Pause the track
    voice_client.pause()
    await interaction.response.send_message("Playback paused!")

@bot.tree.command(name="resume", description="Resume the currently paused song.")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    # Check if the bot is in a voice channel
    if voice_client is None:
        return await interaction.response.send_message("I'm not in a voice channel.")

    # Check if it's actually paused
    if not voice_client.is_paused():
        return await interaction.response.send_message("I’m not paused right now.")

    # Resume playback
    voice_client.resume()
    await interaction.response.send_message("Playback resumed!")



@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    # Check connection first
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("I'm not connected to any voice channel.")
        return

    # Stop playback and clear queue
    guild_id_str = str(interaction.guild_id)
    if guild_id_str in SONG_QUEUES:
        SONG_QUEUES[guild_id_str].clear()

    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    # Send confirmation first BEFORE disconnecting
    await interaction.response.send_message("Stopped playback, cleared the queue, and disconnected!")

    # Then disconnect safely
    await voice_client.disconnect()





#YDL_OPTIONS = {
#    "format": "bestaudio/best",
#    "noplaylist": True,
#    "youtube_include_dash_manifest": False,
#    "youtube_include_hls_manifest": False,
#    "extract_flat": False,
#    "source_address" : "0.0.0.0",
#    "default_search": "ytsearch1",
#    "cookiefile": "/home/debian/GensoBot/ck.txt",
#}
 

YDL_OPTIONS = {
    "format": "bestaudio/best",
    #"format": "bestaudio[ext=webm]/bestaudio/best",
    
    "noplaylist": True,
    #"quiet": True,
    "extract_flat": False,
    "default_search": "ytsearch1",
    "skip_download": True,
    "ignore-errors": True,
    "nocheckcertificate": True,
    "cookiefile": "/home/debian/GensoBot/ck.txt",
}




async def extract_info_async(query):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_OPTIONS).extract_info(query, download=False))

@bot.tree.command(name="play", description="Play a song or playlist, or add it to the queue.")
@app_commands.describe(song_query="Search query or playlist URL")
async def play(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()

    if interaction.user.voice is None or interaction.user.voice.channel is None:
        await interaction.followup.send("You must be in a voice channel to use this command.")
        return

    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_channel != voice_client.channel:
        await voice_client.move_to(voice_channel)

    # extract track or playlist info
    info = await extract_info_async(song_query)

    guild_id = str(interaction.guild_id)
    if SONG_QUEUES.get(guild_id) is None:
        SONG_QUEUES[guild_id] = deque()

    added_titles = []

    if "entries" in info:  # playlist or search result
        for entry in info["entries"]:
            if entry:
                url = entry.get("url")
                title = entry.get("title", "Untitled")
                SONG_QUEUES[guild_id].append((url, title))
                added_titles.append(title)
    else:  # single track
        url = info["url"]
        title = info.get("title", "Untitled")
        SONG_QUEUES[guild_id].append((url, title))
        added_titles.append(title)

    # feedback to user
    if len(added_titles) > 1:
        await interaction.followup.send(f"🎶 Added **{len(added_titles)}** tracks to the queue.")
    else:
        if voice_client.is_playing() or voice_client.is_paused():
            await interaction.followup.send(f"Added to queue: **{added_titles[0]}**")
        else:
            await interaction.followup.send(f"Now playing: **{added_titles[0]}**")
            await play_next_song(voice_client, guild_id, interaction.channel, True)

async def play_next_song(voice_client, guild_id, channel, _msg_flag):
    if SONG_QUEUES[guild_id]:
        audio_url, title = SONG_QUEUES[guild_id].popleft()

        ffmpeg_options = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            "options": "-vn",
            #"options": "-vn -c:a libopus -b:a 96k",
            # Remove executable if FFmpeg is in PATH
        }

        source = discord.FFmpegOpusAudio(audio_url, **ffmpeg_options)

        def after_play(error):
            if error:
                print(f"Error playing {title}: {error}")
            asyncio.run_coroutine_threadsafe(play_next_song(voice_client, guild_id, channel, _msg_flag), bot.loop)

        voice_client.play(source, after=after_play)
        if _msg_flag is False:
            asyncio.create_task(channel.send(f"Now playing: **{title}**"))
        else:
            _msg_flag = False
    else:
        await voice_client.disconnect()
        SONG_QUEUES[guild_id] = deque()


@bot.tree.command(name="radio", description="Play the Shinpu radio stream")
async def radio(interaction: discord.Interaction):
    # defer the response (keeps the interaction alive while we join/connect)
    await interaction.response.defer()

    # require the user to be in a voice channel
    if interaction.user.voice is None or interaction.user.voice.channel is None:
        await interaction.followup.send("You must be in a voice channel to use this command.")
        return

    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    # join or move to the user's voice channel
    try:
        if voice_client is None:
            voice_client = await voice_channel.connect()
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)
    except Exception as e:
        await interaction.followup.send(f"Couldn't join the voice channel: {e}")
        return

    # radio stream URL and FFmpeg options suited for reconnecting live streams
    radio_url = "https://radio.shinpu.top/radio.ogg"
    ffmpeg_options = {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": "-vn -c:a libopus -b:a 96k"
    }

    # create the audio source (use FFmpegOpusAudio for Opus-in-Ogg streams)
    source = discord.FFmpegOpusAudio(radio_url, **ffmpeg_options)

    # stop any current playback and play the radio stream
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    # small after-callback to log errors if any
    def _after_play(error):
        if error:
            print(f"Radio playback error: {error}")

    voice_client.play(source, after=_after_play)

    await interaction.followup.send("📻 Now streaming: **HikiNeet Radio**")


bot.run(TOKEN)


