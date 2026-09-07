# jxskill

两个可独立安装的内容采集 Skill：小红书关键词采集，以及抖音、微信视频号对标账号监控。克隆整个仓库时，可共用一份 `.env`、Python 虚拟环境和浏览器缓存。

## 功能

| Skill | 能力 | 默认数据位置 |
| --- | --- | --- |
| [xiaohongshu-collector](xiaohongshu-collector/SKILL.md) | 按关键词采集小红书笔记、下载图片或视频，执行飞书待采集任务 | 工作目录的 `projects/03小红书/01小红书素材/` |
| [awesome-video-reference-monitor](awesome-video-reference-monitor/SKILL.md) | 登记抖音/视频号账号、筛选近期作品、提取单条视频文案，可同步飞书 | Skill 内的 `1-对标账号/`、`2-素材库/`、`3-对标案例/` |

两个 Skill 各自包含运行源码、配置模板和参考资料，互不导入业务代码。小红书评论正文采集、文案改写、自动发布和后台定时任务不在当前范围内。

## 通过 NPX 安装 Skill

```powershell
npx skills add pork1234cc/jxskill
```

按提示选择需要的 Skill 和 Agent。也可以只安装其中一个：

```powershell
npx skills add pork1234cc/jxskill --skill xiaohongshu-collector
npx skills add pork1234cc/jxskill --skill awesome-video-reference-monitor
```

安装后重新启动 Agent。NPX 安装 Skill 文件不会自动安装 Python、Node.js 或 FFmpeg，也不会替用户填写密钥。

单独使用小红书 Skill 时，在保存配置和采集结果的工作目录执行 `python -m venv .venv`，再按它的 [配置说明](xiaohongshu-collector/references/configuration.md) 填写凭据；Python 运行代码仅使用标准库。

单独使用监控 Skill 时，在安装后的 Skill 目录执行 `scripts/bootstrap.ps1`，按 [配置说明](awesome-video-reference-monitor/references/configuration.md) 完成初始化。

## 克隆仓库，共用运行环境

下面流程以 Windows PowerShell 为例。先准备 Python 3.12、Git、Node.js 及 npm；视频文案提取还需要 FFmpeg 和 ffprobe，并将程序加入 `PATH`。

```powershell
git clone https://github.com/pork1234cc/jxskill.git
Set-Location jxskill
Copy-Item -LiteralPath .env.example -Destination .env
```

填写根目录 `.env`，其中 `PLAYWRIGHT_BROWSERS_PATH` 必须改成当前仓库的绝对路径，例如 `C:/work/jxskill/.cache/ms-playwright`。FFmpeg、ffprobe 和 Node.js 不在 `PATH` 时，再填写相应工具路径。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\awesome-video-reference-monitor\scripts\bootstrap.ps1 -PythonCommand python
```

初始化会创建根目录 `.venv`，安装本地监控 Python 包、ruff、锁定版本的 Playwright 和 Chromium。已有 `.env` 不覆盖，初始化不调用业务 API。

`skills-workspace.json` 显式声明工作区成员。监控只在自己被声明时使用紧邻父目录的共用环境；单独安装后回退到自己的环境。账号、素材、案例和临时媒体仍写入监控 Skill 内。搬动工作目录后，需要重建虚拟环境并更新 `.env` 中的绝对路径。

## 配置参数

| 用途 | 参数 |
| --- | --- |
| 公共 TikHub 凭据 | `TIKHUB_API_KEY` |
| Qwen ASR | `DASHSCOPE_API_KEY`、`DASHSCOPE_ASR_WORKSPACE_ID`，其他 ASR 参数见模板 |
| 飞书应用 | `FEISHU_APP_ID`、`FEISHU_APP_SECRET` |
| 小红书飞书任务和详情表 | `NOTE_TOKEN`、`NOTE_WORK`、`NOTE_CONTENT`；`NOTE_COMMENT` 仅预留 |
| 监控存储模式 | `ARTICLEMONITOR_STORAGE_BACKEND=local` 或 `feishu` |
| 监控飞书账号和案例表 | `FEISHU_APP_TOKEN`、`DUIBIAO_TABLE_ID`、`FEISHU_REFERENCE_TABLE_ID` |

监控默认使用本地模式。小红书即时采集在飞书配置不完整时仅保存本地结果；执行飞书任务队列需要完整配置。两个 Skill 的目标表分别维护，可以共用飞书应用凭据。

真实采集可能产生 TikHub、Qwen 调用费用。真实 `.env`、运行环境、缓存和业务数据均被 Git 忽略，不要把凭据填入公开模板。

## 运行

从仓库根目录使用共用 Python，无需激活虚拟环境：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m article_monitor --help
.\.venv\Scripts\python.exe -X utf8 .\xiaohongshu-collector\scripts\run_collector.py --help
```

实际业务命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m article_monitor record "作品链接" --json
.\.venv\Scripts\python.exe -X utf8 -m article_monitor monitor --json
.\.venv\Scripts\python.exe -X utf8 -m article_monitor extract "作品链接" --json
.\.venv\Scripts\python.exe -X utf8 .\xiaohongshu-collector\scripts\run_collector.py collect "关键词" --count 5
.\.venv\Scripts\python.exe -X utf8 .\xiaohongshu-collector\scripts\run_collector.py tasks
```

## 目录结构

```text
jxskill/
├── .env.example
├── skills-workspace.json
├── xiaohongshu-collector/
│   ├── SKILL.md
│   ├── agents/、assets/、references/
│   ├── scripts/
│   └── tests/
└── awesome-video-reference-monitor/
    ├── SKILL.md
    ├── agents/、assets/、references/
    ├── scripts/article_monitor/
    ├── scripts/wechat-decrypt/
    ├── tests/
    └── pyproject.toml
```

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s awesome-video-reference-monitor/tests -v
.\.venv\Scripts\python.exe -m unittest discover -s xiaohongshu-collector/tests -v
.\.venv\Scripts\ruff.exe check awesome-video-reference-monitor/scripts awesome-video-reference-monitor/tests
```

当前监控有 117 项测试，小红书有 68 项测试。监控中的真实本地 Chromium/WASM 向量测试默认跳过；准备好浏览器后，可设置 `RUN_WECHAT_WASM_TEST=1` 和绝对路径的 `PLAYWRIGHT_BROWSERS_PATH` 再运行。

第三方 API 密钥和飞书权限需要使用者自行配置与验收。监控代码许可证见其 [LICENSE](awesome-video-reference-monitor/LICENSE)，第三方解密资产的许可证随 `vendor/` 保留。
