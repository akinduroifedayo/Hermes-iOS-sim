import { expect, mock, test } from 'bun:test';
// Native controls rendering contract, not installed Desktop AX qualification.
const jsx = (type: unknown, props: any) => ({ type, props });
mock.module('react/jsx-runtime', () => ({ jsx, jsxs: jsx }));
mock.module('react', () => ({ useState: (value: unknown) => [value, () => {}], useSyncExternalStore: (_subscribe: unknown, getSnapshot: () => unknown) => getSnapshot() }));
mock.module('@hermes/plugin-sdk', () => ({
  Button: 'button', Input: 'input', useValue: (atom: any) => atom.get(),
  useQuery: () => ({ data: null, error: null }),
  host: { state: { profile: { get: () => 'synthetic' }, cwd: { get: () => '/authorized-fixture' }, gateway: { get: () => 'connected' } } },
}));
test('native status controls expose named inputs, ordinary buttons, live status and no custom image editor', async () => {
  const { NativeControls } = await import('../../src/desktop/runtime-status-view.js');
  const rendered = NativeControls({ client: { request: async () => ({}), retire: () => {}, subscribe: () => () => {}, snapshot: () => 'synthetic-scope' } });
  const nodes: any[] = [];
  function visit(value: any) { if (Array.isArray(value)) return value.forEach(visit); if (value && typeof value === 'object') { if (value.type) nodes.push(value); visit(value.props?.children); } }
  visit(rendered);
  expect(rendered.props['aria-label']).toBe('iOS native controls');
  expect(nodes.some(node => node.props?.['aria-label'] === 'Authorized worktree')).toBe(true);
  expect(nodes.some(node => node.props?.role === 'status' && node.props?.['aria-live'] === 'polite')).toBe(true);
  const buttons = nodes.filter(node => node.type === 'button');
  expect(buttons.map(node => node.props.children)).toEqual(['Discover targets', 'Check prerequisites', 'Cancel operation', 'Stop owned runtime']);
  for (const button of buttons) expect(typeof button.props.onClick).toBe('function');
  expect(buttons.slice(2).every(button => button.props.disabled)).toBe(true);
  expect(nodes.some(node => ['canvas', 'img', 'iframe', 'webview'].includes(node.type))).toBe(false);
});
