from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uuid
import os
import shutil
import subprocess
import requests

app = FastAPI()

AUDD_API_TOKEN = os.getenv("AUDD_API_TOKEN")
SEGMENT_LENGTH = 30  # seconds
TEMP_DIR = "temp_audio"

class URLRequest(BaseModel):
    url: str

@app.post("/identify")
def identify(req: URLRequest):
    if not AUDD_API_TOKEN:
        raise HTTPException(status_code=500, detail="Missing AUDD_API_TOKEN")

    # Create temp dir
    os.makedirs(TEMP_DIR, exist_ok=True)
    
    try:
        # Download audio using yt-dlp
        audio_filename = f"{uuid.uuid4()}.mp3"
        output_path = os.path.join(TEMP_DIR, audio_filename)
        
        cmd = [
            "yt-dlp",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "-o", output_path,
            req.url
        ]
        subprocess.run(cmd, check=True)

        # Split into segments
        segments = split_audio(output_path, SEGMENT_LENGTH)

        # Send each segment to AudD
        identified_tracks = []
        for idx, segment in enumerate(segments):
            with open(segment, 'rb') as f:
                res = requests.post(
                    "https://api.audd.io/",
                    data={"api_token": AUDD_API_TOKEN, "return": "apple_music,spotify"},
                    files={"file": f}
                )
                data = res.json()
                if data.get("result"):
                    time_stamp = idx * SEGMENT_LENGTH
                    minutes = time_stamp // 60
                    seconds = time_stamp % 60
                    time = f"{minutes:02}:{seconds:02}"
                    title = data["result"]["artist"] + " – " + data["result"]["title"]
                    identified_tracks.append({"time": time, "track": title})

        return identified_tracks or [{"time": "00:00", "track": "No results found"}]

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

@app.get("/")
def read_root():
    return {"message": "Mixid API is live. Use POST /identify to analyze DJ sets."}

def split_audio(input_path, segment_length):
    segments = []
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    output_template = os.path.join(TEMP_DIR, f"{base_name}_%03d.mp3")

    cmd = [
        "ffmpeg",
        "-i", input_path,
        "-f", "segment",
        "-segment_time", str(segment_length),
        "-c", "copy",
        output_template
    ]
    subprocess.run(cmd, check=True)

    for f in sorted(os.listdir(TEMP_DIR)):
        if f.startswith(base_name) and f.endswith(".mp3"):
            segments.append(os.path.join(TEMP_DIR, f))

    return segments

