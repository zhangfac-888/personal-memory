from __future__ import annotations

import argparse
import os
import socket
from pathlib import Path

import uvicorn

from .api import create_app
from .server_lock import ServerLock


class DeterministicE2EModel:
    """Local acceptance model; only selectable in an isolated test process."""
    def extract(self, messages):
        values = []
        for message in messages:
            if message.get("role") != "user":
                continue
            text = message.get("text", "")
            for quote, kind, topic in (
                ("这个项目的发布说明统一先写结论，再列三项变化。", "Rule", "发布说明"),
                ("这个新项目需要一份中文发布说明，先写结论。", "Rule", "发布说明"),
                ("以后和我交流都先给结论，再说明原因。", "Preference", "先给结论"),
            ):
                if quote in text:
                    values.append({"message_id": message["id"], "quote": quote,
                        "type": kind, "topic": topic, "replacement": False})
        return {"items": values[:5]}


def main():
    parser=argparse.ArgumentParser(description="Personal Memory local server")
    parser.add_argument("--data-dir",required=True,help="Absolute directory for this library; choose a separate directory for tests")
    parser.add_argument("--port",type=int,default=8765)
    args=parser.parse_args()
    data_dir=Path(args.data_dir)
    if not data_dir.is_absolute() or args.port<1024 or args.port>65535:
        parser.error("--data-dir must be absolute and --port must be 1024-65535")
    if os.environ.get("PERSONAL_MEMORY_E2E_MODEL") == "1" and (
        args.port == 8765 or data_dir.resolve() == Path(os.environ.get("PERSONAL_MEMORY_DATA", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "PersonalMemoryShare/Data"))).resolve()
        or data_dir.resolve() in {Path.home().resolve(),Path(data_dir.anchor).resolve()}
    ):
        parser.error("隔离模型拒绝正式数据路径、8765、用户主目录和盘符根目录")
    # Fail before creating a library when another service already owns the port.
    with socket.socket() as check:
        try: check.bind(("127.0.0.1",args.port))
        except OSError: parser.error(f"127.0.0.1:{args.port} is already in use")
    try:
        with ServerLock(data_dir.resolve()/"server.lock"):
            model = DeterministicE2EModel() if os.environ.get("PERSONAL_MEMORY_E2E_MODEL") == "1" else None
            app=create_app(data_dir,args.port,automation_model=model)
            print(f"Personal Memory: http://127.0.0.1:{args.port}/  data={data_dir.resolve()}")
            print(f"Admin key file: {data_dir.resolve() / 'admin-key.txt'}")
            uvicorn.run(app,host="127.0.0.1",port=args.port,workers=1,log_level="info")
    except ValueError as exc:
        parser.exit(2,str(exc)+"\n")


if __name__=="__main__":
    main()
