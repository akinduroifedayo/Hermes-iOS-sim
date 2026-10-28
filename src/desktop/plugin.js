import { PALETTE_AREA, host } from '@hermes/plugin-sdk';
import { jsx } from 'react/jsx-runtime';
import { NativeControls } from './runtime-status-view.js';
import { NativeRuntimeClient } from './runtime-client.ts';

export default {
  id: 'hermes-ios', name: 'Hermes-iOS-sim', defaultEnabled: false,
  register(ctx) {
    let route = null;
    let revision = 0;
    let disposed = false;
    const active = () => ({ profile: host.state.profile.get(), worktree: host.state.cwd.get(),
      connectionId: host.state.connectionId.get(), gateway: host.state.gateway.get() });
    let resolvedFor = null;
    const scope = () => {
      const current = active();
      const valid = resolvedFor === JSON.stringify(current);
      return { ...current, targetProfile: valid ? route?.targetProfile ?? null : null,
        mode: valid ? route?.mode ?? 'unknown' : 'unknown' };
    };
    const client = new NativeRuntimeClient({ scope, rest: (path, options) => ctx.rest(path, options) });
    // Atoms call subscribers immediately. Initialize every owner before subscribing.
    const refresh = async () => {
      const ticket = ++revision;
      const captured = active();
      route = null; resolvedFor = null; client.retire(); client.observe();
      try {
        const routes = await host.profileRoutes();
        if (disposed || ticket !== revision || JSON.stringify(active()) !== JSON.stringify(captured)) return;
        const matches = Array.isArray(routes) ? routes.filter(value => value.profile === captured.profile && value.connectionId === captured.connectionId) : [];
        const candidate = matches.length === 1 ? matches[0] : null;
        if (candidate?.mode === 'local' && typeof captured.connectionId === 'string' && captured.connectionId &&
            typeof candidate.targetProfile === 'string' && candidate.targetProfile) route = candidate;
        resolvedFor = JSON.stringify(captured);
        client.observe();
      } catch {
        // Rejected routing stays denied; a later host-state change may resolve it.
      }
    };
    ctx.onDispose(() => { disposed = true; ++revision; route = null; resolvedFor = null; client.dispose(); });
    for (const state of [host.state.profile, host.state.cwd, host.state.gateway, host.state.connectionId]) {
      ctx.onDispose(state.subscribe(() => { if (!disposed) void refresh(); }));
    }
    // Only explicit UI intent creates a tab. Host Close removes its registration;
    // no stored "open" flag or permanent pane can resurrect it on reload/restart.
    let closeControls = null;
    ctx.onDispose(() => { closeControls?.(); closeControls = null; });
    ctx.register({ id: 'open-controls', area: PALETTE_AREA,
      data: { id: 'hermes-ios.open-controls', label: 'Open Hermes-iOS-sim controls', keywords: ['ios', 'native', 'controls'],
        run: () => {
          if (disposed) return;
          if (typeof host.openWorkspace !== 'function') {
            host.notify({ kind: 'error', message: 'Update Hermes Desktop to open Hermes-iOS-sim controls.' });
            return;
          }
          // A distinct workspace id leaves the retired auto-open sidebar id
          // unregistered, even when an older layout still remembers its position.
          closeControls = host.openWorkspace('hermes-ios:controls', {
            title: 'Hermes-iOS-sim', render: () => jsx(NativeControls, { client }),
            onClose: () => { closeControls = null; },
          });
        } } });
  },
};
