"""md_pull_images 的离线测试：本地起一个 HTTP 服务当“远程图床”。"""

from __future__ import annotations

import base64
import http.server
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "md_pull_images.py"

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

SAMPLE_MD = """# 标题

普通图 ![a]({base}/pic/a.png)

带 title 的图 ![b]({base}/pic/b.png "标题")

HTML 图 <img src="{base}/pic/c.png" width="120" />

引用式图 ![d][ref1]

[ref1]: {base}/pic/d.png

本地图 ![local](./local.png)

data URI ![x](data:image/png;base64,AAAA)
"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 静音
        pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PullImagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        pic = cls.root / "pic"
        pic.mkdir()
        for name in ("a.png", "b.png", "c.png", "d.png"):
            (pic / name).write_bytes(PNG_1PX)

        cls.port = _free_port()
        handler = lambda *a, **kw: _Handler(*a, directory=str(cls.root), **kw)  # noqa: E731
        cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", cls.port), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, encoding="utf-8",
        )

    def test_download_and_rewrite(self):
        with tempfile.TemporaryDirectory() as work:
            md = Path(work) / "notes.md"
            md.write_text(SAMPLE_MD.format(base=self.base), encoding="utf-8")

            proc = self._run(str(md), "--rewrite")
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

            assets = Path(work) / "notes.assets"
            self.assertTrue(assets.is_dir())
            names = sorted(p.name for p in assets.iterdir())
            self.assertEqual(names, ["a.png", "b.png", "c.png", "d.png"])

            text = md.read_text(encoding="utf-8")
            self.assertNotIn(self.base, text)
            self.assertIn("![a](notes.assets/a.png)", text)
            self.assertIn('![b](notes.assets/b.png "标题")', text)
            self.assertIn('src="notes.assets/c.png"', text)
            self.assertIn("[ref1]: notes.assets/d.png", text)
            # 本地路径与 data URI 保持原样
            self.assertIn("![local](./local.png)", text)
            self.assertIn("data:image/png;base64", text)

    def test_no_rewrite_keeps_links(self):
        with tempfile.TemporaryDirectory() as work:
            md = Path(work) / "keep.md"
            md.write_text(SAMPLE_MD.format(base=self.base), encoding="utf-8")
            proc = self._run(str(md))
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn(self.base, md.read_text(encoding="utf-8"))
            self.assertEqual(len(list((Path(work) / "keep.assets").iterdir())), 4)

    def test_second_run_is_idempotent(self):
        with tempfile.TemporaryDirectory() as work:
            md = Path(work) / "again.md"
            md.write_text(SAMPLE_MD.format(base=self.base), encoding="utf-8")
            self._run(str(md))
            proc = self._run(str(md))
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("已存在 4", proc.stdout)

    def test_directory_mode_and_dry_run(self):
        with tempfile.TemporaryDirectory() as work:
            nested = Path(work) / "kb"
            (nested / "sub").mkdir(parents=True)
            (nested / "one.md").write_text(f"![x]({self.base}/pic/a.png)", encoding="utf-8")
            (nested / "sub" / "two.md").write_text(f"![y]({self.base}/pic/b.png)", encoding="utf-8")

            dry = self._run(str(nested), "--dry-run")
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertIn("2 张远程图片", dry.stdout.replace("共 2 个文件 / ", ""))

            proc = self._run(str(nested), "--rewrite")
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertTrue((nested / "one.assets" / "a.png").is_file())
            self.assertTrue((nested / "sub" / "two.assets" / "b.png").is_file())
            self.assertIn("one.assets/a.png", (nested / "one.md").read_text(encoding="utf-8"))
            self.assertIn("two.assets/b.png",
                          (nested / "sub" / "two.md").read_text(encoding="utf-8"))

    def test_missing_target(self):
        proc = self._run("definitely-not-here.md")
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
