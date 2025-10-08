# app.py
import os
import time
import tempfile
import traceback
from urllib.parse import urlparse

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import yt_dlp

# ============== Config =================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ALLOW_ORIGIN   = os.getenv("CORS_ALLOW_ORIGIN", "*")
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "80"))   # limite de download para o áudio

# OpenAI (SDK 1.x)
try:
    from openai import OpenAI
    oai_client = OpenAI(api_key=OPENAI_API_KEY)
except Exception:
    oai_client = None

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": ALLOW_ORIGIN}}, supports_credentials=True)

# ======= Helpers comuns =======
def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""

def _json_error(code: str, http=400, detail: str | None = None):
    payload = {"ok": False, "error": code}
    if detail:
        payload["detail"] = detail[:1000]
    return jsonify(payload), http

def _json_ok(data: dict, http=200):
    data = {"ok": True, **data}
    return jsonify(data), http


# ======= Cookies: cache simples por domínio =======
COOKIES_CACHE = {}  # { domain: {"path": "...", "text": "...", "ts": epoch} }
COOKIES_TTL   = 24 * 60 * 60  # 24h

def _save_cookies_text(cookies_text: str | None) -> str | None:
    """Salva texto Netscape em /tmp e retorna o caminho (ou None)."""
    if not cookies_text:
        return None
    path = os.path.join(tempfile.gettempdir(), "sfy-cookies.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(cookies_text.strip() + "\n")
    return path

def _cookies_for(url: str, incoming_text: str | None) -> str | None:
    dom = _domain(url)
    now = int(time.time())

    # se o usuário enviou texto, atualiza cache
    if incoming_text and incoming_text.strip():
        path = _save_cookies_text(incoming_text)
        COOKIES_CACHE[dom] = {"path": path, "text": incoming_text, "ts": now}
        return path

    # senão, tenta cache
    item = COOKIES_CACHE.get(dom)
    if item and now - item["ts"] < COOKIES_TTL:
        return item["path"]

    return None


# ======= yt-dlp options por host =======
COMMON_YDL = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "geo_bypass": True,
    "retries": 3,
    "concurrent_fragment_downloads": 3,
    "outtmpl": os.path.join(tempfile.gettempdir(), "sfy-%(id)s.%(ext)s"),
    "format": "bestaudio/best",
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/123.0 Safari/537.36"
    },
}

def _build_ydl_opts(url: str, cookiefile: str | None):
    host = _domain(url)
    opts = dict(COMMON_YDL)

    if "youtube.com" in host or "youtu.be" in host:
        # melhora a taxa de sucesso sem login (ainda pode pedir cookies)
        opts.setdefault("extractor_args", {}) \
            .setdefault("youtube", {}) \
            .setdefault("player_client", ["android", "web"])
        opts["nocheckcertificate"] = True

    if "instagram.com" in host:
        # IG costuma exigir cookies mesmo; isso só ajuda metadados
        opts.setdefault("extractor_args", {}) \
            .setdefault("instagram", {}) \
            .setdefault("approximate_date", ["True"])

    if cookiefile:
        opts["cookiefile"] = cookiefile

    return opts


# ======= Download de áudio (stream -> arquivo) =======
def _download_to_tmp(audio_url: str) -> str:
    tmp_path = os.path.join(tempfile.gettempdir(), f"sfy-audio-{int(time.time()*1000)}.m4a")
    with requests.get(audio_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        size_mb = 0.0
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                size_mb += len(chunk) / (1024 * 1024)
                if size_mb > MAX_DOWNLOAD_MB:
                    raise RuntimeError("audio_too_large")
    return tmp_path


# ======= Rotas =======

@app.get("/health")
def health():
    return "ok", 200

@app.post("/transcribe")
def transcribe():
    """
    Body: { url: str, cookies?: str }
    Ret:  { ok: true, transcript: { text: str }, platform?: str }
    """
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    cookies_text = data.get("cookies")

    if not url:
        return _json_error("invalid_url", 400)

    cookiefile = _cookies_for(url, cookies_text)

    try:
        # 1) resolve URL do áudio com yt-dlp (sem baixar ainda)
        ydl_opts = _build_ydl_opts(url, cookiefile)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            audio_url = info.get("url")
            platform  = info.get("extractor_key")
            if not audio_url:
                raise RuntimeError("audio_url_not_found")

        # 2) baixa arquivo (OpenAI precisa de arquivo)
        audio_path = _download_to_tmp(audio_url)

        # 3) manda para Whisper (OpenAI)
        if not oai_client:
            raise RuntimeError("openai_client_not_initialized")

        with open(audio_path, "rb") as f:
            tr = oai_client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
                temperature=0
            )
        text = tr if isinstance(tr, str) else str(tr)

        return _json_ok({"transcript": {"text": text}, "platform": platform})

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        host = _domain(url)
        if "Sign in" in msg or "login" in msg or "cookies" in msg:
            code = "auth_required_youtube" if ("youtube" in host or "youtu.be" in host) \
                   else ("auth_required_instagram" if "instagram" in host else "auth_required")
            return _json_error(code, 402)
        return _json_error("download_failed", 502, detail=msg)

    except requests.HTTPError as e:
        return _json_error("audio_fetch_failed", 502, detail=str(e))

    except RuntimeError as e:
        code = str(e)
        if code == "audio_too_large":
            return _json_error("audio_too_large", 413, "Arquivo de áudio excedeu o limite.")
        return _json_error("transcribe_failed", 500, detail=str(e))

    except Exception as e:
        return _json_error("transcribe_failed", 500, detail=traceback.format_exc())

@app.post("/script")
def script():
    """
    Body: { transcript: str, style?: str }
    Ret:  { ok: true, script: str }
    """
    data = request.get_json(force=True, silent=True) or {}
    transcript = (data.get("transcript") or "").strip()
    style = (data.get("style") or "tiktok-narrativo").strip()

    if not transcript:
        return _json_error("missing_transcript", 400)

    if not oai_client:
        return _json_error("openai_client_not_initialized", 500)

    # Prompt simples; ajuste como quiser
    prompt = f"""
Gere um roteiro curto e direto no estilo "{style}" a partir desta transcrição.
- 5–12 falas curtas
- Comece com um hook forte
- Use linguagem natural e objetiva
- Formate com quebras de linha claras

Transcrição:
{transcript}
"""

    try:
        resp = oai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        text = resp.choices[0].message.content.strip()
        return _json_ok({"script": text})
    except Exception as e:
        return _json_error("script_failed", 500, detail=str(e))


# ======= Start local =======
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
