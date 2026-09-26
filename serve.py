"""Start the server with the system python (adds the venv's site-packages manually)."""
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE] + glob.glob(os.path.join(HERE, ".venv/lib/python3*/site-packages"))
os.chdir(HERE)

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=int(os.environ.get("PORT", 8765)))
