import yt_dlp

def download_mp3(query):
    ydl_opts = {
        "format": "bestaudio/best",
        "default_search": "ytsearch1",
        "noplaylist": True,
        "nocheckcertificate": True,
        "cookiefile": "ck.txt",
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "tv"],
            }
        },
        "remote_components": ["ejs:github"],
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "outtmpl": "%(title)s.%(ext)s",
        "verbose": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=True)
        entry = info.get("entries", [info])[0]
        print(f"\n✅ Downloaded: {entry.get('title')}")

if __name__ == "__main__":
    query = input("Enter video name or search term: ")
    download_mp3(query)
