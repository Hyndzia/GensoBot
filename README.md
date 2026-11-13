# 🎧 GensoBot

A Discord bot written in [discord.py](https://github.com/Rapptz/discord.py) – plays music from YouTube,
streams internet radio, and does a few fun extras.

## ✨ Features

- 🎵 **Music** – play songs or whole playlists from YouTube (search query or URL), with a queue
- 🔁 **Loop** – repeat the current song or the whole queue
- 🔊 **Bass boost** – low / medium / high, applied instantly to songs and radio
- 📻 **Radio** – stream any direct radio URL, defaults to **HikiNeet Radio**
- 🎲 **Extras** – dice rolls and random weeb gifs
- 📝 **Logging** – every text channel is logged to `servers/<server>/<channel>.txt`

## 💬 Commands

| Command | Description |
| --- | --- |
| `/play <query or URL>` | Play a song or playlist, or add it to the queue |
| `/queue` | Show what is waiting in the queue |
| `/skip` | Skip the current song |
| `/pause` / `/resume` | Pause or resume playback |
| `/loop [mode]` | Loop the current song or the whole queue (`off` / `song` / `queue`). No argument cycles through the modes |
| `/bass_boost [level]` | Boost the bass (`off` / `low` / `medium` / `high`). No argument switches it on (medium) or off. Applied right away, the song continues from the same spot |
| `/stop` | Stop playback, clear the queue and leave the voice channel |
| `/radio [station]` | Stream a radio. No argument = HikiNeet Radio, or give a direct `http(s)` stream URL (`.pls` / `.m3u` playlist links work too) |
| `/radio_status` | Show the current song on HikiNeet Radio |
| `/roll <start> <stop>` | Roll the dice |
| `/gif` | Random weeb gif |

## 📋 Requirements

- Python **3.10+** (3.11 / 3.12 recommended)
- [ffmpeg](https://ffmpeg.org) on your `PATH` (with `libopus`, which standard builds include)
- [deno](https://deno.com) (or Node.js v22+) on your `PATH` – yt-dlp needs a JS runtime to decode YouTube streams

On Arch Linux: `sudo pacman -S ffmpeg deno`

## 🚀 Installation

```bash
git clone <your-repo-url>
cd GensoBot

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Configuration

Create a `.env` file next to `main.py`:

```env
DISCORD_TOKEN=your-bot-token

# optional
YT_COOKIES_FILE=/path/to/cookies.txt   # only if YouTube shows "confirm you're not a bot"
MAX_PLAYLIST_ITEMS=25                  # max tracks added from one playlist
```

In the [Discord Developer Portal](https://discord.com/developers/applications) enable the
**Message Content** and **Server Members** privileged intents. The bot needs the
*Send Messages*, *Connect* and *Speak* permissions (or Administrator).

### Run

```bash
python main.py
```

On start the bot prints a short self-check (ffmpeg, libopus, JS runtime, voice packages).
A small keep-alive page is served on port `7777`.

## 🛠️ Troubleshooting

- **The bot joins voice but nobody hears anything** – check `python -m discord --version`.
  `davey` 0.2.0 does not work with discord.py 2.7.1 yet, so `requirements.txt` pins `davey<0.2`.
  If you have 0.2.x installed: `pip install "davey>=0.1.6,<0.2"`.
  The pin can be removed once discord.py ships support for davey 0.2.
- **YouTube errors** – keep yt-dlp fresh: `pip install -U "yt-dlp[default]"`, and make sure `deno` or `node` is installed.
- **Nothing plays at all** – make sure `ffmpeg` is on the `PATH` of the process running the bot.

## 📁 Project structure

```
GensoBot/
├── main.py            # bot, commands, music and radio
├── general.py         # logging and message-buffer helpers
├── requirements.txt
└── servers/           # chat logs (created automatically)
```