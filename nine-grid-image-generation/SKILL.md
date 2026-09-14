---
name: nine-grid-image-generation
description: 将用户的一段自然语言或九格分镜描述规范化为 9 个场景，调用 API 易 GPT Image 生图接口生成无可见分隔线的 3×3 九宫格总图，再检测结构并切分为 9 张独立图片。用户提到九宫格生图、九格分镜、一次生成九张图、3×3 场景图、批量场景图或希望把一个主题扩展为九个统一风格画面时，都应使用此 Skill。
---

# 九宫格生图

把用户提示词转换为稳定、可复现的九宫格生图任务。确定性的请求、结构检测和裁切交给随 Skill 提供的脚本完成，不要手工拼接 HTTP 请求或自行估算裁切坐标。

## 运行环境

需要 Python 3.10+、Pillow、可访问 `https://ai.apii.cn/v1`，并通过 `APII_API_KEY` 环境变量提供密钥。依赖安装与配置示例见本 Skill 的 `README.md` 和 `assets/.env.example`。

## 工作流程

1. 完整读取 `references/input-format.md`，按其中的合同理解用户输入。只有需要核对 API 字段、比例能力或响应结构时，再读取 `references/apii-api.md`。
2. 将用户提示词规范化为一个 UTF-8 JSON 计划：
   - 用户已提供 9 个编号场景时，保持场景含义和顺序，不擅自改写事实。
   - 用户只提供一个主题时，自主规划 9 个互补场景；让九格共同覆盖主题，而不是把同一描述重复 9 次。
   - 保持用户指定的人物、地点、时代、商品、色彩、媒介和禁用项。
   - 不确定但不会改变业务含义的细节采用保守默认值；只有缺失信息会造成明显不同结果时才询问用户。
3. 在系统临时目录创建 UTF-8 输入计划，任务结束后删除；不要把输入计划写进交付目录。用户未指定输出目录时，在当前工作目录创建 `nine-grid-output/<简短主题名>/`。
4. 检查环境变量 `APII_API_KEY` 是否存在。不要显示、记录或写入密钥；缺失时只提示用户在本地设置。
5. 从本 Skill 目录运行：

```powershell
python scripts/generate_grid.py --input <nine_grid_input.json> --output-dir <输出目录>
```

6. 脚本会完成以下工作：
   - 构造严格 3×3、隐形边界、满画布、无分隔线的复合提示词；
   - 调用 `https://ai.apii.cn/v1/images/generations`；
   - 使用 `gpt-image-2.0-4k` 和当前比例对应的 4K 尺寸；
   - 检测白色或黑色分隔带、异常边界漂移和外边残留；
   - 结构失败时最多重新生成一次，总计最多 2 次 API 调用；
   - 默认只输出 9 张单图和一个 `manifest.json`；输入计划和脱敏请求直接记录在 manifest 中。
   - 用户明确需要排查结构问题时增加 `--keep-debug-artifacts`，需要联系表时增加 `--contact-sheet`。
7. 检查命令退出状态和 `manifest.json`：
   - `status=processed` 才表示交付完成；
   - `rejected_visible_grid` 表示模型画出了可见分隔线；
   - `rejected_boundary_drift` 表示格子边界偏离三等分超过容差；
   - API 或下载错误应原样概括，不得泄露请求头和密钥。
8. 向用户返回输出目录、9 张单图和 manifest 路径，并简要说明重试次数与裁切模式。

## 输入处理原则

- 九个场景应共享同一视觉风格、角色设定、光线体系和色彩逻辑。
- 每格只呈现一个静态动作阶段，避免在单格中继续生成拼贴或多宫格。
- 关键主体保持安全边距，背景和环境铺满格子边缘。
- 不得生成可见网格线、卡片边框、白边、黑边、间距、页边距或跨格主体。
- 用户未要求文字时，九格中不添加可读文字、Logo、水印或二维码。
- 用户要求准确文字时，把文字原样写入对应场景，并提醒图像模型可能无法稳定还原文字。

## API 约束

- Base URL 固定为 `https://ai.apii.cn`，文生图端点为 `/v1/images/generations`。
- 默认模型为 `gpt-image-2.0-4k`。
- 使用 Bearer 鉴权，密钥只读取 `APII_API_KEY`。
- 请求包含 `model`、`prompt`、`aspect_ratio`、`quality=high`、`output_format=png` 和 `response_format=url`。有效的 `aspect_ratio` 会决定 4K 尺寸，因此不同时发送会被覆盖的 `size`。
- 当前合同不伪造独立的 `negative_prompt` 字段；禁止项直接写入自然语言 Prompt。
- 不进行连接探测或真实生图，除非用户要求执行生图任务。需要检查请求时使用 `--dry-run`，它不会访问网络。

## 失败处理

- API 返回限流时，遵守服务端 `Retry-After`，否则使用短暂递增退避。
- 内容审核拒绝时，停止并说明是哪一批失败，不自动弱化用户的核心主题。
- 两次结构检测均失败时，只保留包含每次检测结果的 `manifest.json`，拒绝交付错误裁片。
- 只有用户明确要求保留调试产物时才使用 `--keep-debug-artifacts`；此时把每次总图一并保留，便于人工复核。

## 输出

默认输出结构：

```text
<output-dir>/
├── scene_001.png
├── scene_002.png
├── ...
├── scene_009.png
└── manifest.json
```

使用 `--keep-debug-artifacts` 时会额外输出 `grid_attempt_*.png`；使用 `--contact-sheet` 时会额外输出 `contact_sheet.jpg`。失败任务默认只输出 `manifest.json`。



