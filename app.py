import os, tempfile, pathlib, base64, re
from urllib.parse import urlparse
from flask import Flask, request, send_file, jsonify
import yt_dlp

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


# ---------- Sanitização forte de cookies (formato Netscape) ----------
def _sanitize_netscape_text(cookies_txt: str) -> str:
    """
    Normaliza um texto possivelmente colado do navegador:
    - remove BOM e normaliza quebras de linha
    - garante cabeçalho Netscape no topo
    - converte múltiplos espaços em TABs para formar 7 colunas
    - mantém linhas de comentário (# ...)
    - remove linhas vazias
    """
    if not cookies_txt:
        return ""

    # remove BOM e normaliza quebras de linha
    txt = cookies_txt.encode("utf-8", "ignore").decode("utf-8", "ignore")
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")

    lines = []
    seen_header = False
    for raw in txt.split("\n"):
        line = raw.strip()
        if not line:
            continue

        # cabeçalho grudado no final de outra linha (caso raro)
        if "# Netscape HTTP Cookie File" in line and line != "# Netscape HTTP Cookie File":
            # força quebra antes
            parts = line.split("# Netscape HTTP Cookie File")
            if parts[0].strip():
                # primeira parte pode ser uma linha de cookie; tenta normalizar
                maybe = parts[0].strip()
                if "\t" not in maybe:
                    p = re.split(r"\s+", maybe, maxsplit=6)
                    if len(p) >= 7:
                        maybe = "\t".join(p[:6]) + "\t" + p[6]
                lines.append(maybe)
            line = "# Netscape HTTP Cookie File"

        if line.startswith("#"):
            if line.startswith("# Netscape HTTP Cookie File"):
                seen_header = True
            lines.append(line)
            continue

        # Se não tem TAB, tentamos reconstruir para 7 colunas
        # Formato: domain \t flag \t path \t secure \t expires \t name \t value
        if "\t" not in line:
            parts = re.split(r"\s+", line, maxsplit=6)
            if len(parts) >= 7:
                line = "\t".join(parts[:6]) + "\t" + parts[6]

        lines.append(line)

    # Garante o cabeçalho no topo
    if not seen_header:
        lines.insert(0, "# Netscape HTTP Cookie File")
        lines.insert(1, "# http://curl.haxx.se/rfc/cookie_spec.html")
        lines.insert(2, "# This is a generated file!  Do not edit.")

    out = "\n".join(lines).strip() + "\n"
    return out


def _cookiefile_from_env_for(url: str) -> str | None:
    """
    Fallback: se o usuário NÃO enviar cookies no request,
    tenta cookies por ambiente (útil para IG e YT).
    - YTDLP_COOKIES_B64  (base64 do cookies.txt em Netscape) para YouTube
    - IG_COOKIES_B64     (base64 do cookies.txt em Netscape) para Instagram
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
        tf.write(raw)
        tf.flush()
        tf.close()
        return tf.name
    except Exception:
        return None


def _cookiefile_from_request(cookies_txt: str) -> str | None:
    """
    Se o usuário colar cookies (formato Netscape) no request,
    sanitiza + grava em arquivo temporário e retorna o caminho.
    """
    if not cookies_txt or not cookies_txt.strip():
        return None
    sanitized = _sanitize_netscape_text(cookies_txt)
    tf = tempfile.NamedTemporaryFile(delete=False)
    tf.write(sanitized.encode("utf-8"))
    tf.flush()
    tf.close()
    return tf.name


def _download_best_audio(url: str, cookies_txt: str | None) -> str:
    """
    Baixa o melhor áudio possível do link. Tenta MP3 via ffmpeg;
    se não rolar, retorna o formato original (m4a/webm/opus/mp4/mkv).
    """
    tmpdir = tempfile.mkdtemp()
    outtpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    # Cookies: prioridade = do request (BYOC) -> env por host -> nenhum
    cookiefile = _cookiefile_from_request(cookies_txt) or _cookiefile_from_env_for(url)

    host = (urlparse(url).netloc or "").lower()
    referer = "https://www.youtube.com/" if "youtu" in host else (
        "https://www.instagram.com/" if "instagram" in host else (
            "https://www.tiktok.com/" if "tiktok" in host else "https://www.facebook.com/"
        )
    )

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
            # headers extras ajudam em alguns cenários do YT
            "Origin": "https://www.youtube.com" if "youtu" in host else referer,
            "X-YouTube-Client-Name": "1",
            "X-YouTube-Client-Version": "2.20240901.00.00",
        },
        "prefer_ffmpeg": True,
        "ffmpeg_location": "ffmpeg",  # portátil no Render
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
        ],
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
