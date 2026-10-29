import { describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { rmSync } from "node:fs";
import { join } from "node:path";
import { assertHomesContained, fixtureEnv, fixtureRoot, HOMES, requireContained, runHostChild } from "./host-isolation";

// Explicit, already-qualified inputs only. Never discover personal npm caches,
// install dependencies, open a chat, start a server, or mutate a simulator here.
const stockRoot = process.env.HERMES_IOS_STOCK_ROOT;
const historicalStockRoot = process.env.HERMES_IOS_HISTORICAL_STOCK_ROOT;
const hostRoot = process.env.HERMES_IOS_HOST_SOURCE;
const python = process.env.HERMES_IOS_HOST_PYTHON;
const evidence = new URL("../../fixtures/stock-runtime/", import.meta.url);
const receipt = (name: string) => Bun.file(new URL(name, evidence)).json();
const sha256 = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex");
function run(argv: string[], env: Record<string, string | undefined> = {}) {
  const result = Bun.spawnSync(argv, { env: { ...process.env, ...env }, timeout: 60000 });
  const stdout = result.stdout.toString();
  const stderr = result.stderr.toString();
  expect({ exitCode: result.exitCode, stderr }).toMatchObject({ exitCode: 0 });
  return stdout;
}

describe("0.1.48 retained observations (historical, not a new live security pass)", () => {
  test("registry identity includes every published file and does not claim SLSA verification", async () => {
    const provenance = await receipt("stock-provenance.json");
    const signatures = await receipt("registry-signatures.json");
    expect(provenance.version).toBe("0.1.48");
    expect(provenance.integrity_verified).toBe("sha512-Kfg++zbtrRHYEOGAVk/9ftZamp40Qpz8WgP3idWaHoGaCJ01TrEZeq0UnWIsrGSaGhGgi0ytxMOsrEo3H/bFiA==");
    expect(provenance.files).toHaveLength(provenance.dist.fileCount);
    expect(provenance.dist.fileCount).toBe(67);
    expect(new Set(provenance.files.map((file: { path: string }) => file.path)).size).toBe(67);
    expect(provenance.files.every((file: { installed_matches: boolean }) => file.installed_matches)).toBe(true);
    expect(signatures.at_least_one_registry_signature_valid).toBe(true);
    expect(signatures.slsa_attestation_verified).toBe(false);
  });

  test("exec rejects foreign/null Origin and missing token; absent Origin is NOT denied", async () => {
    const observations = await receipt("security-probes.json");
    const statuses = Object.fromEntries(observations.http.map((probe: { label: string; status: number }) => [probe.label, probe.status]));
    expect(statuses).toEqual({
      "null-origin": 403, "other-origin": 403, "same-origin-no-token": 401,
      "same-origin-empty-body": 400, "wrong-content-type": 415, "no-origin-empty-body": 400,
    });
    expect(observations.token_obtained_from_own_preview).toBe(true);
    expect(observations.http.every((probe: { body: string }) => !JSON.parse(probe.body).stdout)).toBe(true);
  });

  test("an exec WS handshake is not shell authorization; stream/HID lack authentication", async () => {
    const observations = await receipt("security-probes.json");
    expect(observations.ws.filter((probe: { route: string; error?: string }) => probe.route === "/exec-ws" && probe.error).map((probe: { origin: string }) => probe.origin)).toEqual(["null", "http://example.invalid"]);
    expect(observations.ws.some((probe: { route: string; authSupplied?: boolean; result?: string }) => probe.route === "/exec-ws" && probe.authSupplied === false && probe.result === "open; no messages sent")).toBe(true);
    expect(observations.ws.some((probe: { route: string; origin: string; authSupplied?: boolean; result?: string }) => probe.route.endsWith("/ws") && probe.origin === "null" && probe.authSupplied === false && probe.result === "open; no messages sent")).toBe(true);
    expect(observations.stream.status).toBe(200);
    expect(observations.stream.headers["access-control-allow-origin"]).toBe("*");
    const auth = await receipt("exec-ws-auth-static.json");
    expect(auth.M3).toContain('typeof W.token==="string"&&H3(W.token,Z.execToken)');
    expect(auth.M3).toContain('typeof W.command!=="string"');
    expect(auth.M3).toContain("maxBuffer:16777216");
  });
});

describe.skipIf(!historicalStockRoot)("explicit historical 0.1.48 artifact (not the current pin)", () => {
  test("every historical file still matches the retained 0.1.48 receipt", async () => {
    const provenance = await receipt("stock-provenance.json");
    for (const file of provenance.files) {
      const bytes = new Uint8Array(await Bun.file(join(historicalStockRoot!, file.path)).arrayBuffer());
      expect({ path: file.path, size: bytes.length, hash: sha256(bytes) }).toEqual({ path: file.path, size: file.bytes, hash: file.sha256 });
    }
    const pkg = await Bun.file(join(historicalStockRoot!, "package.json")).json();
    expect(pkg.version).toBe("0.1.48");
    expect(pkg.engines.node).toBe(">=20");
    expect(pkg.bin["serve-sim"]).toBe("dist/serve-sim.js");
    expect(pkg.scripts).not.toHaveProperty("install");
    expect(pkg.scripts).not.toHaveProperty("postinstall");
    expect(pkg.dependencies).toEqual({ "inspect-webkit": "^0.0.5", sonner: "^2.0.7", ws: "^8.21.0" });
  });
});

describe.skipIf(!stockRoot)("explicit current stock artifact qualification", () => {
  test("real CLI exposes exact-device and loopback flags with shell execution opt-in", () => {
    const help = run(["node", join(stockRoot!, "dist/serve-sim.js"), "--help"]);
    for (const flag of ["[devices...]", "--host <addr>", "--port <port>", "--fit", "--panes <panes>"]) expect(help).toContain(flag);
    expect(help).toContain('"127.0.0.1"');
    expect(help).toContain("--allow-exec");
    expect(help).toContain("Off by");
    expect(help).toContain("default; other preview tools");
    expect(run(["node", join(stockRoot!, "dist/serve-sim.js"), "--version"]).trim()).toBe("0.1.49-beta.139.1");
  });

  test.skipIf(process.platform !== "darwin")("published native addon loads in the actual Node runtime without invoking capture/HID", () => {
    const output = run(["node", "-e", 'const m=require(process.argv[1]); console.log(JSON.stringify({node:process.version,arch:process.arch,exports:Object.keys(m).sort()}))', join(stockRoot!, "dist/native/serve-sim-native.node")]);
    const native = JSON.parse(output);
    expect(native.exports).toEqual(["SimCapture", "SimHID", "axDescribe", "axFrontmost"]);
    expect(Number(native.node.slice(1).split(".")[0])).toBeGreaterThanOrEqual(20);
    console.log("NATIVE_LOAD_OBSERVATION", output.trim());
  });
});

describe("archived-host launch guard", () => {
  test("refuses an inherited, credential-bearing or escaping env and cwd before any launch", () => {
    const root = fixtureRoot("hermes-ios-host-guard-");
    try {
      const env = fixtureEnv(root);
      requireContained(env, root, join(root, "cwd"));
      expect(() => requireContained({ ...process.env } as Record<string, string>, root, join(root, "cwd"))).toThrow(/refusing host child/);
      expect(() => requireContained({ ...env, OPENAI_API_KEY: "x" }, root, join(root, "cwd"))).toThrow(/OPENAI_API_KEY is not an allowlisted/);
      expect(() => requireContained({ ...env, HOME: "/Users" }, root, join(root, "cwd"))).toThrow(/HOME=/);
      expect(() => requireContained({ ...env, XDG_CACHE_HOME: "/tmp" }, root, join(root, "cwd"))).toThrow(/XDG_CACHE_HOME=/);
      expect(() => requireContained(env, root, process.cwd())).toThrow(/cwd=/);
      expect(() => requireContained({ PATH: env.PATH }, root, join(root, "cwd"))).toThrow(/HOME=/);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe.skipIf(!hostRoot || !python)("real public backend implementation in a fresh home (SOURCE-RUNTIME only)", () => {
  test("registers and invokes a tool, persists plugin data, cancels a supervised task, and unloads", () => {
    // Every host child: allowlisted env, every home, temp dir and cwd under one fixture root.
    const root = fixtureRoot("hermes-ios-host-contract-");
    const env = fixtureEnv(root, { HERMES_IOS_HOST_SOURCE: hostRoot! });
    const result = runHostChild([python!, "-c", HOMES + String.raw`
import asyncio, json, os, pathlib, sys, tempfile
sys.path.insert(0, os.environ["HERMES_IOS_HOST_SOURCE"])
# Override home before any Hermes import. No copied config, credential or session.
with tempfile.TemporaryDirectory(prefix="hermes-ios-u1-", dir=os.environ["TMPDIR"]) as root:
    os.environ["HERMES_HOME"] = root
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from tools.registry import registry
    from tools.preview_tool import PREVIEW_SCHEMA, _handle_preview
    from tools.open_preview_tool import _normalize_target
    from tools import desktop_ui
    manager = PluginManager(scope_key=root)
    ctx = PluginContext(PluginManifest(name="ios-u1-contract-probe", source="user"), manager)
    name = "ios_u1_contract_probe"
    handle = ctx.register_tool(name=name, toolset="ios_u1_contract_probe", schema={"name": name, "parameters": {"type":"object"}}, handler=lambda args, **kw: json.dumps({"echo":args["value"]}))
    assert handle is not None
    entry = registry.get_entry(name, scope=manager.scope_key)
    assert json.loads(entry.handler({"value":"actual-registry"}))["echo"] == "actual-registry"
    ctx.state.set("qualification", {"pin":"0.1.49-beta.139.1"})
    assert ctx.state.get("qualification") == {"pin":"0.1.49-beta.139.1"}
    import hashlib
    expected_namespace = "agent-plugin-ios-u1-contract-probe-" + hashlib.sha256(b"ios-u1-contract-probe").hexdigest()[:8]
    assert ctx.state.data_dir == pathlib.Path(root)/"plugin-data"/expected_namespace
    assert ctx.state.path.is_file()
    unloaded = []
    ctx.on_unload(lambda: unloaded.append("actual-cleanup"))
    async def exercise():
        started = asyncio.Event()
        async def worker():
            started.set()
            await asyncio.Event().wait()
        task = ctx.spawn_task(worker(), name="ios-u1-owned-task")
        await started.wait()
        assert manager.unload("ios-u1-contract-probe")
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.cancelled()
    asyncio.run(exercise())
    assert unloaded == ["actual-cleanup"]
    assert registry.get_entry(name, scope=manager.scope_key) is None
    assert PREVIEW_SCHEMA["name"] == "desktop_preview"
    assert PREVIEW_SCHEMA["parameters"]["properties"]["action"]["enum"] == ["open","close","read"]
    assert _normalize_target("127.0.0.1:52028") == "http://127.0.0.1:52028"
    # No fake emitter: the real tool must honestly reject a non-Desktop process.
    assert not desktop_ui.available()
    rejected = json.loads(_handle_preview({"action":"open","url":"http://127.0.0.1:52028"}))
    assert "error" in rejected and "desktop" in rejected["error"].lower()
    print(json.dumps({"homes":resolved_homes(), "boundary":"source-runtime-not-installed-host", "python":sys.executable, "plugins_module":sys.modules["hermes_cli.plugins"].__file__, "tool_invocation":"observed", "plugin_data_readback":"observed", "task_cancelled":True, "unload_callback":"observed", "tool_removed":True, "preview_without_emitter":rejected}))
assert not pathlib.Path(root).exists()
print("OWNED_TEMP_HOME_REMOVED")
`], { root, env, cwd: join(root, "cwd"), allow: ["HERMES_IOS_HOST_SOURCE"] });
    try {
      const output = result.stdout.toString();
      expect({ exitCode: result.exitCode, stderr: result.stderr.toString() }).toMatchObject({ exitCode: 0 });
      const observation = JSON.parse(output.split("\n").find((line) => line.includes('"boundary"')) ?? "null");
      assertHomesContained(observation.homes, root, env);
      expect(observation.boundary).toBe("source-runtime-not-installed-host");
      expect(output).toContain("OWNED_TEMP_HOME_REMOVED");
      console.log("BACKEND_OBSERVATION", output.trim());
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
