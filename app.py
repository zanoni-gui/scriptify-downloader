import os
import re
import json
import tempfile
import subprocess
import logging
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask, request, jsonify
from flask_cors import CORS

# ===== Config básica
app = Flask(__name__)
CORS(app)  # CORS liberado (ajuste se precisar)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

# OpenAI opcional
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
client = None
if OPENAI_KEY:
    try:
        from openai import OpenAI
        client = OpenAI()
        log.info("OpenAI habilitado.")
    except Exception as e:
        log.warning("OpenAI não habilitado (%s). Seguirei em modo stub.", e)

# Armazenamento de cookies em memória (troque por DB depois)
COOKIES_STORE = {}  # ex.: {"instagram": [{"name":"...","value":"..."}], ...}

# ===== Utilidades
def detect_platform(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return "unknown"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "tiktok.com" in host:
        return "tiktok"
    if "instagram.com" in host:
        return "instagram"
    if "facebook.com" in host:
        return "facebook"
    return "unknown"


def build_cookies_header(platform: str) -> list[str]:
    """
    Constrói headers --add-header para o yt-dlp a partir dos cookies armazenados
    (modo simples; para casos reais, salve como Netscape cookie file e use --cookies).
    """
    cookies = COOKIES_STORE.get(platform) or []
    if not cookies:
        return []

    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value")])
    if not cookie_str:
        return []
    return ["--add-header", f"Cookie: {cookie_str}"]


def download_audio(url: str, platform: str) -> str:
    """
    Baixa/extrai o áudio em MP3 usando yt-dlp + ffmpeg.
    Retorna o caminho do arquivo de áudio temporário.
    Levanta exceção em falha.
    """
    tmp_path = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name

    # Parâmetros base
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--ffmpeg-location", "/usr/bin/ffmpeg",  # Render/Ubuntu
        "--quiet",
        "--no-warnings",
        "-o", tmp_path,
    ]

    # Cookies (se existirem para a plataforma)
    cmd.extend(build_cookies_header(platform))

    # User-Agent ajuda em alguns casos
    ua = os.getenv("YTDLP_UA", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    cmd.extend(["--user-agent", ua])

    # URL alvo
    cmd.append(url)

    log.info("Executando yt-dlp: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)

    return tmp_path


def transcribe_with_openai(audio_path: str) -> str:
    """
    Transcreve com OpenAI (se disponível). Retorna o texto.
    Levanta exceção em falha.
    """
    if not client:
        raise RuntimeError("OpenAI não configurado")

    with open(audio_path, "rb") as f:
        # Modelos possíveis: "whisper-1" (clássico) ou algum modelo de transcrição disponível na sua conta
        resp = client.audio.transcriptions.create(
            model="whisper-1",
            file=f
        )
    # openai>=1.37 retorna objeto com .text
    return getattr(resp, "text", "") or ""


def stub_text(url: str, platform: str) -> str:
    return (
        "🧠 Essa ferramenta é impressionante! 👉🏻 Se quiser receber o link é só ...\n\n"
        "Este é um texto de exemplo retornado pelo backend.\n"
        f"URL:\n{url}\n"
        f"Plataforma detectada: {platform}\n\n"
        "Quando plugarmos o motor real de transcrição, este campo trará o roteiro do vídeo."
    )


# ===== Rotas
@app.after_request
def add_cors_headers(resp):
    # CORS extra, caso precise
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok", time=datetime.utcnow().isoformat() + "Z"), 200


@app.route("/ingest-cookies", methods=["POST", "OPTIONS"])
def ingest_cookies():
    if request.method == "OPTIONS":
        return ("", 204)

    try:
        payload = request.get_json(force=True)
    except Exception as e:
        log.warning("JSON inválido no /ingest-cookies: %s", e)
        return jsonify(error="invalid_json"), 400

    domain = (payload or {}).get("domain")
    cookies = (payload or {}).get("cookies")

    if not domain or not isinstance(cookies, list):
        return jsonify(error="missing_fields",
                       detail="domain (str) e cookies (list) são obrigatórios"), 400

    # mapeia o domínio para plataforma
    platform = detect_platform(f"https://{domain}")
    if platform == "unknown":
        platform = domain  # guarda com a chave “crua”

    sanitized = []
    for c in cookies:
        name = (c or {}).get("name")
        value = (c or {}).get("value")
        if not name or value is None:
            continue
        sanitized.append({
            "name": name,
            "value": value,
            "domain": (c or {}).get("domain"),
            "path": (c or {}).get("path", "/"),
            "expires": (c or {}).get("expires"),
            "httpOnly": bool((c or {}).get("httpOnly", False)),
            "secure": bool((c or {}).get("secure", False)),
            "sameSite": (c or {}).get("sameSite"),
        })

    if not sanitized:
        return jsonify(error="no_valid_cookies"), 400

    COOKIES_STORE[platform] = sanitized
    log.info("Cookies salvos para %s: %s", platform, [c["name"] for c in sanitized])

    return jsonify(ok=True, stored=len(sanitized), platform=platform), 200


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
    log.info("Recebido link: %s (%s)", url, platform)

    # Tenta baixar áudio e transcrever
    transcript_text = None
    used_stub = False
    error_code = None
    audio_path = None

    try:
        audio_path = download_audio(url, platform)
        log.info("Áudio salvo em %s", audio_path)

        if client:
            transcript_text = transcribe_with_openai(audio_path)
        else:
            # sem OpenAI -> gera stub direto
            used_stub = True
            transcript_text = stub_text(url, platform)

    except subprocess.CalledProcessError as e:
        log.error("❌ Falha ao baixar áudio com yt-dlp: %s", e)
        used_stub = True
        error_code = "download_failed"
        transcript_text = stub_text(url, platform)
    except Exception as e:
        log.warning("⚠️ Falha real de transcrição, caindo pro modo stub: %s", e)
        used_stub = True
        error_code = "transcription_failed"
        transcript_text = stub_text(url, platform)
    finally:
        if audio_path:
            try:
                os.remove(audio_path)
            except Exception:
                pass

    return jsonify(
        ok=True,
        platform=platform,
        title="Roteiro gerado" + (" (stub)" if used_stub else ""),
        transcript=transcript_text,
        stub=used_stub,
        error=error_code
    ), 200


# ===== Inicialização local (Render usa gunicorn)
if __name__ == "__main__":
    # Em dev local: http://localhost:8000
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=False)
