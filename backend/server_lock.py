from __future__ import annotations

import msvcrt
from pathlib import Path


class ServerLock:
    def __init__(self,path:Path): self.path=path;self.handle=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.handle=self.path.open("a+b")
        self.handle.seek(0)
        self.handle.write(b"1");self.handle.flush();self.handle.seek(0)
        try: msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            self.handle.close();self.handle=None
            raise ValueError("数据目录正在被另一个 Personal Memory 进程使用")
        return self
    def __exit__(self,*_):
        if self.handle:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
            self.handle.close();self.handle=None
