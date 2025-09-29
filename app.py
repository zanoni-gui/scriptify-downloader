import os, tempfile, pathlib, base64
from urllib.parse import urlparse
from flask import Flask, request, send_file, jsonify
import yt_dlp

app = Flask(__name__)

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

def _cookiefile_from_env_for(url: str) -> str | None:
    """
    Fallback: se o usuário NÃO enviar cookies no request,
    tenta cookies por ambiente (útil para IG, e YT se você quiser manter)
    - YTDLP_COOKIES_B64 (base64 do cookies.txt em Netscape) para YouTube
    - IG_COOKIES_B64     (base64 do cookies.txt em Netscape) para Instagram
    """
    host = (urlparse(url).netloc or "").lower()
    var = None
    if "youtube.com" in host or "youtu.be" in host:
        var = "YTDLP_COOKIES_B64"
    elif "instagram.com" in host:
        var = "IG_COOKIES_B64"
    else:
        var = None

    if not var:
        return None

    b64 = (os.getenv(var) or "").strip()
    if not b64:
        return None

    try:
        raw = base64.b64decode(b64)
        tf = tempfile.NamedTemporaryFile(delete=False)
        tf.write(raw); tf.flush(); tf.close()
        return tf.name
    except Exception:
        return None

def _cookiefile_from_request(cookies_txt: str) -> str | None:
    """
    Se o usuário colar cookies (formato Netscape) no request,
    gravamos em arquivo temporário e retornamos o caminho.
    """
    if not cookies_txt or not cookies_txt.strip():
        return None
    tf = tempfile.NamedTemporaryFile(delete=False)
    tf.write(cookies_txt.encode("utf-8")); tf.flush(); tf.close()
    return tf.name

def _download_best_audio(url: str, cookies_txt: str | None) -> str:
    """
    Baixa o melhor áudio possível do link. Tenta MP3 via ffmpeg;
    se não rolar, retorna o formato original (m4a/webm/opus/mp4/mkv).
    """
    tmpdir = tempfile.mkdtemp()
    outtpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    # Cookies: prioridade = do request (BYOC) -> env por host -> nenhum
    cookiefile = _cookiefile_from_request(cookies_txt) or _cookiefile_from_env_for(url)

    host = (urlparse(url).netloc or "").lower()
    referer = "https://www.youtube.com/" if "youtu" in host else (
        "https://www.instagram.com/" if "instagram" in host else (
            "https://www.tiktok.com/" if "tiktok" in host else "https://www.facebook.com/"
        )
    )

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
            "Referer": referer,
        },
        "prefer_ffmpeg": True,
        "ffmpeg_location": "/usr/bin/ffmpeg",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
        ],
        "extractor_args": {
            "youtube": {"player_client": ["web", "android", "web_embedded", "ios"]},
            "tiktok": {"download_api": ["Web"]},
        },
    }
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)

    # Preferimos .mp3, senão pegamos o melhor que veio
    for ext in ("mp3", "m4a", "webm", "opus", "mp4", "mkv"):
        files = list(pathlib.Path(tmpdir).glob(f"*.{ext}"))
        if files:
            return str(files[0])

    raise RuntimeError("Nenhum arquivo de áudio foi baixado.")

@app.post("/download")
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    cookies_txt = (data.get("cookies_txt") or "")
    if not url:
        return jsonify(error="missing url"), 400
    try:
        audio_path = _download_best_audio(url, cookies_txt)
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
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
