export function runVerification() {
  const checks = ['typecheck', 'build:plugin', 'test:agent', 'test:contracts', 'test:desktop', 'test:skill', 'test:faults', 'test:privacy', 'test:accessibility', 'test:integration'];
  const results = checks.map(name => {
    const result = Bun.spawnSync({ cmd: ['bun', 'run', name], env: process.env, stdin: 'ignore', stdout: 'inherit', stderr: 'inherit' });
    return { name, exitCode: result.exitCode };
  });
  if (results.some(result => result.exitCode !== 0)) throw new Error(`local_verification_failed:${JSON.stringify(results)}`);
  return { status: 'passed', scope: 'local_contracts_not_installed_visual_qualification', checks: results };
}
if (import.meta.main) console.log(JSON.stringify(runVerification(), null, 2));
