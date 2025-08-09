from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os

app = FastAPI()

# CORS settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all for now
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AUDD_API_TOKEN = os.getenv("AUDD_API_TOKEN")

class URLRequest(BaseModel):
    url: str

@app.get("/")
def read_root():
    return {"message": "Mixid API is live. Use POST /identify to analyze DJ sets."}

@app.post("/identify")
def identify(req: URLRequest):
    try:
        # TEMP: just to confirm request works
        print(f"Received URL: {req.url}")
        if not AUDD_API_TOKEN:
            raise ValueError("Missing AUDD_API_TOKEN environment variable")

        # TODO: Actual track detection logic here
        return [{"time": "00:00", "track": "Artist – Title"}]

    except Exception as e:
        print(f"Error in /identify: {str(e)}")  # Will show in Render logs
        raise HTTPException(status_code=500, detail=str(e))



