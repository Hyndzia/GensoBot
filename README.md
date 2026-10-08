# GensoBot

A multifunctional Discord bot built with **Python** and **discord.py**.

GensoBot provides music playback, radio streaming, utility/fun commands, GIFs, message logging, and basic server interaction.

## Features

* 🎵 Music playback with search and playlists
* ⏯️ Pause, resume, skip and stop music
* 📻 HikiNeet Radio streaming
* 🎲 Dice rolling
* 🖼️ Random GIF command
* 💬 Automatic message logging
* 🗂️ Per-server and per-channel log files
* 🌐 Built-in HTTP endpoint for uptime/health checks
* 🔊 Discord voice channel support

## 📋 Commands

All main commands are **Discord slash commands** (`/`). The bot synchronizes its application commands when it starts.

### Music

| Command   | Usage                | Description                                                 |
| --------- | -------------------- | ----------------------------------------------------------- |
| `/play`   | `/play <song_query>` | Play a song, search YouTube, or add a playlist to the queue |
| `/skip`   | `/skip`              | Skip the currently playing song                             |
| `/pause`  | `/pause`             | Pause the current song                                      |
| `/resume` | `/resume`            | Resume paused playback                                      |
| `/stop`   | `/stop`              | Stop playback, clear the queue, and disconnect              |

The `/play` command accepts either a search query or playlist URL and maintains a separate queue for each Discord server.

> **Note:** You must be connected to a voice channel to use the music commands.

### Radio

| Command         | Usage           | Description                                       |
| --------------- | --------------- | ------------------------------------------------- |
| `/radio`        | `/radio`        | Join your voice channel and stream HikiNeet Radio |
| `/radio_status` | `/radio_status` | Display the currently playing radio track         |

The radio commands use the Shinpu radio stream and its Icecast status endpoint.

### Fun & Utility

| Command | Usage                  | Description                                         |
| ------- | ---------------------- | --------------------------------------------------- |
| `/roll` | `/roll <start> <stop>` | Generate a random number between the supplied range |
| `/gif`  | `/gif`                 | Send a random GIF                                   |

For `/roll`, the `start` value is inclusive and the `stop` value follows Python's `random.randrange()` behavior.

### Legacy / Testing

| Command | Usage         | Description                                                                           |
| ------- | ------------- | ------------------------------------------------------------------------------------- |
| `!test` | `!test <arg>` | Development/test command; currently responds to `hello` or returns a fallback message |

The bot's command prefix is `!`, although the primary user-facing commands are slash commands.

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/Hyndzia/GensoBot.git
cd GensoBot
```

### 2. Use Python 3.12

The repository specifies **Python 3.12.11** in `.python-version`.

Check your version:

```bash
python --version
```

### 3. Create a virtual environment

#### Windows

```powershell
python -m venv .venv
.venv\Scripts\activate
```

#### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

The project currently includes `discord.py`, `yt-dlp`, `PyNaCl`, `python-dotenv`, `aiohttp`, `pydub`, Flask and other dependencies.

### 5. Configure the Discord token

Create a `.env` file in the root of the project:

```env
DISCORD_TOKEN=your_discord_bot_token
```

GensoBot loads the token from the `DISCORD_TOKEN` environment variable.

**Never commit your `.env` file or bot token to GitHub.**

## Discord Bot Setup

Create an application through the Discord Developer Portal and create a bot for it.

Because GensoBot enables several Discord intents, make sure the required intents are enabled for your bot. The code uses:

* `message_content`
* `typing`
* `guilds`
* `messages`
* `voice_states`
* `members`

These intents are configured directly in `main.py`.

When inviting the bot, make sure it has the permissions necessary to:

* View channels
* Send messages
* Read message history
* Connect to voice channels
* Speak in voice channels

## FFmpeg

Music playback uses FFmpeg through `discord.FFmpegOpusAudio`, so **FFmpeg must be installed and available on your system PATH**.

Verify the installation with:

```bash
ffmpeg -version
```

If the command is not found, install FFmpeg for your operating system before using `/play` or `/radio`.

## Running the Bot

Start the bot with:

```bash
python main.py
```

On Windows, the repository also contains `bot_from_terminal.bat`, which activates the project's virtual environment and starts `main.py`. The included batch file currently assumes the project is located at `G:\DiscordBot`, so you may need to edit that path before using it.

## Project Structure

```text
GensoBot/
├── .gitignore
├── .python-version
├── README.md
├── bot_from_terminal.bat
├── general.py
├── main.py
├── requirements.txt
└── test.py
```

The bot automatically creates a `servers/` directory and stores message logs using the server and channel names.

Example:

```text
servers/
└── My Server/
    ├── general.txt
    ├── music.txt
    └── memes.txt
```

## Message Logging

GensoBot records messages from servers it is connected to and writes them to per-server/per-channel text files.

Messages are buffered before being processed, while messages containing attachments are written directly to the appropriate log file along with their attachment URL.

Keep this behavior in mind when deploying the bot on a server where message privacy is important.

## 🌐 Health Check

GensoBot also starts a small Flask web server on:

```text
http://localhost:7777/
```

The endpoint returns:

```text
Bot is running!
```

This can be used by hosting platforms or monitoring services to determine whether the process is alive.

## 🛠️ Troubleshooting

### Slash commands aren't appearing

Restart the bot and make sure it has been invited with the appropriate application-command permissions.

GensoBot calls:

```python
await bot.tree.sync()
```

when it becomes ready, which synchronizes the slash commands with Discord.

### Music isn't playing

Check that:

1. You are connected to a Discord voice channel.
2. The bot can connect and speak.
3. FFmpeg is installed.
4. `yt-dlp` is installed correctly.
5. Your server has network access to the requested media source.

### Bot doesn't start

Make sure your `.env` contains:

```env
DISCORD_TOKEN=your_token_here
```

Also verify that your virtual environment is active and dependencies are installed:

```bash
pip install -r requirements.txt
```

## 🧑‍💻 Development

Run the bot directly during development:

```bash
python main.py
```

The repository also contains `test.py` for testing/development purposes.

Contributions, bug reports and improvements are welcome.

DISCLAIMER: This is just a fun, personal project. Contact me directly in case of issues.
