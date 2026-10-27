# soutubot + nhentai 下载工具

AstrBot 插件：反向搜索用户提供的图片，并按用户要求下载指定 nhentai 作品、生成加密 PDF。仓库名是 `astrbot_search_download`，插件内部名称是 `astrbot_soutu_download`。

本仓库同步自 2026-10-27 的运行服务器源码，保持插件版本 `1.1.0`；AstrBot 版本要求为 `>=4.5.7`。不包含 Cookie、服务器环境变量、用户图片、下载文件和私人配置。

## 安装

在 AstrBot 插件管理中使用仓库地址 `https://github.com/sharmanOwO/astrbot_search_download` 安装；手动安装时，将源码放入 `data/plugins/astrbot_soutu_download/`。

在插件目录中、使用运行 AstrBot 的 Python 环境安装依赖。`python -m pip` 使用当前 Python 对应的包管理器，`-r requirements.txt` 按依赖文件安装。Docker 部署应在 AstrBot 容器所用环境中执行，而不是只安装到宿主机。

```bash
python -m pip install -r requirements.txt
```

依赖包括 Requests（网络请求）、Pillow（图片处理）、pypdf 的 crypto 扩展（生成并加密 PDF）。然后在 AstrBot 插件管理中重载插件，在聊天模型配置中启用下面两个工具。工具调用指聊天模型根据用户要求选择插件函数并填写参数。

## 工具与使用方式

| 工具 | 参数 | 用途 |
|---|---|---|
| `soutubot_search_tool` | `strict=false`、`result_limit=-1` | 搜索当前消息或引用消息中的第一张图片；严格模式提高匹配要求 |
| `nhentai_download_tool` | `gallery_id` | 按作品编号或完整主页 URL 下载并发送 PDF |

搜图时附上一张图片或引用图片，说“请帮我搜索这张图片的来源”。下载时明确提供自己要下载的作品编号或主页地址。普通聊天中的数字不应被模型自动当成下载请求。请只下载有权获取和保存的内容。

### 搜图展示指令

以下指令直接由插件处理，不依赖聊天模型选择工具。示例假设唤醒前缀为 `/`。

| 命令 | 行为 |
|---|---|
| `/detail`、`/detail 1`、`/detail true` | 开启详细结果 |
| `/detail 0`、`/detail false` | 使用简洁结果 |
| `/page 5` | 将本次命中按匹配分数降序排序，展示前 5 条 |

默认简洁模式、10 条。这些设置按“会话＋发送者”隔离，重载或重启后恢复默认。用户设置过 `/page` 时优先使用该值，否则采用工具的单次参数，再回退到环境变量。`/page` 不是网站分页，不会额外搜索更多结果，也不会重新发送上一次结果。

简洁结果包含排名、分数、标题、来源、语言和链接；详细模式还会展示存在的作品、画师、角色、分类、页数、章节和标签。缺失字段省略。

## 配置

环境变量是在程序启动环境中提供的配置值；应设置在实际运行 AstrBot 的进程或容器环境中。插件不会自动读取仓库里的 `.env` 文件。敏感值应由自己的部署配置安全注入，不要提交到 GitHub。

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `ASTRBOT_SOUTU_TIMEOUT` | `60` | 网络读取等待秒数，不是整个任务总时限 |
| `ASTRBOT_SOUTU_RESULT_LIMIT` | `10` | 默认展示条数 |
| `ASTRBOT_NHENTAI_DOWNLOAD_DIR` | `/AstrBot/data/plugin_data/astrbot_soutu_download/downloads` | PDF 输出与共享目录 |
| `NHENTAI_PDF_PASSWORD` | `114514` | PDF 打开密码；默认值是公开代码常量，不是私人凭据，需保密时应自行覆盖 |
| `NHENTAI_COOKIE` | 未设置 | 下载网站要求登录时使用自己的 Cookie；Cookie 是登录状态凭据，不能公开 |

源码不读取所谓的 nhentai API_KEY 或 ACCESS_TOKEN，不要配置不存在的选项。

### 跨容器发送 PDF

容器是相对隔离的程序运行环境。如果 AstrBot 与 NapCat 在不同容器，双方必须能通过发送消息时使用的路径访问同一份 PDF 文件。只把文件生成到 AstrBot 独有目录，会导致另一个容器无法读取。

当前代码为下载子目录设置可遍历权限，为 PDF 设置可读权限，成功发送后安排约一小时的延迟清理。部署时仍需配置正确的共享目录；本仓库不包含原服务器的 Docker 部署文件、网络配置或访问凭据。

### 网站验证辅助服务

当前服务器版在搜图接口明确返回 `cf-mitigated: challenge` 时，会向 `http://soutubot-browser:8191/v1` 请求兼容 `request.get` 的浏览器验证服务。这个服务地址是源码中的 `CLOUDFLARE_SOLVER_URL` 常量，不是环境变量选项。

如需在另一套部署中使用这条恢复流程，需要另行提供兼容服务，并让 AstrBot 能解析 `soutubot-browser` 这个服务名；服务与搜图请求需使用一致的对外网络出口。仓库只提供插件源码，不会自动安装或启动该服务。服务地址属于内部部署约定，不含密码或 Token。

验证时只请求网站首页，不把待搜索图片发给辅助服务；程序只在内存缓存返回的通行 Cookie 和 User-Agent。验证完成后最多重新上传一次图片；仍失败则停止。普通错误不自动重试上传，也不自动跟随上传重定向。没有触发验证时不调用辅助服务。

## 限制与排错

- 搜图接受 JPG、PNG、WebP、GIF；图片必须非空且不超过 5 MiB。参考图片读取由 AstrBot 消息组件处理。
- HTTP 上传采用 multipart 表单，即在一次请求里携带图片文件和参数，由 Requests 自动编码。上传目标为 soutubot 网站。
- “验证服务连接失败”：检查上述内部服务、服务名解析和网络，不是给插件随便填写 API Key。
- 搜图接口结构错误：当前解析 `results` 以及每条结果的第一条 `path_segments`；网站结构变化后可能需要适配。
- PDF 下载失败：检查网站连通性、作品地址、必要的登录 Cookie、依赖及输出目录权限。
- PDF 已生成但发送失败：检查 AstrBot 与消息平台容器的共享路径和读取权限。

`soutubot_search.py` 和 `download_comic.py` 当前作为模块供插件调用，不提供独立命令行入口。不能直接运行这两个文件来替代 AstrBot 工具调用。

## 源码结构与验证范围

| 文件 | 用途 |
|---|---|
| `main.py` | 注册两个工具、展示指令、会话设置与文件发送 |
| `soutubot_search.py` | 搜图请求、网站验证恢复和结果格式化 |
| `download_comic.py` | 作品解析、图片下载、PDF 生成及加密 |
| `plugin_utils.py` | 提取消息图片、临时文件清理 |

这次从运行服务器导出的版本没有附带独立测试目录，因此本 README 不声称仓库内有完整回归测试。发布前做源码语法检查；真实搜图、外部网站访问和 QQ 文件投递仍需要在自己的部署中验证。本次源码发布没有额外发起搜索、下载或 QQ 消息。

`.gitignore` 排除 Cookie、环境文件、本地配置、生成 PDF、日志和缓存；提交前仍应检查文件差异，因为忽略规则不会自动移除已经追踪的文件。
