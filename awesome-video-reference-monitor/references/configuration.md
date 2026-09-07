# 配置说明

配置默认读取环境根的 `.env`，进程环境中的同名变量优先。Skill 紧邻父目录存在 `skills-workspace.json`，且其中 `skills` 名称列表包含当前 Skill 目录名时，以该父目录为环境根；否则以独立 Skill 根目录为环境根。不会继续向上搜索，也不会读取其他未声明项目的配置。

环境根统一保存 `.env`、`.venv/` 和 `.cache/ms-playwright/`。`scripts/bootstrap.ps1` 使用同一定位规则，已有 `.env` 不覆盖。Python 运行包和 Node 依赖仍来自当前 Skill，账号、素材、案例及临时媒体仍写入当前 Skill 目录。

CLI `--env-file` 可以指向 Skill 内文件，或精确指向已声明环境根的 `.env`；这不会放开其他父目录文件的读取或业务数据的写入权限。`PLAYWRIGHT_BROWSERS_PATH` 建议填写环境根缓存的绝对路径，避免切换工作目录后失效。

## 基础配置

```env
TIKHUB_API_KEY=your_tikhub_api_key
ARTICLEMONITOR_STORAGE_BACKEND=local
```

`ARTICLEMONITOR_STORAGE_BACKEND` 只能是 `local` 或 `feishu`，未配置时默认 `local`。

- `local`：账号清单固定保存在项目 `1-对标账号/accounts.md`。
- `feishu`：账号只保存在飞书账号表；项目不读取或写入 `accounts.md`。

## 飞书模式配置

```env
ARTICLEMONITOR_STORAGE_BACKEND=feishu
FEISHU_APP_ID=cli_your_app_id
FEISHU_APP_SECRET=your_app_secret
FEISHU_APP_TOKEN=your_bitable_app_token
DUIBIAO_TABLE_ID=tbl_your_account_table
FEISHU_REFERENCE_TABLE_ID=tbl_your_case_table
```

飞书应用需要多维表格读写权限。两个表 ID 必须属于用户自己的多维表格应用：

- 账号表：主字段 `序号`（文本）；另需 `作者`（文本）、`平台`（单选，包含“抖音”“视频号”）、`账号标识`（文本）、`API查询ID`（文本）、`登记作品链接`（超链接）、`记录时间`（日期）。首次登记会自动创建缺失的非主字段。
- 案例表：主字段 `案例`（文本）；另需 `案例 ID`（文本）、`采集时间`（日期）、`原始链接`（超链接）、`平台`（单选，包含 `douyin`、`wechat_channels`）、`作者`（文本）、`书名`（文本）、`标题/描述`（文本）、`话题`（多选）、`点赞`、`收藏`、`评论`、`转发`（数字）、`时长`（文本）、`清洗逐字稿`（文本）。案例表字段不会自动创建，缺失或类型错误时停止同步。

## 监控新增素材与手动提取额外配置

```env
DASHSCOPE_API_KEY=sk-your_dashscope_api_key
DASHSCOPE_ASR_WORKSPACE_ID=your_workspace_id
DASHSCOPE_ASR_BASE_URL=https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
DASHSCOPE_ASR_REGION=cn-beijing
DASHSCOPE_ASR_MODEL=qwen3-asr-flash
DASHSCOPE_ASR_LANGUAGE=zh
DASHSCOPE_ASR_ENABLE_ITN=false
```

监控处理新增素材和手动提取新作品时，还要求 FFmpeg 和 FFprobe 位于 `PATH`，或分别配置 `FFMPEG_PATH`、`FFPROBE_PATH`。手动提取若命中已有案例，不会再次调用媒体工具或 ASR。

视频号新增监控素材或手动提取新作品还要求：

- Node.js 20+
- `scripts/wechat-decrypt/node_modules/` 已安装
- Playwright Chromium 已安装
- Node 不在 `PATH` 时配置 `WECHAT_DECRYPT_NODE_PATH`

`.env` 包含密钥，不得提交、打印、写入 Markdown 或复制到其他目录。Skill 不得替用户创建飞书应用、修改权限或猜测表 ID。

手动提取在 `local` 模式不访问账号表；在 `feishu` 模式只要求案例表相关配置和字段完整，不读取或写入账号表。新作品需要 TikHub、DashScope 和媒体工具配置，已有案例只需 TikHub 用于解析稳定案例 ID。
