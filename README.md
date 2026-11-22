# kb-tools

知识库相关的小工具集合：把散落在各处的 Markdown / 笔记 / 文档整理成能长期自持的本地知识库。

纯 Python 标准库实现，无第三方依赖，Python 3.8+。

![CI](https://github.com/libai4hao/kb-tools/actions/workflows/ci.yml/badge.svg)

## 工具列表

| 工具 | 作用 |
| --- | --- |
| [`md_pull_images.py`](#md_pull_imagespy) | 把 Markdown 里的远程图片拉到本地，可选回写成本地相对链接 |

---

## md_pull_images.py

### 解决什么问题

从网页、公众号、博客「另存为 Markdown」得到的笔记，图片往往还在原站图床上：断网打不开、图床挂掉图就没了、原站防盗链还会让图变裂图。这个脚本把 Markdown（或整个目录）里的远程图片全部下载到本地，并把正文里的链接改成相对路径，之后这份笔记就完全自持了。

### 识别范围

- `![alt](https://.../a.png)`
- `![alt](https://.../a.png "title")`
- `<img src="https://.../b.gif" width="120" />`
- 引用式：`![alt][ref]` + `[ref]: https://.../c.jpg`（也支持 `![ref][]` 折写）

解析时会忽略围栏代码块、缩进代码、行内代码、`<pre>`/`<code>` 块和 HTML 注释中的示例；Markdown 图片 URL 支持平衡括号，HTML 图片只读取真正的 `src` 属性（不会把 `data-src` 当成 `src`）。只下载 `http(s)` 与 `//` 协议开头的图片；已经是本地相对路径、绝对路径、`data:` URI 的图片会原样跳过。

### 用法

```bash
# 1) 单个文件：只下载，不改原文件
python md_pull_images.py 笔记/排序算法.md

# 2) 单个文件：下载 + 回写成本地相对链接（建议先加 --backup）
python md_pull_images.py 笔记/排序算法.md --rewrite --backup

# 3) 整个目录：递归处理所有 .md，每个文件独立 assets 目录
python md_pull_images.py ./笔记库 --rewrite

# 4) 只看要下什么，不落盘
python md_pull_images.py ./笔记库 --dry-run

# 5) 图已下好、只差回写（跨盘或只读环境补救），读 manifest 不联网
python md_pull_images.py ./笔记库 --rewrite-only --backup

# 6) 顺手清理 manifest 里的陈旧记录（只删记录，不删图片）
python md_pull_images.py ./笔记库 --rewrite --prune-manifest 90
```

默认图片目录是 Markdown 同级的 `<文件名>.assets/`，回写后的链接形如 `排序算法.assets/xxx.png`，所以把 md 和 assets 目录一起搬走即可。图片目录中会额外维护隐藏文件 `.md_pull_images-manifest.json`，记录远程 URL 与本地文件名的对应关系，以及每个 URL 最近一次命中的时间；只有 manifest 确认属于同一 URL 的文件才会被复用，避免把旧的同名图片误认为新图片。建议把该 manifest 与图片一起保留。签名图床会让同一张图每次的 URL 都不同，条目会持续增长，可用 `--prune-manifest DAYS` 清掉文件已缺失或长期没再命中的记录（`0` 只清文件缺失项）。

### 参数

| 参数 | 说明 |
| --- | --- |
| `target` | Markdown 文件或目录 |
| `-o, --assets-dir` | 图片目录。单文件时为该目录；目录模式时为 `<assets-dir>/<md 文件名（不含扩展名）>/` |
| `--rewrite` | 把 md 里的远程链接替换成本地相对路径（默认只下载不改文件） |
| `--backup` | 回写前生成 `.md.bak` |
| `--overwrite` | 重新下载并覆盖同一 URL 在 manifest 中登记的文件；不同 URL 不会互相覆盖。对没有来源记录的旧同名文件，显式指定时仍允许覆盖 |
| `-r, --recursive` / `--no-recursive` | 目录模式是否递归，默认递归 |
| `-j, --jobs` | 并发下载数，默认 8 |
| `--timeout` | 单请求超时秒数，默认 20 |
| `--retries` | 失败重试次数，默认 2（指数退避） |
| `--max-image-size` | 单张图片最大字节数，默认 25 MiB；先检查 Content-Length，再按流式读取限制实际大小 |
| `--allow-private-addresses` | 允许访问本机、私有和特殊用途地址；默认拒绝，以降低不可信 Markdown 引发 SSRF 的风险 |
| `--referer` | 自定义 Referer；不填则用图片域名首页，用于绕过常见防盗链 |
| `--throttle-threshold` | 远程图片总数超过该值时启用限流，默认 `20`；`0` 表示始终启用 |
| `--throttle-scope` | 限流分桶粒度：`origin`（默认，规范化协议/主机/端口）/ `site`（按可注册域名合并同站子域） |
| `--public-suffix-file PATH` | 追加公共后缀表（每行一个后缀，`//` 之后为注释），让 `site` 分桶认识内置表没有的特殊后缀；内置表仍生效 |
| `--per-host-jobs` | 同桶并发上限，默认 `3` |
| `--per-host-interval` | 同桶相邻请求最小间隔秒，默认 `0.4` |
| `--no-throttle` | 关闭限流（小批量、自建图床时用） |
| `--dry-run` | 只列出待下载图片，标注「将下载 / 已存在」各多少张，并提示是否将启用限流 |
| `--rewrite-only` | 只按 manifest 里已有的登记回写本地链接，不联网；用于跨盘、只读等回写失败后的补救 |
| `--prune-manifest DAYS` | 顺带清理 manifest：移除文件已缺失、或超过 `DAYS` 天没被命中的记录；`0` 表示只清理文件已缺失的记录。只删记录，不删图片文件 |
| `-q, --quiet` | 只输出汇总 |

退出码：全部成功 `0`，有失败 `1`，参数或路径问题 `2`。

### 量大时自动限流

一次拉几十上百张图，很容易触发图床的速率限制（返回 429 / 直接掐连接）。所以脚本会在**拉取总量超过阈值时**自动开启同桶限流，两道闸同时生效：

1. **同桶并发上限**：同一个桶里最多 `--per-host-jobs` 个请求同时在飞，其余排队；
2. **同桶最小间隔**：同一个桶相邻两次请求的发起时间至少隔 `--per-host-interval` 秒，并叠一点随机抖动，避免整齐的脉冲流量。

桶按什么划分由 `--throttle-scope` 决定：

- `origin`（默认）：协议 + 规范化主机 + 非默认端口。同一个 HTTPS 源的 `:443` 与省略端口写法会合并；多站点笔记库可以继续跨源并行；
- `site`：按可注册域名归并子域，`img1.example.com`、`img2.example.com` 算一家；常见多级公共后缀（如 `co.uk`、`com.au`、`com.cn`）也会被单独识别。为坚持纯标准库实现，内置的是常见后缀子集，不是完整 Public Suffix List；不在内置表中的特殊后缀会回退到最后两段域名，需将其视为启发式分桶。

阈值判定用的是**全局去重后的远程图片数**，不是单个文件的图片数——目录模式下一次处理 300 篇笔记也会按 300 篇的总量决定是否限流。

遇到 429 / 503 时，脚本支持 `Retry-After` 的秒数和 HTTP-date 两种格式；无效或缺失时退回指数退避。下载会流式执行并验证图片签名和 MIME 一致性，超过 `--max-image-size` 会失败，不会把 HTML 错误页保存成图片。默认会在请求前解析 DNS，并拒绝本机、私有及特殊用途地址；重定向目标也会重新检查。若图片确实托管在内网（例如本机测试图床），需要显式使用 `--allow-private-addresses`。

```bash
# 大库批量：同站合并、每站并发 2、间隔 0.6s
python md_pull_images.py ./笔记库 --rewrite --throttle-scope site \
    --per-host-jobs 2 --per-host-interval 0.6

# 自建图床、确认无限流：直接关掉
python md_pull_images.py ./笔记库 --rewrite --no-throttle
```

运行结束会打印限流统计，方便判断要不要再调大间隔：

```
限流：1 个桶，等待合计 15.6s（并发场景按各请求累加，非实际墙钟）
  cnblogs.com: 12 次请求
```

### 案例：一篇来自博客的排序算法笔记

输入是一篇从 CNBlogs 保存的 `排序算法.md`，正文 12 张图片全部指向 `img2018.cnblogs.com` / `images2017.cnblogs.com` 图床：

```bash
python md_pull_images.py 排序算法.md --rewrite
```

输出：

```
>> 排序算法.md  (assets -> 排序算法.assets)
  [ok] 849589-20190306165258970-1789860540.png  <-  https://img2018.cnblogs.com/...
  [ok] 849589-20180402133438219-1946132192.png  <-  https://images2018.cnblogs.com/...
  [ok] 849589-20171015223238449-2146169197.gif  <-  https://images2017.cnblogs.com/...
  ...
===== 汇总 =====
排序算法.md: 远程 12，下载 12，已存在 0，失败 0，已回写链接
合计：下载 12 / 已存在 0 / 失败 0
```

- 12 张图（含 9 张排序过程动图）约 6 MB 全部落地本机；
- 正文里 0 条远程图片链接残留，全部变为 `排序算法.assets/xxx.gif`；
- 再跑一次会输出「已存在 12」，不会重复下载。

12 张未到默认阈值 20，所以默认行为是不限流、直接拉完。想把这份笔记当成「大库批量」压测时，可以强制开限流：

```bash
python md_pull_images.py 排序算法.md --rewrite \
    --throttle-threshold 5 --throttle-scope site \
    --per-host-jobs 3 --per-host-interval 0.5
```

输出关键行如下——三个 cnblogs 子域被 `site` 合并成 1 个桶，12 个请求按 ≤3 并发、≥0.5s 间隔排开，约 6 秒收工：

```
远程图片 12 张，超过阈值 5，启用同站限流：每桶并发 ≤ 3，同桶间隔 ≥ 0.5s（含抖动）
===== 汇总 =====
排序算法.md: 远程 12，下载 12，已存在 0，失败 0，已回写链接
合计：下载 12 / 已存在 0 / 失败 0
限流：1 个桶，等待合计 15.6s（并发场景按各请求累加，非实际墙钟）
  cnblogs.com: 12 次请求
```

### 已知边界

- 只处理 Markdown 文本里的图片引用，不解析 CSS 背景图、`<video>`、iframe 内嵌内容；
- 写作时请勿并发改同一个 md，`--rewrite` 是整文件重写；
- 少数站点会校验 Referer + Cookie 双重防盗链，这种只能 `--referer` 加上自行处理 Cookie；
- 限流计数在单进程内生效，同时开多个进程跑同一批站点，各自的节奏是独立的；
- `site` 使用内置常见公共后缀子集，不等同于完整 PSL；特殊国家/地区后缀可以补进代码里的 `_MULTI_LABEL_PUBLIC_SUFFIXES`，或用 `--public-suffix-file` 在运行期追加（内置表仍生效，避免 `co.uk` 这类常见后缀失效）；
- 默认私网地址拦截会影响本机/内网图床，确认来源可信后使用 `--allow-private-addresses`；DNS 预检与实际连接之间仍存在标准库 URL 客户端难以完全消除的 DNS 重绑定时间窗口，不应把它当作隔离不可信输入的唯一安全边界；
- 内容校验基于图片文件签名与 MIME 检查，不执行完整的图像解码；当前识别 PNG/APNG/JPEG/GIF/WebP/BMP/TIFF/ICO/AVIF/SVG，HEIC、JXL 等格式会被判为不支持而失败；
- 为避免把代码示例当图片改写，围栏代码、缩进代码、行内代码、`<pre>`/`<code>` 和 HTML 注释会被整体屏蔽；代价是列表项内以 4 空格缩进到「缩进代码」位置的图片也会被跳过，这类笔记请减少缩进或改用围栏外的普通段落；
- 图片文件名含空格、`#`、`%` 时，回写会做百分号编码（`![a](assets/a%20b.png)`），否则 Markdown 会把空格当标题分隔、把 `#` 当片段；括号成对时保持原样，不成对才编码；
- `![a] (url)`（标签和括号之间有空白的写法）按 CommonMark 算普通文本，不会被下载或改写；要当图片处理请写成 `![a](url)`；
- `<assets-dir>` 与 Markdown 不在同一盘符（Windows）时无法生成相对链接：图片仍会落盘，该文件的回写会被跳过并计为失败，程序继续处理其余文件；
- 单个文件出错（只读、无写权限、路径异常）只记录失败，不会中断整批任务；
- 重复运行不会重新下载：manifest 已登记且文件仍在时直接复用记录、不发请求；`--overwrite` 才会强制重拉。本地文件被删除后，下一次运行会自动补下；
- 回写前会比对读取时的 `mtime` 与大小：运行期间文件被编辑器或同步盘改过，就跳过该文件并把「文件在运行期间被修改」记入失败，不会覆盖外部改动；
- 每张图下载完立即写入资产目录的临时文件（`.mdpull-*.part`），命名确定后再原子改名成最终文件；内存峰值只与 `--jobs × --max-image-size` 有关（默认 8 × 25 MiB 封顶），不再随图片总量线性增长。进程被强杀时可能残留 `.mdpull-*.part`，确认没有任务在跑后可直接删除，正常运行（含失败、跳过）都会清理；
- 读取时按 UTF-8（含 BOM）、GB18030 顺序尝试解码，`--rewrite` 统一按 UTF-8 写回：GB18030 的旧笔记会被转码成 UTF-8，带 BOM 的文件会去掉 BOM；若需要保持原编码，请先自行转码再运行；
- 输出编码：写到管道/重定向时统一用 UTF-8，交互终端保留系统编码、只把无法编码的字符替换掉，避免英文 Windows（cp1252）打印中文路径或提示时抛 `UnicodeEncodeError` 中断任务；
- `--rewrite-only` 只认 manifest 里已登记且文件仍存在的 URL：命中的回写、其余计失败；它不会补下载，也不会重建缺失的图片；
- `--prune-manifest` 只删 manifest 记录、不删图片文件；旧版 manifest 没有命中时间，只按「文件已缺失」清理，不会被 `DAYS` 规则误删；
- 每次运行都会把命中（下载成功或确认已存在）的 URL 的最近时间写回 manifest，所以正常使用下 manifest 会随运行更新，`version` 为 `2`；旧版读取方只看 `files` 字段，仍可兼容；
- 体积超限、Content-Type 非图片、格式与 MIME 不符这类失败不会重试；HTTP 4xx（408/429 除外）也不会重试，429/5xx/超时仍按 `--retries` 重试；
- 重试请求同样占用限流位并计入统计，所以「次请求」可能大于图片张数。

---

## 目录结构

```
kb-tools/
├── md_pull_images.py          # 拉取 md 远程图片
├── tests/
│   └── test_md_pull_images.py # 本地 HTTP 服务做的离线测试
├── .github/workflows/ci.yml   # Linux/Windows × 3.8/3.11/3.13 测试矩阵
├── .gitignore
├── LICENSE
└── README.md
```

## 测试

不联网：测试自己起本地 HTTP 服务当图床，并用一个记录请求数、并发峰值和请求时刻的 handler，用来验证限流真的生效（同桶并发不超上限、间隔被拉开、跨源仍并行、`site` 能合并子域）。

```bash
python -m unittest discover -s tests -v
```

可选地做一次静态检查（配置在 `pyproject.toml`，仅 `ruff check`，不格式化、不参与 CI）：

```bash
ruff check md_pull_images.py tests/test_md_pull_images.py
```

测试覆盖下载与回写、引用式重复引用、代码块与 HTML 属性解析、同名文件冲突与 manifest 幂等、manifest 命中时间与清理规则、`--rewrite-only` 不联网回写、二次运行复用本地图片与缺图补下、回写前的并发修改检测、公共后缀文件追加与非法条目拦截、数值参数边界、图片体积/内容校验、私网与重定向拦截、`Retry-After` 日期解析、URL/站点分桶规范化，以及同源限流和跨源并行；另有一组用例校验 README 参数表与 CLI 参数、默认值保持同步，改参数时文档会跟着报错。离线测试的本地 HTTP 服务显式使用 `--allow-private-addresses`。

CI（`.github/workflows/ci.yml`）在 Ubuntu（3.8 固定跑 ubuntu-22.04，新版 runner 已不再提供 3.8）与 Windows 上跑 Python 3.8、3.11、3.13 三档矩阵，覆盖 README 声明的 3.8+ 最低版本；本地只装了 3.13/3.11 也能靠单测里的 `ast.parse(feature_version=(3, 8))` 兜住语法层面的回退。

## 贡献

欢迎补充同类小工具（例如：本地图片反向清理、失效链接检查、笔记目录索引生成）。约定：单文件脚本、只依赖标准库、配套离线测试。

## License

[MIT](LICENSE)
