import os
import sys

CURRENT_DIR = os.path.dirname(__file__)
BACKEND_DIR = os.path.dirname(CURRENT_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.append(BACKEND_DIR)

from cloud_main import app  # noqa: E402
