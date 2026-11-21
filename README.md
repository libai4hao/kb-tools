# kb-tools

知识库相关的小工具集合：把散落在各处的 Markdown / 笔记 / 文档整理成能长期自持的本地知识库。

纯 Python 标准库实现，无第三方依赖，Python 3.8+。

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

只下载 `http(s)` 与 `//` 协议开头的图片；已经是本地相对路径、绝对路径、`data:` URI 的图片会原样跳过。

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
```

默认图片目录是 Markdown 同级的 `<文件名>.assets/`，回写后的链接形如 `排序算法.assets/xxx.png`，所以把 md 和 assets 目录一起搬走即可。

### 参数

| 参数 | 说明 |
| --- | --- |
| `target` | Markdown 文件或目录 |
| `-o, --assets-dir` | 图片目录。单文件时即目标目录；目录模式时为 `<assets-dir>/<md 文件名>/` |
| `--rewrite` | 把 md 里的远程链接替换成本地相对路径（默认只下载不改文件） |
| `--backup` | 回写前生成 `.md.bak` |
| `--overwrite` | 同名文件重新下载覆盖（默认已存在则跳过） |
| `-r, --recursive` / `--no-recursive` | 目录模式是否递归，默认递归 |
| `-j, --jobs` | 并发下载数，默认 8 |
| `--timeout` | 单请求超时秒数，默认 20 |
| `--retries` | 失败重试次数，默认 2（指数退避） |
| `--referer` | 自定义 Referer；不填则用图片域名首页，用于绕过常见防盗链 |
| `--dry-run` | 只列出待下载图片 |
| `-q, --quiet` | 只输出汇总 |

退出码：全部成功 `0`，有失败 `1`，参数或路径问题 `2`。

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

### 已知边界

- 只处理 Markdown 文本里的图片引用，不解析 CSS 背景图、`<video>`、iframe 内嵌内容；
- 写作时请勿并发改同一个 md，`--rewrite` 是整文件重写；
- 少数站点会校验 Referer + Cookie 双重防盗链，这种只能 `--referer` 加上自行处理 Cookie。

---

## 目录结构

```
kb-tools/
├── md_pull_images.py          # 拉取 md 远程图片
├── tests/
│   └── test_md_pull_images.py # 本地 HTTP 服务做的离线测试
├── LICENSE
└── README.md
```

## 测试

不联网，测试自己起一个本地 HTTP 服务当图床：

```bash
python -m unittest discover -s tests -v
```

## 贡献

欢迎补充同类小工具（例如：本地图片反向清理、失效链接检查、笔记目录索引生成）。约定：单文件脚本、只依赖标准库、配套离线测试。

## License

[MIT](LICENSE)
