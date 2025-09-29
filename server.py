import os
import tempfile
import pathlib
import base64
from flask import Flask, request, send_file, jsonify
import yt_dlp

app = Flask(__name__)

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
        "extractor_args": {"youtube": {"player_client": ["web", "android"]}},
    }

    # ---- COOKIES (Base64 -> arquivo temporário) ----
    cookies_b64 = os.getenv("YTDLP_COOKIES_B64", "")
    cookiefile_path = None
    if cookies_b64.strip():
        try:
            decoded = base64.b64decode(cookies_b64).decode("utf-8")
            tf = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
            tf.write(decoded.encode("utf-8"))
            tf.flush()
            tf.close()
            cookiefile_path = tf.name
            ydl_opts["cookiefile"] = cookiefile_path
        except Exception as e:
            print("Falha ao carregar cookies:", e)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        mp3s = list(pathlib.Path(tmpdir).glob("*.mp3"))
        if not mp3s:
            raise RuntimeError("Falha ao converter para MP3.")
        return str(mp3s[0])
    finally:
        if cookiefile_path and os.path.exists(cookiefile_path):
            try:
                os.remove(cookiefile_path)
            except Exception:
                pass

@app.post("/download")
def download():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="missing url"), 400
    try:
        mp3_path = download_mp3(url)
        return send_file(
            mp3_path,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name="audio.mp3",
        )
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/health")
def health():
    cookies_b64 = os.getenv("YTDLP_COOKIES_B64", "")
    return jsonify(
        ok=True,
        cookies=bool(cookies_b64),
        source="env"
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
