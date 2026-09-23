// Clash Verge Rev 全局扩展脚本：AI 域名使用固定 SOCKS5 出口。
const PROXY_NAME = "MyFixedProxy";

const CUSTOM_PROXY = {
  name: PROXY_NAME,
  type: "socks5",
  server: "REPLACE_WITH_SERVER",
  port: 1080,
  username: "REPLACE_WITH_USERNAME",
  password: "REPLACE_WITH_PASSWORD",
  udp: false
};

// 仅列出需要固定出口的明确 AI 服务域名。
const AI_TARGETS = [
  ["DOMAIN-SUFFIX", "anthropic.com"],
  ["DOMAIN-SUFFIX", "anthropic.ai"],
  ["DOMAIN-SUFFIX", "claude.ai"],
  ["DOMAIN-SUFFIX", "claude.com"],
  ["DOMAIN-SUFFIX", "openai.com"],
  ["DOMAIN-SUFFIX", "chatgpt.com"],
  ["DOMAIN-SUFFIX", "oaistatic.com"],
  ["DOMAIN-SUFFIX", "oaiusercontent.com"],
  ["DOMAIN-SUFFIX", "oaistatsig.com"],
  ["DOMAIN-SUFFIX", "sora.com"],
  ["DOMAIN", "gemini.google.com"],
  ["DOMAIN", "aistudio.google.com"],
  ["DOMAIN-SUFFIX", "generativelanguage.googleapis.com"]
];

function main(config) {
  if (!config || !Array.isArray(config.rules)) {
    throw new Error("原配置需要包含 rules 数组");
  }
  if (CUSTOM_PROXY.server === "REPLACE_WITH_SERVER" ||
      CUSTOM_PROXY.username === "REPLACE_WITH_USERNAME" ||
      CUSTOM_PROXY.password === "REPLACE_WITH_PASSWORD" ||
      !Number.isInteger(CUSTOM_PROXY.port) ||
      CUSTOM_PROXY.port < 1 || CUSTOM_PROXY.port > 65535) {
    throw new Error("请先填写有效的 SOCKS5 节点参数");
  }

  config.proxies = Array.isArray(config.proxies) ? config.proxies : [];
  const groups = Array.isArray(config["proxy-groups"]) ? config["proxy-groups"] : [];
  if (groups.some(group => group && group.name === PROXY_NAME)) {
    throw new Error("固定节点名称与原策略组冲突");
  }

  const sameName = config.proxies.filter(proxy => proxy && proxy.name === PROXY_NAME);
  if (sameName.length > 1 ||
      (sameName.length === 1 && !Object.keys(CUSTOM_PROXY).every(
        key => sameName[0][key] === CUSTOM_PROXY[key]
      ))) {
    throw new Error("固定节点名称与原节点冲突");
  }
  if (sameName.length === 0) {
    config.proxies.push(CUSTOM_PROXY);
  }

  const aiRules = AI_TARGETS.map(item => `${item[0]},${item[1]},${PROXY_NAME}`);
  config.rules = [
    ...aiRules,
    ...config.rules.filter(rule => !aiRules.includes(rule))
  ];
  return config;
}
