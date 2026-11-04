# Third-party notices

This application snapshot does not bundle the publisher's installed Python environment, node_modules, uv binaries, Ollama, or model weights. Locked Python dependencies are installed from their respective public package sources; their licenses remain applicable. uv 0.13.7 is downloaded from the official astral-sh/uv release with a pinned SHA256 and is subject to its upstream MIT/Apache-2.0 licensing. Managed Python is downloaded by uv and remains subject to Python/standalone distribution licenses.

The included prebuilt frontend contains React, React DOM and Scheduler. Their copyright and permission text is reproduced below. Frontend build-only dependencies are specified by package-lock.json rather than bundled as installed packages.

## React / React DOM / Scheduler

MIT License

Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Runtime/build dependency source references

- FastAPI: https://github.com/fastapi/fastapi
- Uvicorn: https://github.com/encode/uvicorn
- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- Jieba: https://github.com/fxsjy/jieba
- sqlite-vec: https://github.com/asg017/sqlite-vec
- uv: https://github.com/astral-sh/uv
- Python: https://docs.python.org/3/license.html
- React: https://github.com/facebook/react
- Vite: https://github.com/vitejs/vite

These notices do not assign an open-source license to the application's own code.
