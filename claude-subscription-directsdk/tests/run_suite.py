"""Run the whole suite as N independent pytest processes, split by file (default 4).

Most of the suite's time is waiting on fake Claude Code processes, so one process leaves the CPU idle. Each worker
is a separate interpreter with its own TMPDIR (the shared native workspace lives under the temp dir), so no
module state, warm pool or workspace is shared between workers; inside a worker, files run in their usual order.
``python -m pytest`` still runs the same tests in one process.

Workers run with ``-v`` so the last test id printed is the running one: a worker killed by a signal is reported
with that test. A test that runs past the limit (``-t``, default 45 s) gets its stack printed by faulthandler,
then its worker is killed and the run fails naming it. A worker whose files match none of ``-k``/extra args
exits 5; that counts as a pass while another worker ran tests, and the run exits 5 when no worker ran any.

Usage: python tests/run_suite.py [-n N] [-t SECONDS] [--dir TESTS_DIR] [extra pytest args...]
"""
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NO_TESTS = 5  # pytest.ExitCode.NO_TESTS_COLLECTED
TEST_ID = re.compile(r'^(\S+\.py::\S+)')


def groups(files, n):
    """Largest file first onto the lightest group; file size stands in for its run time."""
    buckets, load = [[] for _ in range(n)], [0] * n
    for path in sorted(files, key=lambda p: -p.stat().st_size):
        i = load.index(min(load))
        buckets[i].append(path)
        load[i] += path.stat().st_size
    return [b for b in buckets if b]


class Worker:
    def __init__(self, group, command, cwd, env):
        self.group, self.lines, self.test, self.hung = group, [], None, False
        self.started = time.monotonic()
        self.proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line.rstrip('\n'))
            match = TEST_ID.match(line)
            if match:  # "-v" prints the id when a test starts, and again with its result
                self.test, self.started = match.group(1), time.monotonic()


def main(argv):
    n, limit, tests = 4, 45.0, ROOT / 'tests'
    while argv[:1] and argv[0] in ('-n', '-t', '--dir'):
        if argv[0] == '-n':
            n = int(argv[1])
        elif argv[0] == '-t':
            limit = float(argv[1])
        else:
            tests = Path(argv[1]).resolve()
        argv = argv[2:]
    root = tests.parent
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='directsdk-suite-') as tmp:
        workers = []
        for index, group in enumerate(groups(sorted(tests.glob('test_*.py')), n)):
            workdir = Path(tmp) / str(index)
            workdir.mkdir()
            env = dict(os.environ, TMPDIR=str(workdir), PYTHONUNBUFFERED='1')
            command = [sys.executable, '-m', 'pytest', '-v', '-p', 'no:cacheprovider',
                       '-o', f'faulthandler_timeout={limit}', *argv, *(str(p.relative_to(root)) for p in group)]
            workers.append(Worker(group, command, root, env))
        # Kill a stuck worker half a second after faulthandler has printed its test's stack.
        while any(w.proc.poll() is None for w in workers):
            for w in workers:
                if w.proc.poll() is None and time.monotonic() - w.started > limit + 0.5:
                    w.hung = True
                    w.proc.kill()
            time.sleep(0.05)
        codes, passed = [], 0
        for w in workers:
            w.reader.join()
            last = w.lines[-1].strip('= ') if w.lines else '(no output)'
            # First "N passed" of "59 passed, 9 subtests passed in 3.9s" or "1 failed, 85 passed in 3.0s".
            count = re.search(r'(\d+) passed', last)
            passed += int(count.group(1)) if count else 0
            code = w.proc.returncode
            if w.hung:
                code, last = 1, f'hung over {limit:g}s in {w.test}'
            elif code < 0:
                last = f'killed by signal {-code} while running {w.test}'
            if code not in (0, NO_TESTS):
                print('\n'.join(w.lines))
            print(f'[{code}] {last} :: {" ".join(p.name for p in w.group)}')
            codes.append(code)
    failed = [c for c in codes if c not in (0, NO_TESTS)]
    status = failed[0] if failed else (NO_TESTS if codes and all(c == NO_TESTS for c in codes) else 0)
    print(f'{passed} passed in {len(workers)} workers, {time.monotonic() - began:.2f}s wall, exit {status}')
    return status


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
