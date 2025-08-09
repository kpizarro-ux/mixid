# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os

app = FastAPI()

# Allowed origins (add more if needed)
ALLOWED_ORIGINS = [
    "https://mixid-frontend.vercel.app",  # Your deployed Vercel frontend
    "http://localhost:5173",              # Local dev
]

# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,   # You can use ["*"] during dev if needed
    allow_credentials=True,
    allow_methods=["*"],             # Allow POST, GET, OPTIONS, etc.
    allow_headers=["*"],             # Allow all headers
)

class URLRequest(BaseModel):
    url: str

@app.get("/")
def root():
    return {"message": "Mixid API is live. Use POST /identify to analyze DJ sets."}

@app.post("/identify")
def identify(req: URLRequest):
    # Temporary placeholder response to confirm CORS and POST are working
    return [{"time": "00:00", "track": "Artist – Title (dummy)"}]


