import { NativeControlSession } from './runway-session.js';

export interface NativeScope { profile: string; targetProfile: string | null; worktree: string; connectionId: string | null; mode: string }
export class NativeRuntimeClient {
  session = new NativeControlSession();
  private disposed = false;
  private listeners = new Set<() => void>();
  snapshot = () => `${this.session.key}:${this.session.generation}`;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private publish(previous: string) { if (previous !== this.snapshot()) for (const listener of this.listeners) listener(); }
  retire() { const previous = this.snapshot(); this.session.retire(); this.session.observe(this.options.scope()); this.publish(previous); }
  dispose() { this.disposed = true; this.retire(); this.listeners.clear(); }
  observe() {
    if (this.disposed) return;
    const previous = this.snapshot(); this.session.observe(this.options.scope()); this.publish(previous);
  }
  constructor(private options: { scope: () => NativeScope; rest: (path: string, options: { method: string; body: Record<string, unknown> }) => Promise<unknown> }) {}
  async request(body: Record<string, unknown>): Promise<any> {
    if (!['discover', 'preflight', 'status', 'cancel', 'stop'].includes(String(body.action))) throw new Error('unsupported_panel_action');
    if (this.disposed) throw new Error('native_scope_retired');
    const scope = this.options.scope();
    const token = this.session.capture(scope);
    if (scope.mode !== 'local' || !scope.connectionId || !scope.targetProfile) throw new Error('local_topology_required');
    // ctx.rest uses the active host transport, not arbitrary connection options.
    // No await or other work may separate this final active-scope check and dispatch.
    if (this.disposed || !this.session.accept(this.options.scope(), token)) throw new Error('native_scope_retired');
    const result = await this.options.rest('/runtime', { method: 'POST', body });
    if (this.disposed || !this.session.accept(this.options.scope(), token)) throw new Error('native_scope_retired');
    return result;
  }
}
