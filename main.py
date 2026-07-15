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
import aiohttp

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.typing = True
intents.guilds = True
intents.messages = True
intents.voice_states = True
intents.members = True

SONG_QUEUES = {}

bot = commands.Bot(command_prefix="!", intents=intents)
now = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
channel_names_by_guild = {}
messages_buffer = {}
messages_buffer2 = {}
BUFFERS_LENGTH = 15

async def messages_handle_troll(message):
    await message.channel.send("ty jesteś troll")

command_map = {
    "troll": messages_handle_troll,
    "trollek": messages_handle_troll,
    "trolluś": messages_handle_troll,
    "trollerka": messages_handle_troll,
}

async def handle_special_user(message):
    await message.channel.send("the lord has spoken")

@bot.event
async def on_connect():
    print("Bot successfully connected!")

@bot.event
async def on_ready():
    print(f"Logged in as: {bot.user}")
    await bot.tree.sync()

    for guild in bot.guilds:
        guild_buffers = general.initialize_guild_buffers(guild, channel_names_by_guild, BUFFERS_LENGTH)
        messages_buffer[guild.id] = guild_buffers

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

    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(base_dir, "servers", message.guild.name, f"{message.channel.name}.txt")
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
    general.test_buffers({message.guild.id: messages_buffer2[message.guild.id]})


@bot.command()
async def test(ctx, arg):
    if arg == "hello":
        await ctx.send("")
    else:
        await ctx.send("nie rozumie, to po polsku?")

@bot.tree.command(name="roll", description="Roll the dice!")
async def roll(interaction: discord.Interaction, start:int, stop:int):
    result = random.randrange(start, stop)
    await interaction.response.send_message(f"{interaction.user.mention} has rolled {result}!")

@bot.tree.command(name="skip", description="Skips the current playing song")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    guild_id = str(interaction.guild_id)

    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        await interaction.response.send_message("⏭ Skipped!")
        voice_client.stop()
    else:
        await interaction.response.send_message("Not playing anything to skip.")

@bot.tree.command(name="pause", description="Pause the currently playing song.")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        return await interaction.response.send_message("I'm not in a voice channel.")

    if not voice_client.is_playing():
        return await interaction.response.send_message("Nothing is currently playing.")

    voice_client.pause()
    await interaction.response.send_message("Playback paused!")

@bot.tree.command(name="resume", description="Resume the currently paused song.")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        return await interaction.response.send_message("I'm not in a voice channel.")

    if not voice_client.is_paused():
        return await interaction.response.send_message("I'm not paused right now.")

    voice_client.resume()
    await interaction.response.send_message("Playback resumed!")

@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("I'm not connected to any voice channel.")
        return

    guild_id_str = str(interaction.guild_id)
    if guild_id_str in SONG_QUEUES:
        SONG_QUEUES[guild_id_str].clear()

    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    await interaction.response.send_message("Stopped playback, cleared the queue, and disconnected!")
    await voice_client.disconnect()
    
    

YDL_OPTIONS_FLAT = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "extract_flat": "in_playlist",
    "default_search": "ytsearch1",
    "skip_download": True,
    "quiet": True,
    "nocheckcertificate": True,
    "cookiefile": "ck.txt",
}

YDL_OPTIONS_FULL = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "extract_flat": False,
    "skip_download": True,
    "nocheckcertificate": True,
    "cookiefile": "ck.txt",
    "extractor_args": {
        "youtube": {
            "player_client": ["web", "tv"],
        }
    },
    "remote_components": ["ejs:github"],
}

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
        "-protocol_whitelist file,http,https,tcp,tls,crypto,m3u8,hls"
    ),
    "options": "-vn -c:a libopus -b:a 96k"
}

async def extract_info_async(query, ydl_opts):
    loop = asyncio.get_event_loop()
    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(query, download=False)
    return await loop.run_in_executor(None, _extract)


def resolve_stable_url(entry):
    webpage_url = entry.get("webpage_url")
    if webpage_url:
        return webpage_url

    url = entry.get("url")
    if url and url.startswith("http"):
        return url


    video_id = entry.get("id") or url
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"

    return url

@bot.tree.command(name="play", description="Play a song or playlist, or add it to the queue.")
@app_commands.describe(song_query="Search query or playlist URL")
async def play(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()

    member = interaction.guild.get_member(interaction.user.id)

    if member is None or member.voice is None or member.voice.channel is None:
        await interaction.followup.send("You must be in a voice channel to use this command.")
        return

    voice_channel = member.voice.channel
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_channel != voice_client.channel:
        await voice_client.move_to(voice_channel)
    try:
        info = await extract_info_async(song_query, YDL_OPTIONS_FLAT)
    except Exception as e:
        print(f"Błąd wyszukiwania '{song_query}': {e}")
        await interaction.followup.send(f"❌ Couldn't search: **{song_query}** ({e})")
        return

    guild_id = str(interaction.guild_id)

    if SONG_QUEUES.get(guild_id) is None:
        SONG_QUEUES[guild_id] = deque()

    added_titles = []

    entries = info.get("entries") if info else None
    if entries is None:
        entries = [info] if info else []

    for entry in entries:
        if entry:
            stable_url = resolve_stable_url(entry)
            if not stable_url:
                continue
            title = entry.get("title", "Untitled")
            SONG_QUEUES[guild_id].append((stable_url, title))
            added_titles.append(title)

    if not added_titles:
        await interaction.followup.send(f"❌ Nothing found for: **{song_query}**")
        return

    if len(added_titles) > 1:
        await interaction.followup.send(f"🎶 Added **{len(added_titles)}** tracks to the queue.")
    else:
        await interaction.followup.send(f"Added to queue: **{added_titles[0]}**")

    if not voice_client.is_playing() and not voice_client.is_paused():
        await play_next_song(voice_client, guild_id, interaction.channel)


async def play_next_song(voice_client, guild_id, channel):
    if not SONG_QUEUES.get(guild_id):
        await voice_client.disconnect()
        SONG_QUEUES[guild_id] = deque()
        return

    stable_url, title = SONG_QUEUES[guild_id].popleft()

    try:
        fresh_info = await extract_info_async(stable_url, YDL_OPTIONS_FULL)
        audio_url = fresh_info.get("url")

        if not audio_url:
            await channel.send(f"❌ Couldn't load: **{title}** — pomijam.")
            await play_next_song(voice_client, guild_id, channel)
            return

        source = discord.FFmpegOpusAudio(audio_url, **FFMPEG_OPTIONS)

        def after_play(error):
            if error:
                print(f"Steaming error {title}: {error}")
            asyncio.run_coroutine_threadsafe(
                play_next_song(voice_client, guild_id, channel), bot.loop
            )

        voice_client.play(source, after=after_play)
        asyncio.create_task(channel.send(f"▶️ Now playing: **{title}**"))

    except Exception as e:
        print(f"Loading error {title}: {e}")
        await channel.send(f"❌ Loading error **{title}**: {e} — skipping.")
        await play_next_song(voice_client, guild_id, channel)


@bot.tree.command(name="radio", description="Play the Shinpu radio stream")
async def radio(interaction: discord.Interaction):
    await interaction.response.defer()

    member = interaction.guild.get_member(interaction.user.id)
    print(f"DEBUG: user={interaction.user}, member={member}, voice={member.voice if member else 'N/A'}")

    if member is None or member.voice is None or member.voice.channel is None:
        await interaction.followup.send("You must be in a voice channel to use this command.")
        return

    voice_channel = member.voice.channel
    voice_client = interaction.guild.voice_client

    try:
        if voice_client is None:
            voice_client = await voice_channel.connect()
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)
    except Exception as e:
        await interaction.followup.send(f"Couldn't join the voice channel: {e}")
        return

    radio_url = "https://radio.shinpu.top/radio.ogg"
    # ffmpeg_options = {
        # "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        # "options": "-vn -c:a libopus -b:a 96k"
    # }

    source = discord.FFmpegOpusAudio(radio_url, **FFMPEG_OPTIONS)

    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    def _after_play(error):
        if error:
            print(f"Radio playback error: {error}")

    voice_client.play(source, after=_after_play)
    await interaction.followup.send("📻 Now streaming: **HikiNeet Radio**")


ICECAST_STATUS_URL = "https://radio.shinpu.top/status-json.xsl"

@bot.tree.command(name="radio_status", description="Show current radio song info")
async def radio_status(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ICECAST_STATUS_URL) as resp:
                if resp.status != 200:
                    await interaction.followup.send("⚠️ Could not fetch radio status.")
                    return
                data = await resp.json()

        sources = data.get("icestats", {}).get("source", [])
        if isinstance(sources, dict):
            sources = [sources]

        song_info = None
        for src in sources:
            if src.get("listenurl", "").endswith("radio.ogg"):
                song_info = src.get("title", None)
                break

        if song_info:
            await interaction.followup.send(f"📻 Now playing on HikiNeet Radio: **{song_info}**")
        else:
            await interaction.followup.send("📻 No current song metadata available.")

    except Exception as e:
        await interaction.followup.send(f"❌ Error fetching radio status: {e}")

@bot.tree.command(name="gif", description="Random weeb gif!")
async def gif(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.shinpu.top/random_gif") as resp:
                if resp.status != 200:
                    await interaction.followup.send("Could not fetch a gif right now.")
                    return
                data = await resp.json()

        gif_url = data.get("gif")
        if not gif_url:
            await interaction.followup.send("API didn't return a gif.")
            return

        await interaction.followup.send(gif_url)

    except Exception as e:
        await interaction.followup.send(f"❌ Error fetching gif: {e}")

bot.run(TOKEN)
