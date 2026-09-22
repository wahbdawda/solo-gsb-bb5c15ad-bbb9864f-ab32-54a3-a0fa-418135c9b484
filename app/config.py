"""Application settings, overridable through environment variables."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Directory used to keep uploaded originals and produced ZIPs.
DATA_DIR = Path(os.getenv("DICOMBENCH_DATA_DIR",
                          tempfile.gettempdir())).resolve() / "dicombench"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Salt used for deterministic pseudonym/UID derivation.  Every deployment may
# override it; changing it changes every pseudonym.
PSEUDONYM_SALT = os.getenv("DICOMBENCH_SALT", "dicombench-default-salt-v1")

# Maximum accepted upload size (0 = no limit).  Starlette spools large
# uploads to a temporary file, so this only guards total memory/disk use.
MAX_UPLOAD_BYTES = _int("DICOMBENCH_MAX_UPLOAD_MB", 2048) * 1024 * 1024

# Remove a job (and its files) this many seconds after creation.
JOB_TTL_SECONDS = _int("DICOMBENCH_JOB_TTL_SECONDS", 60 * 60)

# Maximum file size (bytes) still probed as a possible DICOM object.
MAX_PROBE_BYTES = _int("DICOMBENCH_MAX_PROBE_MB", 256) * 1024 * 1024

HOST = os.getenv("DICOMBENCH_HOST", "0.0.0.0")
PORT = _int("DICOMBENCH_PORT", 8000)
