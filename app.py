# app.py
import os, tempfile, pathlib, base64, re, shutil, json, time, hashlib
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
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
    return "OK - use GET /health, GET /debug, GET /ytdlp/version, POST /download, POST /cookies/push, GET /cookies/fetch", 200

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
    return None

def _supabase_upsert_cookie(domain: str, cookies_txt: str) -> dict:
    """
    Tabela: cookies (host, cookie_text)
    Upsert via on_conflict=host.
    """
    if not _supabase_can_use():
        return {"ok": False, "schema": None, "status": 400, "text": "SUPABASE not configured"}

    url = f"{SUPABASE_URL}/rest/v1/cookies?on_conflict=host"
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }

    payload = {"host": domain, "cookie_text": cookies_txt}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201, 204):
            return {"ok": True, "schema": "new", "status": r.status_code, "text": ""}
        return {"ok": False, "schema": "new", "status": r.status_code, "text": r.text}
    except Exception as e:
        return {"ok": False, "schema": "new", "status": 500, "text": str(e)}

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
LAST_COOKIE_SNAPSHOT: list[str] = []   # primeiras linhas do cookie realmente carregado
AUTH_SNAPSHOT: dict[str, str] = {}     # headers de auth gerados (p/ debug)
AUTH_USING: str | None = None          # qual cookie baseou o SAPISIDHASH
SUCCESS_CLIENT: str | None = None      # rótulo do client que funcionou

def _ensure_consent_cookie(cookie_path: str) -> str:
    """
    Garante que o cookie CONSENT exista (ajuda a evitar 'not a bot').
    Retorna o caminho (pode ser o mesmo ou um arquivo temporário ajustado).
    """
    try:
        txt = pathlib.Path(cookie_path).read_text("utf-8", "ignore")
    except Exception:
        return cookie_path
    if re.search(r"^[.]youtube[.]com\s+TRUE\s+/\s+FALSE\s+0\s+CONSENT\s+", txt, re.M):
        return cookie_path
    line = ".youtube.com\tTRUE\t/\tFALSE\t0\tCONSENT\tYES+cb.20210328-17-p0.en+FX+123\n"
    new_txt = txt if txt.endswith("\n") else (txt + "\n")
    new_txt += line
    tf = tempfile.NamedTemporaryFile(delete=False)
    tf.write(new_txt.encode("utf-8")); tf.flush(); tf.close()
    return tf.name

def _read_cookies_map(cookie_path: str) -> dict[str, str]:
    """
    Lê um cookie file (Netscape) e retorna um dict {name: value} (última ocorrência).
    """
    mp: dict[str, str] = {}
    try:
        with open(cookie_path, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                if not ln or ln.startswith("#"): 
                    continue
                parts = re.split(r"\s+", ln.strip(), maxsplit=6)
                if len(parts) < 7:
                    continue
                name, value = parts[5], parts[6]
                mp[name] = value
    except Exception:
        return {}
    return mp

def _build_sapisidhash_headers(origin: str, cookie_path: str) -> tuple[dict[str, str], str | None]:
    """
    Monta headers Authorization SAPISIDHASH a partir de SAPISID / __Secure-3PAPISID / __Secure-1PAPISID.
    Retorna ({headers}, "cookie_usado_ou_None")
    """
    cookies = _read_cookies_map(cookie_path)
    sapikey = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID") or cookies.get("__Secure-1PAPISID")
    if not sapikey:
        return ({}, None)
    ts = str(int(time.time()))
    dig = hashlib.sha1(f"{ts} {sapikey} {origin}".encode("utf-8")).hexdigest()
    headers = {
        "Authorization": f"SAPISIDHASH {ts}_{dig}",
        "X-Origin": origin,
        "X-Goog-AuthUser": "0",
    }
    used = "SAPISID" if cookies.get("SAPISID") else "__Secure-3PAPISID" if cookies.get("__Secure-3PAPISID") else "__Secure-1PAPISID"
    return (headers, used)

def _choose_cookiefile(url: str, cookies_txt: str | None, prefer: str = "auto") -> str | None:
    """
    prefer: 'auto' | 'request' | 'env' | 'supabase'
    """
    global LAST_COOKIE_SOURCE, LAST_COOKIE_SNAPSHOT
    sources = {
        "request": [_cookiefile_from_request, _cookiefile_from_env_for, _cookiefile_from_supabase],
        "env":     [_cookiefile_from_env_for, _cookiefile_from_request, _cookiefile_from_supabase],
        "supabase":[_cookiefile_from_supabase, _cookiefile_from_request, _cookiefile_from_env_for],
        "auto":    [_cookiefile_from_request, _cookiefile_from_env_for, _cookiefile_from_supabase],
    }.get(prefer or "auto", None)

    if not sources:
        sources = [_cookiefile_from_request, _cookiefile_from_env_for, _cookiefile_from_supabase]

    cf_path = None
    for fn in sources:
        cf = fn(url) if fn in (_cookiefile_from_env_for, _cookiefile_from_supabase) else fn(cookies_txt)  # type: ignore[arg-type]
        if cf:
            if fn is _cookiefile_from_request: LAST_COOKIE_SOURCE = "request"
            elif fn is _cookiefile_from_env_for: LAST_COOKIE_SOURCE = "env"
            else: LAST_COOKIE_SOURCE = "supabase"
            cf_path = cf
            break
    if not cf_path:
        LAST_COOKIE_SOURCE = "none"
        LAST_COOKIE_SNAPSHOT = []
        return None

    # Se YouTube, injeta CONSENT se faltar e guarda snapshot
    host = (urlparse(url).netloc or "").lower()
    if "youtube.com" in host or "youtu.be" in host:
        try:
            cf_path = _ensure_consent_cookie(cf_path)
        except Exception:
            pass
    try:
        lines = []
        with open(cf_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                lines.append(line.strip())
                if len(lines) >= 20:
                    break
        LAST_COOKIE_SNAPSHOT = lines
    except Exception:
        LAST_COOKIE_SNAPSHOT = []

    return cf_path

# ===================== URL helpers (YouTube) =====================
def _normalize_youtube_url(u: str) -> str:
    """
    - Converte /shorts/ID para watch?v=ID
    - Garante has_verified=1 e bpctr=9999999999
    """
    try:
        parsed = urlparse(u)
        host = (parsed.netloc or "").lower()
        if "youtube.com" not in host and "youtu.be" not in host:
            return u

        path = parsed.path or ""
        query = parse_qs(parsed.query, keep_blank_values=True)

        # shorts -> watch?v=
        if "/shorts/" in path:
            parts = path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "shorts":
                vid = parts[1]
                path = "/watch"
                query["v"] = [vid]

        # youtu.be/<ID> -> watch?v=<ID>
        if "youtu.be" in host and not query.get("v"):
            vid = path.strip("/").split("/")[0] if path.strip("/") else ""
            if vid:
                host = "www.youtube.com"
                path = "/watch"
                query["v"] = [vid]

        # flags anti-verificação
        if "has_verified" not in query:
            query["has_verified"] = ["1"]
        if "bpctr" not in query:
            query["bpctr"] = ["9999999999"]

        new_q = urlencode({k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in query.items()}, doseq=True)
        normalized = urlunparse((parsed.scheme or "https", host, path, "", new_q, ""))
        return normalized
    except Exception:
        return u

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

def _download_best_audio(url: str, cookies_txt: str | None, prefer_cookie_source: str = "auto") -> str:
    tmpdir = tempfile.mkdtemp()
    outtpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    # Normaliza YouTube
    url = _normalize_youtube_url(url)

    cookiefile = _choose_cookiefile(url, cookies_txt, prefer_cookie_source)

    host = (urlparse(url).netloc or "").lower()
    referer = (
        "https://www.youtube.com/" if "youtu" in host else
        "https://www.instagram.com/" if "instagram" in host else
        "https://www.tiktok.com/" if "tiktok" in host else
        "https://www.facebook.com/"
    )

    # UAs
    UA_DESKTOP = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36")
    UA_MOBILE = ("Mozilla/5.0 (Linux; Android 10; K) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/126.0.0.0 Mobile Safari/537.36")

    def base_headers(ua=UA_DESKTOP):
        return {
            "User-Agent": ua,
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": referer,
            "Origin": "https://www.youtube.com" if "youtu" in host else referer,
        }

    # Auth (SAPISIDHASH) se possível
    global AUTH_SNAPSHOT, AUTH_USING, SUCCESS_CLIENT
    AUTH_SNAPSHOT, AUTH_USING = {}, None
    SUCCESS_CLIENT = None

    extra_auth_headers: dict[str, str] = {}
    if cookiefile and ("youtube" in host or "youtu.be" in host):
        try:
            extra_auth_headers, AUTH_USING = _build_sapisidhash_headers("https://www.youtube.com", cookiefile)
            AUTH_SNAPSHOT = dict(extra_auth_headers)
        except Exception:
            AUTH_SNAPSHOT, AUTH_USING = {}, None

    # Cabeçalhos por client (sem header Cookie manual!)
    def headers_for(client: str, ua: str):
        hdrs = {**base_headers(ua), **extra_auth_headers}
        # X-YouTube-Client-Name/Version coerentes com client
        if client == "web":
            hdrs["X-YouTube-Client-Name"] = "1"
            hdrs["X-YouTube-Client-Version"] = "2.20241001.00.00"
        elif client == "mweb":
            hdrs["X-YouTube-Client-Name"] = "2"
            hdrs["X-YouTube-Client-Version"] = "2.20241001.00.00"
        elif client == "android":
            hdrs["X-YouTube-Client-Name"] = "3"
            hdrs["X-YouTube-Client-Version"] = "19.33.37"
        elif client == "ios":
            hdrs["X-YouTube-Client-Name"] = "5"
            hdrs["X-YouTube-Client-Version"] = "19.09.3"
        elif client in ("tv", "tv_embedded", "web_embedded"):
            hdrs["X-YouTube-Client-Name"] = "1"
            hdrs["X-YouTube-Client-Version"] = "2.20241001.00.00"
        # alguns headers de navegação comuns
        hdrs.update({
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "navigate",
            "sec-fetch-user": "?1",
            "sec-fetch-dest": "document",
            "upgrade-insecure-requests": "1",
        })
        return hdrs

    def _run(ydl_opts):
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)

    yt_fmt = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/webm/bestaudio/best"
    ig_fmt = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"
    tk_fmt = "bestaudio[ext=m4a]/bestaudio/best"
    fb_fmt = "bestaudio[ext=m4a]/bestaudio/best"
    pick_fmt = yt_fmt if ("youtu" in host) else ig_fmt if ("instagram" in host) else tk_fmt if ("tiktok" in host) else fb_fmt

    # Tentativas com diferentes clients
    attempts = [
        ("web", dict(
            http_headers=headers_for("web", UA_DESKTOP),
            extractor_args={"youtube": {"player_client": ["web"], "player_skip": ["configs"]}},
        )),
        ("mweb", dict(
            http_headers=headers_for("mweb", UA_MOBILE),
            extractor_args={"youtube": {"player_client": ["mweb"], "player_skip": ["configs"]}},
        )),
        ("web_embedded", dict(
            http_headers=headers_for("web_embedded", UA_DESKTOP),
            extractor_args={"youtube": {"player_client": ["web_embedded"], "player_skip": ["configs"]}},
        )),
        ("android", dict(
            http_headers=headers_for("android", UA_MOBILE),
            extractor_args={"youtube": {"player_client": ["android"], "player_skip": ["configs"]}},
        )),
        ("ios", dict(
            http_headers=headers_for("ios", UA_MOBILE),
            extractor_args={"youtube": {"player_client": ["ios"], "player_skip": ["configs"]}},
        )),
        ("tv", dict(
            http_headers=headers_for("tv", UA_DESKTOP),
            extractor_args={"youtube": {"player_client": ["tv"], "player_skip": ["configs"]}},
        )),
        ("tv_embedded", dict(
            http_headers=headers_for("tv_embedded", UA_DESKTOP),
            extractor_args={"youtube": {"player_client": ["tv_embedded"], "player_skip": ["configs"]}},
        )),
    ]

    errors: list[str] = []
    for label, extra in attempts:
        ydl_opts = {
            "outtmpl": outtpl,
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "format": pick_fmt, "geo_bypass": True,
            "retries": 6, "socket_timeout": 30,
            "concurrent_fragment_downloads": 1, "force_ipv4": True,
            "http_chunk_size": 10 * 1024 * 1024,
            "http_headers": extra["http_headers"],
            "prefer_ffmpeg": True,
            "ffmpeg_location": ffmpeg_location_for_ytdlp() or "ffmpeg",
            "extractor_args": extra["extractor_args"],
            "postprocessors": [],
            "allow_unplayable_formats": False,
            "noprogress": True,
        }
        try:
            _run(ydl_opts)
            SUCCESS_CLIENT = label
            break  # sucesso
        except Exception as e:
            errors.append(f"{label.upper()}: {e}")
    else:
        hint = ""
        if "instagram" in host:
            hint = " • Instagram geralmente exige cookies válidos."
        if "youtu" in host:
            hint = " • YouTube pode exigir cookies (YTDLP_COOKIES_B64 ou Supabase host=youtube.com)."
        raise RuntimeError(f"Download falhou. " + " | ".join(errors) + hint)

    # escolhe arquivo sem reencodar
    for ext in ("m4a", "webm", "opus", "mp3", "mp4", "mkv"):
        files = list(pathlib.Path(tmpdir).glob(f"*.{ext}"))
        if files:
            return str(files[0])

    raise RuntimeError("Nenhum arquivo de áudio foi baixado.")

# ===================== Rotas principais =====================
@app.route("/download", methods=["POST", "GET", "OPTIONS"])
def download():
    if request.method == "OPTIONS":
        return ("", 204)

    prefer_cookie_source = (
        (request.args.get("cookies_source") if request.method == "GET" else (request.get_json(silent=True) or {}).get("cookies_source"))
        or "auto"
    )
    if prefer_cookie_source not in ("auto", "request", "env", "supabase"):
        prefer_cookie_source = "auto"

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
        audio_path = _download_best_audio(url, cookies_txt, prefer_cookie_source)
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
        cookie_snapshot=LAST_COOKIE_SNAPSHOT[:20],
        auth_snapshot=AUTH_SNAPSHOT,
        auth_using=AUTH_USING,
        success_client=SUCCESS_CLIENT,
        path=os.environ.get("PATH", "")[:500],
    )

# ===================== Rotas: Cookies (MVP) =====================
@app.route("/cookies/push", methods=["POST", "OPTIONS"])
def cookies_push():
    if request.method == "OPTIONS":
        return ("", 204)

    payload = request.get_json(silent=True) or {}
    domain = (payload.get("domain") or payload.get("host") or "").strip().lower()
    raw = payload.get("cookies_txt") or payload.get("cookie_text") or ""

    if not domain or not raw:
        return jsonify(ok=False, error="missing domain/cookies"), 400

    sanitized = _sanitize_netscape_text(raw)
    res = _supabase_upsert_cookie(domain, sanitized)
    status = 200 if res.get("ok") else (res.get("status") or 500)
    return jsonify(res), status

@app.route("/cookies/fetch", methods=["GET"])
def cookies_fetch():
    domain = (request.args.get("domain") or request.args.get("host") or "").strip().lower()
    if not domain:
        return jsonify(ok=False, error="missing domain"), 400
    txt = _get_latest_cookies_from_supabase(domain)
    if not txt:
        return jsonify(ok=False, domain=domain, cookies_txt=None), 404
    return jsonify(ok=True, domain=domain, cookies_txt=_sanitize_netscape_text(txt)), 200

# ===================== Run (local) =====================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
