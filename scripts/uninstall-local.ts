import { isolatedHome } from './install-local.ts';
import { join } from 'node:path';
export function uninstallPlan(home: string, cli = 'hermes') {
  home = isolatedHome(home);
  return { home, command: [cli, 'plugins', 'remove', 'hermes-ios'],
    retained: [join(home, 'plugin-data/hermes-ios'), 'Historical evidence, simulator devices and app data'],
    effects: 'Public host removal; host owns its managed Desktop copy. Markerless standalone Desktop code is never deleted by this helper.' };
}
if (import.meta.main) {
  const arg = (name: string) => { const i = process.argv.indexOf(name); return i >= 0 ? process.argv[i + 1] : undefined; };
  const plan = uninstallPlan(arg('--home') ?? '', arg('--cli'));
  if (process.argv.includes('--apply')) {
    const child = Bun.spawnSync({ cmd: plan.command, env: { ...process.env, HERMES_HOME: plan.home }, stdin: 'inherit', stdout: 'inherit', stderr: 'inherit' });
    process.exit(child.exitCode);
  }
  console.log(JSON.stringify(plan, null, 2));
}
