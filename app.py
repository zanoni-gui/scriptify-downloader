import os
import tempfile
import subprocess
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI

# === CONFIGURAÇÕES BÁSICAS ===
app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    logging.warning("⚠️ Nenhuma chave OPENAI_API_KEY detectada no ambiente!")

client = OpenAI(api_key=OPENAI_API_KEY)


# === FUNÇÃO PARA BAIXAR O ÁUDIO DO VÍDEO ===
def download_audio(url):
    """Baixa o áudio de um vídeo (YouTube, TikTok, Instagram, etc.) usando yt-dlp"""
    try:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "--ffmpeg-location", "/usr/bin/ffmpeg",
            "--quiet",
            "--no-warnings",
            "-o", temp_file.name,
            url,
        ]
        logging.info(f"🎧 Baixando áudio de {url} ...")
        subprocess.run(cmd, check=True)
        logging.info(f"✅ Áudio baixado: {temp_file.name}")
        return temp_file.name
    except subprocess.CalledProcessError as e:
        logging.error(f"❌ Falha ao baixar áudio com yt-dlp: {e}")
        raise RuntimeError("download_failed")


# === ENDPOINT DE STATUS ===
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": os.popen("date -u").read().strip()})


# === ENDPOINT PRINCIPAL ===
@app.route("/transcribe", methods=["POST"])
def transcribe():
    try:
        data = request.get_json()
        url = data.get("url", "").strip()

        if not url:
            return jsonify({"error": "URL não fornecida"}), 400

        # Detecta plataforma
        platform = (
            "youtube" if "youtu" in url
            else "tiktok" if "tiktok" in url
            else "instagram" if "insta" in url
            else "desconhecida"
        )
        logging.info(f"Recebido link: {url} ({platform})")

        # === TENTATIVA DE DOWNLOAD E TRANSCRIÇÃO REAL ===
        try:
            audio_path = download_audio(url)

            if not OPENAI_API_KEY:
                raise RuntimeError("API key ausente")

            logging.info("🧠 Transcrevendo com OpenAI Whisper...")
            with open(audio_path, "rb") as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="gpt-4o-mini-transcribe",
                    file=audio_file
                )

            logging.info("✅ Transcrição concluída com sucesso!")
            return jsonify({
                "ok": True,
                "platform": platform,
                "script": transcript.text,
                "url": url
            })

        except Exception as e:
            logging.warning(f"⚠️ Falha real de transcrição, caindo pro modo stub: {e}")
            # === FALLBACK: STUB SIMULADO ===
            return jsonify({
                "ok": True,
                "platform": platform,
                "title": "Roteiro gerado (stub)",
                "transcript": (
                    "🧠 Essa ferramenta é impressionante! 👉🏼 Se quiser receber o link é só ...\n\n"
                    "Este é um texto de exemplo retornado pelo backend.\n"
                    f"URL: {url}\n"
                    f"Plataforma detectada: {platform}\n\n"
                    "Quando plugarmos o motor real de transcrição, este campo trará o roteiro do vídeo."
                )
            })

    except Exception as e:
        logging.exception("💥 Erro inesperado no backend")
        return jsonify({"error": str(e)}), 500


# === INICIALIZAÇÃO LOCAL ===
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
