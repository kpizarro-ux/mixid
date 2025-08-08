from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os

app = FastAPI()

# Allow frontend to make requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # use your frontend domain for tighter security
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class URLRequest(BaseModel):
    url: str

@app.post("/identify")
def identify(req: URLRequest):
    return [{"time": "00:00", "track": "Artist – Title"}]

@app.get("/")
def read_root():
    return {"message": "Mixid API is live. Use POST /identify to analyze DJ sets."}

