export class NativeControlSession {
  generation = 0;
  key = null;
  observe(scope) {
    const key = JSON.stringify(scope);
    if (key !== this.key) { this.key = key; this.generation += 1; }
  }
  capture(scope) { this.observe(scope); return this.generation; }
  accept(scope, token) { this.observe(scope); return token === this.generation; }
  retire() { this.key = null; this.generation += 1; }
}
