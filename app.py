from urllib.parse import urlparse
import base64, tempfile, os, pathlib, yt_dlp

def cookiefile_for(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    b64 = None
    if "youtube.com" in host or "youtu.be" in host:
        b64 = os.getenv("YTDLP_COOKIES_B64", "").strip()
    elif "instagram.com" in host:
        b64 = os.getenv("IG_COOKIES_B64", "").strip()
    else:
        b64 = os.getenv("YTDLP_COOKIES_B64", "").strip()  # opcional fallback

    if not b64:
        return None

    try:
        raw = base64.b64decode(b64)
        tf = tempfile.NamedTemporaryFile(delete=False)
        tf.write(raw)
        tf.flush()
        tf.close()
        return tf.name  # caminho do cookiefile
    except Exception:
        return None

def download_mp3(url: str) -> str:
    tmpdir = tempfile.mkdtemp()
    outtpl = os.path.join(tmpdir, "%(title)s.%(ext)s")

    ydl_opts = {
        "outtmpl": outtpl,
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractaudio": True,
        "audioformat": "mp3",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        "force_ipv4": True,
        "socket_timeout": 25,
        "retries": 3,
        "geo_bypass": True,
        "concurrent_fragment_downloads": 1,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        "extractor_args": {
            "youtube": {"player_client": ["web", "android"]},
        },
    }

    cookiefile = cookiefile_for(url)
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        mp3s = list(pathlib.Path(tmpdir).glob("*.mp3"))
        if not mp3s:
            raise RuntimeError("Falha ao converter para MP3.")
        return str(mp3s[0])
    finally:
        if cookiefile and os.path.exists(cookiefile):
            try: os.remove(cookiefile)
            except: pass
