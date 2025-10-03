# app.py
from flask import Flask, request, jsonify
from datetime import datetime
import logging

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

@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok", time=datetime.utcnow().isoformat() + "Z"), 200

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

if __name__ == "__main__":
    # Útil em dev local. No Render, use gunicorn (veja abaixo).
    app.run(host="0.0.0.0", port=8000, debug=False)
