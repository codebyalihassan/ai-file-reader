from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uvicorn
import os
import sys
from urllib.parse import unquote

# Import the processing logic
# We add the current directory to sys.path to ensure local imports work on both Vercel and locally
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

try:
    from qr_from_storage_archive import process_archive_url
except ImportError:
    # Fallback for different execution contexts
    from .qr_from_storage_archive import process_archive_url

app = FastAPI(
    title="AI File Reader API",
    description="API to process .ai files from archives and extract QR codes",
    version="1.0.0"
)

# Add CORS middleware to allow requests from the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class UrlRequest(BaseModel):
    url: str

@app.get("/")
async def root():
    return {"status": "success", "message": "AI File Reader API is running. Use /run (GET/POST) with a 'url' parameter."}

@app.get("/run")
async def read_qr_get(request: Request):
    # Extract the raw 'url' parameter from the query string to preserve %2F encoding
    raw_query = request.url.query
    if not raw_query or 'url=' not in raw_query:
        raise HTTPException(status_code=400, detail={"error": "Missing url parameter"})
    
    # Split by 'url=' and take the rest
    parts = raw_query.split('url=', 1)
    full_url = parts[1]
    
    # We unquote ONCE to turn %3A%2F%2F into :// 
    # but preserve internal encoding like %2F if it was double-encoded by the client.
    full_url = unquote(full_url)
    
    # Log it for debugging
    print(f"Final Reconstructed URL: {full_url}")
            
    try:
        result = process_archive_url(full_url)
        return {"status": "success", "data": result}
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail={"error": str(e)})

@app.post("/run")
async def read_qr_post(request: UrlRequest):
    if not request.url:
        raise HTTPException(status_code=400, detail={"error": "Missing url parameter"})
    try:
        result = process_archive_url(request.url)
        return {"status": "success", "data": result}
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail={"error": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("index:app", host="0.0.0.0", port=port)
