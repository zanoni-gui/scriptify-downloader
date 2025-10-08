# app.py
import os
import sys
import time
import json
import tempfile
import traceback
from urllib.parse import urlparse
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import yt_dlp

# ============== Config =================
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
ALLOW_ORIGIN     = os.getenv("CORS_ALLOW_ORIGIN", "*")
MAX_DOWNLOAD_MB  = int(os.getenv("MAX_DOWNLOAD_MB", "80"))           # limite de download para o áudio (MB)
FORCE_YTDLP_DL   = os.getenv("FORCE_YTDLP_DOWNLOAD", "false").lower() == "true"  # força fallback direto no yt-dlp

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

def _log(*args):
    print("[scriptfy]", *args, file=sys.stderr, flush=True)


# ======= Cookies: cache simples por domínio =======
COOKIES_CACHE: Dict[str, Dict[str, Any]] = {}  # { domain: {"path": "...", "text": "...", "ts": epoch} }
COOKIES_TTL   = 24 * 60 * 60  # 24h

def _save_cookies_text(cookies_text: str | None) -> str | None:
    """Salva texto Netscape em /tmp e retorna o caminho (ou None)."""
    if not cookies_text:
        return None
    path = os.path.join(tempfile.gettempdir(), "sfy-cookies.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(cookies_text.strip().replace("\r\n", "\n").replace("\r", "\n") + "\n")
    return path

def _cookies_for(url: str, incoming_text: str | None) -> str | None:
    dom = _domain(url)
    now = int(time.time())

    # se o usuário enviou texto, atualiza cache
    if incoming_text and incoming_text.strip():
        path = _save_cookies_text(incoming_text)
        COOKIES_CACHE[dom] = {"path": path, "text": incoming_text, "ts": now}
        _log(f"cookies set for {dom}, file={path}")
        return path

    # senão, tenta cache existente
    item = COOKIES_CACHE.get(dom)
    if item and now - item["ts"] < COOKIES_TTL:
        return item["path"]

    return None


# ======= yt-dlp options por host =======
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36"
)

COMMON_YDL = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "geo_bypass": True,
    "retries": 3,
    "concurrent_fragment_downloads": 3,
    "outtmpl": os.path.join(tempfile.gettempdir(), "sfy-%(id)s.%(ext)s"),
    "format": "bestaudio/best",
    "hls_prefer_native": True,
    "http_headers": {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    },
}

def _build_ydl_opts(url: str, cookiefile: str | None):
    host = _domain(url)
    opts = dict(COMMON_YDL)

    # YouTube
    if "youtube.com" in host or "youtu.be" in host:
        opts.setdefault("extractor_args", {}) \
            .setdefault("youtube", {}) \
            .setdefault("player_client", ["android", "web"])
        opts["nocheckcertificate"] = True
        opts["http_headers"].update({
            "Origin":  "https://www.youtube.com",
            "Referer": "https://www.youtube.com/",
        })

    # Instagram
    if "instagram.com" in host:
        opts.setdefault("extractor_args", {}) \
            .setdefault("instagram", {}) \
            .setdefault("approximate_date", ["True"])
        opts["http_headers"].update({
            "Origin":           "https://www.instagram.com",
            "Referer":          "https://www.instagram.com/",
            "Sec-Fetch-Site":   "same-origin",
            "Sec-Fetch-Mode":   "navigate",
            "Sec-Fetch-Dest":   "video",
        })

    # TikTok
    if "tiktok.com" in host:
        opts["http_headers"].update({
            "Origin":  "https://www.tiktok.com",
            "Referer": "https://www.tiktok.com/",
        })

    if cookiefile:
        opts["cookiefile"] = cookiefile

    return opts


# ======= Download helpers (requests + fallback yt-dlp) =======

# mapa simples content-type -> extensão recomendada
CT_TO_EXT = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "m4a",
    "audio/x-m4a": "m4a",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/webm": "webm",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "application/octet-stream": None,  # decidir pela URL
    "binary/octet-stream": None,
    "text/plain": None,  # pode ser playlist
    "application/vnd.apple.mpegurl": "m3u8",  # playlist HLS
    "application/x-mpegURL": "m3u8",         # playlist HLS
}

def _guess_ext_from_url(u: str) -> Optional[str]:
    try:
        p = urlparse(u).path.lower()
    except Exception:
        p = u.lower()
    for ext in (".m4a", ".mp3", ".mp4", ".webm", ".wav", ".ogg", ".oga", ".mpeg", ".mpga"):
        if p.endswith(ext):
            return ext.lstrip(".")
    # se contiver m3u8, indicar playlist
    if ".m3u8" in p:
        return "m3u8"
    return None

def _guess_ext(content_type: Optional[str], url: str) -> Optional[str]:
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in CT_TO_EXT:
            mapped = CT_TO_EXT[ct]
            if mapped:
                return mapped
            # None => tentar URL
    return _guess_ext_from_url(url)

def _requests_headers_for(info: dict, page_url: str) -> dict:
    hdrs = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
    host = _domain(page_url)

    # browser-like headers por host
    if "tiktok.com" in host:
        hdrs["Referer"] = "https://www.tiktok.com/"
        hdrs["Origin"]  = "https://www.tiktok.com"
    elif "instagram.com" in host:
        hdrs.update({
            "Referer":          "https://www.instagram.com/",
            "Origin":           "https://www.instagram.com",
            "Sec-Fetch-Site":   "same-origin",
            "Sec-Fetch-Mode":   "navigate",
            "Sec-Fetch-Dest":   "video",
        })
    elif "youtube.com" in host or "youtu.be" in host:
        hdrs.update({
            "Referer": "https://www.youtube.com/",
            "Origin":  "https://www.youtube.com",
        })

    # Se o yt-dlp expôs headers úteis, mescla
    if isinstance(info, dict) and isinstance(info.get("http_headers"), dict):
        hdrs.update(info["http_headers"])

    return hdrs


def _download_to_tmp_via_requests(audio_url: str, headers: dict, max_mb: int = MAX_DOWNLOAD_MB) -> str:
    """
    Baixa o recurso apontado por audio_url. Se detectar playlist HLS (.m3u8),
    lança exceção para forçar fallback pelo yt-dlp.
    Garante salvar com extensão aceita pelo Whisper.
    """
    with requests.get(audio_url, stream=True, timeout=90, headers=headers) as r:
        r.raise_for_status()

        ct = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        ext = _guess_ext(ct, audio_url)

        # Se for playlist (m3u8) ou content-type suspeito, aborta para fallback
        if ext == "m3u8" or ct in ("application/vnd.apple.mpegurl", "application/x-mpegurl"):
            raise RuntimeError("is_m3u8_playlist")

        # Se não conseguimos inferir, assume m4a (compatível na maioria dos casos)
        if not ext:
            ext = "m4a"

        tmp_path = os.path.join(
            tempfile.gettempdir(),
            f"sfy-audio-{int(time.time()*1000)}.{ext}"
        )

        size_mb = 0.0
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                size_mb += len(chunk) / (1024 * 1024)
                if size_mb > max_mb:
                    raise RuntimeError("audio_too_large")

    return tmp_path


def _download_to_tmp_fallback_with_ytdlp(page_url: str, ydl_opts: dict) -> str:
    """
    Se o link direto falhar, deixa o yt-dlp baixar o arquivo.
    Menos suscetível a 403/HLS, mas pode requerer ffmpeg no build.
    """
    outdir = tempfile.gettempdir()
    ydl_opts = dict(ydl_opts)
    ydl_opts["paths"] = {"home": outdir}
    ydl_opts["outtmpl"] = os.path.join(outdir, "sfy-dl-%(id)s.%(ext)s")
    ydl_opts["noplaylist"] = True

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(page_url, download=True)
        path = ydl.prepare_filename(info)
        if not os.path.exists(path):
            # pega o arquivo mais recente começando com sfy-dl-
            candidates = [os.path.join(outdir, f) for f in os.listdir(outdir) if f.startswith("sfy-dl-")]
            if not candidates:
                raise RuntimeError("download_file_missing")
            path = max(candidates, key=os.path.getmtime)
        return path


# ======= Rotas =======

@app.get("/health")
def health():
    return "ok", 200


@app.post("/cookies/set")
def set_cookies():
    """
    Opcional: define cookies Netscape manualmente.
    Body: { cookies: str, domains?: [ "youtube.com", "instagram.com", ... ] }
    Se 'domains' não for enviado, aplica para youtube.com e instagram.com.
    """
    data = request.get_json(force=True, silent=True) or {}
    cookies_text = (data.get("cookies") or "").strip()
    domains = data.get("domains") or ["youtube.com", "instagram.com"]

    if not cookies_text:
        return _json_error("missing_cookies", 400)

    path = _save_cookies_text(cookies_text)
    now = int(time.time())
    for d in domains:
        COOKIES_CACHE[d] = {"path": path, "text": cookies_text, "ts": now}

    _log(f"/cookies/set ok for {domains}, file={path}")
    return _json_ok({"saved_for": domains})


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
        # 1) resolve URL do áudio (sem baixar)
        ydl_opts = _build_ydl_opts(url, cookiefile)
        _log("ydl_opts for", _domain(url), json.dumps(ydl_opts.get("http_headers", {})))
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            audio_url = info.get("url")
            platform  = info.get("extractor_key")

        _log("extracted", platform, "audio_url exists:", bool(audio_url))

        audio_path: Optional[str] = None

        # Tenta primeiro via requests (salvando com extensão válida)
        if not FORCE_YTDLP_DL and audio_url:
            headers = _requests_headers_for(info, url)
            try:
                audio_path = _download_to_tmp_via_requests(audio_url, headers)
                _log("download via requests ok:", audio_path)
            except Exception as e:
                _log("requests download failed:", repr(e))
                audio_path = None  # fallback

        # Fallback: deixa o ytdlp baixar o arquivo (resiliente p/ HLS/403)
        if not audio_path:
            audio_path = _download_to_tmp_fallback_with_ytdlp(url, ydl_opts)
            _log("download via ytdlp ok:", audio_path)

        # 4) Transcreve (Whisper)
        if not oai_client:
            raise RuntimeError("openai_client_not_initialized")

        # Dica: a OpenAI checa a extensão do arquivo; agora sempre salvamos com ext suportada
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
        _log("DownloadError:", msg)
        if any(s in msg for s in ("Sign in", "login", "cookies", "HTTP Error 403")):
            code = "auth_required_youtube" if ("youtube" in host or "youtu.be" in host) \
                   else ("auth_required_instagram" if "instagram" in host else "auth_required")
            return _json_error(code, 402)
        return _json_error("download_failed", 502, detail=msg)

    except requests.HTTPError as e:
        _log("HTTPError:", repr(e))
        return _json_error("audio_fetch_failed", 502, detail=str(e))

    except RuntimeError as e:
        code = str(e)
        _log("RuntimeError:", code)
        if code == "audio_too_large":
            return _json_error("audio_too_large", 413, "Arquivo de áudio excedeu o limite.")
        if code == "is_m3u8_playlist":
            return _json_error("download_failed", 502, "Conteúdo é playlist HLS; usando fallback.")
        return _json_error("transcribe_failed", 500, detail=str(e))

    except Exception as e:
        _log("Exception:", traceback.format_exc())
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

    prompt = f"""
Gere um roteiro curto e direto no estilo "{style}" a partir desta transcrição.
- 5–12 falas curtas
- Comece com um hook forte
- Use linguagem natural e objetiva
- Formate com quebras de linha claras

Transcrição:
{transcript}
""".strip()

    try:
        resp = oai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        text = resp.choices[0].message.content.strip()
        return _json_ok({"script": text})
    except Exception as e:
        _log("script error:", repr(e))
        return _json_error("script_failed", 500, detail=str(e))


# ======= Start local =======
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
