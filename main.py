# main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import os, glob, tempfile, subprocess, pathlib, requests, logging, json

# ========= Config =========
SEGMENT_SECONDS = int(os.getenv("SEGMENT_SECONDS", "30"))
MAX_SEGMENTS = int(os.getenv("MAX_SEGMENTS", "100"))  # ~50 min default; bump as needed
AUDD_API_TOKEN = os.getenv("AUDD_API_TOKEN")
COOKIE_FILE = os.getenv("COOKIE_FILE")  # e.g. /etc/secrets/youtube_cookies.txt

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

# ========= Models =========
class URLRequest(BaseModel):
    url: str

class IdentifyResult(BaseModel):
    time: str
    track: str
    spotify: Optional[str] = None

# ========= Routes =========
@app.get("/")
def root():
    return {"message": "Mixid API is live. Use POST /identify to analyze DJ sets."}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/identify", response_model=List[IdentifyResult])
def identify(req: URLRequest):
    if not req.url or len(req.url) < 8:
        raise HTTPException(status_code=400, detail="Invalid URL")

    if not AUDD_API_TOKEN:
        raise HTTPException(status_code=500, detail="Missing AUDD_API_TOKEN")

    log.info(f"Identify URL: {req.url}")

    # Lazy import (keeps cold start quick)
    import yt_dlp
    import imageio_ffmpeg

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    ffmpeg_dir = str(pathlib.Path(ffmpeg_exe).parent)

    with tempfile.TemporaryDirectory(prefix="mixid_") as tmp:
        # 1) Download best audio → mp3
        out = os.path.join(tmp, "source.%(ext)s")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": out,
            "noplaylist": True,
            "quiet": True,
            "prefer_ffmpeg": True,
            "ffmpeg_location": ffmpeg_dir,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ],
            # cookies fix YouTube 429 / bot checks
            **({"cookiefile": COOKIE_FILE} if COOKIE_FILE else {}),
            # Slightly friendlier UA
            "http_headers": {"User-Agent": "Mozilla/5.0 (Mixid yt-dlp)"},
            "retries": 3,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([req.url])
        except Exception as e:
            msg = f"Download failed: {e}"
            log.error(msg)
            raise HTTPException(status_code=502, detail=msg)

        # locate mp3
        mp3 = None
        for cand in ("source.mp3", "*.mp3"):
            paths = glob.glob(os.path.join(tmp, cand))
            if paths:
                mp3 = paths[0]
                break
        if not mp3:
            raise HTTPException(status_code=500, detail="Could not locate downloaded MP3")

        # 2) Split into segments
        seg_tpl = os.path.join(tmp, "seg_%05d.mp3")
        try:
            subprocess.run(
                [
                    ffmpeg_exe, "-hide_banner", "-loglevel", "error",
                    "-i", mp3,
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

        # 3) Call AudD for each segment
        results: List[IdentifyResult] = []
        last_title = None

        # Safety cap to control latency/cost
        segments = segments[:MAX_SEGMENTS]

        for idx, seg in enumerate(segments):
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
                log.warning(f"AUDD request failed on seg {idx}: {e}")
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
                # de‑dupe consecutive identical detection
                continue
            last_title = track_title

            mm, ss = divmod(idx * SEGMENT_SECONDS, 60)
            spotify_url = None
            try:
                spotify_url = (
                    result.get("spotify", {})
                          .get("external_urls", {})
                          .get("spotify")
                )
            except Exception:
                pass

            results.append(IdentifyResult(
                time=f"{mm:02}:{ss:02}",
                track=track_title,
                spotify=spotify_url
            ))

        if not results:
            return [IdentifyResult(time="00:00", track="No matches found")]

        return results

