---
name: awesome-video-reference-monitor
description: 使用自包含工具登记并监控抖音或微信视频号对标账号，也可从单条公开视频手动提取文案；用户说“记录对标”“监控对标”“手动提取文案”，或用“监控”描述点赞、收藏、评论、转发、转赞比、时间及本地/飞书模式时使用。不用于文案改写、评分或创建定时任务。
---

# 对标账号登记、监控与手动文案提取

本 Skill 使用通用 Agent Skill 结构，不依赖特定 Agent 产品。业务源码、素材和临时文件都属于当前 Skill，不得导入或调用其他 Skill 的业务代码。配置、Python 虚拟环境和浏览器缓存可以使用显式声明的共用工作区。

## 执行前检查

以本 `SKILL.md` 所在目录作为 Skill 根。如果紧邻父目录的 `skills-workspace.json` 中 `skills` 列表包含当前 Skill 目录名，则该父目录是环境根；否则环境根就是 Skill 根。声明格式错误时停止。下文 `<ENV_ROOT>` 必须替换为解析后的绝对环境根路径；执行命令时工作目录仍设为 Skill 根。

确认存在：

- 环境根 `.venv/Scripts/python.exe`
- `scripts/article_monitor/`
- `scripts/bootstrap.ps1`
- `assets/env.example`
- 环境根 `.env`

缺少虚拟环境或配置时停止，并根据需要读取 [配置说明](references/configuration.md) 报告缺失项；不得自动安装依赖、复制其他项目的 `.env` 或猜测密钥。

## 存储模式选择

用户没有指定存储模式时，使用环境根 `.env` 中的 `ARTICLEMONITOR_STORAGE_BACKEND`。用户在“记录对标”“监控对标”或“手动提取文案”请求中明确说“使用飞书模式”或“使用本地模式”时，将其分别映射为本次命令进程的 `ARTICLEMONITOR_STORAGE_BACKEND=feishu` 或 `ARTICLEMONITOR_STORAGE_BACKEND=local`，并在执行前回显所选模式。

自然语言指定模式只覆盖本次命令，不得修改 `.env`；用户明确要求永久切换时才可修改配置。飞书配置不完整时停止并报告缺失项，不得静默退回本地模式。

## 记录对标

用户输入“记录对标 + 作品链接”或“作品链接 + 记录对标”时，提取唯一的抖音或微信视频号 HTTP/HTTPS 作品链接，在项目根执行：

```powershell
& "<ENV_ROOT>\.venv\Scripts\python.exe" -X utf8 -m article_monitor record "作品链接" --json
```

登记按“平台 + API 查询 ID”向当前模式的唯一账号源幂等写入账号：`local` 模式使用项目 `1-对标账号/accounts.md`，`feishu` 模式使用配置的飞书账号表。命令完成后立即停止，不得读取作品列表、下载媒体或调用 ASR，也不得同时写两个账号源。

没有唯一支持链接时停止并说明；不得因“记录对标”指令而自动提取文案。

## 手动提取文案

用户输入“手动提取文案 + 作品链接”“提取这个视频的文案 + 作品链接”或同义的明确单条提取请求时，提取唯一的抖音或微信视频号 HTTP/HTTPS 作品链接，在项目根执行：

```powershell
& "<ENV_ROOT>\.venv\Scripts\python.exe" -X utf8 -m article_monitor extract "作品链接" --json
```

手动提取不登记账号、不读取账号作品列表，也不应用监控时间窗口或传播指标。新作品直接下载、解密（视频号）、提取音频并调用 ASR，Markdown 固定保存到 `3-对标案例/文案/`。

执行前会按案例 ID 递归检查 `2-素材库/` 与 `3-对标案例/文案/`。相同作品已存在时直接返回现有 Markdown，不下载媒体、不调用 ASR；`feishu` 模式仍将现有或新增案例幂等同步到案例表，`local` 模式只保留本地 Markdown。

没有唯一支持链接时停止并说明。不得添加输出目录参数、写到项目外、把“提取文案”误解为文案改写，或在一次命令中猜测处理多个链接。

## 监控对标

用户明确说“监控对标”且没有额外监控参数时，在项目根执行：

```powershell
& "<ENV_ROOT>\.venv\Scripts\python.exe" -X utf8 -m article_monitor monitor --json
```

用户同时提供自然语言筛选条件时，先用一句话回显本轮实际条件，再转换为 CLI 的结构化参数。核心程序不接收自然语言，也不得猜测含糊条件。

指标映射：

- 点赞 → `like_count`
- 收藏 → `fav_count`
- 评论 → `comment_count`
- 转发 → `forward_count`
- 转赞比或转发点赞比 → `forward_like_ratio`，计算公式固定为转发数除以点赞数

比较词映射：

- “至少”“以上”“不低于”，以及“点赞 500”这类未写比较词的指标数字 → `gte`
- “超过”“大于” → `gt`
- “至多”“以下”“不高于” → `lte`
- “少于”“低于”“小于” → `lt`
- “等于”“恰好” → `eq`

每个指标生成一个 `--filter "字段:运算符:阈值"`。多个条件默认使用 `all`，无需额外参数；只有用户明确说“或”“任一”时才增加 `--filter-logic any`。例如“监控点赞 500，转赞比至少 1.5”执行：

```powershell
& "<ENV_ROOT>\.venv\Scripts\python.exe" -X utf8 -m article_monitor monitor --filter "like_count:gte:500" --filter "forward_like_ratio:gte:1.5" --json
```

“最近 N 小时”增加 `--window-hours N`；“最近 N 天”先换算成小时再传入。未说明时间时不传该参数，使用默认 72 小时。明确说“或”时的示例参数为 `--filter-logic any`。

自定义指标条件整体替换原有指标门槛，没有提到的指标不参与筛选。没有自定义指标条件时继续使用原有默认门槛。混合“且/或”、负数、无法确定指标或比较关系时，必须停止并请用户澄清，不得自行改写逻辑。

监控只从当前模式的唯一账号源读取账号，并把新素材 Markdown 保存到 `2-素材库/<账号中文名称>/`。人工筛选后的文件可移动到 `3-对标案例/文案/`；监控会在原位置刷新数据。`feishu` 模式还会幂等同步案例表。不得增加输出目录参数、写到项目外、并发下载或 ASR。

详细窗口、门槛、本地目录和幂等规则见 [数据契约](references/data-contracts.md)。仅在执行监控或诊断监控结果时读取该文件。

## 结果处理

- JSON 顶层 `ok` 为 `false`，或 `summary.failed` 大于零时，按失败处理并原样报告具体错误。
- 登记成功时报告账号、`account_action` 和本地 `account_id`。
- 手动提取成功时报告 `action`（`create` 或 `existing`）、案例 ID 和 Markdown 路径。
- 监控成功时报告账号数，以及新增、更新、跳过和失败数量。
- 不得伪造成功、吞掉失败、自动重试整轮任务或启动后台定时监控。
- 用户明确发出“记录对标”“监控对标”或“手动提取文案”即授权本次 TikHub 查询和项目内约定目录的读写；安装依赖、删除数据或创建定时任务仍需另行授权。
