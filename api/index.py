from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uvicorn
import os
import sys

# Import the processing logic
# We prioritize importing from the local api/ directory for Vercel bundling
try:
    from .qr_from_storage_archive import process_archive_url
except (ImportError, ValueError):
    try:
        from qr_from_storage_archive import process_archive_url
    except ImportError:
        # Fallback for complex environments
        sys.path.append(os.path.dirname(os.path.abspath(__file__)))
        from qr_from_storage_archive import process_archive_url

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
