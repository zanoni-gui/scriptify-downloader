import os
import json
import tempfile
import subprocess
import logging
from urllib.parse import urlparse

from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI

# -----------------------------------------------------------------------------
# Configuração
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
client = OpenAI(api_key=OPENAI_API_KEY)

# Armazena cookies em memória: {"tiktok": [ {name, value, domain, ...}, ... ], ...}
COOKIE_STORE = {}

# User-Agent moderno para reduzir bloqueios
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# -----------------------------------------------------------------------------
# Utilidades
# -----------------------------------------------------------------------------
def detectar_plataforma(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "tiktok.com" in host:
        return "tiktok"
    if "instagram.com" in host:
        return "instagram"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "facebook.com" in host:
        return "facebook"
    return "desconhecida"


def write_netscape_cookies(cookies: list) -> str:
    """
    Converte lista de cookies em arquivo Netscape e retorna o caminho.
    Formato aceito por yt-dlp/curl.
    """
    fd, path = tempfile.mkstemp(prefix="ck_", suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies or []:
            domain = (c.get("domain") or "").strip()
            include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            path_c = c.get("path") or "/"
            secure = "TRUE" if c.get("secure") else "FALSE"
            # OBS: expires em segundos Unix; se não houver, use 0
            expires = str(int(c.get("expires", 0) or 0))
            name = c.get("name") or ""
            value = c.get("value") or ""
            f.write(f"{domain}\t{include_sub}\t{path_c}\t{secure}\t{expires}\t{name}\t{value}\n")
    return path


def download_bestaudio(url: str, plataforma: str) -> str:
    """
    Baixa SOMENTE o bestaudio (sem pós-processamento/ffprobe) com yt-dlp
    e retorna o caminho do arquivo final.
    Captura o filename via --print filename.
    """
    tmpdir = tempfile.mkdtemp(prefix="aud_")
    outtpl = os.path.join(tmpdir, "clip.%(ext)s")

    cmd = [
        "yt-dlp",
        "-f", "bestaudio/best",
        "--user-agent", USER_AGENT,
        "--no-warnings",
        "--no-playlist",
        "--ignore-config",
        "--restrict-filenames",
        "--no-simulate",           # baixa de verdade
        "-o", outtpl,              # template de output
        "--print", "filename",     # imprime o filename final no stdout
        url,
    ]

    # Se tivermos cookies dessa plataforma, envia
    cookies_file = None
    if plataforma in COOKIE_STORE and COOKIE_STORE[plataforma]:
        cookies_file = write_netscape_cookies(COOKIE_STORE[plataforma])
        cmd.extend(["--cookies", cookies_file])

    logging.info("yt-dlp: %s", " ".join(cmd))

    # Captura stdout para pegar o filename
    proc = subprocess.run(
        cmd, check=True, capture_output=True, text=True, timeout=600
    )
    filename = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    if not filename or not os.path.exists(filename):
        raise RuntimeError("download_failed")

    return filename


def transcrever_arquivo_audio(path: str) -> str:
    """
    Transcreve com OpenAI (whisper-1) e retorna texto puro.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY ausente")

    with open(path, "rb") as f:
        # response_format="text" retorna só a string
        resp_text = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="text",
            temperature=0.0,
        )
    # Quando response_format="text", a lib retorna diretamente a string
    return (resp_text or "").strip()


# -----------------------------------------------------------------------------
# Rotas
# -----------------------------------------------------------------------------
@app.after_request
def add_cors_headers(resp):
    # CORS já vem do flask_cors, mas mantemos por segurança
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok"), 200


@app.route("/ingest-cookies", methods=["POST", "OPTIONS"])
def ingest_cookies():
    if request.method == "OPTIONS":
        return ("", 204)

    try:
        payload = request.get_json(force=True)
    except Exception as e:
        logging.warning("JSON inválido: %s", e)
        return jsonify(error="invalid_json"), 400

    # Aceita "domain" ou "plataforma" (preferimos plataforma)
    plataforma = payload.get("plataforma")
    domain = payload.get("domain")
    cookies = payload.get("cookies")

    if not cookies or not isinstance(cookies, list):
        return jsonify(error="missing_cookies"), 400

    if not plataforma:
        # Tenta deduzir a partir do domain
        plataforma = "desconhecida"
        d = (domain or "").lower()
        if "tiktok" in d:
            plataforma = "tiktok"
        elif "instagram" in d:
            plataforma = "instagram"
        elif "youtube" in d or "youtu.be" in d:
            plataforma = "youtube"
        elif "facebook" in d:
            plataforma = "facebook"

    # Sanitiza (garante campos principais)
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
            "path": (c or {}).get("path") or "/",
            "expires": (c or {}).get("expires") or 0,
            "httpOnly": bool((c or {}).get("httpOnly", False)),
            "secure": bool((c or {}).get("secure", False)),
            "sameSite": (c or {}).get("sameSite"),
        })

    if not sanitized:
        return jsonify(error="no_valid_cookies"), 400

    COOKIE_STORE.setdefault(plataforma, [])
    # substitui os cookies existentes da plataforma (mais simples neste MVP)
    COOKIE_STORE[plataforma] = sanitized

    logging.info("Cookies armazenados para %s: %s", plataforma, [c["name"] for c in sanitized])
    return jsonify(ok=True, stored=len(sanitized), plataforma=plataforma), 200


@app.route("/transcribe", methods=["POST", "OPTIONS"])
def transcribe():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return jsonify(ok=False, error="invalid_url", detail="Envie campo 'url' http(s)://"), 400

    plataforma = detectar_plataforma(url)
    logging.info("Recebido link: %s (%s)", url, plataforma)

    try:
        # 1) Baixa o bestaudio (sem pós-processamento)
        audio_path = download_bestaudio(url, plataforma)
        logging.info("Áudio baixado em: %s", audio_path)

        # 2) Transcreve com OpenAI
        texto = transcrever_arquivo_audio(audio_path)

        # 3) Limpa arquivo
        try:
            os.remove(audio_path)
        except Exception:
            pass

        return jsonify(
            ok=True,
            plataforma=plataforma,
            titulo="Roteiro gerado",
            transcricao=texto
        ), 200

    except subprocess.CalledProcessError as e:
        logging.error("yt-dlp falhou: %s", e)
    except Exception as e:
        logging.error("Transcrição falhou: %s", e)

    # Fallback stub (mostra algo útil ao usuário)
    return jsonify(
        ok=True,
        plataforma=plataforma,
        titulo="Roteiro gerado (stub)",
        transcricao=(
            "⚠️ Não foi possível extrair o áudio real no momento.\n\n"
            f"URL: {url}\nPlataforma detectada: {plataforma}\n\n"
            "Quando o motor real estiver ativo/estável para esse link, o roteiro aparecerá aqui."
        )
    ), 200


# -----------------------------------------------------------------------------
# Inicialização local
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # Em produção (Render) use gunicorn; aqui é apenas para dev local.
    app.run(host="0.0.0.0", port=10000, debug=True)
