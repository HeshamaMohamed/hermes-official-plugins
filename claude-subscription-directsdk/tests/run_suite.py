"""Run the whole suite as N independent pytest processes, split by file (default 4).

Most of the suite's time is waiting on fake Claude Code processes, so one process leaves the CPU idle. Each worker
is a separate interpreter with its own TMPDIR (the shared native workspace lives under the temp dir), so no
module state, warm pool or workspace is shared between workers; inside a worker, files run in their usual order.
``python -m pytest`` still runs the same tests in one process.

Usage: python tests/run_suite.py [-n N] [extra pytest args...]
"""
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def groups(files, n):
    """Largest file first onto the lightest group; file size stands in for its run time."""
    buckets, load = [[] for _ in range(n)], [0] * n
    for path in sorted(files, key=lambda p: -p.stat().st_size):
        i = load.index(min(load))
        buckets[i].append(path)
        load[i] += path.stat().st_size
    return [b for b in buckets if b]


def main(argv):
    n = 4
    if argv[:1] == ['-n']:
        n, argv = int(argv[1]), argv[2:]
    began = time.monotonic()
    procs = []
    with tempfile.TemporaryDirectory(prefix='directsdk-suite-') as tmp:
        for index, group in enumerate(groups(sorted((ROOT / 'tests').glob('test_*.py')), n)):
            workdir = Path(tmp) / str(index)
            workdir.mkdir()
            env = dict(os.environ, TMPDIR=str(workdir))
            command = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *argv,
                       *(str(p.relative_to(ROOT)) for p in group)]
            procs.append((group, subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                                  stderr=subprocess.STDOUT, text=True)))
        status, passed = 0, 0
        for group, proc in procs:
            out = proc.communicate()[0]
            lines = out.strip().splitlines()
            # First "N passed" of "59 passed, 9 subtests passed in 3.9s" or "1 failed, 85 passed in 3.0s".
            count = re.search(r'(\d+) passed', lines[-1]) if lines else None
            passed += int(count.group(1)) if count else 0
            if proc.returncode:
                print(out)
            print(f'[{proc.returncode}] {lines[-1] if lines else "(no output)"} :: {" ".join(p.name for p in group)}')
            status = status or proc.returncode
    print(f'{passed} passed in {len(procs)} workers, {time.monotonic() - began:.2f}s wall, exit {status}')
    return status


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
