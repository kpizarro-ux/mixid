from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uuid, os, shutil, subprocess, requests

app = FastAPI()

AUDD_API_TOKEN = os.getenv("AUDD_API_TOKEN")
SEGMENT_LENGTH = 30
TEMP_DIR = "temp_audio"

class URLRequest(BaseModel):
    url: str

@app.post("/identify")
def identify(req: URLRequest):
    # Replace this with your real track identification logic
    return [{"time": "00:00", "track": "Artist – Title"}]

@app.get("/")
def read_root():
    return {"message": "Mixid API is live. Use POST /identify to analyze DJ sets."}
