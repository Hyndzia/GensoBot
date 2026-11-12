import asyncio
import importlib.util
import os
import random
import re
import shlex
import shutil
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

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

FFMPEG_BEFORE = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-protocol_whitelist file,http,https,tcp,tls,crypto,m3u8,hls"
)

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


def make_audio_source(track):
    """ffmpeg must send the same HTTP headers (User-Agent etc.) that yt-dlp used to
    get the URL - otherwise googlevideo answers 403. This is exactly what yt-dlp's
    own ffmpeg downloader does and what the old bot code was missing."""
    before = FFMPEG_BEFORE
    headers = {k: v for k, v in (track.headers or {}).items() if k.lower() != "accept-encoding"}
    if headers:
        blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        before += f" -headers {shlex.quote(blob)}"
    return discord.FFmpegOpusAudio(track.stream_url, bitrate=96, before_options=before, options="-vn")


# --------------------------------------------------------------------------- #
#  Music commands
# --------------------------------------------------------------------------- #

async def safe_disconnect(guild):
    vc = guild.voice_client
    if vc is not None and vc.is_connected():
        await vc.disconnect()


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
                source = make_audio_source(track)
            except Exception as e:
                print(f"Loading error {track.title}: {e}")
                await channel.send(f"❌ Couldn't load **{track.title}**: {friendly_error(e)} — skipping.")
                continue

            def after_play(error, title=track.title):
                if error:
                    print(f"Streaming error {title}: {error}")
                asyncio.run_coroutine_threadsafe(play_next_song(guild, channel), bot.loop)

            try:
                vc.play(source, after=after_play)
            except Exception as e:
                print(f"vc.play() failed for {track.title}: {e!r}")
                source.cleanup()
                await channel.send(f"❌ Couldn't start **{track.title}**: {e}")
                return

            await channel.send(f"▶️ Now playing: **{track.title}**")
            return

        # queue is empty -> leave the voice channel
        await safe_disconnect(guild)


@bot.tree.command(name="skip", description="Skips the current playing song")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        await interaction.response.send_message("⏭ Skipped!")
        voice_client.stop()   # the after-callback starts the next song
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

    SONG_QUEUES.get(interaction.guild_id, deque()).clear()

    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

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

    if interaction.guild is None:
        await interaction.followup.send("This command only works in a server.")
        return

    member = interaction.guild.get_member(interaction.user.id) or interaction.user
    voice_state = getattr(member, "voice", None)

    if voice_state is None or voice_state.channel is None:
        await interaction.followup.send("You must be in a voice channel to use this command.")
        return

    voice_channel = voice_state.channel
    voice_client = interaction.guild.voice_client

    try:
        if voice_client is None:
            voice_client = await voice_channel.connect(timeout=30.0, self_deaf=True)
        elif voice_channel != voice_client.channel:
            await voice_client.move_to(voice_channel)
    except Exception as e:
        print(f"Voice connect error: {e!r}")
        await interaction.followup.send(f"Couldn't join the voice channel: {e}")
        return

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

@bot.tree.command(name="radio", description="Play the Shinpu radio stream")
async def radio(interaction: discord.Interaction):
    await interaction.response.defer()

    member = interaction.guild.get_member(interaction.user.id) or interaction.user
    voice_state = getattr(member, "voice", None)

    if voice_state is None or voice_state.channel is None:
        await interaction.followup.send("You must be in a voice channel to use this command.")
        return

    voice_channel = voice_state.channel
    voice_client = interaction.guild.voice_client

    try:
        if voice_client is None:
            voice_client = await voice_channel.connect(timeout=30.0, self_deaf=True)
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)
    except Exception as e:
        await interaction.followup.send(f"Couldn't join the voice channel: {e}")
        return

    radio_url = "https://radio.shinpu.top/radio.ogg"
    source = discord.FFmpegOpusAudio(radio_url, bitrate=96, before_options=FFMPEG_BEFORE, options="-vn")

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

    runtimes = [name for name in ("deno", "node", "bun") if shutil.which(name)]
    if runtimes:
        print(f"[env] JS runtime on PATH: {', '.join(runtimes)}")
    else:
        print("[env] !! no JS runtime (deno / node v22+) on PATH - YouTube will return "
              "missing formats. Install deno: https://deno.com")

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
