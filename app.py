import os, tempfile, pathlib, base64
from flask import Flask, request, send_file, jsonify
import yt_dlp

app = Flask(__name__)  # <-- ISSO É ESSENCIAL: variável chama-se 'app'

def _decode_b64_to_temp(varname: str) -> str | None:
    b64 = os.getenv(varname, "").strip()
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        tf = tempfile.NamedTemporaryFile(delete=False)
        tf.write(raw); tf.flush(); tf.close()
        return tf.name
    except Exception:
        return None

def _guess_mimetype(path: str) -> str:
    ext = pathlib.Path(path).suffix.lower()
    return {
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".webm": "audio/webm",
        ".opus": "audio/ogg",
        ".mkv": "video/x-matroska",
    }.get(ext, "application/octet-stream")

def _download_best_audio(url: str) -> str:
    tmpdir = tempfile.mkdtemp()
    outtpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    # Cookies por domínio (YT/IG)
    cookiefile = None
    if "youtube.com" in url or "youtu.be" in url:
        cookiefile = _decode_b64_to_temp("YTDLP_COOKIES_B64")
    elif "instagram.com" in url:
        cookiefile = _decode_b64_to_temp("IG_COOKIES_B64")

    ydl_opts = {
        "outtmpl": outtpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "geo_bypass": True,
        "retries": 3,
        "socket_timeout": 25,
        "concurrent_fragment_downloads": 1,
        "force_ipv4": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.5 Mobile/15E148 Safari/604.1"
            ),
            "Referer": "https://www.tiktok.com/",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        "prefer_ffmpeg": True,
        "ffmpeg_location": "/usr/bin/ffmpeg",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
        ],
        "extractor_args": {
            "youtube": {"player_client": ["web", "android"]},
            "tiktok": {"download_api": ["Web"]},
        },
    }
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)

    # prefere .mp3, senão pega o melhor que veio
    for ext in ("mp3", "m4a", "webm", "opus", "mp4", "mkv"):
        files = list(pathlib.Path(tmpdir).glob(f"*.{ext}"))
        if files:
            return str(files[0])

    raise RuntimeError("Nenhum arquivo de áudio foi baixado.")

@app.post("/download")
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="missing url"), 400
    try:
        audio_path = _download_best_audio(url)
        return send_file(
            audio_path,
            mimetype=_guess_mimetype(audio_path),
            as_attachment=True,
            download_name=os.path.basename(audio_path),
        )
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/health")
def health():
    return jsonify(ok=True)

if __name__ == "__main__":
    # útil para rodar localmente
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
