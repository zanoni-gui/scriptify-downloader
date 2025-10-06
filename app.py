# app.py
from flask import Flask, request, jsonify
from datetime import datetime
import logging
import re
from urllib.parse import urlparse

app = Flask(__name__)

# --- Logging básico e JSON-friendly ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --- CORS simples (para testes) ---
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


# -------------------------------------------------------------
# HEALTH CHECK
# -------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok", time=datetime.utcnow().isoformat() + "Z"), 200


# -------------------------------------------------------------
# INGEST COOKIES
# -------------------------------------------------------------
@app.route("/ingest-cookies", methods=["POST", "OPTIONS"])
def ingest_cookies():
    if request.method == "OPTIONS":
        # preflight CORS
        return ("", 204)

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception as e:
        app.logger.warning("JSON inválido: %s", e)
        return jsonify(error="invalid_json"), 400

    domain = (payload or {}).get("domain")
    cookies = (payload or {}).get("cookies")

    if not domain or not isinstance(cookies, list):
        return jsonify(error="missing_fields", detail="domain (str) e cookies (list) são obrigatórios"), 400

    # Validação superficial dos cookies recebidos
    sanitized = []
    for c in cookies:
        name = (c or {}).get("name")
        value = (c or {}).get("value")
        if not name or value is None:
            continue
        # Campos opcionais aceitos: domain, path, expires, httpOnly, secure, sameSite
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

    # Aqui você pode persistir em banco (ex: Supabase) ou em cache (ex: Redis).
    # Por agora, só vamos logar (para validar fim-a-fim).
    app.logger.info("Cookies recebidos para %s: %s", domain, [c["name"] for c in sanitized])

    # Resposta enxuta p/ extensão
    return jsonify(ok=True, stored=len(sanitized)), 200


# -------------------------------------------------------------
# TRANSCRIBE
# -------------------------------------------------------------
def detect_platform(url: str) -> str:
    """Detecta a plataforma do link (YouTube, TikTok, Instagram, Facebook)"""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return "unknown"

    host = host.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "tiktok.com" in host:
        return "tiktok"
    if "instagram.com" in host:
        return "instagram"
    if "facebook.com" in host:
        return "facebook"
    return "unknown"


@app.route("/transcribe", methods=["POST", "OPTIONS"])
def transcribe():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    # validação básica
    if not url or not re.match(r"^https?://", url, re.I):
        return jsonify(ok=False, error="invalid_url",
                       detail="Envie um campo 'url' iniciando com http(s)://"), 400

    platform = detect_platform(url)

    # --- mock temporário ---
    # Aqui futuramente você vai:
    # 1) Buscar cookies do /ingest-cookies
    # 2) Usar yt-dlp + Whisper (ou API externa) para extrair o áudio e gerar o roteiro
    # 3) Retornar o texto transcrito
    fake_title = "Roteiro gerado (stub)"
    fake_transcript = (
        "🧠 Este é um texto de exemplo retornado pelo backend.\n"
        f"🎥 URL: {url}\n"
        f"🌐 Plataforma detectada: {platform}\n\n"
        "Quando o motor real de transcrição for plugado, este campo exibirá o roteiro completo do vídeo."
    )

    app.logger.info("Transcribe solicitado: %s (%s)", url, platform)
    return jsonify(ok=True,
                   platform=platform,
                   title=fake_title,
                   transcript=fake_transcript), 200


# -------------------------------------------------------------
# MAIN
# -------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
