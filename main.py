import asyncio
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
from datetime import datetime
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

    track = Track(page_url=page_url, title=title)

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