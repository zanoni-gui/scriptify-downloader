# app.py (versão aprimorada)
import os
import sys
import time
import re
import json
import tempfile
import traceback
from urllib.parse import urlparse, parse_qs, urlencode
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import yt_dlp

# ============== Config =================
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
ALLOW_ORIGIN     = os.getenv("CORS_ALLOW_ORIGIN", "*")
MAX_DOWNLOAD_MB  = int(os.getenv("MAX_DOWNLOAD_MB", "80"))
FORCE_YTDLP_DL   = os.getenv("FORCE_YTDLP_DOWNLOAD", "false").lower() == "true"

# Proxy (Render + fallback local)
GLOBAL_PROXY_URL = os.getenv("GLOBAL_PROXY_URL", "") or os.getenv("HTTP_PROXY", "")
YTDLP_PROXY_URL  = os.getenv("YTDLP_PROXY_URL", "") or GLOBAL_PROXY_URL

# ============== Proxy Debug Helper ==============
def _proxy_status():
    if YTDLP_PROXY_URL:
        print(f"\033[92m[proxy] YTDLP proxy ativo → {YTDLP_PROXY_URL}\033[0m", flush=True)
    elif GLOBAL_PROXY_URL:
        print(f"\033[93m[proxy] GLOBAL proxy ativo → {GLOBAL_PROXY_URL}\033[0m", flush=True)
    else:
        print("\033[91m[proxy] Nenhum proxy configurado.\033[0m", flush=True)

_proxy_status()

# OpenAI (SDK 1.x)
try:
    from openai import OpenAI
    oai_client = OpenAI(api_key=OPENAI_API_KEY)
except Exception:
    oai_client = None

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": ALLOW_ORIGIN}}, supports_credentials=True)

# ======= Helpers =======
def _log(*args, color=None):
    """Log com cores no Render para depuração."""
    prefix = "[scriptfy]"
    if color == "green": prefix = f"\033[92m{prefix}\033[0m"
    elif color == "red": prefix = f"\033[91m{prefix}\033[0m"
    elif color == "yellow": prefix = f"\033[93m{prefix}\033[0m"
    print(prefix, *args, file=sys.stderr, flush=True)

def _domain(url: str) -> str:
    try: return urlparse(url).netloc.lower()
    except Exception: return ""

def _canonical_host(host: str) -> str:
    h = (host or "").lower()
    if "youtu" in h: return "youtube.com"
    if "instagram" in h: return "instagram.com"
    if "tiktok" in h: return "tiktok.com"
    return h

# ==========================================================
# 🔧 Inserção principal de Proxy no yt-dlp
# ==========================================================
def _build_ydl_opts(url: str, cookiefile: str | None):
    host = _canonical_host(_domain(url))
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "retries": 3,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": os.path.join(tempfile.gettempdir(), "sfy-%(id)s.%(ext)s"),
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/123.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        },
    }

    # Se proxy ativo, injeta
    if YTDLP_PROXY_URL:
        opts["proxy"] = YTDLP_PROXY_URL
        _log(f"Proxy aplicado para {host}: {YTDLP_PROXY_URL}", color="yellow")

    if cookiefile:
        opts["cookiefile"] = cookiefile
        _log(f"Usando cookies para {host}: {cookiefile}", color="green")
    else:
        _log(f"Sem cookies para {host}", color="yellow")

    # Headers específicos
    if host == "youtube.com":
        opts["http_headers"].update({
            "Origin":  "https://www.youtube.com",
            "Referer": url
        })
    elif host == "instagram.com":
        opts["http_headers"].update({
            "Origin": "https://www.instagram.com",
            "Referer": "https://www.instagram.com/",
        })
    elif host == "tiktok.com":
        opts["http_headers"].update({
            "Origin": "https://www.tiktok.com",
            "Referer": "https://www.tiktok.com/",
        })

    return opts

# ==========================================================
# 🔧 Proxy também aplicado no requests (download direto)
# ==========================================================
def _download_to_tmp_via_requests(audio_url: str, headers: dict, max_mb: int = 80) -> str:
    proxies = None
    if YTDLP_PROXY_URL or GLOBAL_PROXY_URL:
        proxy = YTDLP_PROXY_URL or GLOBAL_PROXY_URL
        proxies = {"http": proxy, "https": proxy}
        _log(f"Requests usando proxy: {proxy}", color="yellow")

    with requests.get(audio_url, stream=True, timeout=90, headers=headers, proxies=proxies) as r:
        r.raise_for_status()
        tmp_path = os.path.join(tempfile.gettempdir(), f"sfy-{int(time.time()*1000)}.m4a")
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
        return tmp_path

# ==========================================================
# 🔧 yt-dlp fallback (download completo)
# ==========================================================
def _download_to_tmp_fallback_with_ytdlp(url: str, ydl_opts: dict) -> str:
    if YTDLP_PROXY_URL:
        ydl_opts["proxy"] = YTDLP_PROXY_URL
        _log(f"yt-dlp com proxy: {YTDLP_PROXY_URL}", color="yellow")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

# ==========================================================
# 🚀 Demais rotas permanecem iguais (health, cookies, transcribe, script)
# ==========================================================
# Mantém o restante do código idêntico ao teu (rotas, transcrição e script)

# ======= Start local =======
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
