import os, tempfile, pathlib, base64, re, shutil, json
from urllib.parse import urlparse
from flask import Flask, request, send_file, jsonify
import yt_dlp
import requests  # Supabase REST

# ===================== FFmpeg discovery =====================
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

# ===================== Flask =====================
app = Flask(__name__)

# CORS básico
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

@app.get("/")
def root():
    return "OK - use GET /health, GET /debug, GET /ytdlp/version, POST /download", 200

# ===================== Supabase (read + upsert) =====================
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or ""

def _supabase_can_use() -> bool:
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY and SUPABASE_URL.startswith("http"))

def _domain_key_for(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if "instagram.com" in host: return "instagram.com"
    if "youtube.com" in host or "youtu.be" in host: return "youtube.com"
    if "tiktok.com" in host: return "tiktok.com"
    if "facebook.com" in host or "fb.watch" in host: return "facebook.com"
    return host or "unknown"

def _get_latest_cookies_from_supabase(domain_key: str) -> str | None:
    if not _supabase_can_use():
        return None

    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    }

    # 1) novo esquema
    try:
        url = f"{SUPABASE_URL}/rest/v1/cookies"
        params = {
            "select": "cookie_text,updated_at",
            "host": f"eq.{domain_key}",
            "order": "updated_at.desc",
            "limit": 1,
        }
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code == 200:
            rows = r.json()
            if rows and rows[0].get("cookie_text"):
                return rows[0]["cookie_text"]
    except Exception:
        pass

    # 2) legado
    try:
        url = f"{SUPABASE_URL}/rest/v1/cookies"
        params = {
            "select": "cookies_txt,updated_at",
            "domain": f"eq.{domain_key}",
            "order": "updated_at.desc",
            "limit": 1,
        }
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code == 200:
            rows = r.json()
            if rows and rows[0].get("cookies_txt"):
                return rows[0]["cookies_txt"]
    except Exception:
        pass

    return None

def _supabase_upsert_cookie(domain: str, cookies_txt: str) -> dict:
    if not _supabase_can_use():
        return {"ok": False, "schema": None, "status": 400, "text": "SUPABASE not configured"}

    url = f"{SUPABASE_URL}/rest/v1/cookies"
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }

    # 1) novo
    try:
        payload_new = {"host": domain, "cookie_text": cookies_txt}
        r = requests.post(url, headers=headers, json=payload_new, timeout=15)
        if r.status_code in (200, 201, 204):
            return {"ok": True, "schema": "new", "status": r.status_code, "text": ""}
        last_err = r.text
    except Exception as e:
        last_err = str(e)

    # 2) legado
    try:
        payload_old = {"domain": domain, "cookies_txt": cookies_txt}
        r2 = requests.post(url, headers=headers, json=payload_old, timeout=15)
        if r2.status_code in (200, 201, 204):
            return {"ok": True, "schema": "old", "status": r2.status_code, "text": ""}
        return {"ok": False, "schema": None, "status": r2.status_code, "text": r2.text}
    except Exception as e2:
        return {"ok": False, "schema": None, "status": 500, "text": f"{last_err} | {e2}"}

# ===================== Helpers: cookies =====================
def _sanitize_netscape_text(cookies_txt: str) -> str:
    if not cookies_txt:
        return ""
    txt = cookies_txt.encode("utf-8", "ignore").decode("utf-8", "ignore")
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")

    lines, seen_header = [], False
    for raw in txt.split("\n"):
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith("# Netscape HTTP Cookie File"):
                seen_header = True
            lines.append(line); continue
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
    elif "tiktok.com" in host:
        var = "TK_COOKIES_B64"
    elif "facebook.com" in host or "fb.watch" in host:
        var = "FB_COOKIES_B64"

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

def _cookiefile_from_supabase(url: str) -> str | None:
    domain_key = _domain_key_for(url)
    txt = _get_latest_cookies_from_supabase(domain_key)
    if not txt:
        return None
    sanitized = _sanitize_netscape_text(txt)
    tf = tempfile.NamedTemporaryFile(delete=False)
    tf.write(sanitized.encode("utf-8")); tf.flush(); tf.close()
    return tf.name

LAST_COOKIE_SOURCE = "none"

def _choose_cookiefile(url: str, cookies_txt: str | None, prefer: str = "auto") -> str | None:
    global LAST_COOKIE_SOURCE
    sources = {
        "request": [_cookiefile_from_request, _cookiefile_from_env_for, _cookiefile_from_supabase],
        "env":     [_cookiefile_from_env_for, _cookiefile_from_request, _cookiefile_from_supabase],
        "supabase":[_cookiefile_from_supabase, _cookiefile_from_request, _cookiefile_from_env_for],
        "auto":    [_cookiefile_from_request, _cookiefile_from_env_for, _cookiefile_from_supabase],
    }.get(prefer or "auto", None)

    if not sources:
        sources = [_cookiefile_from_request, _cookiefile_from_env_for, _cookiefile_from_supabase]

    for fn in sources:
        cf = fn(url) if fn in (_cookiefile_from_env_for, _cookiefile_from_supabase) else fn(cookies_txt)  # type: ignore
        if cf:
            if fn is _cookiefile_from_request: LAST_COOKIE_SOURCE = "request"
            elif fn is _cookiefile_from_env_for: LAST_COOKIE_SOURCE = "env"
            else: LAST_COOKIE_SOURCE = "supabase"
            return cf
    LAST_COOKIE_SOURCE = "none"
    return None

# ===================== Download (yt-dlp) =====================
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

# (download code mantido igual...)

# ===================== Rotas =====================
@app.get("/health")
def health():
    return jsonify(ok=True)

@app.get("/ytdlp/version")
def ytdlp_version():
    try:
        from yt_dlp.version import __version__ as ytv
        ver = ytv
    except Exception:
        ver = getattr(yt_dlp, "__version__", "unknown")
    return jsonify(version=ver)

@app.get("/debug")
def debug():
    return jsonify(
        ffmpeg_found=bool(FFMPEG_BIN),
        ffmpeg_bin=FFMPEG_BIN,
        ffmpeg_location=ffmpeg_location_for_ytdlp(),
        supabase_enabled=_supabase_can_use(),
        env_yt=bool(os.getenv("YTDLP_COOKIES_B64")),
        env_ig=bool(os.getenv("IG_COOKIES_B64")),
        last_cookie_source=LAST_COOKIE_SOURCE,
        path=os.environ.get("PATH", "")[:500],
    )

# (demais rotas iguais: /download, /cookies/push, /cookies/fetch...)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
