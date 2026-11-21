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
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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

MD_IMAGE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<url>[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)"
)
HTML_IMG = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*[\"'](?P<url>[^\"']+)[\"']", re.IGNORECASE
)
MD_IMAGE_REF = re.compile(r"!\[(?P<alt>[^\]]*)\]\[(?P<id>[^\]]*)\]")
REF_DEF = re.compile(
    r"^[ \t]{0,3}\[(?P<id>[^\]\n]+)\]:[ \t]*(?P<url>\S+)", re.MULTILINE
)


# ---------------------------------------------------------------- 图片抽取


@dataclass
class ImageHit:
    """一处远程图片引用。url_span 用于回写时精确定位。"""

    url: str
    start: int
    end: int
    source: str  # md / html / ref


def iter_hits(text: str) -> list[ImageHit]:
    hits: list[ImageHit] = []
    for m in MD_IMAGE.finditer(text):
        hits.append(ImageHit(m.group("url"), m.start("url"), m.end("url"), "md"))
    for m in HTML_IMG.finditer(text):
        hits.append(ImageHit(m.group("url"), m.start("url"), m.end("url"), "html"))

    defs = {}
    for m in REF_DEF.finditer(text):
        defs.setdefault(m.group("id").strip().lower(), (m.group("url"),
                                                        m.start("url"), m.end("url")))
    for m in MD_IMAGE_REF.finditer(text):
        ref_id = (m.group("id") or m.group("alt")).strip().lower()
        entry = defs.get(ref_id)
        if entry:
            hits.append(ImageHit(entry[0], entry[1], entry[2], "ref"))
    return hits


def is_remote(url: str) -> bool:
    lowered = url.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://") \
        or lowered.startswith("//")


def normalize_url(url: str) -> str:
    url = url.strip().replace("&amp;", "&")
    if url.startswith("//"):
        url = "https:" + url
    return url


# ---------------------------------------------------------------- 下载与命名


def _sanitize(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip(" .")
    return name or "image"


def guess_name(url: str, content_type: str, seq: int) -> str:
    path = urllib.parse.urlsplit(url).path
    raw = urllib.parse.unquote(os.path.basename(path))
    stem, ext = os.path.splitext(raw)
    ext = ext.lower()
    if ext not in IMAGE_EXTENSIONS:
        if ext:
            stem = raw  # 例如 a.php，整个当名字，不给错误扩展名
        ext = CONTENT_TYPE_EXT.get((content_type or "").split(";")[0].strip().lower(), "")
    stem = _sanitize(stem) or f"image_{seq:03d}"
    if len(stem) > 80:
        stem = stem[:64] + "-" + hashlib.sha1(url.encode()).hexdigest()[:8]
    return stem + ext


@dataclass
class FetchResult:
    url: str
    data: bytes | None = None
    content_type: str = ""
    error: str = ""


def fetch(url: str, timeout: float, retries: int, referer: str | None) -> FetchResult:
    last_error = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
            if referer:
                req.add_header("Referer", referer)
            else:
                parts = urllib.parse.urlsplit(url)
                req.add_header("Referer", f"{parts.scheme}://{parts.netloc}/")
            req.add_header("Accept", "image/avif,image/webp,image/apng,image/*,*/*;q=0.8")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "")
            if not data:
                raise IOError("响应为空")
            return FetchResult(url, data, ctype)
        except Exception as exc:  # noqa: BLE001 - 网络异常种类很多，统一记录
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(min(2 ** attempt, 5))
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


def process_file(
    md_path: Path,
    assets_dir: Path,
    args: argparse.Namespace,
    used_names: dict[str, str],
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
    if args.jobs > 1 and len(remote_urls) > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {
                pool.submit(fetch, u, args.timeout, args.retries, args.referer): u
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
            results[url] = fetch(url, args.timeout, args.retries, args.referer)

    mapping: dict[str, str] = {}
    assets_dir.mkdir(parents=True, exist_ok=True)

    for seq, url in enumerate(remote_urls, start=1):
        res = results[url]
        if res.error:
            report.failed.append((url, res.error))
            continue
        name = guess_name(url, res.content_type, seq)
        target = assets_dir / name
        if target.exists() and not args.overwrite:
            existing_key = used_names.get(str(target).lower())
            if existing_key is None or existing_key == url:
                report.skipped.append(url)
                mapping[url] = target.name
                used_names[str(target).lower()] = url
                continue
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
            stem, ext = os.path.splitext(name)
            target = assets_dir / f"{stem}-{digest}{ext}"
        used_names[str(target).lower()] = url
        target.write_bytes(res.data or b"")
        report.downloaded.append(target.name)
        mapping[url] = target.name
        if not args.quiet:
            print(f"  [ok] {target.name}  <-  {url}")

    # 回写 Markdown
    if args.rewrite and mapping:
        rel = Path(os.path.relpath(assets_dir, md_path.parent))
        spans = []
        for url, hits_for_url in unique.items():
            local = mapping.get(url)
            if not local:
                continue
            new_text = (rel / local).as_posix()
            for hit in hits_for_url:
                spans.append((hit.start, hit.end, new_text))
        spans.sort()
        out, cursor = [], 0
        for start, end, new_text in spans:
            out.append(text[cursor:start])
            out.append(new_text)
            cursor = end
        out.append(text[cursor:])
        new_content = "".join(out)
        if new_content != text:
            if args.backup:
                bak = md_path.with_suffix(md_path.suffix + ".bak")
                if not bak.exists():
                    bak.write_bytes(md_path.read_bytes())
            md_path.write_text(new_content, encoding="utf-8", newline="")
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
                        help="已存在的同名图片重新下载覆盖")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="目录模式下递归子目录（默认递归）")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false",
                        help="目录模式只处理当前层")
    parser.add_argument("-j", "--jobs", type=int, default=8, help="并发下载数，默认 8")
    parser.add_argument("--timeout", type=float, default=20.0, help="单请求超时秒数，默认 20")
    parser.add_argument("--retries", type=int, default=2, help="失败重试次数，默认 2")
    parser.add_argument("--referer", default=None,
                        help="自定义 Referer；默认使用图片域名首页，用于绕过防盗链")
    parser.add_argument("--dry-run", action="store_true", help="只列出待下载的图片，不落盘")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出汇总")
    parser.set_defaults(recursive=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    target: Path = args.target
    if not target.exists():
        print(f"路径不存在: {target}", file=sys.stderr)
        return 2

    files = collect_markdown(target, args.recursive)
    if not files:
        print(f"没找到 Markdown 文件: {target}", file=sys.stderr)
        return 2

    if args.dry_run:
        total = 0
        for md in files:
            hits = [normalize_url(h.url) for h in iter_hits(read_text(md)) if is_remote(h.url)]
            uniq = sorted(set(hits))
            total += len(uniq)
            print(f"{md}: {len(uniq)} 张远程图片")
            for url in uniq:
                print(f"  - {url}")
        print(f"\n共 {len(files)} 个文件 / {total} 张远程图片（dry-run，未下载）")
        return 0

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
        reports.append(process_file(md, assets_dir, args, used_names))

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
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
