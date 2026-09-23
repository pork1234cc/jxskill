---
name: ai-fixed-proxy
description: 根据用户提供的 VPS Shadowsocks 或静态 SOCKS5 节点参数，生成可粘贴到 Clash Verge Rev 的完整 JavaScript 扩展脚本，让指定 AI 域名走固定出口，其余流量继续匹配原配置。适用于请求 AI 固定出口、Clash 扩展脚本或在两种固定代理之间切换；不负责部署 VPS 或修改正在运行的 Clash 配置。
---

# AI 固定出口扩展脚本

用户提供固定代理参数时，输出一份完整的 Clash Verge Rev 扩展脚本。用户指定 Skill 根目录并要求可直接复制使用时，还要把填好参数的完整脚本保存为根目录的 `extension.js`，交付文件路径；不要只在回复中贴代码。除非用户明确要求，不操作本机 Clash、订阅或 VPS。

## 选择模板

- VPS 已运行 Shadowsocks：读取 [assets/extension-ss.js](assets/extension-ss.js)，填写服务器地址、端口、加密方法和密码。只有服务端支持 UDP 时才保留 `udp: true`。
- 第三方静态 SOCKS5：读取 [assets/extension-socks5.js](assets/extension-socks5.js)，填写服务器地址、端口、用户名和密码。只有供应商确认支持 UDP 时才把 `udp` 改为 `true`。

缺少连接必需参数时询问用户，不猜测密码、端口或加密方法。用户只说“VPS”但没有说明代理协议时，先确认已部署 Shadowsocks；VPS 的 IP 地址本身不是 Clash 代理节点。不要把真实凭据写回 Skill 模板或测试文件；用户明确要求保存成品脚本时，可写入根目录的 `extension.js`，并确保该文件被 Git 忽略，避免误提交凭据。把用户字符串按 JavaScript/JSON 字符串规则转义，避免引号、反斜杠或换行破坏脚本。

## 生成规则

1. 保持模板的 `main(config)` 入口，只替换 `CUSTOM_PROXY` 中的占位值和用户明确要求调整的 AI 域名。输出完整脚本，不只输出节点对象或补丁。
2. 脚本只添加一个固定节点，并把明确的 AI 域名规则放在现有规则之前；不添加 `MATCH`、普通流量规则、进程规则或 `mcp.scys.com` 特例。未命中的流量继续匹配用户原有规则。
3. 默认域名清单覆盖 Claude、OpenAI/ChatGPT 和 Gemini 的明确服务域名。不要为覆盖 Gemini 而加入整个 `google.com`，也不要默认把 GitHub、Cloudflare 或共享认证域名导入固定出口。
4. 固定节点名称与原配置中的节点或策略组冲突时，让用户换名并同步修改脚本；不得静默删除原节点。重复应用同一脚本时，不应累加同名节点或相同规则。
5. 用户提供了现有扩展脚本并要求在其基础上生成时，将注入逻辑合并进它唯一的 `main`，保留原有 DNS、Hosts、策略组、例外与普通流量路由。不要把两个 `main` 简单拼接，也不要用模板覆盖现有脚本。

## 验证与交付

检查输出脚本的语法、必填参数和规则顺序。使用者加载后，应在 Clash 连接记录中分别确认：目标 AI 域名命中固定节点；普通网站仍按原订阅规则走机场节点或直连。`udp: false` 的 SOCKS5 无法保证 AI 的 UDP 请求也走固定出口，应明确告知这一限制；不要伪称所有协议均已固定。仅凭服务器地址固定也不能证明最终出口 IP 固定，需以实际出口验证。
