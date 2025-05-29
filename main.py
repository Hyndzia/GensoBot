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
import nacl

import time
from io import BytesIO

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
    await interaction.response.send_message("Stopped playback and disconnected!")
    voice_client = interaction.guild.voice_client

    # Check if the bot is in a voice channel
    if not voice_client or not voice_client.is_connected():
        return await interaction.response.send_message("I'm not connected to any voice channel.")

    # Clear the guild's queue
    guild_id_str = str(interaction.guild_id)
    if guild_id_str in SONG_QUEUES:
        SONG_QUEUES[guild_id_str].clear()

    # If something is playing or paused, stop it
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    # (Optional) Disconnect from the channel
    await voice_client.disconnect()



@bot.tree.command(name="play", description="Play a song or add it to the queue.")
@app_commands.describe(song_query="Search query")
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

    ydl_options = {
        "format": "bestaudio[abr<=96]/bestaudio",
        "noplaylist": True,
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
    }

    query = "ytsearch1: " + song_query
    results = await general.search_ytdlp_async(query, ydl_options)
    tracks = results.get("entries", [])

    if tracks is None or len(tracks) == 0:
        await interaction.followup.send("No results found.")
        return

    first_track = tracks[0]
    audio_url = first_track["url"]
    title = first_track.get("title", "Untitled")

    guild_id = str(interaction.guild_id)
    if SONG_QUEUES.get(guild_id) is None:
        SONG_QUEUES[guild_id] = deque()

    SONG_QUEUES[guild_id].append((audio_url, title))

    if voice_client.is_playing() or voice_client.is_paused():
        await interaction.followup.send(f"Added to queue: **{title}**")
    else:
        await interaction.followup.send(f"Now playing: **{title}**")
        await play_next_song(voice_client, guild_id, interaction.channel)


async def play_next_song(voice_client, guild_id, channel):
    if SONG_QUEUES[guild_id]:
        audio_url, title = SONG_QUEUES[guild_id].popleft()

        ffmpeg_options = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            "options": "-vn -c:a libopus -b:a 96k",
            # Remove executable if FFmpeg is in PATH
        }

        source = discord.FFmpegOpusAudio(audio_url, **ffmpeg_options, executable="G:\\DiscordBot\\bin\\ffmpeg.exe")

        def after_play(error):
            if error:
                print(f"Error playing {title}: {error}")
            asyncio.run_coroutine_threadsafe(play_next_song(voice_client, guild_id, channel), bot.loop)

        voice_client.play(source, after=after_play)
        asyncio.create_task(channel.send(f"Now playing: **{title}**"))
    else:
        await voice_client.disconnect()
        SONG_QUEUES[guild_id] = deque()


bot.run(TOKEN)


