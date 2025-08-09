# main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import os, glob, tempfile, subprocess, pathlib, requests

# -------- Settings --------
SEGMENT_SECONDS = 30
AUDD_API_TOKEN = os.getenv("AUDD_API_TOKEN")  # set in Render → Environment

ALLOWED_ORIGINS = [
    "https://mixid-frontend.vercel.app",
    "http://localhost:5173",
]

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class URLRequest(BaseModel):
    url: str

@app.get("/")
def root():
    return {"message": "Mixid API is live. Use POST /identify to analyze DJ sets."}

@app.get("/health")
def health():
    return {"ok": True}

# ---------- Helpers ----------
def _timestamp_from_index(idx: int, step: int) -> str:
    total = idx * step
    mm, ss = divmod(total, 60)
    return f"{mm:02}:{ss:02}"

# ---------- Main endpoint ----------
@app.post("/identify")
def identify(req: URLRequest) -> List[dict]:
    if not req.url or len(req.url) < 8:
        raise HTTPException(status_code=400, detail="Invalid URL")

    if not AUDD_API_TOKEN:
        raise HTTPException(status_code=500, detail="Missing AUDD_API_TOKEN")

    # Lazy imports so cold starts are faster
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
            "ffmpeg_location": ffmpeg_dir,  # use bundled ffmpeg
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ],
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([req.url])
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Download failed: {e}")

        # locate mp3
        mp3 = None
        for cand in ("source.mp3", "*.mp3"):
            paths = glob.glob(os.path.join(tmp, cand))
            if paths:
                mp3 = paths[0]
                break
        if not mp3:
            raise HTTPException(status_code=500, detail="Could not locate downloaded MP3")

        # 2) Split into 30s segments using stream copy (fast)
        seg_tpl = os.path.join(tmp, "seg_%05d.mp3")
        try:
            subprocess.run(
                [
                    ffmpeg_exe,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-i", mp3,
                    "-f", "segment",
                    "-segment_time", str(SEGMENT_SECONDS),
                    "-c", "copy",
                    seg_tpl,
                ],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"ffmpeg split failed: {e}")

        segments = sorted(glob.glob(os.path.join(tmp, "seg_*.mp3")))
        if not segments:
            raise HTTPException(status_code=500, detail="No segments created")

        # 3) Send each segment to AudD
        results: List[dict] = []
        last_title = None

        for idx, seg in enumerate(segments):
            # Safety cap for free tier latency; raise/remove for full sets
            # if idx >= 60: break  # ~30 minutes

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
                # Skip transient errors gracefully
                continue

            result = payload.get("result")
            if not result:
                continue

            artist = result.get("artist")
            title = result.get("title")
            if not artist or not title:
                continue

            track_title = f"{artist} – {title}"

            # de‑dupe consecutive identical hits
            if track_title == last_title:
                continue
            last_title = track_title

            results.append({
                "time": _timestamp_from_index(idx, SEGMENT_SECONDS),
                "track": track_title,
                # Optional:
                # "spotify": result.get("spotify", {}).get("external_urls", {}).get("spotify"),
            })

        return results or [{"time": "00:00", "track": "No matches found"}]


