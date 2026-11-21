#!/usr/bin/env python3
"""md_pull_images - 把 Markdown 里的远程图片拉取到本地。

支持单个 .md 文件或整个目录（目录内递归查找 Markdown），自动识别以下写法：

    ![alt](https://example.com/a.png)
    ![alt](https://example.com/a.png "title")
    <img src="https://example.com/b.gif" />
    ![alt][ref]  +  [ref]: https://example.com/c.jpg

只下载 http(s) 远程图片；已经是本地相对路径、data: URI、绝对路径的图片会被跳过。
默认行为是「只下载不改文件」，加 --rewrite 才会把 Markdown 里的远程链接替换成本地相对路径。

用法示例：
    python md_pull_images.py 排序算法.md
    python md_pull_images.py 排序算法.md --rewrite
    python md_pull_images.py ./notes --rewrite --assets-dir assets
    python md_pull_images.py ./notes --rewrite --throttle-scope site --per-host-interval 0.6

批量拉取时，图片总数超过 --throttle-threshold（默认 20）会自动按同源限流：
每个源限制并发数并强制请求间隔，避免被图床按 IP 限速。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import random
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

MD_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd"}
IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp",
    ".avif", ".ico", ".tif", ".tiff", ".apng",
}
CONTENT_TYPE_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg", "image/bmp": ".bmp",
    "image/avif": ".avif", "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
    "image/tiff": ".tiff", "image/apng": ".apng",
}
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 md_pull_images/0.1"
)

# Regexes are applied to a same-length masked copy of the Markdown so code
# examples do not become downloads and source offsets remain stable.
REF_DEF = re.compile(
    r"^[ \t]{0,3}\[(?P<id>[^]\n]+)\]:[ \t]*(?:<(?P<angle>[^>]+)>|(?P<plain>\S+))",
    re.MULTILINE,
)
MD_IMAGE_REF = re.compile(r"!\[(?P<alt>(?:\\.|[^]\\])*)\]\[(?P<id>(?:\\.|[^]\\])*)\]")
FENCE_OPEN = re.compile(r"^[ ]{0,3}(?P<fence>`{3,}|~{3,})")
HTML_TAG = re.compile(r'''<img\b(?:"[^"]*"|'[^']*'|[^'">])*>''', re.IGNORECASE | re.DOTALL)


# ---------------------------------------------------------------- 图片抽取


@dataclass
class ImageHit:
    """一处远程图片引用。url_span 用于回写时精确定位。"""

    url: str
    start: int
    end: int
    source: str  # md / html / ref


def _mask_span(chars: list[str], start: int, end: int) -> None:
    for index in range(start, end):
        if chars[index] not in "\r\n":
            chars[index] = " "


def _mask_markdown_code(text: str) -> str:
    'Mask fenced/indented code, inline code spans, <pre>/<code> blocks and HTML comments.'
    chars = list(text)
    # Mask comments before looking for fences so copied fence markers inside
    # comments cannot accidentally hide the rest of the Markdown document.
    for comment in re.finditer(r"<!--.*?-->", text, re.DOTALL):
        _mask_span(chars, comment.start(), comment.end())
    # Raw HTML code blocks (<pre>) and inline code (<code>) are code too;
    # images inside them must not be downloaded or rewritten.
    for block in re.finditer(r"<(pre|code)\b[^>]*>.*?</\1\s*>", text, re.IGNORECASE | re.DOTALL):
        _mask_span(chars, block.start(), block.end())
    scan_source = "".join(chars)
    offset = 0
    fence_char = ""
    fence_size = 0
    for line in scan_source.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if fence_char:
            close = re.match(r"^[ ]{0,3}" + re.escape(fence_char) + r"{" + str(fence_size) + r",}[ \t]*$", body)
            _mask_span(chars, offset, offset + len(line))
            if close:
                fence_char = ""
                fence_size = 0
        else:
            opening = FENCE_OPEN.match(body)
            if opening and not (opening.group("fence").startswith("`") and "`" in body[opening.end():]):
                marker = opening.group("fence")
                fence_char, fence_size = marker[0], len(marker)
                _mask_span(chars, offset, offset + len(line))
            elif body.startswith("    ") or body.startswith("\t"):
                _mask_span(chars, offset, offset + len(line))
        offset += len(line)

    # Inline code spans use a matching backtick run of the same length.
    masked = "".join(chars)
    index = 0
    while index < len(masked):
        if masked[index] != "`":
            index += 1
            continue
        slash_count = 0
        probe = index - 1
        while probe >= 0 and text[probe] == "\\":
            slash_count += 1
            probe -= 1
        if slash_count % 2:
            index += 1
            continue
        run_end = index + 1
        while run_end < len(masked) and masked[run_end] == "`":
            run_end += 1
        run = masked[index:run_end]
        search_at = run_end
        close_start = -1
        while True:
            candidate = masked.find(run, search_at)
            if candidate < 0:
                break
            before_is_tick = candidate > 0 and masked[candidate - 1] == "`"
            after = candidate + len(run)
            after_is_tick = after < len(masked) and masked[after] == "`"
            if not before_is_tick and not after_is_tick:
                close_start = candidate
                break
            search_at = candidate + 1
        if close_start >= 0:
            _mask_span(chars, index, close_start + len(run))
            masked = "".join(chars)
            index = close_start + len(run)
        else:
            index = run_end
    return "".join(chars)


def _find_closing_bracket(source: str, start: int, opening: str = "[", closing: str = "]") -> int:
    depth = 0
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _markdown_unescape(value: str) -> str:
    return re.sub(r"\\([!\"#$%&'()*+,./:;<=>?@\[\]\\^_`{|}~-])", r"\1", value)


def _markdown_inline_image_hits(text: str, masked: str) -> list[ImageHit]:
    hits: list[ImageHit] = []
    for match in re.finditer(r"!\[", masked):
        # "\![a](b.png)" 是转义后的字面文本，不是图片。
        slashes = 0
        probe = match.start() - 1
        while probe >= 0 and masked[probe] == "\\":
            slashes += 1
            probe -= 1
        if slashes % 2:
            continue
        alt_start = match.end() - 1
        alt_end = _find_closing_bracket(masked, alt_start)
        if alt_end < 0:
            continue
        cursor = alt_end + 1
        while cursor < len(masked) and masked[cursor].isspace():
            cursor += 1
        if cursor >= len(masked) or masked[cursor] != "(":
            continue
        cursor += 1
        while cursor < len(masked) and masked[cursor].isspace():
            cursor += 1
        if cursor >= len(masked):
            continue

        angle_url = masked[cursor] == "<"
        if angle_url:
            url_start = cursor + 1
            url_end = url_start
            escaped = False
            while url_end < len(masked):
                char = masked[url_end]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == ">":
                    break
                url_end += 1
            if url_end >= len(masked):
                continue
        else:
            url_start = cursor
            url_end = cursor
            paren_depth = 0
            escaped = False
            while url_end < len(masked):
                char = masked[url_end]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "(":
                    paren_depth += 1
                elif char == ")":
                    if paren_depth == 0:
                        break
                    paren_depth -= 1
                elif char.isspace() and paren_depth == 0:
                    break
                url_end += 1
            if url_end == url_start:
                continue

        url = _markdown_unescape(text[url_start:url_end])
        tail = url_end + (1 if angle_url else 0)
        while tail < len(masked) and masked[tail].isspace():
            tail += 1
        if tail < len(masked) and masked[tail] in "\"'":
            quote = masked[tail]
            tail += 1
            while tail < len(masked) and masked[tail] != quote:
                tail += 1
            if tail < len(masked):
                tail += 1
            while tail < len(masked) and masked[tail].isspace():
                tail += 1
        elif tail < len(masked) and masked[tail] == "(":
            title_depth = 1
            tail += 1
            while tail < len(masked) and title_depth:
                if masked[tail] == "(":
                    title_depth += 1
                elif masked[tail] == ")":
                    title_depth -= 1
                tail += 1
            while tail < len(masked) and masked[tail].isspace():
                tail += 1
        if tail >= len(masked) or masked[tail] != ")":
            continue
        hits.append(ImageHit(url, url_start, url_end, "md"))
    return hits


def _normalize_ref_id(value: str) -> str:
    return " ".join(_markdown_unescape(value).split()).casefold()


def _html_img_src_span(tag: str) -> tuple[int, int] | None:
    """Return the value span of the real src attribute in an img tag."""
    index = 4  # immediately after '<img'
    length = len(tag)
    while index < length:
        while index < length and (tag[index].isspace() or tag[index] in "/>" ):
            index += 1
        if index >= length or tag[index] == ">":
            break
        name_start = index
        while index < length and not tag[index].isspace() and tag[index] not in "=/>":
            index += 1
        if index == name_start:
            index += 1
            continue
        attr_name = tag[name_start:index].casefold()
        while index < length and tag[index].isspace():
            index += 1
        if index >= length or tag[index] != "=":
            continue
        index += 1
        while index < length and tag[index].isspace():
            index += 1
        if index >= length:
            break
        if tag[index] in "\"'":
            quote = tag[index]
            value_start = index + 1
            value_end = tag.find(quote, value_start)
            if value_end < 0:
                break
            index = value_end + 1
        else:
            value_start = index
            while index < length and not tag[index].isspace() and tag[index] != ">":
                index += 1
            value_end = index
        if attr_name == "src":
            return value_start, value_end
    return None


def iter_hits(text: str) -> list[ImageHit]:
    masked = _mask_markdown_code(text)
    hits = _markdown_inline_image_hits(text, masked)

    # Match an actual src attribute, not the suffix of data-src / data-srcset.
    for tag in HTML_TAG.finditer(masked):
        span = _html_img_src_span(tag.group(0))
        if span is None:
            continue
        start = tag.start() + span[0]
        end = tag.start() + span[1]
        hits.append(ImageHit(text[start:end], start, end, "html"))

    definitions: dict[str, tuple[str, int, int]] = {}
    for match in REF_DEF.finditer(masked):
        ref_id = _normalize_ref_id(match.group("id"))
        group = "angle" if match.group("angle") is not None else "plain"
        start, end = match.span(group)
        definitions.setdefault(ref_id, (text[start:end], start, end))

    for match in MD_IMAGE_REF.finditer(masked):
        ref_id = _normalize_ref_id(match.group("id") or match.group("alt"))
        entry = definitions.get(ref_id)
        if entry:
            hits.append(ImageHit(entry[0], entry[1], entry[2], "ref"))

    # One definition URL span may be referenced many times; rewrite it once.
    unique: dict[tuple[int, int], ImageHit] = {}
    for hit in hits:
        unique.setdefault((hit.start, hit.end), hit)
    return sorted(unique.values(), key=lambda hit: (hit.start, hit.end))


def is_remote(url: str) -> bool:
    lowered = url.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://") \
        or lowered.startswith("//")


def normalize_url(url: str) -> str:
    url = url.strip().replace("&amp;", "&")
    if url.startswith("//"):
        url = "https:" + url
    return url


def _normalized_host(host: str) -> str:
    host = host.rstrip(".").lower()
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        try:
            return host.encode("idna").decode("ascii")
        except UnicodeError:
            return host


def origin_of(url: str) -> str:
    """Canonical origin: scheme + normalized host + non-default port."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    host = _normalized_host(parts.hostname or "")
    if not scheme or not host:
        raise ValueError("URL 缺少有效协议或主机名")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("URL 端口无效") from exc
    default_port = {"http": 80, "https": 443}.get(scheme)
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    host_text = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host_text}{port_suffix}"


# Common multi-label public suffixes. Without an external PSL dependency, use
# this conservative embedded subset and fall back to the last two labels.
_MULTI_LABEL_PUBLIC_SUFFIXES = {
    "ac.uk", "co.uk", "gov.uk", "ltd.uk", "me.uk", "net.uk", "org.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz",
    "com.br", "net.br", "org.br", "com.cn", "net.cn", "org.cn", "gov.cn",
    "com.sg", "net.sg", "org.sg", "gov.sg", "edu.sg",
    "com.hk", "net.hk", "org.hk", "gov.hk", "edu.hk",
    "com.tw", "net.tw", "org.tw", "gov.tw", "edu.tw",
    "co.in", "firm.in", "net.in", "org.in", "gen.in", "ind.in",
    "com.mx", "com.ar", "com.tr", "co.za", "org.za", "net.za",
    "co.kr", "or.kr", "ne.kr", "com.my", "com.ph", "com.vn", "com.pk",
    "co.id", "or.id", "web.id", "com.pl", "com.ua", "com.ru",
    "com.eg", "com.sa", "com.ae", "com.ng", "co.th", "or.th", "ac.th",
}


def _registrable_domain(host: str) -> str:
    normalized = _normalized_host(host)
    try:
        return ipaddress.ip_address(normalized).compressed
    except ValueError:
        pass
    labels = [label for label in normalized.split(".") if label]
    if len(labels) <= 1:
        return normalized
    suffix2 = ".".join(labels[-2:])
    if suffix2 in _MULTI_LABEL_PUBLIC_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return suffix2


def throttle_key(url: str, scope: str = "origin") -> str:
    """Return a canonical origin or registrable-site bucket key."""
    parts = urllib.parse.urlsplit(url)
    if scope == "site":
        return _registrable_domain(parts.hostname or "")
    return origin_of(url)


# ---------------------------------------------------------------- 同源限流


class HostThrottle:
    """按同源（scheme://host[:port]）控制拉取节奏。

    两道限制同时生效：
    1. 同源并发上限：每个源最多 max_concurrent 个请求同时在飞；
    2. 同源最小间隔：同一个源相邻两次请求的发起时间至少隔 interval 秒，
       再叠加一点随机抖动，避免整齐的脉冲式流量。

    不同源之间互不影响，所以多站点笔记库仍然可以并行拉取。
    """

    def __init__(self, max_concurrent: int = 3, interval: float = 0.4,
                 jitter: float = 0.15):
        self.max_concurrent = max(1, max_concurrent)
        self.interval = max(0.0, interval)
        self.jitter = max(0.0, jitter)
        self._guard = threading.Lock()
        self._sems: dict[str, threading.Semaphore] = {}
        self._next_start: dict[str, float] = {}
        self.stats: dict[str, int] = {}   # 源 -> 已发请求数
        self.waited = 0.0                  # 累计因限流而等待的秒数

    def _semaphore(self, origin: str) -> threading.Semaphore:
        with self._guard:
            sem = self._sems.get(origin)
            if sem is None:
                sem = threading.Semaphore(self.max_concurrent)
                self._sems[origin] = sem
                self._next_start[origin] = 0.0
            self.stats[origin] = self.stats.get(origin, 0) + 1
            return sem

    def slot(self, origin: str):
        """占一个同源请求位；退出上下文时归还。返回 context manager。"""
        return _HostSlot(self, origin)


class _HostSlot:
    def __init__(self, throttle: HostThrottle, origin: str):
        self.throttle = throttle
        self.origin = origin
        self._sem = None

    def __enter__(self):
        th = self.throttle
        self._sem = th._semaphore(self.origin)
        self._sem.acquire()
        try:
            with th._guard:
                start_at = max(time.monotonic(), th._next_start.get(self.origin, 0.0))
                gap = th.interval + random.uniform(0, th.jitter)
                th._next_start[self.origin] = start_at + gap
            delay = start_at - time.monotonic()
            if delay > 0:
                th.waited += delay
                time.sleep(delay)
        except BaseException:
            self._sem.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        self._sem.release()
        return False


# ---------------------------------------------------------------- 下载与命名


def _sanitize(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip(" .")
    return name or "image"


def guess_name(
    url: str, content_type: str, seq: int, detected_ext: str = ""
) -> str:
    path = urllib.parse.urlsplit(url).path
    raw = urllib.parse.unquote(os.path.basename(path))
    stem, ext = os.path.splitext(raw)
    ext = ext.lower()
    if detected_ext:
        ext = detected_ext
    elif ext not in IMAGE_EXTENSIONS:
        if ext:
            stem = raw  # e.g. a.php: retain the stem but use a detected image extension
        ext = CONTENT_TYPE_EXT.get((content_type or "").split(";", 1)[0].strip().lower(), "")
    stem = _sanitize(stem) or f"image_{seq:03d}"
    if len(stem) > 80:
        stem = stem[:64] + "-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return stem + ext


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, str(path))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    _atomic_write_bytes(path, content.encode(encoding))


def _manifest_path(assets_dir: Path) -> Path:
    return assets_dir / ".md_pull_images-manifest.json"


def _load_manifest(assets_dir: Path) -> dict[str, str]:
    path = _manifest_path(assets_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
        return {}
    result: dict[str, str] = {}
    for url, name in payload["files"].items():
        if not isinstance(url, str) or not isinstance(name, str):
            continue
        # Manifest values are basenames only; never trust a path from disk.
        if (name and Path(name).name == name and name not in (".", "..")
                and "/" not in name and "\\" not in name and "\x00" not in name):
            result[url] = name
    return result


def _save_manifest(assets_dir: Path, files: dict[str, str]) -> None:
    payload = {"version": 1, "files": dict(sorted(files.items()))}
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(_manifest_path(assets_dir), encoded)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.absolute())).casefold()


def _collision_name(name: str, url: str, assets_dir: Path,
                    owners: dict[str, str], used_names: dict[str, str]) -> Path:
    stem, ext = os.path.splitext(name)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    for size in (12, 16, 24, 32, 64):
        candidate = assets_dir / f"{stem}-{digest[:size]}{ext}"
        key = _path_key(candidate)
        owner = owners.get(candidate.name.casefold()) or used_names.get(key)
        if owner == url:
            return candidate
        if owner is None and not candidate.exists():
            return candidate
    suffix = 1
    while True:
        candidate = assets_dir / f"{stem}-{digest}-{suffix}{ext}"
        key = _path_key(candidate)
        if not candidate.exists() and key not in used_names and candidate.name.casefold() not in owners:
            return candidate
        suffix += 1


def _select_asset_target(
    assets_dir: Path,
    url: str,
    suggested_name: str,
    manifest: dict[str, str],
    owners: dict[str, str],
    used_names: dict[str, str],
    overwrite: bool,
) -> tuple[Path, bool]:
    recorded_name = manifest.get(url)
    candidate_name = recorded_name or suggested_name
    target = assets_dir / candidate_name
    key = _path_key(target)
    owner = owners.get(candidate_name.casefold()) or used_names.get(key)

    if owner is not None and owner != url:
        target = _collision_name(suggested_name, url, assets_dir, owners, used_names)
        return target, False

    if target.exists():
        is_recorded_for_url = recorded_name == candidate_name and owner == url
        if is_recorded_for_url:
            if not overwrite:
                return target, True
            return target, False
        if owner == url:
            if not overwrite:
                return target, True
            return target, False
        if overwrite and owner is None:
            # Explicit --overwrite preserves its legacy meaning for an
            # untracked same-name file, but never overwrites another URL.
            return target, False
        target = _collision_name(suggested_name, url, assets_dir, owners, used_names)
        return target, False

    return target, False


@dataclass
class FetchResult:
    url: str
    data: bytes | None = None
    content_type: str = ""
    error: str = ""
    image_ext: str = ""


class NonRetryableError(Exception):
    """重试不会有不同结果的失败：体积超限、内容不是图片、格式与 MIME 不符。

    429 / 5xx / 超时等仍按可重试处理。
    """


def _parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse Retry-After delta-seconds or HTTP-date; invalid values return None."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _validate_remote_url(url: str, allow_private_addresses: bool = False) -> None:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("仅允许 HTTP/HTTPS 图片 URL")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("URL 主机名无效或包含不允许的用户信息")
    try:
        port = parts.port
        if port is None:
            port = 443 if scheme == "https" else 80
    except ValueError as exc:
        raise ValueError("URL 端口无效") from exc
    if not 1 <= port <= 65535:
        raise ValueError("URL 端口超出有效范围")
    if allow_private_addresses:
        return

    host = parts.hostname
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"无法解析图片主机名 {host}: {exc}") from exc
        addresses = {ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in resolved}
    if not addresses:
        raise ValueError(f"图片主机名没有可用地址: {host}")
    non_global = [address for address in addresses if not address.is_global]
    if non_global:
        raise ValueError(
            "目标地址属于本机、私有或特殊用途网络；如确实需要访问内网图片，"
            "请显式使用 --allow-private-addresses"
        )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private_addresses: bool = False):
        super().__init__()
        self.allow_private_addresses = allow_private_addresses

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        _validate_remote_url(target, self.allow_private_addresses)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _detect_image_extension(data: bytes) -> str:
    if (len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n")
            and data[12:16] == b"IHDR"
            and int.from_bytes(data[16:20], "big") > 0
            and int.from_bytes(data[20:24], "big") > 0):
        return ".png"
    if len(data) >= 4 and data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(data) >= 13 and data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 16 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) >= 26 and data.startswith(b"BM"):
        return ".bmp"
    if len(data) >= 8 and data.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    if len(data) >= 6 and data.startswith(b"\x00\x00\x01\x00"):
        return ".ico"
    if len(data) >= 16 and data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return ".avif"
    sample = data[:8192].lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if re.search(rb"<svg(?:\s|>)", sample, re.IGNORECASE) and not re.search(
        rb"<(?:html|!doctype)\b", sample, re.IGNORECASE
    ):
        return ".svg"
    return ""


def _request(
    url: str,
    timeout: float,
    referer: str | None,
    max_bytes: int = 25 * 1024 * 1024,
    allow_private_addresses: bool = False,
) -> tuple[bytes, str, str]:
    _validate_remote_url(url, allow_private_addresses)
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    if referer:
        req.add_header("Referer", referer)
    else:
        parts = urllib.parse.urlsplit(url)
        req.add_header("Referer", f"{parts.scheme}://{parts.netloc}/")
    req.add_header("Accept", "image/avif,image/webp,image/apng,image/*,*/*;q=0.8")
    opener = urllib.request.build_opener(_SafeRedirectHandler(allow_private_addresses))
    with opener.open(req, timeout=timeout) as resp:
        content_type = resp.headers.get("Content-Type", "")
        declared_length = resp.headers.get("Content-Length")
        if declared_length:
            try:
                declared_size = int(declared_length)
            except (TypeError, ValueError):
                declared_size = None
            if declared_size is not None and declared_size > max_bytes:
                raise NonRetryableError(f"图片超过大小限制 ({max_bytes} bytes)")
        data = bytearray()
        while True:
            chunk = resp.read(min(64 * 1024, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                raise NonRetryableError(f"图片超过大小限制 ({max_bytes} bytes)")
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime and not mime.startswith("image/") and mime not in (
        "application/octet-stream", "binary/octet-stream"
    ):
        raise NonRetryableError(f"响应 Content-Type 不是图片类型: {mime}")
    image_ext = _detect_image_extension(bytes(data))
    if not image_ext:
        raise NonRetryableError(f"响应内容不是受支持的图片格式 (Content-Type: {content_type or 'unknown'})")
    expected_ext = CONTENT_TYPE_EXT.get(mime)
    compatible = {(".jpg", ".jpeg"), (".jpeg", ".jpg"), (".png", ".apng"), (".apng", ".png")}
    if mime.startswith("image/") and expected_ext and expected_ext != image_ext and (expected_ext, image_ext) not in compatible:
        raise NonRetryableError(
            f"图片 MIME 类型与文件内容不匹配 (Content-Type: {mime}, 实际格式: {image_ext})"
        )
    return bytes(data), content_type, image_ext


def fetch(
    url: str,
    timeout: float,
    retries: int,
    referer: str | None,
    throttle: "HostThrottle | None" = None,
    key: str | None = None,
    max_bytes: int = 25 * 1024 * 1024,
    allow_private_addresses: bool = False,
) -> FetchResult:
    if retries < 0:
        return FetchResult(url, error="ValueError: retries 必须大于等于 0")
    if not math.isfinite(timeout) or timeout <= 0:
        return FetchResult(url, error="ValueError: timeout 必须是有限正数")
    if max_bytes <= 0:
        return FetchResult(url, error="ValueError: max_bytes 必须大于 0")
    last_error = "未知下载错误"
    try:
        bucket = key or origin_of(url)
    except ValueError:
        bucket = key or url
    for attempt in range(retries + 1):
        try:
            slot = throttle.slot(bucket) if throttle is not None else nullcontext()
            with slot:
                data, ctype, image_ext = _request(
                    url, timeout, referer, max_bytes, allow_private_addresses
                )
            if not data:
                raise IOError("响应为空")
            return FetchResult(url, data, ctype, image_ext=image_ext)
        except Exception as exc:  # noqa: BLE001 - 网络异常种类很多，统一记录
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt >= retries or isinstance(exc, NonRetryableError):
                break
            if isinstance(exc, urllib.error.HTTPError):
                # 408/429 之外的其他 4xx 重试也不会变好，直接放弃。
                if 400 <= exc.code < 500 and exc.code not in (408, 429):
                    break
            delay = None
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (429, 503):
                delay = _parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers else None
                )
            if delay is None:
                delay = min(2 ** attempt, 5)
            time.sleep(min(delay, 30))
    return FetchResult(url, error=last_error)


# ---------------------------------------------------------------- 单个文件处理


@dataclass
class FileReport:
    path: Path
    assets_dir: Path
    total: int = 0
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    rewritten: bool = False


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _markdown_path(path: str) -> str:
    """把本地路径写成 Markdown 链接目标。

    空格、`#`、`%`、尖括号会被 Markdown/URL 解析截断或当成片段，需要百分号
    编码；括号成对时按原样写，不成对才编码，避免链接在第一个 `)` 处提前结束。
    """
    path = (path.replace("%", "%25").replace(" ", "%20").replace("#", "%23")
            .replace("<", "%3C").replace(">", "%3E"))
    if path.count("(") != path.count(")"):
        path = path.replace("(", "%28").replace(")", "%29")
    return path


def _relative_asset_dir(assets_dir: Path, md_parent: Path) -> Path | None:
    """相对链接的基准目录；Windows 上跨盘符无法表示相对路径，返回 None。"""
    try:
        return Path(os.path.relpath(assets_dir, md_parent))
    except ValueError:
        return None


def process_file(
    md_path: Path,
    assets_dir: Path,
    args: argparse.Namespace,
    used_names: dict[str, str],
    throttle: "HostThrottle | None" = None,
) -> FileReport:
    text = read_text(md_path)
    hits = iter_hits(text)
    report = FileReport(md_path, assets_dir)

    unique: dict[str, list[ImageHit]] = {}
    for hit in hits:
        if not is_remote(hit.url):
            continue
        url = normalize_url(hit.url)
        unique.setdefault(url, []).append(hit)
    report.total = len(unique)

    if not unique:
        return report

    remote_urls = list(unique)
    results: dict[str, FetchResult] = {}
    keys: dict[str, str] = {}
    for url in remote_urls:
        try:
            keys[url] = throttle_key(url, args.throttle_scope)
        except ValueError:
            # Keep malformed URLs isolated; fetch() will report the URL error.
            keys[url] = url
    if args.jobs > 1 and len(remote_urls) > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {
                pool.submit(fetch, u, args.timeout, args.retries,
                            args.referer, throttle, keys[u], args.max_image_size,
                            args.allow_private_addresses): u
                for u in remote_urls
            }
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    results[url] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    results[url] = FetchResult(url, error=str(exc))
    else:
        for url in remote_urls:
            results[url] = fetch(
                url, args.timeout, args.retries, args.referer, throttle, keys[url],
                args.max_image_size, args.allow_private_addresses,
            )

    mapping: dict[str, str] = {}
    assets_dir.mkdir(parents=True, exist_ok=True)
    original_manifest = _load_manifest(assets_dir)
    manifest = dict(original_manifest)
    owners = {name.casefold(): url for url, name in manifest.items()}
    pending_writes: list[tuple[str, FetchResult, Path]] = []

    for seq, url in enumerate(remote_urls, start=1):
        res = results[url]
        if res.error:
            report.failed.append((url, res.error))
            continue
        name = guess_name(url, res.content_type, seq, res.image_ext)
        target, should_skip = _select_asset_target(
            assets_dir, url, name, manifest, owners, used_names, args.overwrite
        )
        if should_skip:
            report.skipped.append(url)
            mapping[url] = target.name
            used_names[_path_key(target)] = url
            continue
        # Reserve the planned target in memory to keep different URLs from
        # colliding in this batch. The manifest is persisted only after the
        # atomic asset write succeeds, so a failed --overwrite cannot claim
        # that an old, unverified file belongs to the new URL.
        owners[target.name.casefold()] = url
        used_names[_path_key(target)] = url
        pending_writes.append((url, res, target))

    successfully_written: list[tuple[str, Path]] = []
    for url, res, target in pending_writes:
        try:
            _atomic_write_bytes(target, res.data or b"")
        except OSError as exc:
            report.failed.append((url, f"资产写入失败: {type(exc).__name__}: {exc}"))
            continue
        manifest[url] = target.name
        mapping[url] = target.name
        report.downloaded.append(target.name)
        successfully_written.append((url, target))
        if not args.quiet:
            print(f"  [ok] {target.name}  <-  {url}")

    if manifest != original_manifest:
        try:
            _save_manifest(assets_dir, manifest)
        except OSError as exc:
            for url, target in successfully_written:
                mapping.pop(url, None)
                if target.name in report.downloaded:
                    report.downloaded.remove(target.name)
                report.failed.append((url, f"资产清单写入失败: {type(exc).__name__}: {exc}"))

    # 回写 Markdown
    if args.rewrite and mapping:
        rel = _relative_asset_dir(assets_dir, md_path.parent)
        if rel is None:
            report.failed.append(
                (str(md_path), "图片目录与 Markdown 不在同一盘符，无法生成相对链接，已跳过回写")
            )
            return report
        # 同一 (start, end) 只能对应一个替换文本；出现分歧说明解析出的区间
        # 互相覆盖，宁可整篇不回写，也不要把两份内容拼到同一段文本上。
        spans_by_range: dict[tuple[int, int], str] = {}
        conflict = ""
        for url, hits_for_url in unique.items():
            local = mapping.get(url)
            if not local:
                continue
            new_text = _markdown_path((rel / local).as_posix())
            for hit in hits_for_url:
                span = (hit.start, hit.end)
                if spans_by_range.setdefault(span, new_text) != new_text:
                    conflict = f"位置 {hit.start}-{hit.end} 被映射到多个本地路径"
                    break
            if conflict:
                break
        if conflict:
            report.failed.append((str(md_path), f"Markdown 回写区间冲突（{conflict}），已跳过回写"))
        else:
            spans = sorted((start, end, replacement)
                           for (start, end), replacement in spans_by_range.items())
            out, cursor, overlap = [], 0, False
            for start, end, new_text in spans:
                if start < cursor:
                    overlap = True
                    break
                out.append(text[cursor:start])
                out.append(new_text)
                cursor = end
            if overlap:
                report.failed.append((str(md_path), "Markdown 回写区间重叠，已跳过回写"))
            else:
                out.append(text[cursor:])
                new_content = "".join(out)
                if new_content != text:
                    try:
                        if args.backup:
                            bak = md_path.with_suffix(md_path.suffix + ".bak")
                            if not bak.exists():
                                _atomic_write_bytes(bak, md_path.read_bytes())
                        _atomic_write_text(md_path, new_content, encoding="utf-8")
                    except OSError as exc:
                        report.failed.append(
                            (str(md_path), f"Markdown 回写失败: {type(exc).__name__}: {exc}")
                        )
                    else:
                        report.rewritten = True
    return report


# ---------------------------------------------------------------- CLI


def collect_markdown(target: Path, recursive: bool) -> list[Path]:
    if target.is_file():
        return [target]
    pattern = "**/*" if recursive else "*"
    return sorted(
        p for p in target.glob(pattern)
        if p.is_file() and p.suffix.lower() in MD_SUFFIXES
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="md_pull_images",
        description="把 Markdown 里的远程图片下载到本地，可选回写链接。",
    )
    parser.add_argument("target", type=Path, help="Markdown 文件或目录")
    parser.add_argument("-o", "--assets-dir", default=None,
                        help="图片存放目录；默认 <md文件名>.assets/")
    parser.add_argument("--rewrite", action="store_true",
                        help="把 md 中的远程链接替换成本地相对路径")
    parser.add_argument("--backup", action="store_true",
                        help="回写前生成 .bak 备份")
    parser.add_argument("--overwrite", action="store_true",
                        help="重新下载覆盖同一 URL 已登记文件；不同 URL 不互相覆盖")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="目录模式下递归子目录（默认递归）")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false",
                        help="目录模式只处理当前层")
    parser.add_argument("-j", "--jobs", type=int, default=8, help="并发下载数，默认 8")
    parser.add_argument("--timeout", type=float, default=20.0, help="单请求超时秒数，默认 20")
    parser.add_argument("--retries", type=int, default=2, help="失败重试次数，默认 2")
    parser.add_argument("--max-image-size", type=int, default=25 * 1024 * 1024,
                        help="单张图片最大字节数，默认 25 MiB")
    parser.add_argument("--allow-private-addresses", action="store_true",
                        help="允许访问本机/私有网络图片；默认阻止以降低 SSRF 风险")
    parser.add_argument("--referer", default=None,
                        help="自定义 Referer；默认使用图片域名首页，用于绕过防盗链")
    parser.add_argument("--throttle-threshold", type=int, default=20,
                        help="远程图片总数超过该值时启用同源限流，默认 20；0 表示始终启用")
    parser.add_argument("--throttle-scope", choices=("origin", "site"), default="origin",
                        help="限流分桶粒度：origin=规范化同源（默认），site=按可注册域名归并子域")
    parser.add_argument("--per-host-jobs", type=int, default=3,
                        help="同源并发上限，默认 3")
    parser.add_argument("--per-host-interval", type=float, default=0.4,
                        help="同源相邻请求最小间隔秒，默认 0.4")
    parser.add_argument("--no-throttle", action="store_true",
                        help="关闭同源限流（小批量或自建图床时用）")
    parser.add_argument("--dry-run", action="store_true", help="只列出待下载的图片，不落盘")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出汇总")
    parser.set_defaults(recursive=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.jobs < 1:
        parser.error("--jobs 必须大于等于 1")
    if args.retries < 0:
        parser.error("--retries 必须大于等于 0")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是有限正数")
    if args.throttle_threshold < 0:
        parser.error("--throttle-threshold 必须大于等于 0")
    if args.per_host_jobs < 1:
        parser.error("--per-host-jobs 必须大于等于 1")
    if not math.isfinite(args.per_host_interval) or args.per_host_interval < 0:
        parser.error("--per-host-interval 必须是有限非负数")
    if args.max_image_size < 1:
        parser.error("--max-image-size 必须大于 0")

    target: Path = args.target
    if not target.exists():
        print(f"路径不存在: {target}", file=sys.stderr)
        return 2

    files = collect_markdown(target, args.recursive)
    if not files:
        print(f"没找到 Markdown 文件: {target}", file=sys.stderr)
        return 2

    # 先摸清全局待拉取图片数量，决定是否启用同源限流
    all_urls: set[str] = set()
    per_file: dict[Path, list[str]] = {}
    for md in files:
        urls = sorted(
            {normalize_url(h.url) for h in iter_hits(read_text(md)) if is_remote(h.url)}
        )
        per_file[md] = urls
        all_urls.update(urls)
    throttle_needed = (
        not args.no_throttle
        and (len(all_urls) > args.throttle_threshold or args.throttle_threshold <= 0)
    )

    if args.dry_run:
        for md in files:
            print(f"{md}: {len(per_file[md])} 张远程图片")
            for url in per_file[md]:
                print(f"  - {url}")
        print(f"\n共 {len(files)} 个文件 / {len(all_urls)} 张唯一远程图片（dry-run，未下载）")
        if throttle_needed:
            print(f"预计启用同源限流：每源并发 ≤ {args.per_host_jobs}，"
                  f"相邻间隔 ≥ {args.per_host_interval}s")
        return 0

    throttle = None
    if throttle_needed:
        throttle = HostThrottle(args.per_host_jobs, args.per_host_interval)
        if not args.quiet:
            scope_text = "同源" if args.throttle_scope == "origin" else "同站"
            print(f"远程图片 {len(all_urls)} 张，超过阈值 {args.throttle_threshold}，"
                  f"启用{scope_text}限流：每桶并发 ≤ {args.per_host_jobs}，"
                  f"同桶间隔 ≥ {args.per_host_interval}s（含抖动）")

    is_single = target.is_file()
    reports: list[FileReport] = []
    used_names: dict[str, str] = {}  # 文件命名统一在主线程做，worker 只跑网络

    for md in files:
        if is_single:
            assets_dir = Path(args.assets_dir) if args.assets_dir else md.parent / f"{md.stem}.assets"
        else:
            assets_dir = (Path(args.assets_dir) / md.stem) if args.assets_dir \
                else md.parent / f"{md.stem}.assets"
        if not args.quiet:
            print(f"\n>> {md}  (assets -> {assets_dir})")
        try:
            reports.append(process_file(md, assets_dir, args, used_names, throttle))
        except Exception as exc:  # noqa: BLE001 - 单个文件出错不应中断整批
            failed = FileReport(md, assets_dir)
            failed.failed.append((str(md), f"处理失败: {type(exc).__name__}: {exc}"))
            reports.append(failed)
            print(f"  [fail] {md}: {type(exc).__name__}: {exc}", file=sys.stderr)

    # 汇总
    ok = sum(len(r.downloaded) for r in reports)
    kept = sum(len(r.skipped) for r in reports)
    bad = sum(len(r.failed) for r in reports)
    print("\n===== 汇总 =====")
    for r in reports:
        line = (f"{r.path.name}: 远程 {r.total}，下载 {len(r.downloaded)}，"
                f"已存在 {len(r.skipped)}，失败 {len(r.failed)}")
        if r.rewritten:
            line += "，已回写链接"
        print(line)
        for url, err in r.failed:
            print(f"  [fail] {url}  ({err})")
    print(f"合计：下载 {ok} / 已存在 {kept} / 失败 {bad}")
    if throttle is not None and throttle.stats:
        print(f"限流：{len(throttle.stats)} 个桶，等待合计 {throttle.waited:.1f}s"
              f"（并发场景按各请求累加，非实际墙钟）")
        for bucket, count in sorted(throttle.stats.items(), key=lambda kv: -kv[1]):
            print(f"  {bucket}: {count} 次请求")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
