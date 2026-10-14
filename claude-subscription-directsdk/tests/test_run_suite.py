"""run_suite.py on fake test files: a crashed worker, a hung worker, workers with nothing selected.

Each case runs run_suite.py as its own process over a temp tests directory, so the real suite is not involved.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

RUN_SUITE = Path(__file__).resolve().parent / 'run_suite.py'


def run(tmp_path, files, *args, limit=20):
    tests = tmp_path / 'tests'
    tests.mkdir()
    for name, body in files.items():
        (tests / name).write_text(textwrap.dedent(body))
    done = subprocess.run([sys.executable, str(RUN_SUITE), '--dir', str(tests), '-n', str(len(files)), *args],
                          capture_output=True, text=True, timeout=limit)
    return done.returncode, done.stdout + done.stderr


OK = 'def test_alpha():\n    pass\n'


def test_a_worker_killed_by_a_signal_names_the_running_test(tmp_path):
    code, out = run(tmp_path, {'test_ok.py': OK, 'test_dies.py': '''
        import os, signal
        def test_before():
            pass
        def test_crash():
            os.kill(os.getpid(), signal.SIGKILL)
    '''})
    assert code != 0, out
    assert 'killed by signal 9 while running tests/test_dies.py::test_crash' in out, out


def test_a_hung_test_prints_its_stack_and_fails_naming_it(tmp_path):
    code, out = run(tmp_path, {'test_ok.py': OK, 'test_hang.py': '''
        import time
        def test_stuck():
            time.sleep(60)
    '''}, '-t', '0.2')
    assert code != 0, out
    assert 'Timeout' in out and 'in test_stuck' in out, out  # faulthandler's stack dump
    assert 'hung over 0.2s in tests/test_hang.py::test_stuck' in out, out


def test_a_worker_with_nothing_selected_does_not_fail_the_run(tmp_path):
    code, out = run(tmp_path, {'test_a.py': OK, 'test_b.py': 'def test_beta():\n    pass\n'}, '-k', 'alpha')
    assert code == 0, out
    assert '1 passed in 2 workers' in out, out


def test_a_run_that_selects_nothing_at_all_fails(tmp_path):
    code, out = run(tmp_path, {'test_a.py': OK, 'test_b.py': OK}, '-k', 'nothing_matches')
    assert code == 5, out
