// Disposable roots for every bun-launched child that imports the archived host; mirrors
// tests/agent/host_isolation.py. Host import is execution: without HERMES_HOME it falls back to
// ~/.hermes and initializes it, and without HOME that is the real account home. So a host child
// gets only an allowlisted env whose account home, Hermes home, runtime, XDG, temp and cwd are
// all under one fixture root, and requireContained refuses to launch one that is not.
import { expect } from "bun:test";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join, relative, resolve } from "node:path";

export const SENTINEL = ".hermes-ios-account-standin";
const ROOTED = ["HOME", "HERMES_HOME", "HERMES_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
  "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "TMPDIR"] as const;
const BASE = ["PATH", "PYTHONDONTWRITEBYTECODE"];

// Child-side readback of every home the host could fall back to (call after host import).
export const HOMES = `
import os, tempfile
from pathlib import Path
def resolved_homes():
    from hermes_constants import get_hermes_home
    return {'home': str(Path.home().resolve()), 'expanduser': os.path.realpath(os.path.expanduser('~')),
            'hermes_home': str(Path(get_hermes_home()).resolve()),
            'tmp': os.path.realpath(tempfile.gettempdir()), 'cwd': os.path.realpath(os.getcwd())}
`;

function under(path: string | undefined, root: string): boolean {
  if (typeof path !== "string" || !isAbsolute(path)) return false;
  const rel = relative(root, existsSync(path) ? realpathSync(path) : resolve(path));
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

export function fixtureRoot(prefix: string): string {
  return realpathSync(mkdtempSync(join(tmpdir(), prefix)));
}

// Allowlisted, credential-free env whose every home-like root is a fresh dir under root.
export function fixtureEnv(root: string, extra: Record<string, string> = {}): Record<string, string> {
  const paths: Record<string, string> = {
    HOME: join(root, "account"), HERMES_HOME: join(root, "hermes"), HERMES_RUNTIME_DIR: join(root, "runtime-root"),
    XDG_CONFIG_HOME: join(root, "xdg", "config"), XDG_CACHE_HOME: join(root, "xdg", "cache"),
    XDG_DATA_HOME: join(root, "xdg", "data"), XDG_STATE_HOME: join(root, "xdg", "state"),
    XDG_RUNTIME_DIR: join(root, "xdg", "runtime"), TMPDIR: join(root, "tmp"),
  };
  for (const path of [...Object.values(paths), join(root, "cwd")]) mkdirSync(path, { recursive: true, mode: 0o700 });
  const sentinel = join(paths.HOME, SENTINEL);
  if (!existsSync(sentinel)) writeFileSync(sentinel, root);
  return { PATH: process.env.PATH ?? "/usr/bin:/bin", PYTHONDONTWRITEBYTECODE: "1", ...paths, ...extra };
}

// Refuse a host child with an env key outside the allowlist, or whose HOME/HERMES_HOME (or any
// other set root, or cwd) escapes root.
export function requireContained(env: Record<string, string | undefined>, root: string, cwd: string,
                                 allow: string[] = []): void {
  for (const key of Object.keys(env)) {
    if (!BASE.includes(key) && !(ROOTED as readonly string[]).includes(key) && !allow.includes(key)) {
      throw new Error(`refusing host child: ${key} is not an allowlisted variable`);
    }
  }
  for (const key of ["HOME", "HERMES_HOME"]) {
    if (!under(env[key], root)) throw new Error(`refusing host child: ${key}=${env[key]} is not under fixture root ${root}`);
  }
  for (const key of ROOTED) {
    if (key in env && !under(env[key], root)) {
      throw new Error(`refusing host child: ${key}=${env[key]} is not under fixture root ${root}`);
    }
  }
  if (!under(cwd, root)) throw new Error(`refusing host child: cwd=${cwd} is not under fixture root ${root}`);
}

export function runHostChild(argv: string[], options: { root: string; env: Record<string, string>; cwd: string;
                                                        allow?: string[]; timeout?: number }) {
  requireContained(options.env, options.root, options.cwd, options.allow);
  return Bun.spawnSync(argv, { env: options.env, cwd: options.cwd, timeout: options.timeout ?? 60000 });
}

// The child's own resolved homes are the fixture stand-ins, so they are the only homes it touched.
export function assertHomesContained(homes: Record<string, string>, root: string, env: Record<string, string>): void {
  const account = realpathSync(env.HOME);
  expect({ home: homes.home, expanduser: homes.expanduser }).toEqual({ home: account, expanduser: account });
  for (const key of ["hermes_home", "tmp", "cwd"]) {
    if (!under(homes[key], root)) throw new Error(`host child resolved ${key} outside fixture root ${root}: ${JSON.stringify(homes)}`);
  }
  expect(readFileSync(join(account, SENTINEL), "utf8")).toBe(root);
}
