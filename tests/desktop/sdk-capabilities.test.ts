import { beforeAll, beforeEach, describe, expect, mock, test } from "bun:test";

const hostSource = process.env.HERMES_IOS_HOST_SOURCE;

describe.skipIf(!hostSource)("v1 public host source prerequisites (NOT installed-host proof)", () => {
  test("SDK exports native contribution/context contracts; URL opening belongs to desktop_preview", async () => {
    const read = (path: string) => Bun.file(`${hostSource}/${path}`).text();
    const sdkSource = await read("apps/desktop/src/sdk/index.ts");
    const context = await read("apps/desktop/src/contrib/plugin.ts");
    const preview = await read("tools/preview_tool.py");
    const opener = await read("tools/open_preview_tool.py");
    const areaExports = /export\s*\*\s*from\s*['"]\.\/areas['"]/.test(sdkSource)
      ? await read("apps/desktop/src/sdk/areas.ts") : sdkSource;
    expect(areaExports).toMatch(/export\s*(?:\{\s*PANES_AREA\b|const\s+PANES_AREA\s*=)/);
    expect(sdkSource).toContain("openWorkspace:");
    expect(sdkSource).not.toMatch(/\bopenPreview\s*:/);
    for (const capability of ["register:", "onDispose:", "rest:", "storage:"]) expect(context).toContain(capability);
    expect(preview).toContain('"name": "desktop_preview"');
    expect(preview).toContain('["open", "close", "read"]');
    expect(opener).toContain('"preview.open"');
    expect(opener).toContain("desktop_ui.emit_or_error(");
    // These inspected source strings do not invent a Browser mount callback,
    // renderer URL setter, annotation SDK or plugin composer requirement.
  });
});

test('rebuilt shipped bundle registers with async routing and immediate subscriptions, without transport pinning fiction', async () => {
  const disposers: Array<() => void> = [];
  const atom = (value: string) => ({ get: () => value, subscribe: (fn: () => void) => { fn(); return () => {}; } });
  const state = { profile: atom('fixture'), cwd: atom('/fixture'), connectionId: atom('active-local'), gateway: atom('connected') };
  let pane: any;
  // Model the host's shown-pane contract: remembered positions alone do not
  // render an unregistered pane (tree-split paneGone / shownPaneIds).
  const rememberedLayout = ['browser', 'hermes-ios:native-controls'];
  const livePanes = new Set(['browser']);
  const visible = () => rememberedLayout.filter(id => livePanes.has(id));
  mock.module('@hermes/plugin-sdk', () => ({ PANES_AREA: 'panes', PALETTE_AREA: 'commandPalette',
    useValue: (atom: { get: () => unknown }) => atom.get(), useQuery: () => ({}), Button: 'button', Input: 'input', host: { state,
    openWorkspace: (id: string, options: any) => {
      const paneId = `plugin-workspace:${id}`;
      if (!rememberedLayout.includes(paneId)) rememberedLayout.push(paneId);
      livePanes.add(paneId); pane = options;
      return () => { livePanes.delete(paneId); pane = null; options.onClose?.(); };
    },
    profileRoutes: async () => [{ profile: 'fixture', targetProfile: 'target', connectionId: 'active-local', mode: 'local' }] } }));
  mock.module('react', () => ({ useState: (value: unknown) => [value, () => {}], useSyncExternalStore: (_subscribe: unknown, get: () => unknown) => get() }));
  mock.module('react/jsx-runtime', () => ({ jsx: (type: unknown, props: unknown) => ({ type, props }), jsxs: (type: unknown, props: unknown) => ({ type, props }) }));
  const plugin = (await import('../../dist/plugin.js')).default;
  let command: any; const sent: any[] = [];
  const register = () => plugin.register({ onDispose: (fn: () => void) => disposers.push(fn), register: (value: any) => {
    if (value.area === 'panes') livePanes.add(`hermes-ios:${value.id}`);
    command = value;
  }, rest: async (path: string, options: unknown) => { sent.push({ path, options }); return { ok: true }; } });
  expect(register).not.toThrow();
  expect(pane).toBeUndefined();
  expect(visible()).toEqual(['browser']);
  expect(plugin.id).toBe('hermes-ios');
  expect(plugin.name).toBe('Hermes-iOS-sim');
  expect(command.id).toBe('open-controls');
  expect(command.data.id).toBe('hermes-ios.open-controls');
  expect(command.data.label).toBe('Open Hermes-iOS-sim controls');
  command.data.run();
  expect(pane.title).toBe('Hermes-iOS-sim');
  expect(visible()).toEqual(['browser', 'plugin-workspace:hermes-ios:controls']);
  const client = pane.render().props.client;
  await expect(client.request({ action: 'status' })).rejects.toThrow('local_topology_required');
  for (let i = 0; i < 8; i++) await Promise.resolve();
  await expect(client.request({ action: 'status' })).resolves.toEqual({ ok: true });
  expect(sent).toEqual([{ path: '/runtime', options: { method: 'POST', body: { action: 'status' } } }]);
  disposers.splice(0).forEach(fn => fn());
  await expect(client.request({ action: 'status' })).rejects.toThrow('native_scope_retired');
  expect(visible()).toEqual(['browser']);
  register(); // Neither the retired sidebar position nor the remembered tab resurrects UI.
  expect(visible()).toEqual(['browser']);
  expect(sent).toHaveLength(1);
  disposers.splice(0).forEach(fn => fn());
});

test("compiled opt-in unified ESM imports only SDK and host React, with no composer/live/editor registration", async () => {
  const source = await Bun.file(new URL("../../dist/plugin.js", import.meta.url)).text();
  const allowed = new Set(["@hermes/plugin-sdk", "react", "react/jsx-runtime"]);
  for (const match of source.matchAll(/from\s+["']([^"']+)["']/g)) expect(allowed.has(match[1])).toBe(true);
  expect(source).toContain("defaultEnabled: false");
  for (const obsolete of ['composer.middleware', 'composer.attachments', 'createReviewMarkContributions', 'new WebSocket', 'stream_url', 'RunwayRuntimeController']) expect(source).not.toContain(obsolete);
});
