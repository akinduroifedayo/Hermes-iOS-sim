import { afterAll, beforeAll, expect, test } from "bun:test";
import { copyFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { request } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { spawn, type ChildProcess } from "node:child_process";

// Exercise the checked-in dependency, never a global install or an npm "latest".
// The CLI boots a simulator before listening. On Linux, mount its real exported
// middleware with the CLI's basePath/device/proxyHelpers options instead.
const scratch = mkdtempSync(join(tmpdir(), "hios-host-rebinding-"));
const manifests = new URL("../../../plugin/dependencies/", import.meta.url);
let child: ChildProcess | undefined;
let port: number;
let stderr = "";

beforeAll(async () => {
  const expected = JSON.parse(readFileSync(new URL("package.json", manifests), "utf8")).dependencies["serve-sim"];
  let stockRoot = process.env.HERMES_IOS_STOCK_ROOT;
  if (!stockRoot) {
    for (const file of ["package.json", "package-lock.json"]) copyFileSync(new URL(file, manifests), join(scratch, file));
    const install = Bun.spawnSync(["npm", "ci", "--ignore-scripts", "--registry=https://registry.npmjs.org",
      "--cache", join(scratch, "npm-cache"), "--no-audit", "--no-fund"], {
      cwd: scratch, env: { PATH: process.env.PATH, HOME: scratch, TMPDIR: scratch }, timeout: 120_000,
    });
    expect({ exitCode: install.exitCode, stderr: install.stderr.toString() }).toMatchObject({ exitCode: 0 });
    stockRoot = join(scratch, "node_modules/serve-sim");
  }
  stockRoot = resolve(stockRoot);
  expect(JSON.parse(readFileSync(join(stockRoot, "package.json"), "utf8")).version).toBe(expected);
  console.log(`HOST_REBINDING serve-sim@${expected} (${process.env.HERMES_IOS_STOCK_ROOT ? "explicit stock root" : "checked-in npm ci tree"})`);
  const server = join(scratch, "server.mjs");
  writeFileSync(server, `
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
const require = createRequire(process.argv[2]);
const { simMiddleware } = require('serve-sim/middleware');
const middleware = simMiddleware({ basePath: '/', device: '00000000-0000-0000-0000-000000000001', proxyHelpers: true });
const server = createServer((req, res) => {
  middleware(req, res, async () => { res.writeHead(404); res.end('Not found'); })
    .catch(err => { console.error(err); res.writeHead(500); res.end('Middleware failed'); });
});
server.on('clientError', (_err, socket) => socket.destroy());
server.on('upgrade', (req, socket, head) => {
  socket.on('error', () => {});
  middleware.handleUpgrade(req, socket, head);
});
server.listen(0, '127.0.0.1', () => {
  middleware.setServerPort?.(server.address().port);
  console.log(JSON.stringify({ port: server.address().port }));
});
`);
  child = spawn("node", [server, join(stockRoot, "package.json")], {
    cwd: scratch, env: { PATH: process.env.PATH, HOME: scratch, TMPDIR: scratch }, stdio: ["ignore", "pipe", "pipe"],
  });
  child.stderr!.on("data", chunk => { stderr += chunk; });
  port = await new Promise<number>((resolvePort, reject) => {
    const timer = setTimeout(() => reject(new Error(`server startup timeout: ${stderr}`)), 10_000);
    const lines = createInterface({ input: child!.stdout! });
    lines.on("line", line => {
      if (!line.startsWith('{"port":')) return;
      clearTimeout(timer);
      lines.close();
      resolvePort(JSON.parse(line).port);
    });
    child!.once("error", err => { clearTimeout(timer); reject(err); });
    child!.once("exit", code => { clearTimeout(timer); reject(new Error(`server exited ${code}: ${stderr}`)); });
  });
}, 150_000);

afterAll(async () => {
  if (child && child.exitCode === null && child.signalCode === null) {
    const exited = new Promise<void>(done => child!.once("exit", () => done()));
    child.kill("SIGKILL");
    await exited;
  }
  rmSync(scratch, { recursive: true, force: true });
});

type Response = { status: number; body: string };
function probe(path: string, foreign: boolean, options: { exec?: boolean; upgrade?: boolean; token?: string } = {}): Promise<Response> {
  const host = `${foreign ? "evil.example" : "127.0.0.1"}:${port}`;
  return new Promise((resolveResponse, reject) => {
    const req = request({ hostname: "127.0.0.1", port, path, method: options.exec ? "POST" : "GET", headers: {
      Host: host, Origin: `http://${host}`,
      ...(options.exec ? { "Content-Type": "application/json", Authorization: `Bearer ${options.token ?? "no-page-token"}` } : {}),
      ...(options.upgrade ? { Connection: "Upgrade", Upgrade: "websocket", "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": "aGlvcy1ob3N0LXByb2JlIQ==" } : {}),
    } }, res => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", chunk => { body += chunk; });
      res.on("end", () => resolveResponse({ status: res.statusCode!, body }));
      res.on("error", reject);
    });
    req.on("upgrade", (res, socket) => {
      // No WS frames/commands are sent, even on the vulnerable version.
      socket.end(() => socket.destroy());
      resolveResponse({ status: res.statusCode!, body: "" });
    });
    req.on("error", reject);
    req.setTimeout(5000, () => req.destroy(new Error(`request timeout: ${path}`)));
    req.end(options.exec ? "{}" : undefined); // No command can run, including on 0.1.48.
  });
}

function config(body: string): Record<string, unknown> {
  const match = body.match(/window\.__SIM_PREVIEW__=(\{.*?\})<\/script>/);
  return match ? JSON.parse(match[1]!) : {};
}

test("foreign Host cannot read preview HTML or its execution token", async () => {
  const response = await probe("/", true);
  console.log("foreign preview", { status: response.status, tokenInPage: !!config(response.body).execToken });
  expect(response.status).toBe(403);
  expect(response.body).not.toContain("window.__SIM_PREVIEW__");
  expect(config(response.body)).not.toHaveProperty("execToken");
});

test("foreign Host+Origin cannot authenticate POST /exec or upgrade /exec-ws", async () => {
  const page = await probe("/", true);
  const token = config(page.body).execToken as string | undefined;
  const http = await probe("/exec", true, { exec: true, token });
  const ws = await probe("/exec-ws", true, { upgrade: true });
  console.log("foreign exec", { http: http.status, ws: ws.status, body: http.body });
  expect(http.status).toBe(403);
  expect(http.body).not.toContain('"stdout":"');
  expect(ws.status).toBe(403);
});

test("loopback Host still receives the preview without an execution token", async () => {
  const page = await probe("/", false);
  console.log("loopback preview", { status: page.status, tokenInPage: !!config(page.body).execToken });
  expect(page.status).toBe(200);
  expect(page.body).toContain("window.__SIM_PREVIEW__");
  expect(config(page.body)).not.toHaveProperty("execToken");
});

test("execution stays disabled even with loopback Host+Origin", async () => {
  const http = await probe("/exec", false, { exec: true });
  // The loopback WS also carries non-shell preview controls. Its handshake can
  // succeed; the command frame must explicitly report disabled execution.
  const ws = await new Promise<Record<string, unknown>>((resolveResponse, reject) => {
    const socket = new WebSocket(`ws://127.0.0.1:${port}/exec-ws`, {
      headers: { Origin: `http://127.0.0.1:${port}` },
    });
    const timer = setTimeout(() => { socket.close(); reject(new Error("exec WS response timeout")); }, 3000);
    socket.onopen = () => socket.send("{}");
    socket.onmessage = event => {
      const message = JSON.parse(String(event.data));
      if (message.ready) socket.send(JSON.stringify({ id: 1, command: "true" }));
      if (message.id === 1) { clearTimeout(timer); socket.close(); resolveResponse(message); }
    };
    socket.onerror = () => { clearTimeout(timer); socket.close(); reject(new Error("exec WS failed")); };
  });
  console.log("loopback exec", { http: http.status, ws });
  expect(http.status).toBe(403);
  expect(ws).toMatchObject({ id: 1, stdout: "", exitCode: 126, error: "exec disabled; start with --allow-exec" });
});
