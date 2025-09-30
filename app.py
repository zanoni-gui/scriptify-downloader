import os, tempfile, pathlib, base64, re, shutil
from urllib.parse import urlparse
from flask import Flask, request, send_file, jsonify
import yt_dlp

app = Flask(__name__)

# ---------------- Util ----------------
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

def _which(name: str) -> str | None:
    try:
        return shutil.which(name)
    except Exception:
        return None

# ------------- Cookies helpers -------------
def _sanitize_netscape_text(cookies_txt: str) -> str:
    """
    Normaliza texto possivelmente colado do navegador:
    - normaliza quebras de linha
    - garante cabeçalho Netscape no topo (único)
    - converte múltiplos espaços em TAB para as 7 colunas
    - remove linhas vazias
    """
    if not cookies_txt:
        return ""

    txt = cookies_txt.replace("\r\n", "\n").replace("\r", "\n")

    # Remover cabeçalhos repetidos no meio e garantir 1 no topo
    lines_raw = [l for l in txt.split("\n")]
    lines = []
    seen_header = False
    for raw in lines_raw:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# Netscape HTTP Cookie File"):
            if not seen_header:
                seen_header = True
            # Ignora cópias subsequentes
            continue
        if line.startswith("# http://curl.haxx.se/rfc/cookie_spec.html"):
            continue
        if line.startswith("# This is a generated file!  Do not edit."):
            continue

        # Mantém comentários
        if line.startswith("#"):
            lines.append(line)
            continue

        # Se não houver TAB, tenta reconstruir 7 colunas
        if "\t" not in line:
            parts = re.split(r"\s+", line, maxsplit=6)
            if len(parts) >= 7:
                line = "\t".join(parts[:6]) + "\t" + parts[6]

        lines.append(line)

    header = [
        "# Netscape HTTP Cookie File",
        "# http://curl.haxx.se/rfc/cookie_spec.html",
        "# This is a generated file!  Do not edit.",
    ]
    return "\n".join(header + lines) + "\n"

def _cookiefile_from_request(cookies_txt: str) -> str | None:
    """Converte o texto colado em um arquivo temporário Netscape válido."""
    if not cookies_txt or not cookies_txt.strip():
        return None
    sanitized = _sanitize_netscape_text(cookies_txt)
    tf = tempfile.NamedTemporaryFile(delete=False)
    tf.write(sanitized.encode("utf-8"))
    tf.flush(); tf.close()
    return tf.name

def _cookiefile_from_env_for(url: str) -> str | None:
    """
    Fallback por ambiente:
    - YTDLP_COOKIES_B64 (base64 netscape) para YouTube
    - IG_COOKIES_B64    (base64 netscape) para Instagram
    """
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

# ------------- Core download -------------
def _download_best_audio(url: str, cookies_txt: str | None) -> str:
    """
    Baixa o melhor áudio. Tenta MP3 via ffmpeg; se não, retorna formato original.
    """
    tmpdir = tempfile.mkdtemp()
    outtpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    # Cookies: BYOC (request) -> ENV -> nenhum
    cookiefile = _cookiefile_from_request(cookies_txt) or _cookiefile_from_env_for(url)

    host = (urlparse(url).netloc or "").lower()
    referer = (
        "https://www.youtube.com/" if "youtu" in host else
        "https://www.instagram.com/" if "instagram" in host else
        "https://www.tiktok.com/" if "tiktok" in host else
        "https://www.facebook.com/"
    )

    # Onde o Render instala via apt.txt
    ffmpeg_bin   = _which("ffmpeg")   or "/usr/bin/ffmpeg"
    ffprobe_bin  = _which("ffprobe")  or "/usr/bin/ffprobe"

    # yt-dlp usa ffmpeg/ffprobe; garantimos localização no PATH ou explicitamos
    ydl_opts = {
        "outtmpl": outtpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "geo_bypass": True,
        "geo_bypass_country": "BR",
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
            # alguns vídeos do YT respondem melhor com client headers
            "Origin": "https://www.youtube.com" if "youtu" in host else referer,
            "X-YouTube-Client-Name": "1",
            "X-YouTube-Client-Version": "2.20240901.00.00",
        },
        "prefer_ffmpeg": True,
        "ffmpeg_location": ffmpeg_bin,   # torna explícito
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
        ],
        "extractor_args": {
            "youtube": {"player_client": ["web", "android", "web_embedded", "ios", "tv"]},
            "tiktok": {"download_api": ["Web"]},
        },
    }
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile

    # Se por algum motivo ffmpeg não existir, remove pós-processamento (evita erro)
    if not ffmpeg_bin or not os.path.exists(ffmpeg_bin):
        ydl_opts.pop("postprocessors", None)
        ydl_opts.pop("ffmpeg_location", None)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)

    # Preferimos .mp3, senão o que veio
    for ext in ("mp3", "m4a", "webm", "opus", "mp4", "mkv"):
        files = list(pathlib.Path(tmpdir).glob(f"*.{ext}"))
        if files:
            return str(files[0])

    raise RuntimeError("Nenhum arquivo de áudio foi baixado.")

# ------------- Routes -------------
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
        ok=True,
        ffmpeg=_which("ffmpeg"),
        ffprobe=_which("ffprobe"),
        env_ffmpeg=os.getenv("FFMPEG_LOCATION"),
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
