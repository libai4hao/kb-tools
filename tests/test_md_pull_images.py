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
import time
import unittest
from contextlib import contextmanager
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
            self.assertIn("2 张唯一远程图片", dry.stdout)

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


# ---------------------------------------------------------------- 同源限流


class _TrackingHandler(http.server.SimpleHTTPRequestHandler):
    """记录并发峰值与请求起始时刻，用来验证限流真的生效。"""

    lock = threading.Lock()
    active = 0
    peak = 0
    delay = 0.08

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.active = 0
            cls.peak = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        with _TrackingHandler.lock:
            _TrackingHandler.active += 1
            _TrackingHandler.peak = max(_TrackingHandler.peak, _TrackingHandler.active)
        try:
            time.sleep(_TrackingHandler.delay)
            super().do_GET()
        finally:
            with _TrackingHandler.lock:
                _TrackingHandler.active -= 1


def _serve(directory: Path, handler_cls) -> tuple[socketserver.ThreadingTCPServer, str]:
    port = _free_port()
    server = socketserver.ThreadingTCPServer(
        ("127.0.0.1", port),
        lambda *a, **kw: handler_cls(*a, directory=str(directory), **kw),
    )
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


@contextmanager
def _second_server():
    """第二个“站点”，用来验证跨源并行 / 同站合并。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pic").mkdir()
        for i in range(2):
            (root / "pic" / f"{i}.png").write_bytes(PNG_1PX)
        server, base = _serve(root, _TrackingHandler)
        try:
            yield base
        finally:
            server.shutdown()
            server.server_close()


_FOUR_IMAGES = (
    "![a]({a}/pic/0.png)\n\n![b]({a}/pic/1.png)\n\n"
    "![c]({b}/pic/0.png)\n\n![d]({b}/pic/1.png)\n"
)


class ThrottleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / "pic").mkdir(parents=True)
        for i in range(9):
            (cls.root / "pic" / f"{i}.png").write_bytes(PNG_1PX)
        cls.server, cls.base = _serve(cls.root, _TrackingHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        _TrackingHandler.reset()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, encoding="utf-8",
        )

    def _md_with(self, work: str, count: int, base: str) -> Path:
        md = Path(work) / "bulk.md"
        lines = [f"![图{i}]({base}/pic/{i}.png)" for i in range(count)]
        md.write_text("\n\n".join(lines), encoding="utf-8")
        return md

    def test_same_origin_is_paced_over_threshold(self):
        with tempfile.TemporaryDirectory() as work:
            md = self._md_with(work, 9, self.base)
            started = time.monotonic()
            proc = self._run(str(md), "-j", "8", "--throttle-threshold", "5",
                             "--per-host-jobs", "2", "--per-host-interval", "0.25")
            elapsed = time.monotonic() - started
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

            self.assertIn("启用同源限流", proc.stdout)
            self.assertIn("次请求", proc.stdout)
            # 同源并发受控：服务端观察到同时在飞的请求不超过 2
            self.assertLessEqual(_TrackingHandler.peak, 2, f"peak={_TrackingHandler.peak}")
            # 9 张、每源 2 并发、间隔 0.25s：至少被摊到 4 个时间片
            self.assertGreaterEqual(elapsed, 0.9, f"elapsed={elapsed:.2f}s")
            self.assertEqual(len(list((Path(work) / "bulk.assets").iterdir())), 9)

    def test_below_threshold_is_not_paced(self):
        with tempfile.TemporaryDirectory() as work:
            md = self._md_with(work, 3, self.base)
            proc = self._run(str(md), "-j", "8")
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertNotIn("同源限流", proc.stdout)
            self.assertGreaterEqual(_TrackingHandler.peak, 2, "小幅批量应保持并行")

    def test_different_origins_run_in_parallel(self):
        with _second_server() as other_base:
            with tempfile.TemporaryDirectory() as work:
                md = Path(work) / "two-sites.md"
                md.write_text(_FOUR_IMAGES.format(a=self.base, b=other_base),
                              encoding="utf-8")
                started = time.monotonic()
                proc = self._run(str(md), "--throttle-threshold", "3",
                                 "--per-host-jobs", "1", "--per-host-interval", "0.3")
                elapsed = time.monotonic() - started
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertEqual(proc.stdout.count("[ok]"), 4)
                # 一个源串行要 0.3s+；两个源各自串行应并行，总时间接近单源
                self.assertLess(elapsed, 0.9, f"elapsed={elapsed:.2f}s")

    def test_site_scope_merges_buckets(self):
        # 本地用不同端口模拟同一站点下的多个子域图床
        with _second_server() as other_base:
            with tempfile.TemporaryDirectory() as work:
                md = Path(work) / "same-site.md"
                md.write_text(_FOUR_IMAGES.format(a=self.base, b=other_base),
                              encoding="utf-8")
                started = time.monotonic()
                proc = self._run(str(md), "--throttle-threshold", "3",
                                 "--per-host-jobs", "1", "--per-host-interval", "0.3",
                                 "--throttle-scope", "site")
                elapsed = time.monotonic() - started
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertEqual(proc.stdout.count("[ok]"), 4)
                self.assertIn("启用同站限流", proc.stdout)
                # 合并成一个桶后 4 个请求串行：3 个间隔 ≈ 0.9s，明显慢于按源并行
                self.assertGreaterEqual(elapsed, 0.85, f"elapsed={elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
