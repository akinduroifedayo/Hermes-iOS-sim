import { nativeStatus } from '../../src/desktop/runtime-view-model';

it('native status separates last successful app from failed latest build and unknown pixels', () => {
  const status = nativeStatus({ last_failure: { evidence: { stage: 'building', detail: 'compile error' } }, last_success: { build_attempt_id: 'build:prior' }, server: { status: 'ready' } });
  expect(status.title).toBe('building failed');
  expect(status.detail).toContain('Last successful app');
  expect(status.detail).toContain('Pixels: unknown');
  expect(status.detail).toContain('Fix compiler diagnostics');
});

it('native status does not turn a prior ready server into current build or stopped success', () => {
  expect(nativeStatus({ phase: 'building', server: { status: 'ready' } }).title).toBe('Building app');
  const stopped = nativeStatus({ phase: 'ready', server: { status: 'stopped' }, last_success: { build_attempt_id: 'build:prior' } });
  expect(stopped.title).toBe('Runtime stopped');
  expect(stopped.detail).toContain('Last successful app');
});

import { expect, it } from "bun:test";
