from flask import Flask, request, send_file, jsonify
import tempfile, os, pathlib, yt_dlp

app = Flask(__name__)

def fetch_audio(url: str) -> str:
    tmpdir = tempfile.mkdtemp()
    outtpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtpl,
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractaudio": True,
        "audioformat": "mp3",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        "force_ipv4": True,
        "socket_timeout": 25,
        "retries": 3,
        "geo_bypass": True,
        "concurrent_fragment_downloads": 1,
        "http_headers": {
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        "extractor_args": {"youtube": {"player_client": ["web", "android"]}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)

    mp3s = list(pathlib.Path(tmpdir).glob("*.mp3"))
    if not mp3s:
        raise RuntimeError("Falha ao converter o áudio para MP3.")
    return str(mp3s[0])

@app.route("/health")
def health():
    return jsonify(ok=True)

@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    if not url:
        return jsonify(error="missing url"), 400
    try:
        mp3_path = fetch_audio(url)
        return send_file(mp3_path, mimetype="audio/mpeg", as_attachment=False)
    except Exception as e:
        return jsonify(error=str(e)), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
