import os
import re
from collections import deque


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
    os.makedirs(path, exist_ok=True)


def create_textfile(path):
    try:
        with open(path, "x", encoding="utf-8") as file:
            file.write("")
        print(f"Text file {path} successfully created!")
    except FileExistsError:
        pass


def append_to_file(path, message):
    # make sure the parent folder exists
    os.makedirs(os.path.dirname(path), exist_ok=True)

    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(message + "\n")
    except Exception as e:
        print(f"Error writing to {path}: {e}")


def create_message_buffers(channel_names, buffers_length):
    return {name: deque(maxlen=buffers_length) for name in channel_names}


def append_to_message_buffer(messages_buffer, guild_id, channel_name, message, maxlen=30):
    if guild_id not in messages_buffer:
        messages_buffer[guild_id] = {}

    if channel_name not in messages_buffer[guild_id]:
        messages_buffer[guild_id][channel_name] = deque(maxlen=maxlen)

    messages_buffer[guild_id][channel_name].append(message)


# kept for backwards compatibility with old code
safe_append_to_buffer = append_to_message_buffer


def append_buffer_to_file(buffers, channel_name, path):
    try:
        with open(path, "a", encoding="utf-8") as f:
            while True:
                f.write(buffers[channel_name].popleft() + "\n")
    except KeyError:
        print(f"No buffer for file: {path}")
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


def safe_name(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def initialize_guild_buffers(guild, channel_names_by_guild, buffers_length):
    base_dir = os.path.dirname(os.path.abspath(__file__))  # folder where main.py lives
    guild_dir = os.path.join(base_dir, "servers", safe_name(guild.name))
    create_folder(guild_dir)
    print(f"Server: {guild.name}")
    print("Text channels:")
    channel_names = []

    for channel in guild.text_channels:
        channel_names.append(channel.name)
        print(f" - {channel.name}")
        create_textfile(os.path.join(guild_dir, f"{safe_name(channel.name)}.txt"))

    channel_names_by_guild[guild.id] = channel_names
    return create_message_buffers(channel_names, buffers_length)
