# =========================
# Scriptify - Backend (com suporte a cookies YouTube)
# =========================
import os
import tempfile
import base64
from flask import Flask, request, send_file, jsonify
import yt_dlp

app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/download", methods=["POST"])
def download_audio():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL inválida"}), 400

    # --- Configuração de cookies (suporte Base64 ou texto plano) ---
    cookies_env = os.getenv("YTDLP_COOKIES", "")
    cookies_b64 = os.getenv("YTDLP_COOKIES_B64", "")

    cookiefile_path = None
    if cookies_b64.strip():
        try:
            decoded = base64.b64decode(cookies_b64).decode("utf-8")
            tf = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
            tf.write(decoded.encode("utf-8"))
            tf.flush()
            tf.close()
            cookiefile_path = tf.name
        except Exception as e:
            return jsonify({"error": f"Falha ao decodificar cookies: {str(e)}"}), 500
    elif cookies_env.strip():
        tf = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        tf.write(cookies_env.encode("utf-8"))
        tf.flush()
        tf.close()
        cookiefile_path = tf.name

    # --- Opções do yt-dlp ---
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "outtmpl": tempfile.mktemp(suffix=".mp3"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    if cookiefile_path:
        ydl_opts["cookiefile"] = cookiefile_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        audio_path = ydl_opts["outtmpl"]
        return send_file(audio_path, mimetype="audio/mpeg")

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
