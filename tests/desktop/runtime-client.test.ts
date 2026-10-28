import { describe, expect, it, mock } from 'bun:test';
import { NativeRuntimeClient } from '../../src/desktop/runtime-client';

const local = { profile: 'fixture', targetProfile: 'fixture', worktree: '/fixture', connectionId: 'local', mode: 'local' };
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

it('registers synchronously with shipped async routes and immediate atoms; denies unresolved, invalid and stale routes', async () => {
  const disposers: Array<() => void> = [];
  const listeners: Array<() => void> = [];
  const values = { profile: 'fixture', cwd: '/fixture', gateway: 'connected', connectionId: 'local' };
  const state = Object.fromEntries(Object.keys(values).map(key => [key, {
    get: () => values[key as keyof typeof values],
    subscribe: (fn: () => void) => { listeners.push(fn); fn(); return () => {}; },
  }]));
  let routes: any = [local];
  let deferred: ((value: any) => void) | null = null;
  let reject = false;
  let pane: any;
  let close: (() => void) | null = null;
  const opened: string[] = [];
  mock.module('@hermes/plugin-sdk', () => ({ PANES_AREA: 'panes', PALETTE_AREA: 'commandPalette', host: { state,
    openWorkspace: (id: string, options: any) => {
      opened.push(id); pane = options;
      close = () => { pane = null; close = null; options.onClose?.(); };
      return close;
    },
    profileRoutes: async () => { if (reject) throw Error('unavailable'); if (deferred) return await new Promise(resolve => { deferred = resolve; }); return routes; } },
    useValue: (atom: any) => atom.get(), useQuery: () => ({ data: null, refetch: async () => {} }), Button: 'button', Input: 'input' }));
  const hookState: any[] = []; let hookIndex = 0;
  mock.module('react', () => ({ useState: (value: any) => {
    const index = hookIndex++; if (!(index in hookState)) hookState[index] = value;
    return [hookState[index], (next: any) => { hookState[index] = next; }];
  }, useSyncExternalStore: (_subscribe: any, get: any) => get() }));
  mock.module('react/jsx-runtime', () => ({ jsx: (type: any, props: any) => ({ type, props }), jsxs: (type: any, props: any) => ({ type, props }) }));
  const plugin = (await import('../../src/desktop/plugin.js')).default;
  let command: any;
  const contributions: any[] = [];
  const calls: unknown[] = [];
  let response: any = { ok: true };
  const register = () => plugin.register({ register: (value: any) => { contributions.push(value); command = value; },
    onDispose: (fn: () => void) => disposers.push(fn),
    rest: async (_path: string, options: any) => { calls.push(options.body); return response; } });
  expect(register).not.toThrow();
  expect(contributions.some(value => value.area === 'panes')).toBe(false);
  expect(pane).toBeUndefined();
  expect(opened).toEqual([]);
  expect(command.data.label).toBe('Open Hermes-iOS-sim controls');
  command.data.run();
  const client = pane.render().props.client;
  command.data.run(); // Repeated explicit open reuses the same host tab id.
  expect(opened).toEqual(['hermes-ios:controls', 'hermes-ios:controls']);
  expect(calls).toEqual([]);
  await expect(client.request({ action: 'preflight' })).rejects.toThrow('local_topology_required');
  await flush();
  expect(pane.render().type(pane.render().props).props['aria-label']).toBe('iOS native controls');
  await client.request({ action: 'preflight' });
  const render = () => { hookIndex = 0; const node = pane.render(); return node.type(node.props); };
  const nodes = (node: any): any[] => node && typeof node === 'object' ? [node, ...[node.props?.children].flat().flatMap(nodes)] : [];
  const button = (label: string) => nodes(render()).find(node => node.type === 'button' && node.props.children === label);
  response = { worktree: '/fixture', targets: [{ project: 'Fixture.xcodeproj', scheme: 'Fixture', configurations: ['Debug'], compatible_udids: ['device'] }], devices: [{ udid: 'device', runtime: 'iOS' }] };
  button('Discover targets').props.onClick(); await flush();
  response = { ready: true }; button('Check prerequisites').props.onClick(); await flush();
  expect(nodes(render()).some(node => node.type === 'details')).toBe(true);
  let select = nodes(render()).find(node => node.type === 'select');
  expect(select).toBeDefined();
  select.props.onChange({ target: { value: select.props.children[1].props.value } });
  expect(button('Stop owned runtime').props.disabled).toBe(false);
  expect(nodes(render()).some(node => node.type === 'select')).toBe(true);
  const originalKey = client.snapshot();
  const staleStop = button('Stop owned runtime');
  const staleSelect = nodes(render()).find(node => node.type === 'select');
  for (const invalid of [[], [local, local], [{ ...local, mode: 'ssh' }], [{ ...local, connectionId: 'other' }], [{ ...local, targetProfile: null }]]) {
    routes = invalid; listeners.forEach(fn => fn()); await flush();
    await expect(client.request({ action: 'stop' })).rejects.toThrow('local_topology_required');
    expect(nodes(render()).some(node => node.type === 'select' || node.type === 'details')).toBe(false);
    expect(button('Stop owned runtime').props.disabled).toBe(true);
  }
  reject = true; listeners.forEach(fn => fn()); await flush();
  await expect(client.request({ action: 'stop' })).rejects.toThrow('local_topology_required');
  reject = false; routes = [local]; listeners.forEach(fn => fn()); await flush();
  expect(client.snapshot()).not.toBe(originalKey);
  staleStop.props.onClick(); await flush();
  staleSelect.props.onChange({ target: { value: staleSelect.props.children[1].props.value } });
  expect(nodes(render()).some(node => node.type === 'select')).toBe(false);
  expect(calls).toHaveLength(3);
  routes = [{ ...local, targetProfile: 'other' }]; listeners.forEach(fn => fn()); await flush();
  const targetKey = client.snapshot();
  routes = [local]; listeners.forEach(fn => fn()); await flush();
  expect(client.snapshot()).not.toBe(targetKey);
  deferred = () => {}; listeners[0]();
  const stale = deferred!;
  deferred = null; values.connectionId = 'other'; routes = [{ ...local, connectionId: 'other' }]; listeners.forEach(fn => fn()); await flush();
  const newKey = client.snapshot(); stale([local]); await flush();
  expect(client.snapshot()).toBe(newKey);
  const oldCommand = command;
  close!(); // Native host Close tears down its workspace registration, not the plugin.
  expect(pane).toBeNull();
  expect(calls).toHaveLength(3);
  deferred = () => {}; listeners[0](); const afterDispose = deferred!;
  disposers.splice(0).forEach(fn => fn()); afterDispose(routes); await flush();
  await expect(client.request({ action: 'stop' })).rejects.toThrow();
  const opensBeforeReload = opened.length;
  oldCommand.data.run(); // A stale command from a retired registration cannot reopen UI.
  expect(opened).toHaveLength(opensBeforeReload);
  deferred = null;
  register(); await flush(); // Reload/remount does not undo dismissal.
  expect(pane).toBeNull();
  expect(opened).toHaveLength(opensBeforeReload);
  command.data.run();
  expect(pane.render().props.client).not.toBe(client);
  expect(opened.at(-1)).toBe('hermes-ios:controls');
  // Unload while open also cleans up the host-owned tab, with no native action.
  disposers.splice(0).forEach(fn => fn());
  expect(pane).toBeNull();
  register(); await flush(); // New registration after restart starts closed too.
  expect(pane).toBeNull();
  expect(calls).toHaveLength(3);
  expect(contributions.every(value => value.area === 'commandPalette')).toBe(true);
  disposers.splice(0).forEach(fn => fn());
});

describe('native controls client', () => {
  it('retires in-flight results and UI identity on connection and target profile changes', async () => {
    for (const change of [{ targetProfile: 'other' }, { connectionId: 'other' }, { profile: 'other' }, { worktree: '/other' }]) {
      let scope = { ...local };
      let release: (value: unknown) => void = () => {};
      const client = new NativeRuntimeClient({ scope: () => scope,
        rest: async () => await new Promise(resolve => { release = resolve; }) });
      const pending = client.request({ action: 'status' });
      const key = client.snapshot(); scope = { ...scope, ...change }; client.observe();
      expect(client.snapshot()).not.toBe(key);
      if (change.targetProfile) expect(client.snapshot()).toContain('"targetProfile":"other"');
      release({ ok: true }); await expect(pending).rejects.toThrow('native_scope_retired');
      await expect(client.request({ action: 'run' })).rejects.toThrow('unsupported_panel_action');
    }
  });
  it('guards the active scope immediately before dispatch, not just initial capture', async () => {
    let reads = 0; let sent = false;
    const client = new NativeRuntimeClient({ scope: () => ++reads > 1 ? { ...local, connectionId: 'other' } : local,
      rest: async () => { sent = true; return {}; } });
    await expect(client.request({ action: 'stop' })).rejects.toThrow('native_scope_retired');
    expect(sent).toBe(false);
  });
  it('rejects unresolved and remote topology without sending REST', async () => {
    for (const scope of [{ ...local, mode: 'unknown' }, { ...local, mode: 'ssh' }, { ...local, connectionId: null }]) {
      let sent = false;
      const client = new NativeRuntimeClient({ scope: () => scope, rest: async () => { sent = true; return {}; } });
      await expect(client.request({ action: 'stop' })).rejects.toThrow('local_topology_required'); expect(sent).toBe(false);
    }
  });
});
