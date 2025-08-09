# main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, shutil, glob, tempfile, subprocess, math, requests, pathlib
from typing import List

# -------- Config --------
SEGMENT_SECONDS = 30
AUDD_API_TOKEN = os.getenv("AUDD_API_TOKEN")  # set this in Render → Environment
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

@app.post("/identify")
def identify(req: URLRequest) -> List[dict]:
    if not req.url or len(req.url) < 8:
        raise HTTPException(status_code=400, detail="Invalid URL")

    if not AUDD_API_TOKEN:
        raise HTTPException(status_code=500, detail="Missing AUDD_API_TOKEN")

    # lazy import so container starts fast
    import yt_dlp
    import imageio_ffmpeg

    # get ffmpeg path (bundled binary from imageio-ffmpeg)
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    ffmpeg_dir = str(pathlib.Path(ffmpeg_exe).parent)

    with tempfile.TemporaryDirectory(prefix="mixid_") as tmp:
        # 1) Download audio as MP3
        target = os.path.join(tmp, "source.%(ext)s")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": target,
            "noplaylist": True,
            "quiet": True,
            "prefer_ffmpeg": True,
            "ffmpeg_location": ffmpeg_dir,        # ← important
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ],
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([req.url])
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Download failed: {e}")

        # the postprocessor should give us a .mp3 file now
        mp3_files = glob.glob(os.path.join(tmp, "source.mp3"))
        if not mp3_files:
            # fallback: any mp3 in tmp
            mp3_files = glob.glob(os.path.join(tmp, "*.mp3"))
        if not mp3_files:
            raise HTTPException(status_code=500, detail="Could not locate downloaded MP3")

        input_mp3 = mp3_files[0]

        # 2) Split into 30s segments (copy to keep speed; OK for MP3)
        seg_template = os.path.join(tmp, "seg_%05d.mp3")
        split_cmd = [
            ffmpeg_exe,
            "-hide_banner", "-loglevel", "error",
            "-i", input_mp3,
            "-f", "segment",
            "-segment_time", str(SEGMENT_SECONDS),
            "-c", "copy",
            seg_template,
        ]
        try:
            subprocess.run(split_cmd, check=True)
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"ffmpeg split failed: {e}")

        segments = sorted(glob.glob(os.path.join(tmp, "seg_*.mp3")))
        if not segments:
            raise HTTPException(status_code=500, detail="No segments were created")

        # 3) Send each segment to AudD
        identified = []
        last_title = None

        for idx, seg_path in enumerate(segments):
            # Optional safety cap (e.g., 50 segments = ~25 mins). Remove/calc for full sets.
            # if idx >= 50: break

            try:
                with open(seg_path, "rb") as f:
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
                # Skip this segment on transient error
                continue

            result = payload.get("result")
            if not result:
                continue

            artist = result.get("artist")
            title = result.get("title")
            if not artist or not title:
                continue

            track_title = f"{artist} – {title}"

            # dedupe consecutive identical results
            if track_title == last_title:
                continue
            last_title = track_title

            # compute hh:mm:ss from segment index
            seconds_from_start = idx * SEGMENT_SECONDS
            mm = seconds_from_start // 60
            ss = seconds_from_start % 60
            time_str = f"{mm:02}:{ss:02}"

            identified.append({
                "time": time_str,
                "track": track_title,
                # Optionally include links:
                # "spotify": result.get("spotify", {}).get("external_urls", {}).get("spotify")
            })

        if not identified:
            return [{"time": "00:00", "track": "No matches found"}]

        return identified


