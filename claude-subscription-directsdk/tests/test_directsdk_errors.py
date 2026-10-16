"""Failures carry the status Hermes routes retry and fallback on, and a watchdog kill reads as a timeout."""
import os
import sys

import pytest

import directsdk
from test_directsdk import FAKE

REQUEST = dict(model='sonnet', messages=[{'role': 'user', 'content': 'fixture'}])


def _native(tmp_path, native_error, create_client=directsdk.Client):
    native = tmp_path / 'native.py'
    native.write_text(FAKE)
    return create_client(command=[sys.executable, str(native)], env={'PATH': os.defpath, 'HOME': str(tmp_path), 'NATIVE_ERROR': native_error})


NATIVE_ERRORS = {  # native's error code -> (its text, the status Hermes sees)
    'rate_limit': ("You've hit your session limit · resets 3:45pm", 429),
    'billing_error': ('Credit balance is too low', 402),
    'overloaded': ('API Error: Overloaded', 529),
    'server_error': ('API Error: 500 Internal server error. This is a server-side issue, usually temporary', 503),
    'unknown': ('API Error: something else', None),
}


@pytest.mark.parametrize('code', NATIVE_ERRORS)
@pytest.mark.parametrize('streaming', [False, True])
def test_native_errors_carry_the_status_for_their_code(tmp_path, code, streaming):
    text, status = NATIVE_ERRORS[code]
    client = _native(tmp_path, f'{code}:{text}')
    try:
        with pytest.raises(RuntimeError) as raised:
            result = client.create(**REQUEST, stream=streaming)
            if streaming:
                list(result)
    finally:
        client.close()
    assert str(raised.value) == 'Native API error: ' + text
    assert raised.value.status_code == status


def test_missing_claude_code_carries_503(tmp_path):
    client = directsdk.Client(command=str(tmp_path / 'no-such-claude'), env={})
    with pytest.raises(directsdk.ClaudeCodeMissing) as raised:
        client.create(**REQUEST)
    assert raised.value.status_code == 503


def test_session_limit_falls_back_as_a_rate_limit(profile, tmp_path):
    from agent.error_classifier import FailoverReason, classify_api_error

    client = _native(tmp_path, "rate_limit:You've hit your session limit · resets 3:45pm", profile.create_client)
    try:
        with pytest.raises(RuntimeError) as raised:
            client.create(**REQUEST)
    finally:
        client.close()
    # Not billing: the text names no quota, and the profile hook declines it.
    verdict = classify_api_error(raised.value, provider=profile.name, model='sonnet')
    assert verdict.status_code == 429 and verdict.reason == FailoverReason.rate_limit and verdict.should_fallback


def test_watchdog_kill_classifies_like_a_timeout(profile):
    from agent.error_classifier import FailoverReason, classify_api_error

    def verdict(error, provider=profile.name):
        v = classify_api_error(error, provider=provider, model='sonnet')
        return v.reason, v.retryable, v.should_fallback

    killed = RuntimeError('Claude request cancelled')
    # Without the hook Hermes retries the kill as unknown, each attempt waiting out the full stale timeout again.
    assert verdict(killed, provider='')[0] == FailoverReason.unknown
    assert verdict(killed) == verdict(TimeoutError('Claude request timed out')) == (FailoverReason.timeout, True, False)
    for other in (RuntimeError('Native request failed: error_during_execution'), InterruptedError('Agent interrupted during API call'),
                  TimeoutError('Claude request timed out'), ValueError('model is required')):
        assert profile.classify_api_error(other) is None


TOOLS = [{'type': 'function', 'function': {'name': 'probe', 'description': 'long ' * 600 + 'TAIL', 'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}}}}]


def test_a_command_shaped_turn_reaches_the_model(tmp_path):
    """Hermes composes every turn, so native delivers "/status please" as written. Unmarked, native 2.1.301 answered it
    itself ("/status isn't available in this environment.") with no upstream request and the step failed."""
    client = _native(tmp_path, '')
    try:
        result = client.create(model='sonnet', messages=[{'role': 'user', 'content': '/status please'}], tools=TOOLS)
    finally:
        client.close()
    assert result.choices[0].message.tool_calls[0].function.name == 'probe'


def test_a_turn_native_answers_itself_fails_loud(tmp_path):
    """A CLI that still dispatches the text as its own command: its reply is never shown as the model's (ADR-0012)."""
    client = _native(tmp_path, '')
    client.env['FAKE_IGNORE_COMPOSED'] = '1'
    try:
        with pytest.raises(directsdk.NativeAnsweredLocally) as raised:
            client.create(model='sonnet', messages=[{'role': 'user', 'content': '/status please'}], tools=TOOLS)
    finally:
        client.close()
    assert str(raised.value).startswith("HERMES_NATIVE_ANSWERED_LOCALLY: native answered this turn itself and sent no request to "
                                        "the model (native said: \"/status isn't available in this environment.\")")
    assert raised.value.status_code is None
