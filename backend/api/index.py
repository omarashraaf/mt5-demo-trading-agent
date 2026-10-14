import os
import sys

CURRENT_DIR = os.path.dirname(__file__)
BACKEND_DIR = os.path.dirname(CURRENT_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.append(BACKEND_DIR)

backend_mode = (os.getenv("VERCEL_BACKEND_MODE", "full") or "full").strip().lower()

if backend_mode in {"cloud", "cloud-lite", "lite"}:
    from cloud_main import app  # noqa: E402
else:
    from main import app  # noqa: E402
