import os
import json
import tempfile
import shutil
import uuid
import subprocess
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
import yt_dlp as ytdlp

# -----------------------------
# Config
# -----------------------------
app = Flask(__name__)
CORS(app, supports_credentials=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Resolve ffmpeg/ffprobe path (Render installs via apt)
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/usr/bin/ffprobe"

TMP_DIR = Path(os.getenv("TMP_DIR", tempfile.gettempdir()))
TMP_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------------
# Helpers
# -----------------------------

def _run(cmd: list[str]):
    """Run a command and raise with stderr attached on failure."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDERR:\n{proc.stderr[:4000]}")
    return proc


def normalize_to_mp3(input_path: Path) -> Path:
    """Ensure we have an MP3 (mono, 16kHz) to feed the ASR for stability."""
    out = input_path.with_suffix(".normalized.mp3")
    cmd = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-i", str(input_path),
        "-ac", "1",            # mono
        "-ar", "16000",        # 16 kHz
        "-vn",
        str(out)
    ]
    _run(cmd)
    return out


def download_audio_with_ytdlp(url: str) -> Path:
    """Download bestaudio using yt-dlp and extract to mp3 via ffmpeg.
    Handles TikTok/YouTube/etc. Uses an explicit ffmpeg path to avoid ffprobe errors.
    """
    tmp_id = uuid.uuid4().hex
    base = TMP_DIR / f"dl_{tmp_id}"
    outtmpl = str(base)  # yt-dlp appends extension

    # Some sites (TikTok) are sensitive to headers.
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "Referer": "https://www.tiktok.com/",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "concurrent_fragment_downloads": 3,
        "http_headers": headers,
        "retries": 3,
        "fragment_retries": 3,
        # Explicitly set ffmpeg/ffprobe locations
        "ffmpeg_location": os.path.dirname(FFMPEG),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "postprocessor_args": [
            "-hide_banner",
        ],
    }

    try:
        with ytdlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # postprocessor should have produced an mp3
            if "requested_downloads" in info and info["requested_downloads"]:
                filename = info["requested_downloads"][0]["_filename"]
            else:
                filename = ydl.prepare_filename(info)
            # Find the mp3 next to base
            mp3 = Path(str(base) + ".mp3")
            if mp3.exists():
                return mp3
            # Fallback: if we got a different ext, normalize to mp3
            return normalize_to_mp3(Path(filename))
    except Exception as e:
        # Fallback approach: download original container, then convert with ffmpeg
        # This helps when FFmpegExtractAudio fails due to codec probing edge-cases
        raw_out = str(base) + ".m4a"
        fallback_opts = {
            "format": "bestaudio/best",
            "outtmpl": raw_out,
            "quiet": True,
            "no_warnings": True,
            "http_headers": headers,
            "retries": 2,
            "ffmpeg_location": os.path.dirname(FFMPEG),
        }
        with ytdlp.YoutubeDL(fallback_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded = Path(raw_out)
            if not downloaded.exists():
                # Last resort: try prepare_filename
                downloaded = Path(ydl.prepare_filename(info))
        return normalize_to_mp3(downloaded)


def transcribe_file(mp3_path: Path, language: str = "pt") -> dict:
    with open(mp3_path, "rb") as f:
        tr = client.audio.transcriptions.create(
            file=f,
            model="gpt-4o-mini-transcribe",
            language=language,
            # "verbose_json" is not guaranteed on every engine yet
        )
    # SDK may return a simple object with .text
    if hasattr(tr, "text"):
        return {"text": tr.text}
    try:
        return json.loads(tr) if isinstance(tr, str) else tr
    except Exception:
        return {"text": str(tr)}


def generate_script(transcript_text: str, style: str = "youtube-shorts") -> str:
    system = (
        f"Você é roteirista sênior. Gere roteiro em PT-BR no estilo {style}.\n"
        "- Hook forte em 1–2 linhas; frases curtas; ritmo rápido.\n"
        "- Evite inventar fatos específicos; prefira generalizações.\n"
        "- Termine com CTA sutil para seguir a página."
    )
    user = (
        "Transcrição (trecho):\n" + transcript_text[:8000] +
        "\n\nGere TÍTULO e ROTEIRO FINAL (sem numeração de cenas)."
    )
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.7,
    )
    return resp.choices[0].message.content


# -----------------------------
# Routes
# -----------------------------
@app.route("/health", methods=["GET"])  # for Render health checks
def health():
    return jsonify({
        "status": "ok",
        "ffmpeg": FFMPEG,
        "ffprobe": FFPROBE,
        "openai": bool(OPENAI_API_KEY),
    })


@app.route("/transcribe", methods=["POST"])  # accepts {url} JSON or multipart file
def transcribe():
    try:
        language = request.args.get("language", "pt")

        # 1) Get media
        if request.content_type and request.content_type.startswith("application/json"):
            data = request.get_json(force=True)
            url = data.get("url")
            if not url:
                return jsonify({"error": "missing_url"}), 400
            audio_path = download_audio_with_ytdlp(url)
        else:
            # multipart/form-data with file
            if "file" not in request.files:
                return jsonify({"error": "missing_file"}), 400
            f = request.files["file"]
            if f.filename == "":
                return jsonify({"error": "empty_filename"}), 400
            suffix = Path(f.filename).suffix or ".mp4"
            raw_path = TMP_DIR / (uuid.uuid4().hex + suffix)
            f.save(raw_path)
            audio_path = normalize_to_mp3(raw_path)

        # 2) Transcribe
        transcript = transcribe_file(audio_path, language)

        return jsonify({
            "status": "completed",
            "transcript": transcript,
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)[:1200],
        }), 500


@app.route("/script", methods=["POST"])  # body: {"transcript": "...", "style": "youtube-shorts"}
def script():
    try:
        data = request.get_json(force=True)
        transcript_text = data.get("transcript", "").strip()
        if not transcript_text:
            return jsonify({"error": "missing_transcript"}), 400
        style = data.get("style", "youtube-shorts")
        script_text = generate_script(transcript_text, style)
        return jsonify({
            "status": "completed",
            "script": script_text,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)[:1200]}), 500


# -----------------------------
# Entrypoint for local dev: `python app.py`
# In Render we use: gunicorn app:app
# -----------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)
