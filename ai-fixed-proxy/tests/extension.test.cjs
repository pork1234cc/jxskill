const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const assets = path.resolve(__dirname, '../assets');
const originalRules = [
  'DOMAIN,mcp.scys.com,DIRECT',
  'DOMAIN-SUFFIX,github.com,Proxies',
  'MATCH,Final',
];

function load(mode) {
  const file = mode === 'ss' ? 'extension-ss.js' : 'extension-socks5.js';
  let script = fs.readFileSync(path.join(assets, file), 'utf8');
  const values = {
    REPLACE_WITH_SERVER: 'fixed.example.test',
    REPLACE_WITH_USERNAME: 'name"\\\n测试',
    REPLACE_WITH_PASSWORD: 'pw"\\\n测试',
  };
  for (const [placeholder, value] of Object.entries(values)) {
    script = script.replace(`"${placeholder}"`, JSON.stringify(value));
  }
  const context = vm.createContext({});
  vm.runInContext(script, context, { filename: file });
  return context;
}

function baseConfig() {
  return {
    proxies: [{ name: '机场节点', type: 'ss' }],
    'proxy-groups': [{ name: 'Final', type: 'select', proxies: ['机场节点', 'DIRECT'] }],
    rules: [...originalRules],
    dns: { enable: true },
  };
}

for (const mode of ['ss', 'socks5']) {
  test(`${mode}: AI 规则优先，原规则与其他配置保留`, () => {
    const context = load(mode);
    const input = baseConfig();
    const groups = input['proxy-groups'];
    const dns = input.dns;
    const output = context.main(input);

    assert.equal(output, input);
    assert.equal(output.proxies.length, 2);
    assert.equal(output.proxies[1].name, 'MyFixedProxy');
    assert.equal(output.proxies[1].type, mode);
    assert.equal(output.proxies[1].password, 'pw"\\\n测试');
    assert.equal(output['proxy-groups'], groups);
    assert.equal(output.dns, dns);
    assert.equal(output.rules.length, 13 + originalRules.length);
    assert.equal(output.rules[0], 'DOMAIN-SUFFIX,anthropic.com,MyFixedProxy');
    assert.equal(output.rules[12], 'DOMAIN-SUFFIX,generativelanguage.googleapis.com,MyFixedProxy');
    assert.equal(JSON.stringify(output.rules.slice(13)), JSON.stringify(originalRules));
    assert.equal(output.rules.at(-1), 'MATCH,Final');
  });

  test(`${mode}: 重复执行不会累加节点或规则`, () => {
    const context = load(mode);
    const output = context.main(baseConfig());
    const firstRules = JSON.stringify(output.rules);
    context.main(output);
    assert.equal(output.proxies.length, 2);
    assert.equal(JSON.stringify(output.rules), firstRules);
  });

  test(`${mode}: 同名节点或策略组冲突时停止`, () => {
    const context = load(mode);
    const nodeConflict = baseConfig();
    nodeConflict.proxies.push({ name: 'MyFixedProxy', type: 'direct' });
    assert.throws(() => context.main(nodeConflict), /冲突/);

    const groupConflict = baseConfig();
    groupConflict['proxy-groups'].push({ name: 'MyFixedProxy', type: 'select' });
    assert.throws(() => context.main(groupConflict), /冲突/);
  });

  test(`${mode}: 未填写参数或缺少原规则时停止`, () => {
    const source = fs.readFileSync(path.join(assets, mode === 'ss' ? 'extension-ss.js' : 'extension-socks5.js'), 'utf8');
    const context = vm.createContext({});
    vm.runInContext(source, context);
    assert.throws(() => context.main(baseConfig()), /请先填写/);
    assert.throws(() => load(mode).main({}), /rules/);
  });
}

test('SOCKS5 默认不声称支持 UDP；Shadowsocks 模板按文档启用 UDP', () => {
  assert.equal(load('socks5').main(baseConfig()).proxies[1].udp, false);
  assert.equal(load('ss').main(baseConfig()).proxies[1].udp, true);
});
