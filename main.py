import uvicorn
import os
import sys

# Add the current directory to path so we can import from the api folder
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from api.index import app

if __name__ == "__main__":
    # This allows you to still run 'python main.py' locally
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
