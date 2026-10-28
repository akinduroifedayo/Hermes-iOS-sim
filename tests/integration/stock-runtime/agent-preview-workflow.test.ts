import { describe, expect, test } from "bun:test";
import { rmSync } from "node:fs";
import { join } from "node:path";
import { selectPython } from "../../../scripts/python-tests";
import { assertHomesContained, fixtureEnv, fixtureRoot, HOMES, runHostChild } from "./host-isolation";

// Local producer contract with fake native dispatch/server only. This cannot
// certify LLM compliance, installed Desktop routing, annotation Send or pixels.
const root = new URL("../../../", import.meta.url).pathname;
const host = process.env.HERMES_IOS_HOST_SOURCE;
const python = process.env.HERMES_IOS_HOST_PYTHON;
function run(python: string, code: string, env: Record<string, string> = {}) {
  const result = Bun.spawnSync([python, "-c", code], {
    cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", ...env }, timeout: 60000,
  });
  expect({ code: result.exitCode, stderr: result.stderr.toString(), stdout: result.stdout.toString() }).toMatchObject({ code: 0 });
  return JSON.parse(result.stdout.toString().trim());
}
// Worktree paths come from the env, so the producer runs from any cwd.
const producer = String.raw`
import asyncio, json, os, sys
sys.path[:0] = [os.path.join(os.environ['HERMES_IOS_WORKTREE'], 'tests/agent'), os.environ['HERMES_IOS_WORKTREE']]
from test_plugin_api_tool_resolution import CoordinatedRunTests
async def exercise():
    fixture = CoordinatedRunTests()
    fixture.setUp()
    try:
        first = await fixture.backend.execute(fixture.args)
        second = await fixture.backend.execute(fixture.args)
        for result in [first, second]:
            assert result['ok'], result
            assert result['target'] == result['receipt']['target']
            assert result['outcomes'] == result['receipt']['outcomes']
            assert result['receipt']['operation_id']
            assert result['receipt']['build_attempt_id']
            assert result['preview_action']['tool'] == 'desktop_preview'
            assert result['preview_action']['arguments'] == {'action':'open','url':result['preview_url']}
            assert result['preview_action']['instruction'] == 'Call this public tool in the same originating agent turn before replying; this is an open request, not visible-pixel proof.'
            assert result['preview']['open_requested'] is False
            assert result['preview']['visible_pixels'] == 'unknown'
        assert first['preview_url'] == second['preview_url']
        assert fixture.manager.starts == 1
        fixture.failure = True
        failed = await fixture.backend.execute(fixture.args)
        assert not failed['ok']
        assert 'preview_action' not in failed and 'preview_url' not in failed
        print(json.dumps({'boundary':'mock-native-local-result-contract', 'first':first, 'second':second,'failure':failed}))
    finally:
        fixture.tearDown()
asyncio.run(exercise())
`;

describe("executable run-result preview contract (not installed workflow proof)", () => {
  test("exact successful result supplies a public same-turn action; repeated explicit run reuses URL; failure supplies none", () => {
    const output = run(process.env.HERMES_IOS_TEST_PYTHON || selectPython(), producer,
                       { HERMES_IOS_WORKTREE: root });
    expect(output.boundary).toBe("mock-native-local-result-contract");
    expect(output.first.preview_action.arguments.url).toBe(output.first.preview_url);
    expect(output.second.receipt.build_attempt_id).not.toBe(output.first.receipt.build_attempt_id);
    // A scripted trace oracle, NOT execution of an LLM or an installed host.
    // Omitting the open, using a different URL/turn or closing first must fail.
    const requireOpen = (trace: Array<{ tool: string; turn: string; arguments: unknown }>) => {
      expect(trace).toEqual([{ tool: output.first.preview_action.tool, turn: "originating-run", arguments: output.first.preview_action.arguments }]);
    };
    requireOpen([{ tool: "desktop_preview", turn: "originating-run", arguments: output.first.preview_action.arguments }]);
    expect(() => requireOpen([])).toThrow();
    expect(() => requireOpen([{ tool: "desktop_preview", turn: "later-user-request", arguments: output.first.preview_action.arguments }])).toThrow();
    expect(() => requireOpen([{ tool: "desktop_preview", turn: "originating-run", arguments: { action: "open", url: "http://127.0.0.1:49999" } }])).toThrow();
    expect(() => requireOpen([{ tool: "desktop_preview", turn: "originating-run", arguments: { action: "close" } }])).toThrow();
  });
});

describe.skipIf(!host || !python)("current public host SOURCE contract in isolated home", () => {
  test("producer arguments satisfy real registered preview schema; real non-Desktop rejection stays separate from native success", () => {
    // Every host child: allowlisted env, every home, temp dir and cwd under one fixture root.
    const fixture = fixtureRoot("hermes-ios-agent-preview-");
    const env = fixtureEnv(fixture, { HERMES_IOS_HOST_SOURCE: host!, HERMES_IOS_WORKTREE: root });
    const result = runHostChild([python!, "-c", HOMES + producer.replace("print(json.dumps({'boundary':'mock-native-local-result-contract', 'first':first, 'second':second,'failure':failed}))", String.raw`
        import ast, os, pathlib, tempfile
        from jsonschema import validate
        with tempfile.TemporaryDirectory(prefix='u9-public-', dir=os.environ['TMPDIR']) as home:
            os.environ['HERMES_HOME'] = home
            sys.path.insert(0, os.environ['HERMES_IOS_HOST_SOURCE'])
            from tools.preview_tool import PREVIEW_SCHEMA, _handle_preview
            from tools.registry import registry
            from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
            from plugin import register
            from plugin.schemas import IOS_ITERATION_SCHEMA
            manager = PluginManager(scope_key=home)
            ctx = PluginContext(PluginManifest(name='ios-u9-contract-probe', source='user'), manager)
            register(ctx)
            entry = registry.get_entry('ios_iteration', scope=manager.scope_key)
            assert entry.schema == IOS_ITERATION_SCHEMA
            assert entry.description == IOS_ITERATION_SCHEMA['description']
            assert 'same originating agent turn' in entry.description
            assert manager.unload('ios-u9-contract-probe')
            assert registry.get_entry('desktop_preview').schema == PREVIEW_SCHEMA
            validate(first['preview_action']['arguments'], PREVIEW_SCHEMA['parameters'])
            rejected = json.loads(_handle_preview(first['preview_action']['arguments']))
            assert 'error' in rejected and 'desktop' in rejected['error'].lower()
            assert first['ok'] and first['preview']['open_requested'] is False
            # Evaluate only the literal public schema: no model/provider/image call.
            tree = ast.parse((pathlib.Path(os.environ['HERMES_IOS_HOST_SOURCE'])/'tools/vision_tools.py').read_text())
            node = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VISION_ANALYZE_SCHEMA' for t in n.targets))
            schema = ast.literal_eval(node.value)
            assert schema['name'] == 'vision_analyze'
            assert schema['parameters']['required'] == ['image_url','question']
            validate({'image_url':'/synthetic/ordinary-send.png','question':'Describe the marked region before editing.'}, schema['parameters'])
            assert schema['parameters']['properties']['region']['minItems'] == 4
            print(json.dumps({'homes':resolved_homes(),'boundary':'source-runtime-not-installed-desktop','preview_schema':'validated','preview_rejected':rejected,'image_schema':'validated-no-image-inspected'}))
`)], { root: fixture, env, cwd: join(fixture, "cwd"), allow: ["HERMES_IOS_HOST_SOURCE", "HERMES_IOS_WORKTREE"] });
    try {
      expect({ code: result.exitCode, stderr: result.stderr.toString(), stdout: result.stdout.toString() }).toMatchObject({ code: 0 });
      const output = JSON.parse(result.stdout.toString().trim().split("\n").at(-1) ?? "null");
      assertHomesContained(output.homes, fixture, env);
      expect(output.boundary).toBe("source-runtime-not-installed-desktop");
      expect(output.image_schema).toBe("validated-no-image-inspected");
    } finally {
      rmSync(fixture, { recursive: true, force: true });
    }
  });
});
