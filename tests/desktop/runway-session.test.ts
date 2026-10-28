import { NativeControlSession } from '../../src/desktop/runway-session.js';

it('native session generations retire switch-away-and-back results', () => {
  const session = new NativeControlSession();
  const scope = { profile: 'fixture', worktree: '/fixture', mode: 'local', connectionId: 'local' };
  const token = session.capture(scope);
  session.observe({ ...scope, profile: 'other' });
  session.observe(scope);
  expect(session.accept(scope, token)).toBe(false);
});

import { describe, expect, it } from "bun:test";
