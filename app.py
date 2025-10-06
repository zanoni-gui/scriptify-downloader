# app.py
import os
import re
import json
import tempfile
import logging
from urllib.parse import urlparse
from datetime import datetime
from typing import Optional, Tuple

from flask import Flask, request, jsonify
from yt_dlp import YoutubeDL

# ---------- Config ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # defina no Render
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")  # modelo da OpenAI
SUMMARIZE_WITH_GPT = os.getenv("SUMMARIZE_WITH_GPT", "1") in ("1", "true", "True")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------- CORS básico ----------
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


# ---------- Utilidades ----------
def detect_platform(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return "unknown"
    if any(s in host for s in ("youtube.com", "youtu.be")):
        return "youtube"
    if "tiktok.com" in host:
        return "tiktok"
    if "instagram.com" in host:
        return "instagram"
    if "facebook.com" in host:
        return "facebook"
    return "unknown"


def validate_url(url: str) -> bool:
    return bool(re.match(r"^https?://", url, re.I))


def ytdlp_download_audio(url: str) -> Tuple[str, dict]:
    """
    Baixa o áudio e extrai para MP3 usando yt-dlp + ffmpeg.
    Retorna (caminho_mp3, info_dict).
    """
    tmpdir = tempfile.mkdtemp(prefix="scriptfy_")
    outtmpl = os.path.join(tmpdir, "audio.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "retries": 3,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # yt-dlp retorna caminho do arquivo final em info["requested_downloads"][0]["filepath"]
        # mas com postprocessor, o final vira .mp3 e fica em tmpdir
        mp3_path = os.path.join(tmpdir, "audio.mp3")
        if not os.path.exists(mp3_path):
            # fallback: tenta descobrir pelo info
            # (algumas versões gravam com outro nome, mas dentro do tmpdir)
            for root, _, files in os.walk(tmpdir):
                for f in files:
                    if f.lower().endswith(".mp3"):
                        mp3_path = os.path.join(root, f)
                        break

        if not os.path.exists(mp3_path):
            raise RuntimeError("Falha ao gerar MP3 com ffmpeg/yt-dlp")

        return mp3_path, info


def openai_transcribe(mp3_path: str) -> dict:
    """
    Usa OpenAI Whisper API para transcrever o arquivo MP3.
    Retorna dict com campos básicos.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não definido no ambiente")

    # SDK novo da OpenAI (>= 1.0)
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    with open(mp3_path, "rb") as f:
        tr = client.audio.transcriptions.create(
            model=WHISPER_MODEL,  # "whisper-1"
            file=f,
            response_format="json",
            language=None,         # deixe a API detectar
            temperature=0.0,
        )

    # tr.text contém a transcrição
    return {
        "text": getattr(tr, "text", "") or "",
        "language": getattr(tr, "language", None),
        "duration": getattr(tr, "duration", None),
    }


def openai_summarize(text: str, url: str, platform: str) -> str:
    """
    Resume a transcrição em um 'roteiro' em tópicos,
    com tom direto e pronto para leitura.
    """
    if not OPENAI_API_KEY:
        return ""

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    prompt = f"""
Você é um roteirista. Com base na transcrição abaixo, gere um roteiro em tópicos claros e acionáveis,
sem floreios, no estilo "conteúdo dark". Mantenha concisão e ordem lógica.

URL: {url}
Plataforma: {platform}

### Transcrição:
{text}
"""

    chat = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Você é um roteirista sênior."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    return chat.choices[0].message.content.strip()


# ---------- Endpoints ----------
@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok", time=datetime.utcnow().isoformat() + "Z"), 200


@app.route("/ingest-cookies", methods=["POST", "OPTIONS"])
def ingest_cookies():
    if request.method == "OPTIONS":
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

    # TODO: salvar em banco (Supabase/Redis) por plataforma para uso em downloads
    app.logger.info("Cookies recebidos para %s: %s", domain, [c["name"] for c in sanitized])

    return jsonify(ok=True, stored=len(sanitized)), 200


@app.route("/transcribe", methods=["POST", "OPTIONS"])
def transcribe():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url or not validate_url(url):
        return jsonify(ok=False, error="invalid_url",
                       detail="Envie 'url' iniciando com http(s)://"), 400

    platform = detect_platform(url)
    app.logger.info("Transcribe solicitado: %s (%s)", url, platform)

    mp3_path = None
    info = {}
    try:
        # 1) Baixa áudio (yt-dlp + ffmpeg)
        mp3_path, info = ytdlp_download_audio(url)
        title = info.get("title") or "Sem título"
        duration = info.get("duration")  # segundos

        # 2) Transcreve (OpenAI Whisper)
        tr = openai_transcribe(mp3_path)
        transcript = tr.get("text", "").strip()

        if not transcript:
            return jsonify(ok=False, error="empty_transcript"), 500

        # 3) (Opcional) sumariza em “roteiro”
        script_text = ""
        if SUMMARIZE_WITH_GPT and len(transcript) > 20:
            try:
                script_text = openai_summarize(transcript, url, platform)
            except Exception as e:
                app.logger.warning("Falha ao resumir com GPT: %s", e)

        return jsonify(
            ok=True,
            platform=platform,
            title=title,
            duration=duration,
            transcript=transcript,
            script=script_text or transcript  # se resumo falhar, devolve transcrição
        ), 200

    except Exception as e:
        app.logger.exception("Erro na transcrição: %s", e)
        return jsonify(ok=False, error="transcription_failed", detail=str(e)), 500

    finally:
        # limpeza básica do arquivo tmp
        try:
            if mp3_path and os.path.exists(mp3_path):
                base = os.path.dirname(mp3_path)
                for root, dirs, files in os.walk(base, topdown=False):
                    for f in files:
                        try:
                            os.remove(os.path.join(root, f))
                        except Exception:
                            pass
                    for d in dirs:
                        try:
                            os.rmdir(os.path.join(root, d))
                        except Exception:
                            pass
                try:
                    os.rmdir(base)
                except Exception:
                    pass


if __name__ == "__main__":
    # Em dev local
    app.run(host="0.0.0.0", port=8000, debug=False)
