"""Deterministically interleave warm detach between cancel's check and abort."""
import inspect
import sys
from types import SimpleNamespace

import directsdk


def test_cancel_captures_admission_before_detach_and_still_sweeps_parked(monkeypatch):
    client = directsdk.Client(env={})
    request = directsdk.Request(client)
    aborted, swept, detached = [], [], []

    def abort():
        assert request.cancelled.is_set()
        assert request.admission is None
        assert not request.lock.locked()
        aborted.append(True)

    request.admission = SimpleNamespace(abort=abort)
    request.process = object()
    client._requests.add(request)
    monkeypatch.setattr(directsdk, '_warm_cancel_owned', lambda owner: swept.append(owner))
    # A line trace pauses immediately before the abort expression. In the old
    # code the preceding None check succeeded, but detach clears the attribute
    # before its second lookup. The fixed code uses the reference it captured.
    lines, start = inspect.getsourcelines(directsdk.Request.cancel)
    abort_line = next(start + i for i, line in enumerate(lines) if 'admission.abort()' in line)

    def trace(frame, event, arg):
        if frame.f_code is directsdk.Request.cancel.__code__ and event == 'line' and frame.f_lineno == abort_line:
            directsdk.WarmSession.detach(request)
            detached.append(True)
        return trace

    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        client.cancel()
    finally:
        sys.settrace(previous)
        client._requests.clear()
        client.close()
    assert detached == aborted == [True]
    assert swept == [client]
