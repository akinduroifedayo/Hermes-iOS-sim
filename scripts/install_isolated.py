"""Delegate candidate admission to the public CLI; never materialize two roots."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--cli')
    parser.add_argument('--identifier')
    parser.add_argument('--ref')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    command = ['bun', str(Path(__file__).with_name('install-local.ts')), '--home', args.home,
               '--candidate', args.candidate, '--manifest', args.manifest]
    for key in ['cli', 'identifier', 'ref']:
        if getattr(args, key): command.extend(['--' + key, getattr(args, key)])
    if args.apply: command.append('--apply')
    return subprocess.run(command, check=False, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}).returncode

if __name__ == '__main__':
    raise SystemExit(main())
