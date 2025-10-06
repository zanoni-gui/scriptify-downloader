import os
import tempfile
import subprocess
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
import yt_dlp

# ------------------------------------------------------
# Configuração inicial
# ------------------------------------------------------
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------------------------------------------
# Funções auxiliares
# ------------------------------------------------------

def detectar_plataforma(url: str) -> str:
    if "tiktok.com" in url:
        return "tiktok"
    elif "instagram.com" in url:
        return "instagram"
    elif "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    elif "facebook.com" in url:
        return "facebook"
    else:
        return "desconhecida"


def baixar_audio(url: str) -> str:
    """Baixa apenas o áudio do vídeo e retorna o caminho do arquivo."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp.close()

    cmd = [
        "yt-dlp", "-x", "--audio-format", "mp3",
        "--ffmpeg-location", "/usr/bin/ffmpeg",
        "--quiet", "--no-warnings",
        "-o", tmp.name, url
    ]

    try:
        subprocess.run(cmd, check=True)
        logging.info(f"✅ Áudio baixado: {tmp.name}")
        return tmp.name
    except subprocess.CalledProcessError as e:
        logging.error(f"❌ Falha ao baixar áudio: {e}")
        raise RuntimeError("Falha ao baixar áudio com yt-dlp.")


def transcrever_audio(path: str) -> str:
    """Transcreve o áudio usando a OpenAI."""
    try:
        with open(path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=audio_file
            )
        texto = transcript.text.strip()
        return texto
    except Exception as e:
        logging.error(f"Erro na transcrição: {e}")
        raise RuntimeError("Erro ao transcrever o áudio.")


# ------------------------------------------------------
# Rotas
# ------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/transcribe", methods=["POST"])
def transcribe():
    data = request.get_json()
    url = data.get("url")
    if not url:
        return jsonify({"error": "Faltando URL"}), 400

    plataforma = detectar_plataforma(url)
    logging.info(f"🔍 Recebido link: {url} ({plataforma})")

    try:
        audio_path = baixar_audio(url)
        transcricao = transcrever_audio(audio_path)
        os.remove(audio_path)

        resposta = {
            "ok": True,
            "plataforma": plataforma,
            "titulo": "Roteiro gerado com sucesso",
            "transcricao": transcricao,
        }
        return jsonify(resposta), 200

    except Exception as e:
        logging.warning(f"⚠️ Falha real de transcrição: {e}")
        resposta_stub = {
            "ok": True,
            "plataforma": plataforma,
            "titulo": "Roteiro gerado (stub)",
            "transcricao": (
                "⚠️ Não foi possível extrair o áudio real. "
                "Este é apenas um texto de exemplo.\n\n"
                f"URL: {url}\nPlataforma detectada: {plataforma}\n\n"
                "Quando o motor real estiver ativo, o roteiro do vídeo aparecerá aqui."
            ),
        }
        return jsonify(resposta_stub), 200


# ------------------------------------------------------
# Inicialização local
# ------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, debug=True)
