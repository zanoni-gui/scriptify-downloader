import os
import re
import tempfile
import subprocess
from datetime import datetime
from urllib.parse import urlparse
from flask import Flask, request, jsonify
import logging
from openai import OpenAI

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("scriptfy")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("❌ OPENAI_API_KEY não configurada no Render.")

client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------------------
# Funções auxiliares
# ---------------------------
def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "tiktok.com" in host:
        return "tiktok"
    if "instagram.com" in host:
        return "instagram"
    if "facebook.com" in host:
        return "facebook"
    return "unknown"

def download_audio(url: str) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp.close()
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--quiet",
        "--no-warnings",
        "-o", tmp.name,
        url,
    ]
    subprocess.run(cmd, check=True)
    return tmp.name

def transcrever_audio(filepath: str) -> str:
    with open(filepath, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=audio_file
        )
    return transcript.text

def gerar_roteiro(texto: str) -> str:
    prompt = f"""
    Você é um roteirista profissional. Pegue esta transcrição e transforme em um roteiro estruturado e coeso,
    com título, introdução e trechos divididos em cenas, mantendo naturalidade e clareza.

    Transcrição:
    {texto}
    """
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6,
    )
    return response.choices[0].message.content.strip()

# ---------------------------
# Rotas
# ---------------------------
@app.after_request
def cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp

@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok", time=datetime.utcnow().isoformat() + "Z")

@app.route("/transcribe", methods=["POST", "OPTIONS"])
def transcribe():
    if request.method == "OPTIONS":
        return ("", 204)

    try:
        data = request.get_json(force=True)
        url = data.get("url", "").strip()
        if not re.match(r"^https?://", url):
            return jsonify(ok=False, error="invalid_url"), 400

        plataforma = detect_platform(url)
        logger.info("Recebido link: %s (%s)", url, plataforma)

        # 1️⃣ Download do áudio
        audio_path = download_audio(url)
        logger.info("Áudio baixado: %s", audio_path)

        # 2️⃣ Transcrição
        texto = transcrever_audio(audio_path)
        logger.info("Transcrição concluída (%d caracteres)", len(texto))

        # 3️⃣ Geração do roteiro
        roteiro = gerar_roteiro(texto)

        # 4️⃣ Limpeza temporária
        os.remove(audio_path)

        return jsonify(
            ok=True,
            platform=plataforma,
            title="Roteiro gerado automaticamente",
            transcript=roteiro
        ), 200

    except subprocess.CalledProcessError:
        logger.exception("Falha ao baixar áudio com yt-dlp")
        return jsonify(ok=False, error="download_failed"), 500
    except Exception as e:
        logger.exception("Erro no /transcribe: %s", e)
        return jsonify(ok=False, error="internal_error", detail=str(e)), 500

# ---------------------------
# Main (dev local)
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
