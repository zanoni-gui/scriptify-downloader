import os, tempfile, pathlib, base64, re, shutil
from urllib.parse import urlparse
from flask import Flask, request, send_file, jsonify
import yt_dlp

# ---------- FFmpeg discovery ----------
FFMPEG_BIN = shutil.which("ffmpeg")
try:
    if not FFMPEG_BIN:
        import imageio_ffmpeg
        FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_BIN = shutil.which("ffmpeg")

def ffmpeg_location_for_ytdlp() -> str | None:
    if not FFMPEG_BIN:
        return None
    p = pathlib.Path(FFMPEG_BIN)
    return str(p.parent) if p.exists() else str(FFMPEG_BIN)


app = Flask(__name__)

# ---------- CORS básico ----------
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

@app.get("/")
def root():
    return "OK - use GET /health ou POST /download", 200


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
    """
    Se nada vier na requisição, tenta cookies de env (base64):
      - YTDLP_COOKIES_B64 p/ YouTube
      - IG_COOKIES_B64    p/ Instagram
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


def _cookiefile_from_request(cookies_txt: str) -> str | None:
    if not cookies_txt or not cookies_txt.strip():
        return None
    sanitized = _sanitize_netscape_text(cookies_txt)
    tf = tempfile.NamedTemporaryFile(delete=False)
    tf.write(sanitized.encode("utf-8")); tf.flush(); tf.close()
    return tf.name


# ---------- Download: SEM reencode (mantém trilha original) ----------
def _download_best_audio(url: str, cookies_txt: str | None) -> str:
    """
    Baixa a melhor trilha de ÁUDIO possível SEM reencodar.
    - Priorizamos formatos originais (m4a/webm/opus) p/ máxima fidelidade.
    - Cookies por domínio (request > env).
    - 1ª tentativa: client ANDROID (YT) com chunk; 2ª: WEB.
    - Sem FFmpegExtractAudio (que costuma reencodar p/ mp3).
    """
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

    # Cabeçalhos “realistas”
    common_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": referer,
        "Origin": "https://www.youtube.com" if "youtu" in host else referer,
    }

    def _run(ydl_opts):
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)

    # Formatos por domínio (sem reencodar):
    # - YouTube: m4a (AAC) costuma vir "limpo"; senão webm/opus.
    # - Instagram: frequentemente mp4/m4a.
    # - TikTok: m4a/webm (varia).
    yt_fmt = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/webm/bestaudio/best"
    ig_fmt = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"
    tk_fmt = "bestaudio[ext=m4a]/bestaudio/best"
    fb_fmt = "bestaudio[ext=m4a]/bestaudio/best"

    pick_fmt = (
        yt_fmt if ("youtu" in host) else
        ig_fmt if ("instagram" in host) else
        tk_fmt if ("tiktok" in host) else
        fb_fmt
    )

    # ===== 1ª tentativa: ANDROID (YT) com chunks (mitiga 403) =====
    ydl_opts_android = {
        "outtmpl": outtpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": pick_fmt,
        "geo_bypass": True,
        "retries": 10,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1,
        "force_ipv4": True,
        "http_chunk_size": 10 * 1024 * 1024,  # ~10MB
        "http_headers": {**common_headers},
        "prefer_ffmpeg": True,
        "ffmpeg_location": ffmpeg_location_for_ytdlp() or "ffmpeg",
        "extractor_args": {
            "youtube": {
                "player_client": ["android"],
                "player_skip": ["configs"],
            },
            "tiktok": {"download_api": ["Web"]},
            # Instagram tende a exigir cookies válidos; sem “magia” aqui
        },
        # IMPORTANTE: sem postprocessors (evita reencode/conversão)
        "postprocessors": [],
        "allow_unplayable_formats": False,
        "noprogress": True,
    }

    try:
        _run(ydl_opts_android)
    except Exception as e_android:
        # ===== 2ª tentativa: WEB =====
        ydl_opts_web = {
            "outtmpl": outtpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": pick_fmt,
            "geo_bypass": True,
            "retries": 10,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 1,
            "force_ipv4": True,
            "http_chunk_size": 10 * 1024 * 1024,
            "http_headers": {
                **common_headers,
                "X-YouTube-Client-Name": "1",
                "X-YouTube-Client-Version": "2.20240901.00.00",
            },
            "prefer_ffmpeg": True,
            "ffmpeg_location": ffmpeg_location_for_ytdlp() or "ffmpeg",
            "extractor_args": {
                "youtube": {"player_client": ["web", "web_embedded", "ios"]},
                "tiktok": {"download_api": ["Web"]},
            },
            "postprocessors": [],
            "allow_unplayable_formats": False,
            "noprogress": True,
        }
        try:
            _run(ydl_opts_web)
        except Exception as e_web:
            raise RuntimeError(f"YT fallback falhou. ANDROID: {e_android} | WEB: {e_web}")

    # Pegamos o melhor que veio (sem converter). Ordem de preferência:
    for ext in ("m4a", "webm", "opus", "mp3", "mp4", "mkv"):
        files = list(pathlib.Path(tmpdir).glob(f"*.{ext}"))
        if files:
            return str(files[0])

    raise RuntimeError("Nenhum arquivo de áudio foi baixado.")


# ---------- Rotas ----------
@app.route("/download", methods=["POST", "GET", "OPTIONS"])
def download():
    if request.method == "OPTIONS":
        return ("", 204)

    if request.method == "GET":
        url = (request.args.get("url") or "").strip()
        cookies_b64 = request.args.get("cookies_b64") or ""
        cookies_txt = ""
        if cookies_b64:
            try:
                cookies_txt = base64.b64decode(cookies_b64).decode("utf-8", "ignore")
            except Exception:
                cookies_txt = ""
    else:
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
        path=os.environ.get("PATH", "")[:500],
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
