import os
import re
import tempfile
import subprocess
import logging
from urllib.parse import urlparse

from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI

# ffmpeg portátil (sem apt-get)
import imageio_ffmpeg

# ------------------------------------------------------
# Configuração inicial
# ------------------------------------------------------
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ------------------------------------------------------
# Utilidades
# ------------------------------------------------------
def detectar_plataforma(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return "desconhecida"
    if "tiktok.com" in host:
        return "tiktok"
    if "instagram.com" in host:
        return "instagram"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "facebook.com" in host:
        return "facebook"
    return "desconhecida"

def baixar_audio(url: str) -> str:
    """
    Baixa/extrai o áudio do vídeo usando yt-dlp + ffmpeg portátil do imageio-ffmpeg.
    Retorna o caminho de um arquivo .mp3 temporário.
    """
    # ffmpeg portátil
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    ffmpeg_dir = os.path.dirname(ffmpeg_bin)

    # arquivo temporário de saída
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp.close()

    # Observações:
    # - --extract-audio / -x: extrai só o áudio.
    # - --audio-format mp3: converte para mp3 (usa ffmpeg portátil).
    # - --prefer-ffmpeg e --ffmpeg-location: garante que o yt-dlp use nosso ffmpeg.
    # - --no-warnings e --quiet: logs limpos.
    cmd = [
        "yt-dlp",
        "-x",
        "--extract-audio",
        "--audio-format", "mp3",
        "--prefer-ffmpeg",
        "--ffmpeg-location", ffmpeg_dir,
        "--no-progress",
        "--no-warnings",
        "--quiet",
        "-o", tmp.name,
        url,
    ]

    try:
        subprocess.run(cmd, check=True)
        logging.info(f"✅ Áudio baixado: {tmp.name}")
        return tmp.name
    except subprocess.CalledProcessError as e:
        logging.error("❌ Falha ao baixar áudio com yt-dlp: %s", e)
        raise RuntimeError("download_failed")

def transcrever_audio(path: str) -> str:
    """
    Transcreve o áudio via OpenAI. Usa o modelo Whisper API (stt) da linha o-mini-transcribe.
    """
    if not client:
        raise RuntimeError("missing_openai_key")

    try:
        with open(path, "rb") as audio_file:
            # Se preferir Whisper-1 (clássico), troque o nome do modelo:
            #   model="whisper-1"
            # Aqui usamos "gpt-4o-mini-transcribe" (mais novo/rápido).
            result = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=audio_file,
            )
        texto = (result.text or "").strip()
        return texto
    except Exception as e:
        logging.error("❌ Erro na transcrição: %s", e)
        raise RuntimeError("transcribe_failed")

# ------------------------------------------------------
# Rotas
# ------------------------------------------------------
@app.after_request
def add_cors_headers(resp):
    # CORS simples
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp

@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok"), 200

@app.route("/transcribe", methods=["POST", "OPTIONS"])
def transcribe():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    # validação básica
    if not url or not re.match(r"^https?://", url, re.I):
        return jsonify(ok=False, error="invalid_url", detail="Envie um 'url' com http(s)://"), 400

    plataforma = detectar_plataforma(url)
    logging.info("🔍 Recebido link: %s (%s)", url, plataforma)

    try:
        # 1) download do áudio
        audio_path = baixar_audio(url)

        # 2) transcrição
        texto = transcrever_audio(audio_path)

        # 3) limpeza
        try:
            os.remove(audio_path)
        except Exception:
            pass

        return jsonify(
            ok=True,
            plataforma=plataforma,
            title="Roteiro gerado com sucesso",
            transcript=texto or "(sem conteúdo detectado)"
        ), 200

    except Exception as e:
        err = str(e)
        logging.warning("⚠️ Falha real, retornando stub. Motivo: %s", err)
        # stub amigável (mostra algo no front)
        return jsonify(
            ok=True,
            plataforma=plataforma,
            title="Roteiro gerado (stub)",
            transcript=(
                "⚠️ Não foi possível extrair/transcrever o áudio real agora.\n\n"
                f"URL: {url}\nPlataforma detectada: {plataforma}\n\n"
                "Quando o motor real estiver ativo, o roteiro do vídeo aparecerá aqui."
            )
        ), 200

# ------------------------------------------------------
# Inicialização local
# ------------------------------------------------------
if __name__ == "__main__":
    # Em dev local rode:  python app.py
    # No Render, o gunicorn chama app:app
    app.run(host="0.0.0.0", port=10000, debug=False)
