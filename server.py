import os, tempfile, base64, pathlib
from flask import Flask, request, send_file, jsonify
import yt_dlp

app = Flask(__name__)

def make_ydl_opts(out_mp3_path, cookiefile_path=None):
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": out_mp3_path,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        # Rede robusta
        "force_ipv4": True,
        "socket_timeout": 30,
        "retries": 3,
        "geo_bypass": True,
        "concurrent_fragment_downloads": 1,
        # Cabeçalhos “mobile Safari” + referer p/ TikTok
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.5 Mobile/15E148 Safari/604.1"
            ),
            "Referer": "https://www.tiktok.com/",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        # Pequenas ajudas para YT e TikTok
        "extractor_args": {
            "youtube": {"player_client": ["web", "android"]},
            # Esses args fazem o TikTok preferir o fluxo web (evita alguns 403)
            "tiktok": {"download_api": ["Web"]},
        },
    }
    if cookiefile_path:
        ydl_opts["cookiefile"] = cookiefile_path
    return ydl_opts

@app.post("/download")
def download_audio():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="missing url"), 400

    # cookies (YT opcional, TikTok geralmente não precisa)
    cookies_b64 = os.getenv("YTDLP_COOKIES_B64", "").strip()
    cookies_txt = os.getenv("YTDLP_COOKIES", "").strip()
    cookiefile_path = None
    try:
        if cookies_b64:
            decoded = base64.b64decode(cookies_b64).decode("utf-8")
            tf = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
            tf.write(decoded.encode("utf-8")); tf.flush(); tf.close()
            cookiefile_path = tf.name
        elif cookies_txt:
            tf = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
            tf.write(cookies_txt.encode("utf-8")); tf.flush(); tf.close()
            cookiefile_path = tf.name
    except Exception as e:
        return jsonify(error=f"cookie error: {e}"), 500

    out_mp3 = tempfile.mktemp(suffix=".mp3")
    ydl_opts = make_ydl_opts(out_mp3, cookiefile_path)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return send_file(out_mp3, mimetype="audio/mpeg", as_attachment=False)
    except Exception as e:
        # Devolve erro completo para o front (facilita debug)
        return jsonify(error=str(e)), 500
    finally:
        if cookiefile_path and os.path.exists(cookiefile_path):
            try: os.remove(cookiefile_path)
            except: pass

@app.get("/health")
def health():
    return jsonify(ok=True)
