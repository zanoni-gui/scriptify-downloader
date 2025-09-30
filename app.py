import os, tempfile, pathlib, base64, re, shutil
from urllib.parse import urlparse
from flask import Flask, request, send_file, jsonify
import yt_dlp

# tenta resolver um FFmpeg local (via PATH) ou embutido (imageio-ffmpeg)
FFMPEG_BIN = shutil.which("ffmpeg")
try:
    if not FFMPEG_BIN:
        import imageio_ffmpeg
        FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()  # caminho completo do binário
except Exception:
    FFMPEG_BIN = shutil.which("ffmpeg")  # última tentativa

def ffmpeg_location_for_ytdlp() -> str | None:
    """
    yt-dlp aceita:
      - caminho da pasta contendo ffmpeg/ffprobe
      - OU caminho do executável
    Vamos devolver a pasta se possível, senão o executável.
    """
    if not FFMPEG_BIN:
        return None
    p = pathlib.Path(FFMPEG_BIN)
    return str(p.parent) if p.exists() else str(FFMPEG_BIN)

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

# ---------- Sanitização de cookies colados (Netscape) ----------
def _sanitize_netscape_text(cookies_txt: str) -> str:
    """
    Normaliza um texto possivelmente colado do navegador:
    - remove BOM e normaliza quebras de linha
    - garante cabeçalho Netscape
    - reconstrói linhas sem TAB (7 colunas) separando por espaços
    - remove linhas vazias em excesso
    """
    if not cookies_txt:
        return ""
    txt = cookies_txt.encode("utf-8", "ignore").decode("utf-8", "ignore")
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")

    lines, seen_header = [], False
    for raw in txt.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith("# Netscape HTTP Cookie File"):
                seen_header = True
            lines.append(line)
            continue
        # Se a linha não tem TAB, tenta reconstruir: 7 colunas
        if "\t" not in line:
            parts = re.split(r"\s+", line, maxsplit=6)
            if len(parts) >= 7:
                line = "\t".join(parts[:6]) + "\t" + parts[6]
        lines.append(line)

    if not seen_header:
        lines.insert(0, "# Netscape HTTP Cookie File")
        lines.insert(1, "# http://curl.haxx.se/rfc/cookie_spec.html")
        lines.insert(2, "# This is a generated file!  Do not edit.")
    return "\n".join(lines) + "\n"

def _cookiefile_from_env_for(url: str) -> str | None:
    host = (urlparse(url).netloc or "").lower()
    var = None
    if "youtube.com" in host or "youtu.be" in host:
        var = "YTDLP_COOKIES_B64"
    elif "instagram.com" in host:
        var = "IG_COOKIES_B64"

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
    if not cookies_txt or not cookies_txt.strip():
        return None
    sanitized = _sanitize_netscape_text(cookies_txt)
    tf = tempfile.NamedTemporaryFile(delete=False)
    tf.write(sanitized.encode("utf-8")); tf.flush(); tf.close()
    return tf.name

def _download_best_audio(url: str, cookies_txt: str | None) -> str:
    tmpdir = tempfile.mkdtemp()
    outtpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    cookiefile = _cookiefile_from_request(cookies_txt) or _cookiefile_from_env_for(url)

    host = (urlparse(url).netloc or "").lower()
    referer = (
        "https://www.youtube.com/" if "youtu" in host else
        "https://www.instagram.com/" if "instagram" in host else
        "https://www.tiktok.com/" if "tiktok" in host else
        "https://www.facebook.com/"
    )

    # Se o ffmpeg embutido existir, coloca na frente do PATH (por via das dúvidas)
    ff_loc = ffmpeg_location_for_ytdlp()
    if ff_loc:
        os.environ["PATH"] = f"{ff_loc}:{os.environ.get('PATH','')}"

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
        # usa ffmpeg se estiver disponível
        "prefer_ffmpeg": bool(ff_loc),
        "ffmpeg_location": ff_loc,  # pode ser None; yt-dlp lida com isso
        # pós-processamento: tenta mp3 se houver ffmpeg; caso contrário, deixa original (m4a/webm/opus…)
        "postprocessors": (
            [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
            if ff_loc else []
        ),
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

@app.get("/debug")
def debug():
    return jsonify(
        ffmpeg_found=bool(FFMPEG_BIN),
        ffmpeg_bin=FFMPEG_BIN,
        ffmpeg_location=ffmpeg_location_for_ytdlp(),
        path=os.environ.get("PATH","")[:500],
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
