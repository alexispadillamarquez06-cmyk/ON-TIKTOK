"""
TikTok Downloader API · solo fuente ttdownloader

Recibe un link de TikTok y devuelve el .mp4 directamente a quien lo pidió.
No recodifica nada: entrega el archivo tal cual lo sirve ttdownloader.

Variables de entorno (todas opcionales):
  API_KEY          clave que deben mandar los clientes en el header X-API-Key (vacía = API pública)
  MAX_CONCURRENT   descargas simultáneas máximas (default 2)
  RETRIES          reintentos por link si ttdownloader falla (default 3)
  ALLOWED_ORIGINS  orígenes CORS separados por coma (default *)
"""
import os
import re
import json
import time
import shutil
import secrets
import logging
import tempfile
import threading
import subprocess
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tiktok-api")

# ───────── Configuración ─────────
API_KEY = os.getenv("API_KEY", "")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
RETRIES = max(1, int(os.getenv("RETRIES", "3")))
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
EXPOSED = ["X-Source", "X-Resolution", "X-FPS", "X-Bitrate-Kbps", "X-Watermark", "X-Video-Id"]

# ───────── Fuente única: ttdownloader (del repo krypton-byte/tiktok-downloader) ─────────
IMPORT_ERROR = None
try:
    import tiktok_downloader as td
except Exception as e:
    td = None
    IMPORT_ERROR = str(e)
    log.error("No pude importar tiktok_downloader: %s", e)


def _find_ttdownloader():
    """Busca la función ttdownloader sin importar mayúsculas (o dentro del submódulo)."""
    if not td:
        return None
    for name in dir(td):
        if name.lower() == "ttdownloader":
            obj = getattr(td, name)
            if callable(obj):
                return obj
            inner = getattr(obj, "ttdownloader", None)
            if callable(inner):
                return inner
    return None


TTD = _find_ttdownloader()
if TTD is None and td is not None:
    log.error("tiktok_downloader no expone ttdownloader. Disponible: %s",
              [n for n in dir(td) if not n.startswith("_")])


class DownloadError(Exception):
    pass


# ───────── Utilidades ─────────
def stream_to(link, path):
    """Guarda el archivo tal cual lo sirve el servidor (sin recodificar)."""
    with httpx.stream("GET", link, headers=UA, follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)


def resolve(url):
    """Expande links cortos (vt.tiktok.com) y devuelve (id_del_video, link_canonico | None)."""
    try:
        r = httpx.get(url, headers=UA, follow_redirects=True, timeout=15)
        final = str(r.url)
        m = re.search(r"(https://[^/?#]*tiktok\.com/@[^/?#]+/(?:video|photo)/(\d+))", final)
        if m:
            return m.group(2), m.group(1)
        m = re.search(r"/(?:video|photo)/(\d+)", final)
        if m:
            return m.group(1), None
    except Exception:
        pass
    return str(int(time.time())), None


def probe(path):
    size = os.path.getsize(path)
    try:  # ffprobe (preciso)
        j = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,r_frame_rate:format=duration",
             "-of", "json", path], capture_output=True, text=True, check=True).stdout)
        s, dur = j["streams"][0], float(j["format"].get("duration") or 0)
        a, b = s["r_frame_rate"].split("/")
        return dict(w=int(s["width"]), h=int(s["height"]), codec=s["codec_name"],
                    fps=round(int(a) / int(b), 2) if int(b) else 0, size=size,
                    kbps=int(size * 8 / dur / 1000) if dur else 0)
    except Exception:
        try:  # respaldo: moviepy (solo lee metadatos, no recodifica)
            from moviepy.editor import VideoFileClip
            with VideoFileClip(path) as c:
                w, h = c.size
                return dict(w=int(w), h=int(h), codec="?", fps=round(c.fps or 0, 2), size=size,
                            kbps=int(size * 8 / c.duration / 1000) if c.duration else 0)
        except Exception:
            return dict(w=0, h=0, codec="?", fps=0, size=size, kbps=0)


def is_tiktok_url(url):
    """Solo acepta links de TikTok (evita que usen la API para otras webs)."""
    try:
        u = urlparse(url)
        h = (u.hostname or "").lower()
        return u.scheme in ("http", "https") and (h == "tiktok.com" or h.endswith(".tiktok.com"))
    except Exception:
        return False


# ───────── Descarga con ttdownloader ─────────
def _sorted_items(url):
    items = list(TTD(url))
    if not items:
        raise RuntimeError("ttdownloader no devolvió enlaces")
    items.sort(key=lambda i: bool(getattr(i, "watermark", False)))  # sin marca de agua primero
    return items


def _save(item, path):
    if os.path.exists(path):
        os.remove(path)
    try:
        item.download(path)
    except Exception:
        link = getattr(item, "url", None)
        if not link:
            raise
        stream_to(link, path)
    if not (os.path.exists(path) and os.path.getsize(path) > 50_000):
        raise RuntimeError("archivo vacío")


def fetch_video(url, workdir):
    vid, canonical = resolve(url)
    base = f"tiktok_{vid}"
    path = os.path.join(workdir, f"{base}.mp4")

    # prueba primero el link tal cual y luego el link largo (algunos servicios fallan con los cortos)
    targets = [url]
    if canonical and canonical != url:
        targets.append(canonical)

    last_err = None
    for target in targets:
        for attempt in range(1, RETRIES + 1):
            try:
                for item in _sorted_items(target):
                    try:
                        _save(item, path)
                        wm = bool(getattr(item, "watermark", False))
                        c = dict(path=path, base=base, wm=wm, **probe(path))
                        log.info("✓ ttdownloader: %sx%s · %.1f MB · marca=%s",
                                 c["w"], c["h"], c["size"] / 1e6, "sí" if wm else "no")
                        return c
                    except Exception as e:
                        last_err = e
                        log.info("  enlace descartado: %s", str(e)[:90])
                raise last_err or RuntimeError("sin resultados")
            except Exception as e:
                last_err = e
                log.info("✗ ttdownloader intento %s/%s (%s): %s", attempt, RETRIES,
                         "largo" if target != url else "original", str(e)[:90])
                if attempt < RETRIES:
                    time.sleep(1.5 * attempt)

    raise DownloadError(str(last_err)[:200] if last_err else "sin resultados")


# ───────── API ─────────
app = FastAPI(title="TikTok Downloader API (ttdownloader)", version="1.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
    expose_headers=EXPOSED,
)
gate = threading.BoundedSemaphore(MAX_CONCURRENT)


class DownloadBody(BaseModel):
    url: str


def _auth(x_api_key, key):
    if not API_KEY:
        return
    given = (x_api_key or key or "").encode()
    if not secrets.compare_digest(given, API_KEY.encode()):
        raise HTTPException(401, "API key inválida")


def _check(url: str) -> str:
    url = url.strip()
    if not is_tiktok_url(url):
        raise HTTPException(400, "Manda un link válido de TikTok")
    if TTD is None:
        raise HTTPException(503, "ttdownloader no está disponible en el servidor (revisa los logs de build)")
    return url


def _serve(url: str):
    url = _check(url)
    if not gate.acquire(timeout=30):
        raise HTTPException(503, "Servidor ocupado, intenta de nuevo en unos segundos")

    workdir = tempfile.mkdtemp(prefix="tt_")
    try:
        best = fetch_video(url, workdir)
    except DownloadError as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(502, f"ttdownloader no pudo con ese link: {e}")
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        log.exception("fallo inesperado descargando %s", url)
        raise HTTPException(500, "Error interno al descargar el video")
    finally:
        gate.release()

    headers = {
        "X-Source": "ttdownloader",
        "X-Resolution": f"{best['w']}x{best['h']}",
        "X-FPS": str(best["fps"]),
        "X-Bitrate-Kbps": str(best["kbps"]),
        "X-Watermark": "yes" if best["wm"] else "no",
        "X-Video-Id": best["base"].replace("tiktok_", ""),
    }
    return FileResponse(
        best["path"],
        media_type="video/mp4",
        filename=f"{best['base']}.mp4",
        headers=headers,
        background=BackgroundTask(shutil.rmtree, workdir, True),  # borra el temporal al terminar de enviar
    )


@app.get("/")
def home():
    return {
        "status": "ok",
        "fuente": "ttdownloader",
        "ttdownloader_disponible": TTD is not None,
        "error_import": IMPORT_ERROR,
        "uso": {
            "GET": "/download?url=<link de TikTok>",
            "POST": '/download con JSON {"url": "<link de TikTok>"}',
            "info": "/info?url=<link>  (lista los enlaces que encuentra ttdownloader, sin descargar)",
            "auth": "header X-API-Key (solo si el servidor tiene API_KEY)",
            "respuesta": "archivo .mp4 + headers X-Source, X-Resolution, X-FPS, X-Bitrate-Kbps, X-Watermark",
        },
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"status": "ok", "ttdownloader": TTD is not None}


@app.get("/info")
def info(
    url: str = Query(..., description="Link del video de TikTok"),
    x_api_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    """Diagnóstico: muestra qué enlaces devuelve ttdownloader sin descargar el video."""
    _auth(x_api_key, key)
    url = _check(url)
    vid, canonical = resolve(url)
    for target in ([url] + ([canonical] if canonical and canonical != url else [])):
        try:
            items = _sorted_items(target)
            return {
                "video_id": vid,
                "consultado": target,
                "enlaces": [{"url": str(getattr(i, "url", "")),
                             "watermark": bool(getattr(i, "watermark", False))} for i in items],
            }
        except Exception as e:
            last = str(e)[:200]
    raise HTTPException(502, f"ttdownloader no devolvió enlaces: {last}")


@app.get("/download")
def download_get(
    url: str = Query(..., description="Link del video de TikTok"),
    x_api_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    _auth(x_api_key, key)
    return _serve(url)


@app.post("/download")
def download_post(
    body: DownloadBody,
    x_api_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    _auth(x_api_key, key)
    return _serve(body.url)
