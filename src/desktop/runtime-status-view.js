import { jsx, jsxs } from "react/jsx-runtime";
import { useState, useSyncExternalStore } from 'react';
import { host, useValue, useQuery, Button, Input } from '@hermes/plugin-sdk';
import { nativeStatus } from './runtime-view-model.ts';

export function NativeControls({ client }) {
  const profile = useValue(host.state.profile);
  const cwd = useValue(host.state.cwd);
  const gateway = useValue(host.state.gateway);
  const [selection, setSelection] = useState(null);
  const [discovery, setDiscovery] = useState(null);
  const [prerequisites, setPrerequisites] = useState(null);
  const [error, setError] = useState(null);
  const [pending, setPending] = useState(null);
  const [worktreeInput, setWorktree] = useState(null);
  const scope = useSyncExternalStore(client.subscribe, client.snapshot);
  const worktree = worktreeInput?.scope === scope ? worktreeInput.value : cwd;

  const current = selection?.scope === scope ? selection.target : null;
  const status = useQuery({ queryKey: ['hermes-ios', profile, cwd, gateway, scope, current],
    enabled: Boolean(current), refetchInterval: 3000,
    queryFn: () => {
      if (client.snapshot() !== scope) throw new Error('native_scope_retired');
      return client.request({ ...current, action: 'status' });
    } });
  const presentation = nativeStatus(status.data?.runtime);
  async function action(name) {
    const operationScope = client.snapshot();
    if (operationScope !== scope) return;
    setPending({ scope: operationScope }); setError(null);
    try {
      const result = await client.request(name === 'discover' || name === 'preflight'
        ? { action: name, worktree } : { ...current, action: name });
      if (client.snapshot() !== operationScope) return;
      if (name === 'discover') setDiscovery({ scope: operationScope, result });
      if (name === 'preflight') setPrerequisites({ scope: operationScope, result });
      if (name === 'cancel' || name === 'stop') await status.refetch();
    } catch (failure) { if (client.snapshot() === operationScope && !String(failure).includes('native_scope_retired')) setError({ scope: operationScope, message: String(failure) }); }
    finally { if (client.snapshot() === operationScope) setPending(null); }
  }
  const choices = [];
  const found = discovery?.scope === scope ? discovery.result : null;
  for (const target of found?.targets ?? []) for (const configuration of target.configurations ?? []) {
    for (const device of found.devices ?? []) if (target.compatible_udids.includes(device.udid)) {
      choices.push({ worktree: found.worktree, project: target.project, scheme: target.scheme,
                     configuration, runtime: device.runtime, udid: device.udid });
    }
  }
  const button = (label, name, disabled = false) => jsx(Button, { size: 'sm', disabled: pending?.scope === scope || disabled,
    onClick: () => void action(name), children: label });
  return jsxs('section', { 'aria-label': 'iOS native controls', style: { padding: 12, display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 12,
    color: 'var(--ui-text-primary)', overflow: 'auto', overflowWrap: 'anywhere', maxHeight: '100%' }, children: [
    jsx('strong', { children: 'iOS target and run status' }),
    jsx('label', { children: jsxs('span', { children: ['Authorized worktree', jsx(Input, {
      value: worktree, onChange: event => { if (client.snapshot() !== scope) return; client.retire(); setWorktree({ scope: client.snapshot(), value: event.target.value }); }, 'aria-label': 'Authorized worktree' })] }) }),
    jsxs('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' }, children: [button('Discover targets', 'discover'), button('Check prerequisites', 'preflight')] }),
    choices.length ? jsx('select', { 'aria-label': 'Exact iOS target', value: current ? JSON.stringify(current) : '',
      onChange: event => {
        if (client.snapshot() !== scope) return;
        client.retire();
        const nextScope = client.snapshot();
        setDiscovery({ scope: nextScope, result: found });
        setWorktree({ scope: nextScope, value: worktree });
        setSelection({ scope: nextScope, target: JSON.parse(event.target.value) });
      },
      style: { maxWidth: '100%', color: 'var(--ui-text-primary)', background: 'var(--ui-bg-primary)' },
      children: [jsx('option', { disabled: true, value: '', children: 'Choose project / scheme / configuration / device' }),
        ...choices.map(target => jsx('option', { value: JSON.stringify(target), children:
          `${target.project} · ${target.scheme} · ${target.configuration} · ${target.runtime} · ${target.udid}` }, JSON.stringify(target)))] }) : null,
    current ? jsx('div', { style: { overflowWrap: 'anywhere' }, children: `${current.project} · ${current.scheme} · ${current.configuration} · ${current.runtime} · ${current.udid}` }) : null,
    jsx('div', { role: 'status', 'aria-live': 'polite', children: jsxs('div', { children: [
      jsx('strong', { children: presentation.title }), jsx('p', { children: presentation.detail })] }) }),
    jsx('p', { children: 'Ask Hermes in chat to build and run this exact target. Preview and annotations use Hermes Browser; this panel does not open it.' }),
    jsxs('div', { style: { display: 'flex', gap: 8 }, children: [button('Cancel operation', 'cancel', !current), button('Stop owned runtime', 'stop', !current)] }),
    (error?.scope === scope) || status.error ? jsx('p', { role: 'alert', style: { overflowWrap: 'anywhere' }, children: error?.scope === scope ? error.message : String(status.error) }) : null,
    prerequisites?.scope === scope ? jsxs('details', { children: [
      jsx('summary', { children: prerequisites.result.ready ? 'Prerequisites ready' : 'Prerequisites need attention' }),
      jsx('pre', { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }, children: JSON.stringify(prerequisites.result, null, 2) })] }) : null,
    found?.diagnostics?.length ? jsx('pre', { style: { whiteSpace: 'pre-wrap' }, children: JSON.stringify(found.diagnostics, null, 2) }) : null,
  ] });
}
