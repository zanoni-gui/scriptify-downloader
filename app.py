# app.py FINAL (Scriptfy API)
import os, sys, time, re, json, tempfile, traceback
from urllib.parse import urlparse
from typing import Optional, Dict, Any
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests, yt_dlp

# ================= Config =================
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
ALLOW_ORIGIN     = os.getenv("CORS_ALLOW_ORIGIN", "*")
GLOBAL_PROXY_URL = os.getenv("GLOBAL_PROXY_URL", "") or os.getenv("HTTP_PROXY", "")
YTDLP_PROXY_URL  = os.getenv("YTDLP_PROXY_URL", "") or GLOBAL_PROXY_URL
MAX_DOWNLOAD_MB  = int(os.getenv("MAX_DOWNLOAD_MB", "80"))

# ============== Proxy Debug ==============
def _proxy_status():
    if YTDLP_PROXY_URL:
        print(f"\033[92m[proxy] YTDLP proxy ativo → {YTDLP_PROXY_URL}\033[0m", flush=True)
    elif GLOBAL_PROXY_URL:
        print(f"\033[93m[proxy] GLOBAL proxy ativo → {GLOBAL_PROXY_URL}\033[0m", flush=True)
    else:
        print("\033[91m[proxy] Nenhum proxy configurado.\033[0m", flush=True)
_proxy_status()

# ============== OpenAI (Whisper/GPT) ==============
try:
    from openai import OpenAI
    oai_client = OpenAI(api_key=OPENAI_API_KEY)
except Exception:
    oai_client = None

# ============== Flask App ==============
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": ALLOW_ORIGIN}}, supports_credentials=True)

# ============== Utils ==============
def _log(*args, color=None):
    prefix = "[scriptfy]"
    if color == "green": prefix = f"\033[92m{prefix}\033[0m"
    elif color == "red": prefix = f"\033[91m{prefix}\033[0m"
    elif color == "yellow": prefix = f"\033[93m{prefix}\033[0m"
    print(prefix, *args, file=sys.stderr, flush=True)

def _canonical_host(u: str) -> str:
    host = (urlparse(u).netloc or "").lower()
    if "youtu" in host: return "youtube.com"
    if "instagram" in host: return "instagram.com"
    if "tiktok" in host: return "tiktok.com"
    return host

# ============== yt-dlp Config ==============
def _build_ydl_opts(url: str, cookiefile: Optional[str]):
    host = _canonical_host(url)
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

    if YTDLP_PROXY_URL:
        opts["proxy"] = YTDLP_PROXY_URL
        _log(f"Proxy aplicado → {YTDLP_PROXY_URL}", color="yellow")

    if cookiefile:
        opts["cookiefile"] = cookiefile
        _log(f"Usando cookies → {cookiefile}", color="green")
    else:
        _log("Sem cookies", color="yellow")

    headers = opts["http_headers"]
    if host == "youtube.com":
        headers.update({"Origin": "https://www.youtube.com", "Referer": url})
    elif host == "instagram.com":
        headers.update({"Origin": "https://www.instagram.com", "Referer": "https://www.instagram.com/"})
    elif host == "tiktok.com":
        headers.update({"Origin": "https://www.tiktok.com", "Referer": "https://www.tiktok.com/"})

    return opts

# ============== Downloads ==============
def _download_via_requests(audio_url, headers):
    proxies = None
    if GLOBAL_PROXY_URL or YTDLP_PROXY_URL:
        proxy = YTDLP_PROXY_URL or GLOBAL_PROXY_URL
        proxies = {"http": proxy, "https": proxy}
        _log(f"Requests com proxy → {proxy}", color="yellow")

    tmp_path = os.path.join(tempfile.gettempdir(), f"sfy-{int(time.time()*1000)}.m4a")
    with requests.get(audio_url, stream=True, timeout=90, headers=headers, proxies=proxies) as r:
        r.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)
    return tmp_path

def _download_fallback(url, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

# ============== Rotas ==============
@app.get("/")
def root():
    return "ok", 200

@app.get("/health")
def health():
    return jsonify({"ok": True, "status": "alive"}), 200

@app.route("/cookies/set", methods=["POST", "OPTIONS"])
def cookies_set():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(force=True)
    text = data.get("cookies")
    if not text:
        return jsonify({"ok": False, "error": "missing_cookies"}), 400
    path = os.path.join(tempfile.gettempdir(), "sfy-cookies.txt")
    with open(path, "w") as f: f.write(text)
    return jsonify({"ok": True, "path": path}), 200

@app.route("/transcribe", methods=["POST", "OPTIONS"])
def transcribe():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(force=True)
    url = data.get("url")
    cookies_text = data.get("cookies", "")

    if not url:
        return jsonify({"ok": False, "error": "missing_url"}), 400

    cookiefile = None
    if cookies_text:
        cookiefile = os.path.join(tempfile.gettempdir(), "sfy-cookies.txt")
        with open(cookiefile, "w") as f:
            f.write(cookies_text)

    try:
        ydl_opts = _build_ydl_opts(url, cookiefile)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            audio_url = info.get("url")

        headers = ydl_opts["http_headers"]
        try:
            path = _download_via_requests(audio_url, headers)
        except Exception:
            path = _download_fallback(url, ydl_opts)

        if not oai_client:
            raise RuntimeError("openai_client_not_initialized")

        with open(path, "rb") as f:
            tr = oai_client.audio.transcriptions.create(model="whisper-1", file=f, response_format="text")
        text = tr if isinstance(tr, str) else str(tr)
        return jsonify({"ok": True, "transcript": text})

    except Exception as e:
        _log("Erro transcribe:", traceback.format_exc(), color="red")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/script", methods=["POST", "OPTIONS"])
def script():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(force=True)
    transcript = (data.get("transcript") or "").strip()
    style = (data.get("style") or "tiktok-narrativo").strip()

    if not transcript:
        return jsonify({"ok": False, "error": "missing_transcript"}), 400

    if not oai_client:
        return jsonify({"ok": False, "error": "openai_client_not_initialized"}), 500

    prompt = f"""
Gere um roteiro curto e direto no estilo "{style}" a partir desta transcrição:
- 5–12 falas curtas
- Comece com um hook forte
- Use linguagem natural e objetiva
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
        return jsonify({"ok": True, "script": text})
    except Exception as e:
        _log("Erro script:", traceback.format_exc(), color="red")
        return jsonify({"ok": False, "error": str(e)}), 500

# ============== Run ==============
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))# app.py FINAL (Scriptfy API)
import os, sys, time, re, json, tempfile, traceback
from urllib.parse import urlparse
from typing import Optional, Dict, Any
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests, yt_dlp

# ================= Config =================
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
ALLOW_ORIGIN     = os.getenv("CORS_ALLOW_ORIGIN", "*")
GLOBAL_PROXY_URL = os.getenv("GLOBAL_PROXY_URL", "") or os.getenv("HTTP_PROXY", "")
YTDLP_PROXY_URL  = os.getenv("YTDLP_PROXY_URL", "") or GLOBAL_PROXY_URL
MAX_DOWNLOAD_MB  = int(os.getenv("MAX_DOWNLOAD_MB", "80"))

# ============== Proxy Debug ==============
def _proxy_status():
    if YTDLP_PROXY_URL:
        print(f"\033[92m[proxy] YTDLP proxy ativo → {YTDLP_PROXY_URL}\033[0m", flush=True)
    elif GLOBAL_PROXY_URL:
        print(f"\033[93m[proxy] GLOBAL proxy ativo → {GLOBAL_PROXY_URL}\033[0m", flush=True)
    else:
        print("\033[91m[proxy] Nenhum proxy configurado.\033[0m", flush=True)
_proxy_status()

# ============== OpenAI (Whisper/GPT) ==============
try:
    from openai import OpenAI
    oai_client = OpenAI(api_key=OPENAI_API_KEY)
except Exception:
    oai_client = None

# ============== Flask App ==============
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": ALLOW_ORIGIN}}, supports_credentials=True)

# ============== Utils ==============
def _log(*args, color=None):
    prefix = "[scriptfy]"
    if color == "green": prefix = f"\033[92m{prefix}\033[0m"
    elif color == "red": prefix = f"\033[91m{prefix}\033[0m"
    elif color == "yellow": prefix = f"\033[93m{prefix}\033[0m"
    print(prefix, *args, file=sys.stderr, flush=True)

def _canonical_host(u: str) -> str:
    host = (urlparse(u).netloc or "").lower()
    if "youtu" in host: return "youtube.com"
    if "instagram" in host: return "instagram.com"
    if "tiktok" in host: return "tiktok.com"
    return host

# ============== yt-dlp Config ==============
def _build_ydl_opts(url: str, cookiefile: Optional[str]):
    host = _canonical_host(url)
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

    if YTDLP_PROXY_URL:
        opts["proxy"] = YTDLP_PROXY_URL
        _log(f"Proxy aplicado → {YTDLP_PROXY_URL}", color="yellow")

    if cookiefile:
        opts["cookiefile"] = cookiefile
        _log(f"Usando cookies → {cookiefile}", color="green")
    else:
        _log("Sem cookies", color="yellow")

    headers = opts["http_headers"]
    if host == "youtube.com":
        headers.update({"Origin": "https://www.youtube.com", "Referer": url})
    elif host == "instagram.com":
        headers.update({"Origin": "https://www.instagram.com", "Referer": "https://www.instagram.com/"})
    elif host == "tiktok.com":
        headers.update({"Origin": "https://www.tiktok.com", "Referer": "https://www.tiktok.com/"})

    return opts

# ============== Downloads ==============
def _download_via_requests(audio_url, headers):
    proxies = None
    if GLOBAL_PROXY_URL or YTDLP_PROXY_URL:
        proxy = YTDLP_PROXY_URL or GLOBAL_PROXY_URL
        proxies = {"http": proxy, "https": proxy}
        _log(f"Requests com proxy → {proxy}", color="yellow")

    tmp_path = os.path.join(tempfile.gettempdir(), f"sfy-{int(time.time()*1000)}.m4a")
    with requests.get(audio_url, stream=True, timeout=90, headers=headers, proxies=proxies) as r:
        r.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)
    return tmp_path

def _download_fallback(url, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

# ============== Rotas ==============
@app.get("/")
def root():
    return "ok", 200

@app.get("/health")
def health():
    return jsonify({"ok": True, "status": "alive"}), 200

@app.route("/cookies/set", methods=["POST", "OPTIONS"])
def cookies_set():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(force=True)
    text = data.get("cookies")
    if not text:
        return jsonify({"ok": False, "error": "missing_cookies"}), 400
    path = os.path.join(tempfile.gettempdir(), "sfy-cookies.txt")
    with open(path, "w") as f: f.write(text)
    return jsonify({"ok": True, "path": path}), 200

@app.route("/transcribe", methods=["POST", "OPTIONS"])
def transcribe():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(force=True)
    url = data.get("url")
    cookies_text = data.get("cookies", "")

    if not url:
        return jsonify({"ok": False, "error": "missing_url"}), 400

    cookiefile = None
    if cookies_text:
        cookiefile = os.path.join(tempfile.gettempdir(), "sfy-cookies.txt")
        with open(cookiefile, "w") as f:
            f.write(cookies_text)

    try:
        ydl_opts = _build_ydl_opts(url, cookiefile)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            audio_url = info.get("url")

        headers = ydl_opts["http_headers"]
        try:
            path = _download_via_requests(audio_url, headers)
        except Exception:
            path = _download_fallback(url, ydl_opts)

        if not oai_client:
            raise RuntimeError("openai_client_not_initialized")

        with open(path, "rb") as f:
            tr = oai_client.audio.transcriptions.create(model="whisper-1", file=f, response_format="text")
        text = tr if isinstance(tr, str) else str(tr)
        return jsonify({"ok": True, "transcript": text})

    except Exception as e:
        _log("Erro transcribe:", traceback.format_exc(), color="red")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/script", methods=["POST", "OPTIONS"])
def script():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(force=True)
    transcript = (data.get("transcript") or "").strip()
    style = (data.get("style") or "tiktok-narrativo").strip()

    if not transcript:
        return jsonify({"ok": False, "error": "missing_transcript"}), 400

    if not oai_client:
        return jsonify({"ok": False, "error": "openai_client_not_initialized"}), 500

    prompt = f"""
Gere um roteiro curto e direto no estilo "{style}" a partir desta transcrição:
- 5–12 falas curtas
- Comece com um hook forte
- Use linguagem natural e objetiva
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
        return jsonify({"ok": True, "script": text})
    except Exception as e:
        _log("Erro script:", traceback.format_exc(), color="red")
        return jsonify({"ok": False, "error": str(e)}), 500

# ============== Run ==============
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
