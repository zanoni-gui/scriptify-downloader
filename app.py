# app.py
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List
from urllib.parse import urlparse

from flask import Flask, jsonify, request

app = Flask(__name__)

# ------------------------------
# Logging
# ------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("scriptfy")

# ------------------------------
# CORS simples (para frontend)
# ------------------------------
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp

# ------------------------------
# Armazenamentos em memória (ephemerais no Render free)
# ------------------------------
COOKIES_DB: Dict[str, List[Dict[str, Any]]] = {}
# Se você quiser jobs mais tarde, pode usar:
JOBS: Dict[str, Dict[str, Any]] = {}

# ------------------------------
# Helpers
# ------------------------------
URL_REGEX = re.compile(r"^https?://", re.I)

def is_valid_url(url: str) -> bool:
    if not url or not URL_REGEX.match(url):
        return False
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme and parsed.netloc)
    except Exception:
        return False

def detect_platform(url: str) -> str:
    host = ""
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

def sanitize_cookie(c: Dict[str, Any]) -> Dict[str, Any] | None:
    if not isinstance(c, dict):
        return None
    name = c.get("name")
    value = c.get("value")
    if not name or value is None:
        return None
    return {
        "name": str(name),
        "value": str(value),
        "domain": c.get("domain"),
        "path": c.get("path", "/"),
        "expires": c.get("expires"),
        "httpOnly": bool(c.get("httpOnly", False)),
        "secure": bool(c.get("secure", False)),
        "sameSite": c.get("sameSite"),
    }

# ------------------------------
# Rotas
# ------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok", time=datetime.utcnow().isoformat() + "Z"), 200

@app.route("/ingest-cookies", methods=["POST", "OPTIONS"])
def ingest_cookies():
    if request.method == "OPTIONS":
        return ("", 204)

    # leitura/validação do JSON
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception as e:
        logger.warning("JSON inválido no /ingest-cookies: %s", e)
        return jsonify(error="invalid_json"), 400

    if not isinstance(payload, dict):
        return jsonify(error="invalid_payload"), 400

    domain = payload.get("domain")
    cookies = payload.get("cookies")

    if not domain or not isinstance(cookies, list):
        return jsonify(error="missing_fields", detail="domain (str) e cookies (list) são obrigatórios"), 400

    sanitized: List[Dict[str, Any]] = []
    for c in cookies:
        sc = sanitize_cookie(c)
        if sc:
            sanitized.append(sc)

    if not sanitized:
        return jsonify(error="no_valid_cookies"), 400

    # Guardamos por plataforma (derivada do domínio passado)
    platform = detect_platform(f"https://{domain.strip('.')}")
    if platform == "unknown":
        platform = "generic"

    # Mesclamos com o que já existe em memória
    exist = COOKIES_DB.get(platform, [])
    # dedup por name+domain+path
    seen = {f"{e.get('name')}|{e.get('domain')}|{e.get('path')}" for e in exist}
    for c in sanitized:
        key = f"{c['name']}|{c.get('domain')}|{c.get('path')}"
        if key not in seen:
            exist.append(c)
            seen.add(key)
    COOKIES_DB[platform] = exist

    logger.info("Cookies armazenados para %s: %s", platform, [c["name"] for c in sanitized])

    return jsonify(ok=True, platform=platform, stored=len(sanitized)), 200

@app.route("/transcribe", methods=["POST", "OPTIONS"])
def transcribe():
    if request.method == "OPTIONS":
        return ("", 204)

    try:
        data = request.get_json(silent=True) or {}
    except Exception as e:
        logger.warning("JSON inválido no /transcribe: %s", e)
        return jsonify(ok=False, error="invalid_json"), 400

    url = (data.get("url") or "").strip()

    if not is_valid_url(url):
        return jsonify(ok=False, error="invalid_url",
                       detail="Envie um campo 'url' iniciando com http(s)://"), 400

    platform = detect_platform(url)

    # Aqui viremos a buscar cookies do COOKIES_DB[platform] quando plugarmos a coleta real
    # cookies = COOKIES_DB.get(platform, [])

    # ------------------------------
    # STUB de transcrição — substitua pelo motor real (yt-dlp + whisper + OpenAI, etc.)
    # ------------------------------
    try:
        fake_title = "Roteiro gerado (stub)"
        fake_transcript = (
            "Este é um texto de exemplo retornado pelo backend.\n"
            f"URL: {url}\nPlataforma detectada: {platform}\n\n"
            "Quando plugarmos o motor real de transcrição, este campo trará o roteiro do vídeo."
        )
        logger.info("Transcribe solicitado: %s (%s)", url, platform)
        return jsonify(
            ok=True,
            platform=platform,
            title=fake_title,
            transcript=fake_transcript
        ), 200
    except Exception as e:
        logger.exception("Falha no stub de transcrição: %s", e)
        return jsonify(ok=False, error="internal_error"), 500


# ------------------------------
# Main (apenas para dev local)
# ------------------------------
if __name__ == "__main__":
    # Em dev local: python app.py
    # Em produção (Render): use o Gunicorn (start command), ex: gunicorn app:app --timeout 600
    app.run(host="0.0.0.0", port=8000, debug=False)
