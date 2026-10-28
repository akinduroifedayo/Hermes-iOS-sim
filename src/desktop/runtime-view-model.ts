export interface NativeSnapshot {
  phase?: string;
  last_failure?: { error?: string; evidence?: { stage?: string; detail?: string } } | null;
  last_success?: { build_attempt_id?: string } | null;
  server?: { status?: string; url?: string } | null;
}
export function nativeStatus(runtime: NativeSnapshot | null | undefined) {
  const active: Record<string, string> = { building: 'Building app', booting: 'Preparing simulator', installing: 'Installing app', launching: 'Launching app' };
  const recovery: Record<string, string> = { building: 'Fix compiler diagnostics, then ask Hermes to rebuild this target.', resolve_artifact: 'Check the selected application product and build settings.', booting: 'Check the selected simulator runtime and device availability.', installing: 'Check install diagnostics; retry this target without erasing app data.', verify_install: 'Check the installed executable before retrying.', launching: 'Check launch diagnostics, then retry this target.', verify_pid: 'Check launch process verification before retrying.', server: 'Check prerequisites and stock server diagnostics, then explicitly retry.' };
  const failure = runtime?.last_failure;
  const phase = runtime?.phase ?? '';
  const stage = failure?.evidence?.stage ?? '';
  const title = active[phase] ?? (failure ? `${stage || 'Run'} failed`
    : runtime?.server?.status === 'ready' ? 'Server ready'
    : runtime?.server?.status === 'stopped' ? 'Runtime stopped'
    : runtime?.server?.status === 'crashed' ? 'Server unavailable'
    : 'Choose exact target');
  return {
    title,
    detail: [failure?.evidence?.detail ?? failure?.error, failure ? recovery[stage] ?? 'Check diagnostics, then explicitly retry this target.' : null,
      runtime?.last_success ? `Last successful app: ${runtime.last_success.build_attempt_id}` : 'No successful app recorded',
      `Server: ${runtime?.server?.status ?? 'absent'}`, 'Pixels: unknown'].filter(Boolean).join(' · '),
  };
}
