# app.py — Render
import os, tempfile, pathlib, base64
from flask import Flask, request, send_file, jsonify
import yt_dlp

app = Flask(__name__)

def _decode_cookies_to_temp() -> str | None:
    """Decodifica YTDLP_COOKIES_B64 (base64 do arquivo Netscape) para um arquivo temporário."""
    b64 = os.getenv("YTDLP_COOKIES_B64", "").strip()
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        tf = tempfile.NamedTemporaryFile(delete=False)
        tf.write(raw)
        tf.flush(); tf.close()
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
    """
    Baixa o melhor áudio possível. Tenta converter para MP3; se não rolar,
    retorna o arquivo original (m4a/webm/opus/...).
    """
    tmpdir = tempfile.mkdtemp()
    outtpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    cookiefile = _decode_cookies_to_temp()

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
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        # mp3 se ffmpeg estiver disponível
        "prefer_ffmpeg": True,
        "ffmpeg_location": "/usr/bin/ffmpeg",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
        ],
        # YouTube: alterna clientes para reduzir bloqueio
        "extractor_args": {
            "youtube": {"player_client": ["web", "android"]}
        },
    }
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        # 1) preferimos mp3
        for ext in ("mp3", "m4a", "webm", "opus", "mp4", "mkv"):
            files = list(pathlib.Path(tmpdir).glob(f"*.{ext}"))
            if files:
                return str(files[0])

        raise RuntimeError("Nenhum arquivo de áudio foi baixado.")

    finally:
        # não removemos cookiefile; é temporário e pequeno
        pass

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
