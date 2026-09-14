# 九宫格图片生成 Skill

将一个主题或 9 个分镜场景转换为统一风格的 3×3 九宫格总图，并自动完成结构检测、九格裁切和结果审计。

项目面向 Codex Skill 使用，也可以直接通过 Python 命令行运行。生图服务使用 [API 易](https://ai.apii.cn) 的 `gpt-image-2.0-4k` 模型。

## 功能特性

- 支持单主题扩展为 9 个互补场景，也支持保留用户给定的 9 个场景及顺序。
- 支持 `1:1`、`3:4`、`4:3`、`9:16`、`16:9` 五种比例。
- 自动构造无可见分隔线、无边距、主体不跨格的 3×3 复合提示词。
- 检测白色或黑色分隔带以及异常边界漂移，结构不合格时最多重新生成一次。
- 根据实际边界或固定三等分坐标裁切，输出 9 张标准尺寸图片。
- 默认只交付 9 张裁片和一个 `manifest.json`，输入、脱敏请求、调用次数、裁切模式与质量告警集中记录在 manifest 中。
- 可通过命令行开关按需保留九宫格总图或生成联系表。
- 提供 `--dry-run`，可在不访问网络、不提供 API 密钥的情况下验证输入和请求内容。

## 工作流程

```text
自然语言主题或九格分镜
        ↓
标准输入 JSON
        ↓
构造 3×3 隐形边界提示词
        ↓
调用 API 生成九宫格总图
        ↓
结构检测 ──不合格──→ 最多重试一次
        ↓ 合格
裁切 9 张单图 + 审计清单
```

只有 `manifest.json` 中的 `status` 为 `processed` 时，生成结果才可视为交付完成。

## 环境要求

- Python 3.10 或更高版本
- Pillow 10.x～12.x
- 可访问 `https://ai.apii.cn/v1`
- 正式生图时需要环境变量 `APII_API_KEY`

## 安装

从 `jxskill` 仓库安装这个独立 Skill：

```powershell
npx skills add pork1234cc/jxskill --skill nine-grid-image-generation
```

NPX 只安装 Skill 文件，不会自动安装 Python 依赖。直接运行脚本时，在 Skill 目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

配置模板位于 `assets/.env.example`。脚本不会自动加载 `.env`，正式生图前需为当前 PowerShell 会话设置 API 密钥：

```powershell
$env:APII_API_KEY = "你的 API Key"
```

脚本直接读取环境变量，不会自动加载 `.env` 文件。请勿把真实密钥提交到版本库或写入输入 JSON、日志和截图。

## 快速开始

### 1. 准备输入文件

新建 UTF-8 编码的 `nine_grid_input.json`：

```json
{
  "title": "春日江南生活",
  "ratio": "9:16",
  "style": "电影感国风摄影",
  "global_requirements": "柔和薄雾、低饱和青绿色，不出现文字和水印",
  "scenes": [
    {"slot": 1, "prompt": "清晨薄雾中的江南古镇"},
    {"slot": 2, "prompt": "河边洗衣的妇人"},
    {"slot": 3, "prompt": "石桥上撑伞行人"},
    {"slot": 4, "prompt": "临河茶馆"},
    {"slot": 5, "prompt": "乌篷船穿过水巷"},
    {"slot": 6, "prompt": "老人在窗边读书"},
    {"slot": 7, "prompt": "雨水落在青石板上"},
    {"slot": 8, "prompt": "黄昏灯笼亮起"},
    {"slot": 9, "prompt": "夜色中的古镇全景"}
  ]
}
```

字段约束：

| 字段 | 是否必填 | 说明 |
| --- | --- | --- |
| `title` | 是 | 非空标题，用于输出记录 |
| `ratio` | 是 | 支持 `1:1`、`3:4`、`4:3`、`9:16`、`16:9` |
| `style` | 否 | 统一的媒介、色彩、光线与镜头风格 |
| `global_requirements` | 否 | 应用于全部九格的统一要求和禁止项 |
| `scenes` | 是 | 必须恰好包含 9 个场景 |
| `scenes[].slot` | 是 | 必须完整覆盖整数 1～9，不得重复 |
| `scenes[].prompt` | 是 | 非空场景描述，每格建议只描述一个静态动作阶段 |

更完整的输入约定见 [`references/input-format.md`](references/input-format.md)。

### 2. 先执行 dry-run

```powershell
python scripts\generate_grid.py `
  --input nine_grid_input.json `
  --output-dir nine-grid-output\spring-jiangnan `
  --dry-run
```

该命令会验证输入并生成 `manifest.json`，脱敏请求直接记录在 manifest 的 `request` 字段中；不会调用生图接口，也不要求设置 API 密钥。

### 3. 正式生成并裁切

```powershell
python scripts\generate_grid.py `
  --input nine_grid_input.json `
  --output-dir nine-grid-output\spring-jiangnan
```

成功时，命令行会输出类似结果：

```json
{
  "status": "processed",
  "manifest": "...\\nine-grid-output\\spring-jiangnan\\manifest.json",
  "cells": 9
}
```

## 输出文件

```text
nine-grid-output/spring-jiangnan/
├── scene_001.png
├── scene_002.png
├── ...
├── scene_009.png
└── manifest.json
```

- `scene_001.png`～`scene_009.png`：按从左到右、从上到下顺序裁切的 9 张图片。
- `manifest.json`：规范化输入、脱敏请求、调用次数、裁切方式、结构告警及每张裁片信息。

默认生成过程使用临时工作目录，总图和中间裁片不会进入交付目录。需要人工排查结构检测时，可以保留每次 API 返回的总图：

```powershell
python scripts\generate_grid.py `
  --input nine_grid_input.json `
  --output-dir nine-grid-output\spring-jiangnan `
  --keep-debug-artifacts
```

需要九张裁片的缩略联系表时，可以增加：

```powershell
--contact-sheet
```

两个开关可以同时使用。`--keep-debug-artifacts` 会额外输出 `grid_attempt_*.png`，`--contact-sheet` 会额外输出 `contact_sheet.jpg`。

各比例对应尺寸：

| 比例 | 总图请求尺寸 | 单格输出尺寸 |
| --- | ---: | ---: |
| `1:1` | 2880×2880 | 960×960 |
| `3:4` | 2448×3264 | 816×1088 |
| `4:3` | 3264×2448 | 1088×816 |
| `9:16` | 2160×3840 | 720×1280 |
| `16:9` | 3840×2160 | 1280×720 |

## 结果状态

| 状态 | 含义 |
| --- | --- |
| `dry_run` | 输入和脱敏请求已写入 manifest，未访问 API |
| `processed` | 结构检测通过，9 张单图已生成 |
| `rejected_visible_grid` | 检测到可见分隔线或分隔带 |
| `rejected_boundary_drift` | 场景边界偏离理论三等分位置超过容差 |
| `api_error` | API 请求、响应解析或图片下载失败 |

如果两次结构检测都失败，程序默认只保留包含各次错误检测信息的 `manifest.json`，并以非零状态退出，不会把错误裁片当作成功结果交付。只有显式使用 `--keep-debug-artifacts` 时才保留失败总图。

## 仅处理已有九宫格图片

不调用生图 API，只检测并裁切一张本地九宫格图片：

```powershell
python scripts\grid_processor.py `
  --grid-image path\to\grid.png `
  --output-dir processed-grid `
  --ratio 9:16
```

默认审计文件为 `processed-grid/grid_cells_manifest.json`。也可以通过 `--plan` 提供批处理计划，或用 `--manifest` 指定审计文件路径：

```powershell
python scripts\grid_processor.py `
  --plan path\to\grid_batches.json `
  --output-dir processed-grids `
  --ratio 16:9 `
  --manifest processed-grids\manifest.json
```

## 作为 Codex Skill 使用

`SKILL.md` 定义了触发场景、输入规范、生成流程和失败处理策略。将本项目放入 Codex 可发现的 Skill 目录后，可以直接提出自然语言需求，例如：

```text
帮我生成一组 9:16 九宫格，主题是清晨菜市场从开市到热闹起来的过程，
统一纪实摄影风格，不要文字和水印。
```

如果只提供一个主题，Skill 会按环境建立、核心主体、关键动作、空间关系、主画面、细节、变化、结果和收束全景的职责规划九格；如果已经提供 9 个编号场景，则保持场景含义与顺序。

## 测试

运行全部单元测试：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

测试覆盖输入校验、提示词约束、API 请求结构、Base64 响应、最简输出、可选调试产物、API 失败无残留、dry-run、非整除尺寸的完整像素归属、自适应边界裁切和可见分隔带拒绝等行为。测试不会执行真实生图请求。

## 项目结构

```text
.
├── SKILL.md                       # Codex Skill 主说明
├── requirements.txt               # Python 依赖
├── agents/
│   └── openai.yaml                 # Skill UI 元数据
├── assets/
│   └── .env.example                # API 密钥配置模板
├── scripts/
│   ├── generate_grid.py           # 生图、重试、裁切与审计入口
│   └── grid_processor.py          # 九宫格结构检测与裁切
├── references/
│   ├── input-format.md            # 标准输入合同
│   └── apii-api.md                # API 字段与响应格式参考
├── tests/                          # 单元测试
└── evals/
    └── evals.json                 # Skill 评估用例
```

## 注意事项

- 图像模型不一定能稳定还原准确文字；需要文字时，应把原文写入对应场景并在交付前人工复核。
- 关键主体应与格子边缘保持安全距离，避免裁切后缺失。
- 不要在单格描述中再次要求拼贴、多宫格或多个时间阶段。
- API 限流或临时服务错误会有限重试；内容审核拒绝不会自动弱化用户的核心主题。
- 详细接口合同见 [`references/apii-api.md`](references/apii-api.md)。

