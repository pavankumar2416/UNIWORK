"""Local development entry point for UNIWORK. Use Gunicorn for deployment."""
import os
from app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # Bind all interfaces so container platforms can route traffic to the process.
    app.run(host="0.0.0.0", port=port, debug=False)
