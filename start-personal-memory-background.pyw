"""Run the local service without a console, keeping diagnostics in its data directory."""

from __future__ import annotations

import argparse
import os
import sys
import subprocess
import urllib.request
import traceback
from pathlib import Path


def ensure_embedding_service(data_dir):
    """Start or reuse Ollama only for the BGE embedding model; no local generation."""
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:11434/api/tags",timeout=.5):
            return "already_running"
    except OSError:
        pass
    binary=Path(os.environ.get("LOCALAPPDATA",str(Path.home()/"AppData/Local")))/"Programs/Ollama/ollama.exe"
    if not binary.is_file():
        return "not_installed"
    environment=dict(os.environ)
    environment["OLLAMA_HOST"]="127.0.0.1:11434"
    with (data_dir/"ollama.stdout.log").open("a",encoding="utf8") as out,(data_dir/"ollama.stderr.log").open("a",encoding="utf8") as err:
        subprocess.Popen([str(binary),"serve"],env=environment,stdout=out,stderr=err,stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    return "started_hidden"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_absolute() or not (data_dir / ".personal-memory-data").is_file():
        raise SystemExit("Personal Memory data directory is not initialized")

    project_root = Path(__file__).resolve().parent
    os.chdir(project_root)
    with (data_dir / "server.stdout.log").open("a", encoding="utf-8", buffering=1) as out, (
        data_dir / "server.stderr.log"
    ).open("a", encoding="utf-8", buffering=1) as err:
        sys.stdout, sys.stderr = out, err
        try:
            print("Embedding service is optional; installer does not start it automatically")
            sys.argv = [str(project_root / "backend"), "--data-dir", str(data_dir), "--port", str(args.port)]
            from backend.__main__ import main as run_server

            run_server()
        except BaseException:
            traceback.print_exc(file=err)
            raise


if __name__ == "__main__":
    main()
