# AI File Reader API

This project is a FastAPI-based web service that processes `.ai` (Adobe Illustrator) files from a given URL, extracts QR codes, and returns the data.

## Features
- Extract QR codes from `.ai` files via URL.
- Support for Firebase Storage URL formats.
- FastAPI-powered endpoints for both GET and POST requests.

## Prerequisites
- Python 3.x
- pip (Python package installer)

## Setup & Installation

1. **Clone or download** the project to your local machine.
2. **Install dependencies**:
   Open your terminal in the project directory and run:
   ```powershell
   python -m pip install -r requirements.txt
   ```

## Running the Project

You can start the server using either of the following commands:

### Option 1: Direct Script Run (Recommended)
```powershell
python main.py
```

### Option 2: Using Uvicorn directly
```powershell
python -m uvicorn main:app --reload --port 10000
```

Once running, the API will be available at `http://localhost:10000`.

## API Usage

### 1. Check if API is running
- **GET** `http://localhost:10000/`

### 2. Process a file (GET)
- **Endpoint**: `/run`
- **Query Parameter**: `url`
- **Example**: 
  `http://localhost:10000/run?url=https://example.com/file.ai`

### 3. Process a file (POST)
- **Endpoint**: `/run`
- **Body (JSON)**:
  ```json
  {
    "url": "https://example.com/file.ai"
  }
  ```

## Project Structure
- `main.py`: The FastAPI application and endpoints.
- `qr_from_storage_archive.py`: The core logic for processing `.ai` files and QR extraction.
- `requirements.txt`: List of required Python libraries.
- `.gitignore`: Files and directories to be ignored by Git.
