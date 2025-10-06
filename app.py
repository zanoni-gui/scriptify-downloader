# app.py
from flask import Flask, request, jsonify
from datetime import datetime
import logging
import os
import re
import tempfile
from urllib.parse import urlparse

import yt_dlp
import imageio_ffmpeg

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# -------------------- CORS simples --------------------
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp

# -------------------- Saúde --------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok", time=datetime.utcnow().isoformat() + "Z"), 200

# -------------------- “Banco” de cookies em memória (demo) --------------------
# Em produção, salve em um DB (Supabase/Redis/etc.)
COOKIES_DB = {}  # ex.: {"instagram.com": [ {name,value,domain,...}, ... ]}

def _netscape_cookie_file(cookies: list) -> str | None:
    """Gera arquivo Netscape de cookies (se houver)."""
    if not cookies:
        return None
    fd, path = tempfile.mkstemp(prefix="cookies_", suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            name = c.get("name")
            value = c.get("value")
            if not name or value is None:
                continue
            domain = (c.get("domain") or "").strip() or "."
            path_c = c.get("path") or "/"
            secure = "TRUE" if c.get("secure") else "FALSE"
            expires = int(c.get("expires") or 0)
            domflag = "TRUE" if domain.startswith(".") else "FALSE"
            f.write(f"{domain}\t{domflag}\t{path_c}\t{secure}\t{expires}\t{name}\t{value}\n")
    return path

@app.route("/ingest-cookies", methods=["POST", "OPTIONS"])
def ingest_cookies():
    if request.method == "OPTIONS":
        return ("", 204)

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception as e:
        app.logger.warning("JSON inválido: %s", e)
        return jsonify(error="invalid_json"), 400

    domain = (payload or {}).get("domain")
    cookies = (payload or {}).get("cookies")
    if not domain or not isinstance(cookies, list):
        return jsonify(error="missing_fields",
                       detail="domain (str) e cookies (list) são obrigatórios"), 400

    sanitized = []
    for c in cookies:
        name = (c or {}).get("name")
        value = (c or {}).get("value")
        if not name or value is None:
            continue
        sanitized.append({
            "name": name,
            "value": value,
            "domain": (c or {}).get("domain") or (("." + domain) if not domain.startswith(".") else domain),
            "path": (c or {}).get("path", "/"),
            "expires": (c or {}).get("expires"),
            "httpOnly": bool((c or {}).get("httpOnly", False)),
            "secure": bool((c or {}).get("secure", False)),
            "sameSite": (c or {}).get("sameSite"),
        })

    if not sanitized:
        return jsonify(error="no_valid_cookies"), 400

    base = domain.lower()
    if base.startswith("."):
        base = base[1:]
    COOKIES_DB[base] = sanitized
    app.logger.info("Cookies salvos p/ %s: %d itens", base, len(sanitized))
    return jsonify(ok=True, stored=len(sanitized)), 200

# -------------------- Utilidades --------------------
def detect_platform(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return "unknown"
    if "youtube.com" in host or "youtu.be" in host: return "youtube"
    if "tiktok.com" in host: return "tiktok"
    if "instagram.com" in host: return "instagram"
    if "facebook.com" in host: return "facebook"
    return "unknown"

def _build_ydl_opts(url: str, platform: str, cookie_file: str | None):
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()  # garante ffmpeg (não depende de ffprobe)
    tmpdir = tempfile.mkdtemp(prefix="dl_")

    opts = {
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"},
        "ffmpeg_location": ffmpeg_bin,      # yt-dlp usará esse binário do ffmpeg
        "format": "bestaudio/best",         # baixa o melhor ÁUDIO possível, sem pós-processar
        "nocheckcertificate": True,
        "retries": 3,
        "fragment_retries": 3,
        "ignoreerrors": False,
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts

def _pick_cookies_for(url: str, platform: str):
    host = (urlparse(url).hostname or "").lower()
    candidates = [
        host,
        ("." + host) if not host.startswith(".") else host[1:],
        f"{platform}.com",
        "instagram.com",
        "tiktok.com",
        "youtube.com",
        "facebook.com",
    ]
    for key in candidates:
        if key in COOKIES_DB:
            return COOKIES_DB[key]
    return None

def _download_audio(url: str, platform: str) -> dict:
    """
    Baixa melhor trilha de ÁUDIO (m4a/webm etc.) sem pós-processar.
    Retorna: {ok, filepath, title, err, needs_cookies}
    """
    cookies = _pick_cookies_for(url, platform)
    cookie_file = _netscape_cookie_file(cookies) if cookies else None
    ydl_opts = _build_ydl_opts(url, platform, cookie_file)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            outpath = info.get("_filename")
            title = info.get("title") or ""
            return {"ok": True, "filepath": outpath, "title": title}
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        app.logger.error("yt-dlp DownloadError: %s", msg)
        need_ck = any(s in msg.lower() for s in [
            "login required", "private", "sign in", "cookies", "403", "forbidden"
        ])
        return {"ok": False, "err": msg, "needs_cookies": need_ck}
    except Exception as e:
        app.logger.exception("yt-dlp unknown error")
        return {"ok": False, "err": str(e), "needs_cookies": False}
    finally:
        if cookie_file and os.path.exists(cookie_file):
            try: os.remove(cookie_file)
            except: pass

# -------------------- Transcrever (stub por enquanto) --------------------
@app.route("/transcribe", methods=["POST", "OPTIONS"])
def transcribe():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or not re.match(r"^https?://", url, re.I):
        return jsonify(ok=False, error="invalid_url",
                       detail="Envie um campo 'url' iniciando com http(s)://"), 400

    platform = detect_platform(url)
    app.logger.info("Transcribe solicitado: %s (%s)", url, platform)

    # 1) Baixar áudio (sem pós-processamento/ffprobe)
    dl = _download_audio(url, platform)
    if not dl["ok"]:
        return jsonify(ok=False,
                       error="download_failed",
                       needs_cookies=bool(dl.get("needs_cookies")),
                       detail=dl.get("err", "")), 200

    # 2) (placeholder) – ainda não chamamos Whisper/OpenAI
    fake_title = dl.get("title") or "Roteiro gerado (stub)"
    fake_transcript = (
        "Este é um texto de exemplo retornado pelo backend.\n"
        f"URL: {url}\nPlataforma detectada: {platform}\n\n"
        "Quando plugarmos o motor real de transcrição, este campo trará o roteiro do vídeo."
    )

    return jsonify(ok=True,
                   platform=platform,
                   title=fake_title,
                   transcript=fake_transcript), 200

# -------------------- Main (apenas dev local) --------------------
if __name__ == "__main__":
    # No Render usamos gunicorn. Localmente: python app.py
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
