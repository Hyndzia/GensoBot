import asyncio
import fnmatch
import importlib.util
import ipaddress
import os
import random
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask

import general

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE") or None
MAX_PLAYLIST_ITEMS = int(os.getenv("MAX_PLAYLIST_ITEMS", "25"))
STREAM_URL_TTL = 30 * 60          # re-resolve a stream URL if it is older than this (seconds)
YTDL_COOLDOWN = 0.75              # pause after every yt-dlp call (seconds) - keeps request rate low

intents = discord.Intents.default()
intents.message_content = True
intents.typing = True
intents.guilds = True
intents.messages = True
intents.voice_states = True
intents.members = True


class GensoBot(commands.Bot):
    async def setup_hook(self):
        # runs once per start (on_ready can fire many times, syncing there gets rate limited)
        await self.tree.sync()


bot = GensoBot(command_prefix="!", intents=intents)
channel_names_by_guild = {}
messages_buffer = {}
BUFFERS_LENGTH = 15

SONG_QUEUES = {}   # guild_id (int) -> deque[Track]
PLAY_LOCKS = {}    # guild_id (int) -> asyncio.Lock
RADIO_ACTIVE = {}  # guild_id (int) -> (station name, token), while a radio stream is playing
LOOP_MODES = {}      # guild_id (int) -> "song" | "queue"  (missing = loop off)
STOP_REASON = {}     # guild_id (int) -> "skip" | "discard", set when a track is stopped on purpose
BASS_LEVELS = {}     # guild_id (int) -> "low" | "medium" | "high"  (missing = bass boost off)
PLAYBACK = {}        # guild_id (int) -> {"track", "t0", "paused_at"}: lets us restart a song at the same position
BASS_PRESETS = {"low": 6, "medium": 10, "high": 14}   # gain in dB


def stamp():
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


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

    for guild in bot.guilds:
        messages_buffer[guild.id] = general.initialize_guild_buffers(
            guild, channel_names_by_guild, BUFFERS_LENGTH
        )


@bot.event
async def on_guild_join(guild):
    messages_buffer[guild.id] = general.initialize_guild_buffers(
        guild, channel_names_by_guild, BUFFERS_LENGTH
    )
    await general.greeting_message(guild, bot)


@bot.event
async def on_typing(channel, user, when):
    if channel.id == 279581584821190665 and not user.bot:
        await channel.send(f"{user.mention} właśnie wypisuje głupoty...", delete_after=4)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # DMs have no guild -> nothing to log, but commands should still work
    if message.guild is None:
        await bot.process_commands(message)
        return

    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(
        base_dir, "servers",
        general.safe_name(message.guild.name),
        f"{general.safe_name(message.channel.name)}.txt",
    ) if hasattr(message.channel, "name") else None

    log_message = f"{stamp()} [{message.author}] {message.content}"

    if message.attachments and log_path:
        for attachment in message.attachments:
            general.append_to_file(log_path, log_message + f" {attachment.url}")
            print(log_message + f" {attachment.url}")
    else:
        channel_name = getattr(message.channel, "name", str(message.channel.id))
        general.append_to_message_buffer(
            messages_buffer, message.guild.id, channel_name, log_message, BUFFERS_LENGTH
        )
        print(log_message)

    lowered = message.content.lower()
    for trigger, handler in command_map.items():
        if trigger in lowered:
            await handler(message)
            break

    await bot.process_commands(message)


@bot.command()
async def test(ctx, arg):
    if arg == "hello":
        await ctx.send("hello!")
    else:
        await ctx.send("nie rozumie, to po polsku?")


@bot.tree.command(name="roll", description="Roll the dice!")
async def roll(interaction: discord.Interaction, start: int, stop: int):
    if stop <= start:
        await interaction.response.send_message("`stop` must be bigger than `start`.", ephemeral=True)
        return
    result = random.randrange(start, stop)
    await interaction.response.send_message(f"{interaction.user.mention} has rolled {result}!")


# --------------------------------------------------------------------------- #
#  yt-dlp
# --------------------------------------------------------------------------- #

class YTDLLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        print(f"[yt-dlp WARNING] {msg}")

    def error(self, msg):
        print(f"[yt-dlp ERROR] {msg}")


COMMON_OPTS = {
    "quiet": True,
    "no_color": True,                  
    "logger": YTDLLogger(),
    "skip_download": True,
    "socket_timeout": 20,
    "retries": 3,
    "extractor_retries": 3,
    "sleep_interval_requests": 0.5,
    "js_runtimes": {"deno": {}, "node": {}, "bun": {}},
}

YDL_OPTIONS_FLAT = {
    **COMMON_OPTS,
    "format": "bestaudio/best",
    "noplaylist": False,
    "extract_flat": "in_playlist",
    "default_search": "ytsearch1",
    "playlistend": MAX_PLAYLIST_ITEMS,
}

YDL_OPTIONS_FULL = {
    **COMMON_OPTS,
    "format": "bestaudio/best",
    "noplaylist": True,
    "extract_flat": False,
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

# Radio URLs come from Discord users, so ffmpeg may only speak plain http(s) for them
# (no file://, no concat:, etc.).
FFMPEG_RADIO_BEFORE = FFMPEG_BEFORE + " -protocol_whitelist http,https,tcp,tls,crypto"

DEFAULT_RADIO_NAME = "HikiNeet Radio"
DEFAULT_RADIO_URL = "https://radio.shinpu.top/radio.ogg"

# YouTube answers that retrying will never fix
PERMANENT_ERROR_MARKERS = (
    "not a bot", "sign in", "video unavailable", "private video", "members-only",
    "not available in your country", "copyright", "has been removed", "age-restricted",
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
YTDL_LOCK = asyncio.Lock()   # only one yt-dlp extraction at a time


@dataclass
class Track:
    page_url: str
    title: str
    stream_url: Optional[str] = None
    headers: dict = field(default_factory=dict)
    resolved_at: float = 0.0
    duration: Optional[float] = None   # seconds, if known
    looped: bool = False   # True while being replayed by /loop (no 'Now playing' spam)

    def stream_is_fresh(self):
        return bool(self.stream_url) and (time.time() - self.resolved_at) < STREAM_URL_TTL


def _make_cookie_copy():

    if not YT_COOKIES_FILE:
        return None
    if not os.path.isfile(YT_COOKIES_FILE):
        print(f"[yt-dlp WARNING] YT_COOKIES_FILE not found: {YT_COOKIES_FILE}")
        return None
    fd, tmp = tempfile.mkstemp(prefix="ytck_", suffix=".txt")
    os.close(fd)
    shutil.copyfile(YT_COOKIES_FILE, tmp)
    return tmp


def _extract(query, ydl_opts):
    opts = dict(ydl_opts)
    cookie_copy = _make_cookie_copy()
    if cookie_copy:
        opts["cookiefile"] = cookie_copy
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
            return ydl.sanitize_info(info) if info else None
    finally:
        if cookie_copy:
            try:
                os.remove(cookie_copy)
            except OSError:
                pass


async def extract_info_async(query, ydl_opts, attempts=2):
    last_error = None
    async with YTDL_LOCK:
        loop = asyncio.get_running_loop()
        for attempt in range(attempts):
            try:
                return await loop.run_in_executor(None, _extract, query, ydl_opts)
            except yt_dlp.utils.DownloadError as e:
                last_error = e
                text = _ANSI_RE.sub("", str(e)).lower()
                if any(marker in text for marker in PERMANENT_ERROR_MARKERS):
                    break
                await asyncio.sleep(2 * (attempt + 1))
            finally:
                await asyncio.sleep(YTDL_COOLDOWN)
    raise last_error


def friendly_error(exc):
    text = _ANSI_RE.sub("", str(exc)).replace("ERROR: ", "").strip()
    low = text.lower()
    if "not a bot" in low or "sign in" in low:
        return ("YouTube is blocking this server right now (\"confirm you're not a bot\"). "
                "The bot owner needs to refresh the cookies / update yt-dlp.")
    if "requested format is not available" in low or "only images are available" in low:
        return ("YouTube returned no playable audio. The bot owner should update yt-dlp "
                "and make sure deno or node (v22+) is installed.")
    return text[:300] or exc.__class__.__name__


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


def pick_stream(info):
    fmt = info
    if not info.get("url"):
        for f in info.get("requested_formats") or []:
            if f.get("url") and f.get("acodec") not in (None, "none"):
                fmt = f
                break
    headers = fmt.get("http_headers") or info.get("http_headers") or {}
    return fmt.get("url"), headers


def track_from_entry(entry):
    page_url = resolve_stable_url(entry)
    if not page_url:
        return None
    title = entry.get("title") or "Untitled"
    if title in ("[Private video]", "[Deleted video]"):
        return None

    track = Track(page_url=page_url, title=title, duration=entry.get("duration"))

    if entry.get("_type") in (None, "video") and entry.get("url") and entry.get("formats") is not None:
        url, headers = pick_stream(entry)
        if url:
            track.stream_url, track.headers, track.resolved_at = url, headers, time.time()
    return track


async def ensure_stream(track):
    if track.stream_is_fresh():
        return
    info = await extract_info_async(track.page_url, YDL_OPTIONS_FULL)
    if not info:
        raise RuntimeError("yt-dlp returned nothing")
    url, headers = pick_stream(info)
    if not url:
        raise RuntimeError("no playable audio stream found")
    track.stream_url, track.headers, track.resolved_at = url, headers, time.time()
    track.duration = info.get("duration") or track.duration


def audio_options(guild_id):
    """ffmpeg output options. Bass boost is a low-shelf EQ plus a limiter so it doesn't clip."""
    level = BASS_LEVELS.get(guild_id)
    if not level:
        return "-vn"
    return f"-vn -af bass=g={BASS_PRESETS[level]}:f=100:w=0.6,alimiter=limit=0.9"


def make_audio_source(track, guild_id, seek=0.0):
    """ffmpeg must send the same HTTP headers (User-Agent etc.) that yt-dlp used to
    get the URL - otherwise googlevideo answers 403. This is exactly what yt-dlp's
    own ffmpeg downloader does and what the old bot code was missing."""
    before = FFMPEG_BEFORE
    if seek >= 1:
        before += f" -ss {seek:.1f}"
    headers = {k: v for k, v in (track.headers or {}).items() if k.lower() != "accept-encoding"}
    if headers:
        blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        before += f" -headers {shlex.quote(blob)}"
    return discord.FFmpegOpusAudio(track.stream_url, bitrate=96, before_options=before,
                                   options=audio_options(guild_id))


# --------------------------------------------------------------------------- #
#  Music commands
# --------------------------------------------------------------------------- #

async def safe_disconnect(guild):
    RADIO_ACTIVE.pop(guild.id, None)
    vc = guild.voice_client
    if vc is not None and vc.is_connected():
        await vc.disconnect()


async def join_user_channel(interaction):
    """Joins (or moves to) the voice channel of whoever ran the command.
    Returns the VoiceClient, or None after telling the user what went wrong."""
    if interaction.guild is None:
        await interaction.followup.send("This command only works in a server.")
        return None

    member = interaction.guild.get_member(interaction.user.id) or interaction.user
    voice_state = getattr(member, "voice", None)
    if voice_state is None or voice_state.channel is None:
        await interaction.followup.send("You must be in a voice channel to use this command.")
        return None

    voice_channel = voice_state.channel

    # The #1 reason for "it joined but I hear nothing": no Speak permission.
    perms = voice_channel.permissions_for(interaction.guild.me)
    missing = [name for name, ok in (("Connect", perms.connect), ("Speak", perms.speak),
                                           ("Use Voice Activity", perms.use_voice_activation)) if not ok]
    if missing:
        await interaction.followup.send(
            f"I'm missing the **{' / '.join(missing)}** permission in **{voice_channel.name}**, "
            "so you would not hear me. Fix the channel permissions and try again."
        )
        return None

    voice_client = interaction.guild.voice_client
    try:
        if voice_client is None:
            voice_client = await voice_channel.connect(timeout=30.0, self_deaf=True)
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)
    except Exception as e:
        print(f"Voice connect error: {e!r}")
        await interaction.followup.send(f"Couldn't join the voice channel: {e}")
        return None

    print(f"[voice] connected to '{voice_channel.name}' in '{interaction.guild.name}'")
    return voice_client


def report_quick_exit(channel, label, started, error, min_life=3.0):
    """If ffmpeg dies within a few seconds the stream never really played.
    Say so in the chat instead of failing silently (details are in the console)."""
    lived = time.monotonic() - started
    failed = bool(error) or lived < min_life
    if failed:
        reason = f"error: {error}" if error else f"it ended after {lived:.1f}s"
        print(f"[voice] '{label}' stopped early - {reason}")
        asyncio.run_coroutine_threadsafe(
            channel.send(f"⚠️ **{label}** stopped almost immediately ({reason}). "
                         "Check the bot console for the ffmpeg message."),
            bot.loop,
        )
    return failed


def stop_playback(guild_id, voice_client, reason="discard"):
    """Stops whatever is playing and marks it as intentional.
    reason "skip"    -> /loop song stops repeating it, /loop queue still sends it to the back
    reason "discard" -> the track is dropped for good (/stop, switching to radio)"""
    was_radio = RADIO_ACTIVE.pop(guild_id, None) is not None
    if not was_radio:
        STOP_REASON[guild_id] = reason
    voice_client.stop()


async def on_track_finished(guild, channel, track, failed):
    reason = STOP_REASON.pop(guild.id, None)
    mode = LOOP_MODES.get(guild.id)
    vc = guild.voice_client

    if mode and not failed and reason != "discard" and vc is not None and vc.is_connected():
        queue = SONG_QUEUES.setdefault(guild.id, deque())
        if mode == "song" and reason is None:
            track.looped = True            # same song again, no "Now playing" spam
            queue.appendleft(track)
        elif mode == "queue":
            track.looped = not queue       # a one-song queue repeating silently; otherwise announce each song
            queue.append(track)

    await play_next_song(guild, channel)


def start_track(guild, vc, channel, track, seek=0.0):
    """Starts playing a track (optionally from `seek` seconds). Raises if it can't start."""
    source = make_audio_source(track, guild.id, seek)
    started = time.monotonic()

    def after_play(error, track=track, started=started):
        if error:
            print(f"Streaming error {track.title}: {error}")
        intentional = guild.id in STOP_REASON      # skip / stop / restart, not a failure
        failed = False if intentional else report_quick_exit(
            channel, track.title, started, error, min_life=0 if seek else 3.0)
        asyncio.run_coroutine_threadsafe(on_track_finished(guild, channel, track, failed), bot.loop)

    try:
        vc.play(source, after=after_play)
    except Exception:
        source.cleanup()
        raise
    PLAYBACK[guild.id] = {"track": track, "t0": started - seek, "paused_at": None}


async def play_next_song(guild, channel):
    lock = PLAY_LOCKS.setdefault(guild.id, asyncio.Lock())
    async with lock:
        vc = guild.voice_client
        if vc is None or not vc.is_connected():
            return
        if vc.is_playing() or vc.is_paused():
            return

        queue = SONG_QUEUES.setdefault(guild.id, deque())

        while queue:
            track = queue.popleft()
            try:
                await ensure_stream(track)
            except Exception as e:
                print(f"Loading error {track.title}: {e}")
                await channel.send(f"❌ Couldn't load **{track.title}**: {friendly_error(e)} — skipping.")
                continue

            try:
                start_track(guild, vc, channel, track)
            except Exception as e:
                print(f"vc.play() failed for {track.title}: {e!r}")
                await channel.send(f"❌ Couldn't start **{track.title}**: {e}")
                return

            if not track.looped:
                await channel.send(f"▶️ Now playing: **{track.title}**")
            track.looped = False
            return

        # queue is empty -> leave the voice channel
        await safe_disconnect(guild)


def start_radio(guild, channel, voice_client, name, url):
    """Starts (or restarts) a radio stream, replacing whatever is playing. Raises if it can't start."""
    source = discord.FFmpegOpusAudio(url, bitrate=96, before_options=FFMPEG_RADIO_BEFORE,
                                     options=audio_options(guild.id))

    if voice_client.is_playing() or voice_client.is_paused():
        stop_playback(guild.id, voice_client)
    token = object()
    RADIO_ACTIVE[guild.id] = (name, token, url)
    PLAYBACK.pop(guild.id, None)
    started = time.monotonic()

    def _after_play(error):
        current = RADIO_ACTIVE.get(guild.id)
        if current is None or current[1] is not token:
            return   # stopped / skipped / replaced on purpose
        RADIO_ACTIVE.pop(guild.id, None)
        lived = time.monotonic() - started
        print(f"[voice] radio '{name}' ended after {lived:.1f}s, error={error}")
        detail = f" ({error})" if error else ""
        asyncio.run_coroutine_threadsafe(
            channel.send(f"⚠️ **{name}** stopped after {lived:.0f}s{detail}. The stream may be offline, "
                         "or the URL is not a direct audio stream."),
            bot.loop,
        )
        asyncio.run_coroutine_threadsafe(safe_disconnect(guild), bot.loop)

    try:
        voice_client.play(source, after=_after_play)
    except Exception:
        RADIO_ACTIVE.pop(guild.id, None)
        source.cleanup()
        raise


async def restart_current(guild, channel):
    """ffmpeg filters can't be changed mid-stream, so re-open the current song at the same
    position (or the radio stream) with the new settings. Returns True if something was restarted."""
    vc = guild.voice_client
    if vc is None or not (vc.is_playing() or vc.is_paused()):
        return False
    was_paused = vc.is_paused()

    radio_state = RADIO_ACTIVE.get(guild.id)
    if radio_state:
        start_radio(guild, channel, vc, radio_state[0], radio_state[2])
    else:
        pb = PLAYBACK.get(guild.id)
        if not pb:
            return False
        track = pb["track"]
        position = (pb["paused_at"] or time.monotonic()) - pb["t0"]
        await ensure_stream(track)
        if PLAYBACK.get(guild.id) is not pb or not (vc.is_playing() or vc.is_paused()):
            return False   # the song changed while we were refreshing its URL
        STOP_REASON[guild.id] = "discard"    # the old stream ends on purpose: no loop re-add, no warning
        vc.stop()
        start_track(guild, vc, channel, track, seek=position)

    if was_paused:
        vc.pause()
        if guild.id in PLAYBACK:
            PLAYBACK[guild.id]["paused_at"] = time.monotonic()
    return True


@bot.tree.command(name="bass_boost", description="Boost the bass of songs and radio.")
@app_commands.describe(level="Off / low / medium / high. Leave empty to switch it on (medium) or off.")
@app_commands.choices(level=[
    app_commands.Choice(name="Off", value="off"),
    app_commands.Choice(name="Low", value="low"),
    app_commands.Choice(name="Medium", value="medium"),
    app_commands.Choice(name="High", value="high"),
])
async def bass_boost(interaction: discord.Interaction, level: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer()
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("This command only works in a server.")
        return

    if level is not None:
        new_level = level.value
    else:
        new_level = "off" if guild.id in BASS_LEVELS else "medium"

    if new_level == "off":
        BASS_LEVELS.pop(guild.id, None)
        text = "🔈 Bass boost **off**."
    else:
        BASS_LEVELS[guild.id] = new_level
        text = f"🔊 Bass boost **{new_level}** (+{BASS_PRESETS[new_level]} dB)."

    try:
        restarted = await restart_current(guild, interaction.channel)
    except Exception as e:
        print(f"Bass boost restart error: {e!r}")
        text += f"\n⚠️ Couldn't re-open the current stream ({e}); it applies from the next song."
    else:
        text += "\nApplied right away." if restarted else "\nApplies to everything played from now on."
    await interaction.followup.send(text)


LOOP_LABELS = {
    "off": "➡️ Loop **off** - the queue continues normally.",
    "song": "🔂 Loop **current song** - it will repeat until you `/skip` or change the mode.",
    "queue": "🔁 Loop **whole queue** - finished songs go to the back of the queue.",
}


@bot.tree.command(name="loop", description="Loop the current song or the whole queue.")
@app_commands.describe(mode="Off / current song / whole queue. Leave empty to cycle through the modes.")
@app_commands.choices(mode=[
    app_commands.Choice(name="Off", value="off"),
    app_commands.Choice(name="Current song", value="song"),
    app_commands.Choice(name="Whole queue", value="queue"),
])
async def loop_cmd(interaction: discord.Interaction, mode: Optional[app_commands.Choice[str]] = None):
    guild_id = interaction.guild_id
    if guild_id in RADIO_ACTIVE:
        await interaction.response.send_message("🔁 Loop doesn't apply to radio streams.")
        return

    if mode is not None:
        new_mode = mode.value
    else:
        order = ["off", "song", "queue"]
        new_mode = order[(order.index(LOOP_MODES.get(guild_id, "off")) + 1) % len(order)]

    if new_mode == "off":
        LOOP_MODES.pop(guild_id, None)
    else:
        LOOP_MODES[guild_id] = new_mode
    await interaction.response.send_message(LOOP_LABELS[new_mode])


@bot.tree.command(name="skip", description="Skips the current playing song")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        await interaction.response.send_message("⏭ Skipped!")
        stop_playback(interaction.guild_id, voice_client, reason="skip")   # the after-callback starts the next song
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
    if interaction.guild_id in PLAYBACK:
        PLAYBACK[interaction.guild_id]["paused_at"] = time.monotonic()
    await interaction.response.send_message("Playback paused!")


@bot.tree.command(name="resume", description="Resume the currently paused song.")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        return await interaction.response.send_message("I'm not in a voice channel.")

    if not voice_client.is_paused():
        return await interaction.response.send_message("I'm not paused right now.")

    voice_client.resume()
    pb = PLAYBACK.get(interaction.guild_id)
    if pb and pb["paused_at"]:
        pb["t0"] += time.monotonic() - pb["paused_at"]
        pb["paused_at"] = None
    await interaction.response.send_message("Playback resumed!")


@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("I'm not connected to any voice channel.")
        return

    SONG_QUEUES.get(interaction.guild_id, deque()).clear()
    LOOP_MODES.pop(interaction.guild_id, None)

    if voice_client.is_playing() or voice_client.is_paused():
        stop_playback(interaction.guild_id, voice_client)

    await interaction.response.send_message("Stopped playback, cleared the queue, and disconnected!")
    await safe_disconnect(interaction.guild)


@bot.tree.command(name="queue", description="Show what is waiting in the queue.")
async def show_queue(interaction: discord.Interaction):
    queue = SONG_QUEUES.get(interaction.guild_id)
    if not queue:
        await interaction.response.send_message("The queue is empty.")
        return
    lines = [f"{i}. {t.title}" for i, t in enumerate(list(queue)[:10], start=1)]
    more = f"\n…and {len(queue) - 10} more" if len(queue) > 10 else ""
    await interaction.response.send_message("**Up next:**\n" + "\n".join(lines) + more)


def fmt_time(seconds):
    seconds = int(max(seconds, 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


async def fetch_icecast_title(session, url):
    """Icecast servers publish the current track on /status-json.xsl - pick the entry of our mount point."""
    parsed = urlparse(url)
    status_url = f"{parsed.scheme}://{parsed.netloc}/status-json.xsl"
    async with session.get(status_url, allow_redirects=False) as resp:
        if resp.status != 200:
            return None
        data = await resp.json(content_type=None)

    sources = data.get("icestats", {}).get("source", [])
    if isinstance(sources, dict):
        sources = [sources]
    for src in sources:
        if urlparse(src.get("listenurl", "")).path == parsed.path:
            title, artist = src.get("title"), src.get("artist")
            if title and artist and artist not in title:
                return f"{artist} - {title}"
            return title or None
    return None


async def fetch_icy_title(session, url):
    """Shoutcast/Icecast in-stream metadata (ICY). Returns (title, station name)."""
    async with session.get(url, headers={"Icy-MetaData": "1"}) as resp:
        station = resp.headers.get("icy-name")
        try:
            metaint = int(resp.headers.get("icy-metaint", 0))
        except ValueError:
            metaint = 0
        if not 0 < metaint <= 262144:
            return None, station
        await resp.content.readexactly(metaint)                 # audio data before the first metadata block
        length = (await resp.content.readexactly(1))[0] * 16
        if not length:
            return None, station
        block = (await resp.content.readexactly(length)).decode("utf-8", "replace")

    match = re.search(r"StreamTitle='(.*?)';", block, re.S)
    return ((match.group(1).strip() or None) if match else None), station


async def fetch_radio_metadata(url):
    """Returns (track title, station name); either can be None if the station doesn't publish it."""
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "GensoBot"}) as session:
        try:
            title = await fetch_icecast_title(session, url)
            if title:
                return title, None
        except Exception as e:
            print(f"[radio] icecast status lookup failed: {e!r}")
        try:
            return await fetch_icy_title(session, url)
        except Exception as e:
            print(f"[radio] ICY lookup failed: {e!r}")
            return None, None


@bot.tree.command(name="now_playing", description="Show the song or radio track that is playing right now.")
@app_commands.guild_only()
async def now_playing(interaction: discord.Interaction):
    guild = interaction.guild
    vc = guild.voice_client
    no_pings = discord.AllowedMentions.none()   # titles come from the internet - never let them ping anyone

    if vc is None or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message("Nothing is playing right now.")
        return

    # ---- radio: metadata comes from the stream itself ----
    radio_state = RADIO_ACTIVE.get(guild.id)
    if radio_state:
        await interaction.response.defer()
        name, _, url = radio_state
        title, station = await fetch_radio_metadata(url)
        name = discord.utils.escape_markdown(station or name)
        if title:
            text = f"📻 **{name}**\n🎶 Now playing: **{discord.utils.escape_markdown(title)}**"
        else:
            text = f"📻 Streaming **{name}**\nThis station doesn't publish track information."
        if BASS_LEVELS.get(guild.id):
            text += f"\n🔊 Bass boost: {BASS_LEVELS[guild.id]}"
        await interaction.followup.send(text, allowed_mentions=no_pings)
        return

    # ---- queue: we know the track and how far along it is ----
    pb = PLAYBACK.get(guild.id)
    if not pb:
        await interaction.response.send_message("Something is playing, but I have no details about it.")
        return

    track = pb["track"]
    elapsed = (pb["paused_at"] or time.monotonic()) - pb["t0"]
    title = discord.utils.escape_markdown(track.title)
    lines = [f"{'⏸' if vc.is_paused() else '▶️'} **Now playing:** [{title}](<{track.page_url}>)"]

    if track.duration:
        elapsed = min(elapsed, track.duration)
        filled = round(10 * elapsed / track.duration)
        lines.append(f"`{'▰' * filled}{'▱' * (10 - filled)}` {fmt_time(elapsed)} / {fmt_time(track.duration)}")
    else:
        lines.append(f"`{fmt_time(elapsed)}` elapsed")

    extras = []
    loop_mode = LOOP_MODES.get(guild.id)
    if loop_mode:
        extras.append("🔂 loop: song" if loop_mode == "song" else "🔁 loop: queue")
    if BASS_LEVELS.get(guild.id):
        extras.append(f"🔊 bass: {BASS_LEVELS[guild.id]}")
    waiting = len(SONG_QUEUES.get(guild.id, ()))
    extras.append(f"⏭ {waiting} in queue" if waiting else "queue is empty")
    lines.append(" · ".join(extras))

    await interaction.response.send_message("\n".join(lines), allowed_mentions=no_pings)


@bot.tree.command(name="play", description="Play a song or playlist, or add it to the queue.")
@app_commands.describe(song_query="Search query or playlist URL")
async def play(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()

    voice_client = await join_user_channel(interaction)
    if voice_client is None:
        return

    # a running radio stream would block the queue forever -> replace it
    if interaction.guild_id in RADIO_ACTIVE and (voice_client.is_playing() or voice_client.is_paused()):
        stop_playback(interaction.guild_id, voice_client)

    try:
        info = await extract_info_async(song_query, YDL_OPTIONS_FLAT)
    except Exception as e:
        print(f"Search error '{song_query}': {e}")
        await interaction.followup.send(f"❌ Couldn't search **{song_query}**: {friendly_error(e)}")
        if not voice_client.is_playing() and not SONG_QUEUES.get(interaction.guild_id):
            await safe_disconnect(interaction.guild)
        return

    queue = SONG_QUEUES.setdefault(interaction.guild_id, deque())

    entries = info.get("entries") if info else None
    if entries is None:
        entries = [info] if info else []

    added_titles = []
    for entry in entries:
        if not entry:
            continue
        track = track_from_entry(entry)
        if track:
            queue.append(track)
            added_titles.append(track.title)

    if not added_titles:
        await interaction.followup.send(f"❌ Nothing found for: **{song_query}**")
        if not voice_client.is_playing() and not queue:
            await safe_disconnect(interaction.guild)
        return

    if len(added_titles) > 1:
        await interaction.followup.send(f"🎶 Added **{len(added_titles)}** tracks to the queue.")
    else:
        await interaction.followup.send(f"Added to queue: **{added_titles[0]}**")

    if not voice_client.is_playing() and not voice_client.is_paused():
        await play_next_song(interaction.guild, interaction.channel)


# --------------------------------------------------------------------------- #
#  Radio / gif
# --------------------------------------------------------------------------- #

# Names that can be typed instead of a URL. Add your own favourites here.
RADIO_PRESETS = {
    "hikineet": (DEFAULT_RADIO_NAME, DEFAULT_RADIO_URL),
    "default": (DEFAULT_RADIO_NAME, DEFAULT_RADIO_URL),
}


async def validate_stream_url(url):
    """Only public http(s) hosts - the bot must not be usable to poke at the server's LAN."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Please give a full `http://` or `https://` stream URL.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise ValueError(f"Couldn't resolve the host `{parsed.hostname}`.")
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise ValueError("That address is not a public host.")
    return url


async def resolve_radio_url(url):
    """Validates the URL and unwraps .pls / .m3u playlist files into the real stream URL."""
    await validate_stream_url(url)
    if not urlparse(url).path.lower().endswith((".pls", ".m3u")):
        return url

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            text = (await resp.content.read(65536)).decode("utf-8", "replace")

    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("file1="):      # .pls format
            line = line.split("=", 1)[1].strip()
        if line.lower().startswith(("http://", "https://")):
            return await validate_stream_url(line)
    raise ValueError("Couldn't find a stream URL inside that playlist file.")


@bot.tree.command(name="radio", description="Play a radio stream (defaults to HikiNeet Radio)")
@app_commands.describe(station="Direct stream URL (http/https). Leave empty for HikiNeet Radio.")
async def radio(interaction: discord.Interaction, station: Optional[str] = None):
    await interaction.response.defer()

    station = (station or "").strip()
    preset = RADIO_PRESETS.get(station.lower()) if station else RADIO_PRESETS["hikineet"]
    if preset:
        name, url = preset
    else:
        try:
            url = await resolve_radio_url(station)
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}")
            return
        except Exception as e:
            await interaction.followup.send(f"❌ Couldn't load that station: {e}")
            return
        name = urlparse(url).hostname

    voice_client = await join_user_channel(interaction)
    if voice_client is None:
        return

    # radio replaces whatever was playing and discards the song queue
    SONG_QUEUES.get(interaction.guild_id, deque()).clear()
    try:
        start_radio(interaction.guild, interaction.channel, voice_client, name, url)
    except Exception as e:
        print(f"Radio start error: {e!r}")
        await interaction.followup.send(f"❌ Couldn't start the stream: {e}")
        return

    await interaction.followup.send(f"📻 Now streaming: **{name}**")


ICECAST_STATUS_URL = "https://radio.shinpu.top/status-json.xsl"


@bot.tree.command(name="radio_status", description="Show current radio song info")
async def radio_status(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(ICECAST_STATUS_URL) as resp:
                if resp.status != 200:
                    await interaction.followup.send("⚠️ Could not fetch radio status.")
                    return
                data = await resp.json(content_type=None)

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


# --------------------------------------------------------------------------- #
#  /booru - random picture from Danbooru
# --------------------------------------------------------------------------- #
DANBOORU_URL = "https://danbooru.donmai.us"
DANBOORU_LOGIN = os.getenv("DANBOORU_LOGIN") or None        # optional: higher rate limits / more tag slots
DANBOORU_API_KEY = os.getenv("DANBOORU_API_KEY") or None
BOORU_COOLDOWN = 3.0                                         # seconds between uses, per user
BOORU_LAST_USE = {}                                          # user_id -> time.monotonic()

BOORU_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}     # videos / zips can't be shown in an embed
# Never shown in any rating, whatever the user searches for.
BOORU_BLOCKED_TAGS = {"loli", "lolicon", "shota", "shotacon", "toddlercon"}
BOORU_RATINGS = {"g": "General", "s": "Sensitive", "q": "Questionable", "e": "Explicit"}
BOORU_COLORS = {"g": 0x2ECC71, "s": 0xF1C40F, "q": 0xE67E22, "e": 0xE74C3C}
# Learned at runtime: the smallest number of tags for which search strategy 1 / 2 got an HTTP 422 from
# Danbooru, so later searches skip requests that are known to fail (saves rate limit).
BOORU_FAILED_AT = {1: 99, 2: 99}


class BooruError(Exception):
    """Something the user should be told about (shown as-is in chat)."""


# Danbooru search operators such as "rating:e" or "order:score" are not plain tags; the bot sets rating
# and ordering itself, so they are refused. (Series tags like "re:zero..." contain a colon too - those are fine.)
BOORU_METATAG_RE = re.compile(
    r"^[-~]?(rating|order|status|user|id|score|favcount|filetype|ratio|width|height|parent|child|source|md5|"
    r"random|limit|date|age|tagcount|gentags|chartags|copytags|arttags|metatags|pool|fav|search|"
    r"approver|commenter|noter|uploader|mpixels|filesize|unaffiliated|has|is):")


def parse_booru_tags(query):
    """'hatsune miku' -> ['hatsune_miku'];  'hatsune miku, solo' -> ['hatsune_miku', 'solo']."""
    query = (query or "").strip().lower()
    parts = query.split(",") if "," in query else [query]
    tags = [re.sub(r"\s+", "_", part.strip()) for part in parts if part.strip()]
    if len(tags) > 6 or any(len(tag) > 100 for tag in tags):
        raise BooruError("Too many / too long tags.")
    for tag in tags:
        if BOORU_METATAG_RE.match(tag):
            raise BooruError(f"`{tag}` is a Danbooru search operator, not a tag - only plain tags are supported.")
    return tags


def post_has_tags(post, tags):
    """Client-side tag check for the case where Danbooru could only be asked for one of the tags.
    Supports -negation and * wildcards."""
    post_tags = (post.get("tag_string") or "").split()
    for tag in tags:
        negated = tag.startswith("-")
        pattern = tag.lstrip("-~")
        found = any(fnmatch.fnmatchcase(t, pattern) for t in post_tags)
        if found == negated:
            return False
    return True


def usable_post(post, rating):
    """The real safety net: the rating and the blocklist are enforced HERE, on every post we got back,
    so no search syntax the user types can sneak a different rating or a blocked tag through."""
    if post.get("rating") != rating:
        return False
    if post.get("is_banned") or post.get("is_deleted"):
        return False
    if (post.get("file_ext") or "").lower() not in BOORU_IMAGE_EXTS:
        return False
    if not (post.get("large_file_url") or post.get("file_url")):
        return False
    return not (set((post.get("tag_string") or "").split()) & BOORU_BLOCKED_TAGS)


def pretty_tags(tag_string, limit=4, title_case=True):
    items = [t for t in (tag_string or "").split() if t]
    if not items:
        return None
    names = [t.replace("_", " ") for t in items[:limit]]
    if title_case:
        names = [n.title() for n in names]
    text = ", ".join(discord.utils.escape_markdown(n) for n in names)
    return text + (f" +{len(items) - limit} more" if len(items) > limit else "")


def booru_embed(post):
    rating = post["rating"]
    post_url = f"{DANBOORU_URL}/posts/{post['id']}"
    characters = pretty_tags(post.get("tag_string_character"), limit=3)
    series = pretty_tags(post.get("tag_string_copyright"), limit=3)
    artist = pretty_tags(post.get("tag_string_artist"), limit=3, title_case=False)

    embed = discord.Embed(title=characters or "Random pick", url=post_url, color=BOORU_COLORS[rating])
    if series:
        embed.description = f"from **{series}**"
    embed.set_image(url=post.get("large_file_url") or post["file_url"])

    if artist:
        embed.add_field(name="🎨 Artist", value=artist, inline=True)
    embed.add_field(name="⭐ Score", value=str(post.get("score", 0)), inline=True)
    embed.add_field(name="❤️ Favorites", value=str(post.get("fav_count", 0)), inline=True)
    embed.add_field(name="📐 Size", value=f"{post.get('image_width')}×{post.get('image_height')}", inline=True)
    embed.add_field(name="🔞 Rating", value=BOORU_RATINGS[rating], inline=True)

    source = post.get("source") or ""
    if source.startswith(("http://", "https://")) and len(source) < 300:
        embed.add_field(name="🔗 Source", value=f"[open]({source.replace(')', '%29')})", inline=True)
    embed.set_footer(text=f"Danbooru · #{post['id']}")
    return embed


async def booru_suggestions(session, tag):
    """'Did you mean' helper for searches that found nothing."""
    try:
        async with session.get(f"{DANBOORU_URL}/autocomplete.json",
                               params={"search[query]": tag, "search[type]": "tag_query", "limit": 6}) as resp:
            if resp.status != 200:
                return []
            items = await resp.json(content_type=None)
        names = [str(i.get("value") or i.get("label") or "") for i in items if isinstance(i, dict)]
        return [n for n in names if n and n != tag][:5]
    except Exception:
        return []


async def booru_pick_search_tag(session, tags):
    """Without an account Danbooru can only be asked for very few tags. Ask for the RAREST positive tag
    (most specific - usually the character) and let the bot check the remaining tags itself."""
    candidates = [t for t in tags if not t.startswith(("-", "~"))][:4]
    if not candidates:
        return None

    async def count(tag):
        try:
            async with session.get(f"{DANBOORU_URL}/counts/posts.json", params={"tags": tag}) as resp:
                if resp.status != 200:
                    return float("inf")
                data = await resp.json(content_type=None)
            return int(data.get("counts", {}).get("posts") or 0) or float("inf")
        except Exception:
            return float("inf")

    counts = await asyncio.gather(*(count(t) for t in candidates))
    return min(zip(counts, candidates))[1] if any(c != float("inf") for c in counts) else candidates[0]


async def booru_random_embed(tags, rating):
    auth = aiohttp.BasicAuth(DANBOORU_LOGIN, DANBOORU_API_KEY) if DANBOORU_LOGIN and DANBOORU_API_KEY else None
    timeout = aiohttp.ClientTimeout(total=20)
    # Danbooru rejects the default python User-Agent, so identify ourselves
    async with aiohttp.ClientSession(timeout=timeout, auth=auth,
                                     headers={"User-Agent": "GensoBot (Discord bot)"}) as session:

        async def fetch(search_tags, limit):
            params = {"tags": " ".join(search_tags), "limit": limit, "random": "true"}
            async with session.get(f"{DANBOORU_URL}/posts.json", params=params) as resp:
                if resp.status == 429:
                    raise BooruError("Danbooru says I'm sending too many requests - try again in a few seconds.")
                if resp.status == 422:
                    # "You cannot search for more than 2 tags at a time" - 'random' and 'rating:' can count too
                    print(f"[booru] 422 for tags={params['tags']!r}: {(await resp.text())[:200]}")
                    return None
                if resp.status != 200:
                    raise BooruError(f"Danbooru answered with HTTP {resp.status}.")
                return await resp.json(content_type=None)

        client_side_tags = []
        posts = None
        # 1) ideal: Danbooru does everything (all tags + rating)
        if len(tags) < BOORU_FAILED_AT[1]:
            posts = await fetch(tags + [f"rating:{rating}"], 20)
            if posts is None:
                BOORU_FAILED_AT[1] = min(BOORU_FAILED_AT[1], len(tags))
        # 2) rating is checked locally instead
        if posts is None and len(tags) < BOORU_FAILED_AT[2]:
            posts = await fetch(tags, 100)
            if posts is None:
                BOORU_FAILED_AT[2] = min(BOORU_FAILED_AT[2], len(tags))
        # 3) too many tags for an anonymous account: ask for the rarest tag only, check the rest locally
        if posts is None:
            main_tag = await booru_pick_search_tag(session, tags)
            posts = await fetch([main_tag] if main_tag else [], 100)
            client_side_tags = tags
        if posts is None:
            raise BooruError("Danbooru refused this search. Try fewer tags.")

        valid = [p for p in posts if isinstance(p, dict) and usable_post(p, rating)
                 and post_has_tags(p, client_side_tags)]
        if not valid:
            text = f"No **{BOORU_RATINGS[rating]}**-rated pictures found for `{', '.join(tags) or 'random'}`."
            if client_side_tags and len(tags) > 1:
                text += ("\nWithout a Danbooru account I can only search for one tag directly and check "
                         "the rest myself, so try fewer or more common tags.")
            elif tags:
                hints = await booru_suggestions(session, tags[0])
                if hints:
                    text += "\nDid you mean: " + ", ".join(f"`{h}`" for h in hints) + "?"
            raise BooruError(text)
        return booru_embed(random.choice(valid))


def booru_cooldown_left(user_id):
    left = BOORU_COOLDOWN - (time.monotonic() - BOORU_LAST_USE.get(user_id, 0.0))
    if left > 0:
        return left
    BOORU_LAST_USE[user_id] = time.monotonic()
    return 0.0


class BooruView(discord.ui.View):
    def __init__(self, owner_id, tags, rating):
        super().__init__(timeout=180)
        self.owner_id, self.tags, self.rating = owner_id, tags, rating
        self.message = None

    @discord.ui.button(label="Another one", emoji="🎲", style=discord.ButtonStyle.secondary)
    async def another(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Run `/booru` yourself to get your own picture.", ephemeral=True)
            return
        left = booru_cooldown_left(interaction.user.id)
        if left:
            await interaction.response.send_message(f"Slow down - try again in {left:.0f}s.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            embed = await booru_random_embed(self.tags, self.rating)
        except BooruError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            print(f"[booru] error: {e!r}")
            await interaction.followup.send("❌ Couldn't reach Danbooru.", ephemeral=True)
            return
        await interaction.edit_original_response(embed=embed, view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.tree.command(name="booru", description="Random anime picture from Danbooru, optionally for a character / tag.")
@app_commands.describe(
    tags="Character, series or tag, e.g. hatsune miku (several tags: separate them with commas)",
    rating="General by default. Any other rating only works in age-restricted channels.")
@app_commands.choices(rating=[
    app_commands.Choice(name="General", value="g"),
    app_commands.Choice(name="Sensitive", value="s"),
    app_commands.Choice(name="Questionable", value="q"),
    app_commands.Choice(name="Explicit", value="e"),
])
async def booru(interaction: discord.Interaction, tags: Optional[str] = None,
                rating: Optional[app_commands.Choice[str]] = None):
    level = rating.value if rating else "g"
    channel = interaction.channel
    is_nsfw = bool(getattr(channel, "is_nsfw", lambda: False)())
    if level != "g" and not is_nsfw:
        await interaction.response.send_message(
            "🔞 Only **General** pictures can be posted here. "
            "Other ratings work only in age-restricted channels.", ephemeral=True)
        return

    left = booru_cooldown_left(interaction.user.id)
    if left:
        await interaction.response.send_message(f"Slow down - try again in {left:.0f}s.", ephemeral=True)
        return

    try:
        tag_list = parse_booru_tags(tags)
    except BooruError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        embed = await booru_random_embed(tag_list, level)
    except BooruError as e:
        await interaction.followup.send(str(e))
        return
    except Exception as e:
        print(f"[booru] error: {e!r}")
        await interaction.followup.send("❌ Couldn't reach Danbooru, try again later.")
        return

    view = BooruView(interaction.user.id, tag_list, level)
    view.message = await interaction.followup.send(embed=embed, view=view, wait=True)


TRUN_SCAN_LIMIT = 1000   # how far back /trun looks for the user's messages


@bot.tree.command(name="trun", description="Delete the latest X messages of a user in this channel (admins only).")
@app_commands.default_permissions(administrator=True)   # hidden for everyone without Administrator
@app_commands.guild_only()
@app_commands.describe(user="Whose messages to delete", count="How many of their latest messages to delete (1-100)")
async def trun(interaction: discord.Interaction, user: discord.User, count: app_commands.Range[int, 1, 100]):
    # server owners can re-expose a command in Server Settings -> Integrations, so check again here
    if not interaction.permissions.administrator:
        await interaction.response.send_message("❌ Only administrators can use this command.", ephemeral=True)
        return

    channel = interaction.channel
    my_perms = channel.permissions_for(interaction.guild.me)
    if not (my_perms.manage_messages and my_perms.read_message_history):
        await interaction.response.send_message(
            "❌ I need the **Manage Messages** and **Read Message History** permissions in this channel.",
            ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    found = []
    async for message in channel.history(limit=TRUN_SCAN_LIMIT):
        if message.author.id == user.id:
            found.append(message)
            if len(found) >= count:
                break

    if not found:
        await interaction.followup.send(
            f"No messages from **{user.display_name}** in the last {TRUN_SCAN_LIMIT} messages of this channel.")
        return

    # Discord only bulk-deletes messages younger than 14 days; older ones must be deleted one by one
    cutoff = discord.utils.utcnow() - timedelta(days=13, hours=23)
    recent = [m for m in found if m.created_at > cutoff]
    old = [m for m in found if m.created_at <= cutoff]

    deleted = 0
    try:
        for i in range(0, len(recent), 100):
            chunk = recent[i:i + 100]
            if len(chunk) == 1:
                await chunk[0].delete()
            else:
                await channel.delete_messages(chunk, reason=f"/trun by {interaction.user}")
            deleted += len(chunk)
        for message in old:
            await message.delete()
            deleted += 1
    except discord.HTTPException as e:
        await interaction.followup.send(f"⚠️ Deleted {deleted} of {len(found)} messages, then failed: {e}")
        return

    print(f"[trun] {interaction.user} deleted {deleted} messages of {user} in #{channel} ({interaction.guild.name})")
    note = f" (only {deleted} found out of the requested {count})" if deleted < count else ""
    await interaction.followup.send(f"🗑️ Deleted **{deleted}** message(s) from **{user.display_name}**{note}.")


@bot.tree.command(name="gif", description="Random weeb gif!")
async def gif(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://api.shinpu.top/random_gif") as resp:
                if resp.status != 200:
                    await interaction.followup.send("Could not fetch a gif right now.")
                    return
                data = await resp.json(content_type=None)

        gif_url = data.get("gif")
        if not gif_url:
            await interaction.followup.send("API didn't return a gif.")
            return

        await interaction.followup.send(gif_url)

    except Exception as e:
        await interaction.followup.send(f"❌ Error fetching gif: {e}")


# --------------------------------------------------------------------------- #
#  Keep-alive web page + start-up self check
# --------------------------------------------------------------------------- #

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is running!", 200


def run_web():
    app.run(host="0.0.0.0", port=7777, use_reloader=False)


def check_environment():
    """Prints the things that usually make YouTube playback fail."""
    from yt_dlp.version import __version__ as ytdlp_version

    print(f"[env] yt-dlp {ytdlp_version}  |  discord.py {discord.__version__}")

    if shutil.which("ffmpeg") is None:
        print("[env] !! ffmpeg NOT FOUND on PATH - install it, otherwise nothing can be played")
    else:
        print(f"[env] ffmpeg: {shutil.which('ffmpeg')}")
        try:
            encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                      capture_output=True, text=True, timeout=15).stdout
            if "libopus" not in encoders:
                print("[env] !! this ffmpeg build has no libopus encoder - the bot will connect "
                      "but play SILENCE. Install a full ffmpeg build.")
        except Exception as e:
            print(f"[env] could not inspect ffmpeg: {e!r}")

    runtimes = [name for name in ("deno", "node", "bun") if shutil.which(name)]
    if runtimes:
        print(f"[env] JS runtime on PATH: {', '.join(runtimes)}")
    else:
        print("[env] !! no JS runtime (deno / node v22+) on PATH - YouTube will return "
              "missing formats. Install deno: https://deno.com")

    try:
        from importlib import metadata
        davey_ver = metadata.version("davey")
        if davey_ver.startswith("0.") and int(davey_ver.split(".")[1]) >= 2 and discord.__version__.startswith("2.7"):
            print(f"[env] !! davey {davey_ver} with discord.py {discord.__version__} makes the bot play in SILENCE. "
                  "Run:  pip install \"davey>=0.1.6,<0.2\"")
    except Exception:
        pass

    for module, why in (("nacl", "PyNaCl (voice)"), ("davey", "davey (Discord voice E2EE, required)")):
        if importlib.util.find_spec(module) is None:
            print(f"[env] !! missing Python package: {why}  ->  pip install -U \"discord.py[voice]\"")

    if YT_COOKIES_FILE:
        print(f"[env] YouTube cookies: {YT_COOKIES_FILE}")
    else:
        print("[env] YouTube cookies: none (set YT_COOKIES_FILE in .env only if you get bot-check errors)")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing - put it in the .env file.")

    check_environment()
    threading.Thread(target=run_web, daemon=True).start()
    bot.run(TOKEN)
