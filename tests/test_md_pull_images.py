"""md_pull_images 的离线测试：本地起一个 HTTP 服务当“远程图床”。"""

from __future__ import annotations

import ast
import base64
import http.server
import re
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from contextlib import contextmanager
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "md_pull_images.py"
sys.path.insert(0, str(SCRIPT.parent))
import md_pull_images as pull_images  # noqa: E402

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
            [sys.executable, str(SCRIPT), "--allow-private-addresses", *args],
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
            names = sorted(p.name for p in assets.iterdir() if p.is_file() and not p.name.startswith("."))
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
            self.assertEqual(
                len([p for p in (Path(work) / "keep.assets").iterdir() if not p.name.startswith(".")]),
                4,
            )

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


class ParserAndSafetyTest(unittest.TestCase):
    def test_parser_ignores_code_and_parses_parentheses_and_exact_src(self):
        text = """```markdown
![code](https://example.test/code.png)
```
Inline `![inline](https://example.test/inline.png)` should be ignored.
![real](https://example.test/files/plot_(1).png)
<img data-src="https://example.test/no.png" src="https://example.test/yes.png">
<!-- ![comment](https://example.test/comment.png)
```markdown
![hidden](https://example.test/hidden.png)
```
-->
![after-comment](https://example.test/after.png)
"""
        hits = pull_images.iter_hits(text)
        self.assertEqual(
            [hit.url for hit in hits],
            ["https://example.test/files/plot_(1).png", "https://example.test/yes.png",
             "https://example.test/after.png"],
        )
        misleading_alt = '<img alt="a src=\'https://example.test/fake.png\' attribute" src="https://example.test/actual.png">'
        self.assertEqual(
            [hit.url for hit in pull_images.iter_hits(misleading_alt)],
            ["https://example.test/actual.png"],
        )

    def test_reference_definition_span_is_deduplicated(self):
        text = (
            "![first][pic]\n![second][pic]\n![collapsed][]\n\n"
            "[pic]: https://example.test/a.png\n"
            "[collapsed]: https://example.test/b.png\n"
        )
        hits = pull_images.iter_hits(text)
        self.assertEqual(len(hits), 2)
        self.assertEqual({hit.url for hit in hits}, {
            "https://example.test/a.png", "https://example.test/b.png"
        })

    def test_retry_after_supports_seconds_and_http_date(self):
        now = datetime(2026, 11, 22, 1, 0, 0, tzinfo=timezone.utc)
        future = now + timedelta(seconds=75)
        self.assertEqual(pull_images._parse_retry_after("12", now), 12.0)
        self.assertEqual(pull_images._parse_retry_after(format_datetime(future), now), 75.0)
        self.assertIsNone(pull_images._parse_retry_after("not-a-date", now))

    def test_origin_normalization_and_multilabel_public_suffixes(self):
        self.assertEqual(pull_images.origin_of("HTTPS://Example.COM:443/a"), "https://example.com")
        self.assertEqual(pull_images.origin_of("http://Example.COM:80/a"), "http://example.com")
        self.assertEqual(pull_images.origin_of("https://example.com:8443/a"), "https://example.com:8443")
        self.assertEqual(
            pull_images.throttle_key("https://img1.foo.example.co.uk/a", "site"),
            "example.co.uk",
        )
        self.assertEqual(
            pull_images.throttle_key("https://img2.bar.other.co.uk/a", "site"),
            "other.co.uk",
        )
        self.assertEqual(pull_images.throttle_key("http://127.0.0.1:9000/a", "site"), "127.0.0.1")

    def test_private_addresses_are_blocked_unless_explicitly_allowed(self):
        with self.assertRaisesRegex(ValueError, "私有"):
            pull_images._validate_remote_url("http://127.0.0.1:8080/image.png")
        pull_images._validate_remote_url(
            "http://127.0.0.1:8080/image.png", allow_private_addresses=True
        )
        with self.assertRaisesRegex(ValueError, "端口"):
            pull_images._validate_remote_url(
                "http://127.0.0.1:0/image.png", allow_private_addresses=True
            )
        with self.assertRaises(ValueError):
            pull_images._validate_remote_url("file:///etc/passwd", allow_private_addresses=True)

    def test_fetch_negative_retries_returns_error_not_success(self):
        result = pull_images.fetch("https://example.test/a.png", 1.0, -1, None)
        self.assertTrue(result.error)
        self.assertIsNone(result.data)

    def test_redirect_handler_rejects_private_redirect_target(self):
        request = pull_images.urllib.request.Request("https://example.test/start")
        handler = pull_images._SafeRedirectHandler()
        with self.assertRaisesRegex(ValueError, "私有"):
            handler.redirect_request(
                request, None, 302, "Found", {}, "http://127.0.0.1/internal.png"
            )

    def test_source_and_tests_parse_with_python38_grammar(self):
        """README 承诺 Python 3.8+，这里守住语法下限。"""
        for path in (SCRIPT, Path(__file__).resolve()):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path),
                          feature_version=(3, 8))


class _CountingHtmlHandler(http.server.SimpleHTTPRequestHandler):
    lock = threading.Lock()
    count = 0
    status = 200

    def log_message(self, *args):
        pass

    def do_GET(self):
        with _CountingHtmlHandler.lock:
            _CountingHtmlHandler.count += 1
        body = b"<html><body>not an image</body></html>"
        self.send_response(_CountingHtmlHandler.status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RewriteAndCollisionTest(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--allow-private-addresses", *args],
            capture_output=True, text=True, encoding="utf-8",
        )

    def test_repeated_reference_rewrites_definition_only_once(self):
        with tempfile.TemporaryDirectory() as work, _single_image_server() as base:
            md = Path(work) / "refs.md"
            md.write_text(
                f"![one][pic]\n![two][pic]\n\n[pic]: {base}/pic/a.png\n",
                encoding="utf-8",
            )
            proc = self._run(str(md), "--rewrite")
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            rewritten = md.read_text(encoding="utf-8")
            self.assertEqual(
                rewritten.count("[pic]: refs.assets/a.png"), 1,
                rewritten,
            )
            self.assertIn("![one][pic]", rewritten)
            self.assertIn("![two][pic]", rewritten)

    def test_untracked_same_name_is_never_reused_without_overwrite(self):
        with tempfile.TemporaryDirectory() as work, _single_image_server() as base:
            md = Path(work) / "stale.md"
            md.write_text(f"![a]({base}/pic/a.png)", encoding="utf-8")
            assets = Path(work) / "stale.assets"
            assets.mkdir()
            stale = assets / "a.png"
            stale.write_bytes(b"stale-user-file")
            proc = self._run(str(md), "--rewrite")
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(stale.read_bytes(), b"stale-user-file")
            rewritten = md.read_text(encoding="utf-8")
            self.assertNotIn("stale.assets/a.png)", rewritten)
            self.assertRegex(rewritten, r"stale\.assets/a-[0-9a-f]{12}\.png\)")
            self.assertTrue((assets / ".md_pull_images-manifest.json").is_file())

    def test_different_urls_with_same_basename_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as work, \
             tempfile.TemporaryDirectory() as root_a, \
             tempfile.TemporaryDirectory() as root_b:
            a_root, b_root = Path(root_a), Path(root_b)
            (a_root / "pic").mkdir()
            (b_root / "pic").mkdir()
            (a_root / "pic" / "same.png").write_bytes(PNG_1PX)
            (b_root / "pic" / "same.png").write_bytes(PNG_1PX + b"second-origin")
            server_a, base_a = _serve(a_root, _Handler)
            server_b, base_b = _serve(b_root, _Handler)
            try:
                md = Path(work) / "collision.md"
                md.write_text(
                    f"![a]({base_a}/pic/same.png)\n![b]({base_b}/pic/same.png)\n",
                    encoding="utf-8",
                )
                proc = self._run(str(md), "--rewrite", "--overwrite")
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                assets = Path(work) / "collision.assets"
                names = sorted(p.name for p in assets.glob("*.png"))
                self.assertEqual(len(names), 2, names)
                self.assertNotEqual(names[0], names[1])
                rewritten = md.read_text(encoding="utf-8")
                links = re.findall(r"collision\.assets/[^)]+", rewritten)
                self.assertEqual(len(links), 2, rewritten)
                self.assertNotEqual(links[0], links[1])
                self.assertEqual(len({(assets / name).read_bytes() for name in names}), 2)
            finally:
                for server in (server_a, server_b):
                    server.shutdown()
                    server.server_close()

    def test_cli_rejects_invalid_numeric_options(self):
        with tempfile.TemporaryDirectory() as work:
            md = Path(work) / "empty.md"
            md.write_text("no images", encoding="utf-8")
            invalid_sets = (
                ("--retries", "-1"), ("--jobs", "0"), ("--timeout", "0"),
                ("--per-host-jobs", "0"), ("--per-host-interval", "-0.1"),
                ("--throttle-threshold", "-1"), ("--max-image-size", "0"),
            )
            for option, value in invalid_sets:
                with self.subTest(option=option):
                    proc = self._run(str(md), option, value)
                    self.assertEqual(proc.returncode, 2, proc.stderr + proc.stdout)

    def test_html_response_is_rejected_as_non_image(self):
        class HtmlHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b"<html><body>not an image</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as root:
            server, base = _serve(Path(root), HtmlHandler)
            try:
                md = Path(work) / "bad-content.md"
                original = f"![a]({base}/image.png)"
                md.write_text(original, encoding="utf-8")
                proc = self._run(str(md), "--rewrite", "--retries", "0")
                self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
                self.assertIn("Content-Type 不是图片类型", proc.stdout)
                self.assertEqual(md.read_text(encoding="utf-8"), original)
                self.assertFalse((Path(work) / "bad-content.assets" / "image.png").exists())
            finally:
                server.shutdown()
                server.server_close()

    def test_mime_signature_mismatch_is_rejected(self):
        class MismatchedMimeHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = PNG_1PX
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as root:
            server, base = _serve(Path(root), MismatchedMimeHandler)
            try:
                md = Path(work) / "mismatch.md"
                original = f"![a]({base}/image.jpg)"
                md.write_text(original, encoding="utf-8")
                proc = self._run(str(md), "--rewrite", "--retries", "0")
                self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
                self.assertIn("MIME 类型与文件内容不匹配", proc.stdout)
                self.assertEqual(md.read_text(encoding="utf-8"), original)
            finally:
                server.shutdown()
                server.server_close()

    def test_max_image_size_fails_without_rewriting(self):
        with tempfile.TemporaryDirectory() as work, _single_image_server() as base:
            md = Path(work) / "large.md"
            original = f"![a]({base}/pic/a.png)"
            md.write_text(original, encoding="utf-8")
            proc = self._run(str(md), "--rewrite", "--max-image-size", "10")
            self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
            self.assertIn("超过大小限制", proc.stdout)
            self.assertEqual(md.read_text(encoding="utf-8"), original)

    def test_default_cli_blocks_private_network_target(self):
        with tempfile.TemporaryDirectory() as work, _single_image_server() as base:
            md = Path(work) / "private.md"
            md.write_text(f"![a]({base}/pic/a.png)", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), str(md), "--retries", "0"],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
            self.assertIn("私有", proc.stdout)

    def test_permanent_failures_are_not_retried(self):
        """内容校验失败和 4xx 重试也不会有不同结果，不应白白重试。"""
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as root:
            _CountingHtmlHandler.count = 0
            _CountingHtmlHandler.status = 200
            server, base = _serve(Path(root), _CountingHtmlHandler)
            try:
                md = Path(work) / "permanent.md"
                md.write_text(f"![a]({base}/image.png)", encoding="utf-8")
                proc = self._run(str(md), "--retries", "2")
                self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
                self.assertEqual(_CountingHtmlHandler.count, 1)

                _CountingHtmlHandler.count = 0
                _CountingHtmlHandler.status = 404
                proc = self._run(str(md), "--retries", "2")
                self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
                self.assertEqual(_CountingHtmlHandler.count, 1)
            finally:
                _CountingHtmlHandler.status = 200
                server.shutdown()
                server.server_close()

    def test_server_errors_are_still_retried(self):
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as root:
            _CountingHtmlHandler.count = 0
            _CountingHtmlHandler.status = 503
            server, base = _serve(Path(root), _CountingHtmlHandler)
            try:
                md = Path(work) / "retry503.md"
                md.write_text(f"![a]({base}/image.png)", encoding="utf-8")
                proc = self._run(str(md), "--retries", "2")
                self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
                self.assertEqual(_CountingHtmlHandler.count, 3)
            finally:
                _CountingHtmlHandler.status = 200
                server.shutdown()
                server.server_close()


# ---------------------------------------------------------------- 同源限流


class _TrackingHandler(http.server.SimpleHTTPRequestHandler):
    """记录并发峰值与请求起始时刻，用来验证限流真的生效。"""

    lock = threading.Lock()
    active = 0
    peak = 0
    delay = 0.08
    starts = []

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.active = 0
            cls.peak = 0
            cls.starts = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        with _TrackingHandler.lock:
            _TrackingHandler.active += 1
            _TrackingHandler.peak = max(_TrackingHandler.peak, _TrackingHandler.active)
            _TrackingHandler.starts.append((self.server.server_address[1], time.monotonic()))
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
def _single_image_server():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pic").mkdir()
        for name in ("a.png", "b.png", "same.png"):
            (root / "pic" / name).write_bytes(PNG_1PX)
        server, base = _serve(root, _Handler)
        try:
            yield base
        finally:
            server.shutdown()
            server.server_close()


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
            [sys.executable, str(SCRIPT), "--allow-private-addresses", *args],
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
            arrivals = sorted(stamp for port, stamp in _TrackingHandler.starts)
            self.assertEqual(len(arrivals), 9)
            self.assertGreaterEqual(min(b - a for a, b in zip(arrivals, arrivals[1:])), 0.24)
            # 9 张、每源 2 并发、间隔 0.25s：至少被摊到 4 个时间片
            self.assertGreaterEqual(elapsed, 0.9, f"elapsed={elapsed:.2f}s")
            self.assertEqual(len([p for p in (Path(work) / "bulk.assets").iterdir() if not p.name.startswith(".")]), 9)

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
                _TrackingHandler.reset()
                original_delay = _TrackingHandler.delay
                _TrackingHandler.delay = 0.2
                try:
                    proc = self._run(str(md), "--throttle-threshold", "3",
                                     "--per-host-jobs", "1", "--per-host-interval", "0.3")
                finally:
                    _TrackingHandler.delay = original_delay
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertEqual(proc.stdout.count("[ok]"), 4)
                # Each origin has a one-request semaphore; global overlap proves
                # different origin buckets did not serialize one another.
                self.assertGreaterEqual(_TrackingHandler.peak, 2,
                                        f"server-observed peak={_TrackingHandler.peak}")

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
