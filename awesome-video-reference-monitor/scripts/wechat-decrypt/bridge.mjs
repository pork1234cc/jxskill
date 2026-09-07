/**
 * 通过本地 Chromium 加载微信兼容 WASM，并输出反转后的 128 KiB ISAAC64 密钥流。
 * decode_key 只从 stdin JSON 读取，禁止作为命令行参数或日志输出。
 */

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const ROOT = dirname(fileURLToPath(import.meta.url));
const KEYSTREAM_SIZE = 131072;
const RESOURCE_MAP = new Map([
  ["/worker.html", [join(ROOT, "worker.html"), "text/html; charset=utf-8"]],
  [
    "/vendor/wasm_video_decode.js",
    [join(ROOT, "vendor", "wasm_video_decode.js"), "text/javascript; charset=utf-8"],
  ],
  [
    "/vendor/wasm_video_decode.wasm",
    [join(ROOT, "vendor", "wasm_video_decode.wasm"), "application/wasm"],
  ],
]);

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function parseDecodeKey(input) {
  let payload;
  try {
    payload = JSON.parse(input);
  } catch {
    throw new Error("stdin 必须是 JSON");
  }
  const decodeKey = payload?.decode_key;
  if (typeof decodeKey !== "string" || !/^\d+$/.test(decodeKey)) {
    throw new Error("decode_key 必须是非空数字字符串");
  }
  return decodeKey;
}

function createAssetServer() {
  return createServer(async (request, response) => {
    const pathname = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
    const resource = RESOURCE_MAP.get(pathname);
    if (!resource) {
      response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      response.end("Not Found");
      return;
    }
    try {
      const body = await readFile(resource[0]);
      response.writeHead(200, {
        "Cache-Control": "no-store",
        "Content-Length": body.length,
        "Content-Type": resource[1],
      });
      response.end(body);
    } catch {
      response.writeHead(500, { "Content-Type": "text/plain; charset=utf-8" });
      response.end("Asset Error");
    }
  });
}

async function listenLocal(server) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("无法获取本地资源服务端口");
  }
  return address.port;
}

async function closeServer(server) {
  if (!server.listening) {
    return;
  }
  await new Promise((resolve) => server.close(resolve));
}

async function generateKeystream(decodeKey) {
  const server = createAssetServer();
  let browser;
  try {
    const port = await listenLocal(server);
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.hostname === "127.0.0.1" && url.port === String(port)) {
        await route.continue();
      } else {
        await route.abort("blockedbyclient");
      }
    });
    await page.goto(`http://127.0.0.1:${port}/worker.html`, {
      waitUntil: "load",
      timeout: 60000,
    });
    await page.waitForFunction(
      () => typeof Module !== "undefined" && typeof Module.WxIsaac64 !== "undefined",
      undefined,
      { timeout: 60000 },
    );
    const base64 = await page.evaluate(
      async (key) => window.generateKeystream(key),
      decodeKey,
    );
    if (Buffer.from(base64, "base64").length !== KEYSTREAM_SIZE) {
      throw new Error("生成的密钥流长度无效");
    }
    return base64;
  } finally {
    await browser?.close();
    await closeServer(server);
  }
}

async function main() {
  let decodeKey = "";
  try {
    decodeKey = parseDecodeKey(await readStdin());
    const keystream = await generateKeystream(decodeKey);
    process.stdout.write(`${JSON.stringify({ keystream, size: KEYSTREAM_SIZE })}\n`);
  } catch (error) {
    const rawMessage = error instanceof Error ? error.message : "未知错误";
    const safeMessage = decodeKey
      ? rawMessage.replace(new RegExp(`(?<!\\d)${decodeKey}(?!\\d)`, "g"), "[REDACTED]")
      : rawMessage;
    process.stderr.write(`视频号密钥流生成失败: ${safeMessage}\n`);
    process.exitCode = 1;
  }
}

await main();
