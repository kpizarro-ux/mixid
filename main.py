# main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import os, glob, tempfile, subprocess, pathlib, requests, logging, shutil

# ========= Config =========
SEGMENT_SECONDS = int(os.getenv("SEGMENT_SECONDS", "30"))
MAX_SEGMENTS = int(os.getenv("MAX_SEGMENTS", "100"))  # ~50 minutes cap; raise as needed
AUDD_API_TOKEN = os.getenv("AUDD_API_TOKEN")
COOKIE_FILE = os.getenv("COOKIE_FILE")  # e.g., /etc/secrets/youtube_cookies.txt

ALLOWED_ORIGINS = [
    "https://mixid-frontend.vercel.app",
    "http://localhost:5173",
]

# ========= App =========
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mixid")

class URLRequest(BaseModel):
    url: str

class IdentifyResult(BaseModel):
    time: str
    track: str
    spotify: Optional[str] = None

@app.get("/")
def root():
    return {"message": "Mixid API is live. Use POST /identify to analyze DJ sets."}

@app.get("/health")
def health():
    return {"ok": True}

def ts_from_idx(idx: int, step: int) -> str:
    mm, ss = divmod(idx * step, 60)
    return f"{mm:02}:{ss:02}"

@app.post("/identify", response_model=List[IdentifyResult])
def identify(req: URLRequest):
    if not req.url or len(req.url) < 8:
        raise HTTPException(status_code=400, detail="Invalid URL")
    if not AUDD_API_TOKEN:
        raise HTTPException(status_code=500, detail="Missing AUDD_API_TOKEN")

    log.info(f"mixid: Identify URL: {req.url}")

    # Lazy import for faster cold start
    import yt_dlp
    import imageio_ffmpeg

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    ffmpeg_dir = str(pathlib.Path(ffmpeg_exe).parent)

    with tempfile.TemporaryDirectory(prefix="mixid_") as tmp:
        # 0) If cookies are provided, copy to a writable temp path
        cookiefile_for_ytdlp = None
        if COOKIE_FILE and os.path.exists(COOKIE_FILE):
            cookiefile_for_ytdlp = os.path.join(tmp, "cookies.txt")
            try:
                shutil.copy(COOKIE_FILE, cookiefile_for_ytdlp)
            except Exception as e:
                log.warning(f"Could not copy cookies file: {e}")
                cookiefile_for_ytdlp = None

        # 1) Download best audio WITHOUT postprocessors (avoid ffprobe requirement)
        #    Let yt-dlp pick container (webm/m4a/opus/etc.)
        out_tmpl = os.path.join(tmp, "source.%(ext)s")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "noplaylist": True,
            "quiet": True,
            "retries": 3,
            "http_headers": {"User-Agent": "Mozilla/5.0 (Mixid yt-dlp)"},
            # Use our ffmpeg for any internal remux if needed
            "ffmpeg_location": ffmpeg_dir,
        }
        if cookiefile_for_ytdlp:
            ydl_opts["cookiefile"] = cookiefile_for_ytdlp

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([req.url])
        except Exception as e:
            msg = f"Download failed: {e}"
            log.error(msg)
            raise HTTPException(status_code=502, detail=msg)

        # 2) Find the downloaded file (unknown extension)
        src_candidates = glob.glob(os.path.join(tmp, "source.*"))
        if not src_candidates:
            # fallback catch-all
            src_candidates = glob.glob(os.path.join(tmp, "*.*"))
        if not src_candidates:
            raise HTTPException(status_code=500, detail="No downloaded file found")
        src_path = src_candidates[0]

        # 3) Convert to MP3 ourselves with ffmpeg (no ffprobe needed)
        mp3_path = os.path.join(tmp, "audio.mp3")
        try:
            # Re-encode to mp3 CBR 192k to keep AudD happy
            subprocess.run(
                [
                    ffmpeg_exe, "-hide_banner", "-loglevel", "error",
                    "-i", src_path,
                    "-vn", "-acodec", "libmp3lame", "-b:a", "192k",
                    mp3_path,
                ],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            msg = f"ffmpeg convert failed: {e}"
            log.error(msg)
            raise HTTPException(status_code=500, detail=msg)

        # 4) Split MP3 into segments using stream copy (fast)
        seg_tpl = os.path.join(tmp, "seg_%05d.mp3")
        try:
            subprocess.run(
                [
                    ffmpeg_exe, "-hide_banner", "-loglevel", "error",
                    "-i", mp3_path,
                    "-f", "segment",
                    "-segment_time", str(SEGMENT_SECONDS),
                    "-c", "copy",
                    seg_tpl,
                ],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            msg = f"ffmpeg split failed: {e}"
            log.error(msg)
            raise HTTPException(status_code=500, detail=msg)

        segments = sorted(glob.glob(os.path.join(tmp, "seg_*.mp3")))
        if not segments:
            raise HTTPException(status_code=500, detail="No segments created")

        # 5) Send each segment to AudD
        results: List[IdentifyResult] = []
        last_title = None
        segs = segments[:MAX_SEGMENTS]  # keep latency in check

        for idx, seg in enumerate(segs):
            try:
                with open(seg, "rb") as f:
                    r = requests.post(
                        "https://api.audd.io/",
                        data={
                            "api_token": AUDD_API_TOKEN,
                            "return": "timecode,spotify,apple_music",
                        },
                        files={"file": f},
                        timeout=60,
                    )
                payload = r.json()
            except Exception as e:
                log.warning(f"AUDD request failed seg {idx}: {e}")
                continue

            result = payload.get("result")
            if not result:
                continue

            artist = result.get("artist")
            title = result.get("title")
            if not artist or not title:
                continue

            track_title = f"{artist} – {title}"
            if track_title == last_title:
                continue
            last_title = track_title

            spotify_url = None
            try:
                spotify_url = (
                    result.get("spotify", {})
                          .get("external_urls", {})
                          .get("spotify")
                )
            except Exception:
                pass

            results.append(
                IdentifyResult(
                    time=ts_from_idx(idx, SEGMENT_SECONDS),
                    track=track_title,
                    spotify=spotify_url,
                )
            )

        return results or [IdentifyResult(time="00:00", track="No matches found")]
