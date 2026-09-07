# awesome-video-reference-monitor

## 共用环境与独立安装

本 Skill 可以单独安装，也可以由紧邻父目录的 `skills-workspace.json` 在 `skills` 列表中显式声明为共用工作区成员。共用模式下，`.env`、`.venv` 和浏览器缓存位于工作区根；源码、账号、素材、案例仍在当前 Skill 内。

在 Skill 根目录运行下面命令，确定后续示例使用的环境根：

```powershell
$envRoot = python -X utf8 -c "import sys; sys.path.insert(0, 'scripts'); from article_monitor.project import environment_root; print(environment_root())"
```

初始化脚本会自动采用这个环境根，已有 `.env` 不覆盖。以下 `.env` 配置操作均针对环境根，业务目录说明均针对 Skill 根。

一个可独立安装的 Agent Skill，用于登记和监控抖音、微信视频号对标账号，也可从单条公开视频手动提取口播文案。仓库根目录本身就是 `awesome-video-reference-monitor` Skill。

项目默认使用纯本地 Markdown，也可切换到飞书多维表格协作模式。两种模式不会同时维护账号清单；素材 Markdown 始终保存在当前项目中。

## 功能列表

- 从任意一条抖音或微信视频号作品链接解析并登记对标账号。
- 按“平台 + API 查询 ID”幂等保存账号，支持本地 Markdown 或飞书账号表。
- 串行监控全部账号最近 72 小时内发布的视频作品。
- 支持按点赞、收藏、评论、转发和转赞比筛选新素材，并可通过 Agent 使用自然语言描述条件。
- 下载媒体、提取音频，并通过 DashScope Qwen ASR 生成口播逐字稿。
- 根据单条作品链接手动提取文案，跳过账号登记、监控窗口和传播指标筛选。
- 将作品信息、互动数据和逐字稿保存为本地 Markdown。
- 飞书模式下把新素材和已有案例数据幂等同步到用户自己的案例表。
- 跨素材库和人工案例目录去重；已有素材只刷新点赞、收藏、评论、转发和更新时间。
- 提供 `记录对标`、`监控对标` 与 `手动提取文案` 三种 Agent Skill 触发方式。

默认筛选规则：

- 作品发布时间位于执行时刻向前 72 小时内，包含边界，排除未来时间。
- 转发数至少为 `500`。
- 点赞数大于零时，`转发数 / 点赞数` 至少为 `1.5`。
- 点赞数为零或负值时，只检查转发数。

提供自定义指标条件时，会整体替换上述转发和转赞比门槛；没有提到的指标不参与筛选。多个条件默认使用“且”，明确指定“或”时才使用“或”。时间窗口仍默认 72 小时，可单独覆盖。转赞比固定为 `转发数 / 点赞数`，自定义比例条件遇到点赞为零或缺失时不通过。

## 快速开始

### 1. 准备环境

运行环境：

- Windows PowerShell
- Python 3.10 或更高版本
- Node.js 20 或更高版本
- FFmpeg 与 FFprobe
- TikHub API Key
- DashScope Qwen ASR API Key 和 Workspace ID

“记录对标”不需要 FFmpeg；“监控对标”处理新增达标素材、或“手动提取文案”处理新作品时，需要 FFmpeg 与 FFprobe。命中已有案例的手动提取不会重复调用媒体工具。初始化脚本不会自动安装 FFmpeg。

#### 安装 FFmpeg（Windows）

- 官方下载入口：[Download FFmpeg](https://ffmpeg.org/download.html)
- Windows 可执行文件：[gyan.dev builds](https://www.gyan.dev/ffmpeg/builds/)（FFmpeg 官方下载页列出的构建提供方）

从 Windows 构建页面下载发行版压缩包并解压，例如放到 `C:\Tools\ffmpeg`。然后把 `C:\Tools\ffmpeg\bin` 加入系统 `PATH`，重新打开 PowerShell，并验证：

```powershell
ffmpeg -version
ffprobe -version
```

如果不想修改系统 `PATH`，可在环境根 `.env` 中配置可执行文件的绝对路径：

```env
FFMPEG_PATH=C:\Tools\ffmpeg\bin\ffmpeg.exe
FFPROBE_PATH=C:\Tools\ffmpeg\bin\ffprobe.exe
```

克隆项目：

```powershell
git clone https://github.com/pork1234cc/jxskill.git
Set-Location jxskill
Copy-Item -LiteralPath .env.example -Destination .env
Set-Location awesome-video-reference-monitor
```

执行初始化脚本：

先填写父目录 `.env` 中的凭据，并把 `PLAYWRIGHT_BROWSERS_PATH` 改为仓库根 `.cache/ms-playwright` 的绝对路径。共用环境的完整流程见[仓库说明](../README.md)。

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
```

脚本会创建或修复 `.venv`、以 editable 模式安装 Python 项目、校验 Node.js 20+、通过 Windows `npm.cmd`/`npx.cmd` 安装视频号解密依赖，并默认安装 Playwright Chromium。脚本还会检查 FFmpeg、FFprobe 和配置占位值；缺少媒体工具时会提示，但不会阻止只需要 TikHub 的“记录对标”。

如暂时不需要安装浏览器，可执行：

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -SkipBrowserInstall
```

### 2. 配置密钥

初始化脚本首次执行时会自动从 `assets/env.example` 创建根目录 `.env`。如果跳过初始化，也可以手动复制：

```powershell
Copy-Item -LiteralPath .\assets\env.example -Destination (Join-Path $envRoot '.env')
```

至少填写：

```env
TIKHUB_API_KEY=your_tikhub_api_key
DASHSCOPE_API_KEY=sk-your_dashscope_api_key
DASHSCOPE_ASR_WORKSPACE_ID=your_workspace_id
```

变量用途：

- `TIKHUB_API_KEY`：解析账号并读取近期作品时使用。
- `DASHSCOPE_API_KEY`：调用阿里云百炼 Qwen-ASR 的鉴权密钥。
- `DASHSCOPE_ASR_WORKSPACE_ID`：阿里云百炼业务空间的唯一 ID。项目会用它组成 Qwen-ASR 的业务空间专属请求地址；它不是 API Key，也不是应用 APP ID。

“记录对标”只需要 `TIKHUB_API_KEY`，不会调用 ASR；后两项在“监控对标”处理新增达标素材，或“手动提取文案”处理新作品并生成逐字稿时才需要。

#### 获取 TikHub API Key

- TikHub 官网：[https://www.tikhub.io](https://www.tikhub.io)
- API Key 控制台：[https://user.tikhub.io/dashboard/api](https://user.tikhub.io/dashboard/api)
- API 文档：[https://docs.tikhub.io](https://docs.tikhub.io)

1. 在 TikHub 官网注册并登录，完成邮箱验证。
2. 打开 API Key 控制台，在用户中心创建 API Key；创建后的密钥只展示一次，请立即安全保存。
3. 将密钥写入环境根 `.env`：

   ```env
   TIKHUB_API_KEY=你的真实_API_Key
   ```

4. 中国大陆用户保持默认接口地址 `https://api.tikhub.dev`；中国大陆以外用户可在 `.env` 中设置：

   ```env
   TIKHUB_BASE_URL=https://api.tikhub.io
   ```

TikHub 是独立第三方服务。不要把真实 API Key 提交到 Git、粘贴到 Issue 或写入公开日志。

#### 获取 DashScope API Key 和 Workspace ID

1. 登录[阿里云百炼控制台](https://bailian.console.aliyun.com/?tab=model)，选择项目默认使用的“华北 2（北京）”地域。
2. 按[官方 API Key 获取说明](https://help.aliyun.com/zh/model-studio/get-api-key)进入密钥管理。使用已有 Key，或创建新 Key；创建时选择它所属的账号和业务空间，然后复制到 `DASHSCOPE_API_KEY`。
3. 按[官方 Workspace ID 获取说明](https://help.aliyun.com/zh/model-studio/obtain-the-app-id-and-workspace-id)，在百炼控制台右上角查看当前业务空间 ID，或在业务空间管理页面的 `Workspace ID` 列复制目标值，填写到 `DASHSCOPE_ASR_WORKSPACE_ID`。
4. 确认 API Key 与 Workspace ID 属于同一业务空间，并且该空间有权调用 `qwen3-asr-flash`。北京与其他地域的 Key、Workspace ID 和服务地址不要混用。
5. 将两个值写入环境根 `.env`，保留原值的完整前缀，不要加示例中的占位符。

本项目默认通过 Qwen-ASR 的北京业务空间专属地址调用 `qwen3-asr-flash`，具体接口格式见[官方 Qwen-ASR API 参考](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference)。不要把真实 API Key 发到聊天、Issue、日志或截图中；`.env` 也不要提交到 Git。

完整配置及 FFmpeg、Node.js 自定义路径见 [assets/env.example](assets/env.example)。`.env` 已被 Git 忽略，不要提交或公开其中的密钥。

默认配置为纯本地模式：

```env
ARTICLEMONITOR_STORAGE_BACKEND=local
```

如需多人协作，可切换为飞书模式：

```env
ARTICLEMONITOR_STORAGE_BACKEND=feishu
FEISHU_APP_ID=cli_your_app_id
FEISHU_APP_SECRET=your_app_secret
FEISHU_APP_TOKEN=your_bitable_app_token
DUIBIAO_TABLE_ID=tbl_your_account_table
FEISHU_REFERENCE_TABLE_ID=tbl_your_case_table
```

飞书应用需要多维表格读写权限。请在同一个多维表格应用中准备“账号表”和“案例表”，将账号表主字段命名为 `序号`，案例表主字段命名为 `案例`。账号表的其他缺失字段会在首次执行 `record` 时补齐；案例表字段需按[配置说明](references/configuration.md)预先建立。

### 3. 记录对标账号

使用该账号下任意一条公开视频链接：

```powershell
& "$envRoot\.venv\Scripts\python.exe" -X utf8 -m article_monitor record "作品链接" --json
```

命令只解析并登记账号，不会扫描作品、下载媒体或调用 ASR。本地模式写入 `1-对标账号/accounts.md`；飞书模式只写飞书账号表，不会同时修改本地账号清单。

### 4. 手动提取单条文案

提供唯一的抖音或微信视频号公开视频链接：

```powershell
& "$envRoot\.venv\Scripts\python.exe" -X utf8 -m article_monitor extract "作品链接" --json
```

命令不会登记账号、扫描账号作品或应用监控筛选。新作品直接生成到 `3-对标案例/文案/`；执行前会按案例 ID 递归检查素材库和案例库，已有作品直接返回原文件，不重复下载或调用 ASR。飞书模式会把已有或新增案例幂等同步到案例表，但不会访问账号表。

### 5. 监控全部账号

```powershell
& "$envRoot\.venv\Scripts\python.exe" -X utf8 -m article_monitor monitor --json
```

命令从当前模式的唯一账号源读取账号，依次扫描近期作品。达标作品生成在 `2-素材库/<账号名称>/`，这里是自动筛选后的待人工确认区，并不表示素材已经成为正式对标案例。媒体和音频中间文件在任务结束后清理。飞书模式会立即把这些候选素材同步到案例表；因此飞书案例表当前同时承担监控候选素材镜像的作用。同步失败时保留本地 Markdown，并在下轮监控时重试而不重复 ASR。

CLI 使用经过校验的结构化筛选参数。例如，筛选点赞至少 500 且转赞比至少 1.5：

```powershell
& "$envRoot\.venv\Scripts\python.exe" -X utf8 -m article_monitor monitor --filter "like_count:gte:500" --filter "forward_like_ratio:gte:1.5" --json
```

支持字段为 `like_count`、`fav_count`、`comment_count`、`forward_count`、`forward_like_ratio`；支持运算符为 `gte`、`gt`、`lte`、`lt`、`eq`。显式“或”关系增加 `--filter-logic any`，自定义时间窗口使用 `--window-hours`。不传 `--filter` 时继续使用默认指标门槛。

## 使用 Agent Skill

当前目录就是独立 Skill，可从集合仓库中单独安装到支持的 Agent：

```powershell
npx skills add pork1234cc/jxskill --skill awesome-video-reference-monitor
```

也可以克隆仓库后作为本地 Skill 使用。Skill 不依赖特定 Agent 产品。

在支持项目级 Agent Skill 的环境中打开本仓库后，可直接使用：

```text
$awesome-video-reference-monitor 记录对标 https://v.douyin.com/示例链接/
```

或：

```text
$awesome-video-reference-monitor 手动提取文案 https://v.douyin.com/示例链接/
```

或：

```text
$awesome-video-reference-monitor 监控对标
```

通过支持 Agent Skills 的 Agent（如 Codex、Claude 或 HERMES）调用时，也可以直接使用自然语言条件；HERMES 不是必需依赖：

```text
监控点赞 500，转赞比至少 1.5
```

自然语言由 Agent 根据 `SKILL.md` 转换成结构化 CLI 参数；CLI 本身不解析自然语言。Skill 会先回显本轮条件，含糊或混合“且/或”的表达会先要求澄清。

也可以在同一句话中为本次任务选择飞书模式：

```text
使用飞书模式监控对标，筛选点赞至少 500、转赞比至少 1.5 的视频
```

Agent 会把“飞书模式”映射为本次命令进程的 `ARTICLEMONITOR_STORAGE_BACKEND=feishu`，不会自动修改 `.env`。飞书应用凭据和数据表参数仍需提前配置；配置不完整时停止执行，不会退回本地模式。直接使用 CLI 时仍需通过环境变量或 `.env` 选择模式。

Skill 只会调用当前项目的 `record`、`extract` 或 `monitor` 命令。安装依赖、删除数据和创建定时任务仍需单独授权。

## 本地数据目录

```text
awesome-video-reference-monitor/
├─ 1-对标账号/
│  └─ accounts.md                # 本地账号清单，首次登记时创建
├─ 2-素材库/
│  └─ <账号名称>/*.md            # 自动筛选达标、等待人工确认的候选素材
├─ 3-对标案例/
│  └─ 文案/*.md                  # 人工筛选案例及手动提取文案
└─ .tmp/                         # 临时媒体与 ASR 中间文件
```

### 素材库与对标案例的区别

`2-素材库/` 相当于自动监控收件箱：只有满足本轮筛选条件的新视频才会进入，每条视频保存一个 Markdown，内容包括作者、标题、发布时间、互动数据、原始链接和口播逐字稿。视频、音频及其他处理中间文件不会长期保存在这里。

人工确认值得长期保留的素材后，可把对应 Markdown 从 `2-素材库/` 移动到 `3-对标案例/文案/`。后续监控会同时检查两个目录以避免重复采集，并在文件当前所在位置刷新点赞、收藏、评论、转发和数据更新时间，不会把文件移回素材库。

手动提取的新作品直接进入 `3-对标案例/文案/`，因为用户已经显式选定了该作品；如果相同案例 ID 已存在于任一目录，则返回现有文件而不重复 ASR。

飞书模式下，候选素材进入本地素材库时就会同步到飞书案例表，无需等到人工移动。因此本地目录负责区分“候选素材”和“人工确认案例”，飞书案例表则保存自动筛选结果及其后续数据更新。

账号清单、素材库和人工案例目录默认都被 Git 忽略，避免误提交业务数据。本地模式下不要修改 `accounts.md` 的格式标记、表头或账号 ID；飞书模式不会读取或写入该文件。如确需对素材 Markdown 做版本管理，应先单独调整 `.gitignore` 并检查敏感内容。

## 目录结构

```text
awesome-video-reference-monitor/
├─ SKILL.md                                 # 根级 Skill 入口
├─ agents/openai.yaml                       # UI 元数据与调用策略
├─ references/                              # 配置和监控数据契约
├─ assets/env.example                       # 可安装的配置模板
├─ LICENSE                                  # MIT 许可证
├─ scripts/
│  ├─ bootstrap.ps1                         # Windows 初始化脚本
│  ├─ article_monitor/                      # Python 运行源码
│  └─ wechat-decrypt/                       # 视频号本地解密桥接
├─ tests/                                   # 单元测试
├─ 1-对标账号/
├─ 2-素材库/
├─ 3-对标案例/文案/
└─ pyproject.toml
```

## 开发指南

运行全部测试：

```powershell
& "$envRoot\.venv\Scripts\python.exe" -X utf8 -m unittest discover -s tests -v
```

运行 lint：

```powershell
& "$envRoot\.venv\Scripts\ruff.exe" check scripts tests
```

当前测试集包含 110 项测试，其中 109 项通过，1 项真实 Chromium/WASM 密钥流向量测试默认跳过；该测试需要显式启用并准备好本机浏览器环境。

## 使用边界

- 仅支持抖音和微信视频号公开视频链接。
- 飞书模式不会把媒体、音频或 `.env` 上传到飞书，只同步账号字段、案例元数据和清洗逐字稿。
- `local` 与 `feishu` 是互斥账号源；切换模式不会自动迁移既有账号。
- 手动提取一次只接受一条公开视频链接，不提供文案改写、BGM 提取、评分或互动率计算。
- 不启动后台服务或定时任务；每次监控都必须由用户或 Agent 明确触发。
- 不采集或写入播放数、`read_count`。
- 真实运行依赖第三方 API、平台响应字段和本机媒体环境，首次使用前建议先用少量账号验收。

## 更多文档

- [配置说明](references/configuration.md)
- [数据契约](references/data-contracts.md)

## 许可证

本项目使用 [MIT License](LICENSE)。
