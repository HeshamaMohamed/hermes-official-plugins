"""The one request the gate admits per step must be the Hermes turn; a native side request is refused, loudly.

2026-10-16: native, launched with an inherited Claude host marker, sent a session-title request ahead of the turn.
The gate admitted it, denied the real turn as HERMES_MODEL_ADMISSION_CONSUMED, and Hermes answered the user with
the title JSON and no error. Shapes below are what real native 2.1.301 sent (loopback capture, no tokens): its side
requests carry their own system prompt in place of Hermes' and wrap the conversation in their own instruction; Hermes'
response_format, merged in from the extra body, overrides the title request's own format.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk

HEAD = [{'type': 'text', 'text': 'x-anthropic-billing-header: cc_version=2.1.301.fixture; cc_entrypoint=sdk-cli;'},
        {'type': 'text', 'text': "You are a Claude agent, built on Anthropic's Claude Agent SDK."}]
TITLE_SCHEMA = {'type': 'object', 'properties': {'title': {'type': 'string'}}, 'required': ['title'], 'additionalProperties': False}
SIDE = {
    'session-title': {
        'system': HEAD + [{'type': 'text', 'text': 'You are naming a coding session so the user can pick it out of a long list.'}],
        'output_config': {'effort': 'medium', 'format': {'type': 'json_schema', 'schema': TITLE_SCHEMA}},
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': '<session>\nQuick check. Reply with the date.\n</session>\n\nWrite the title.'}]}]},
    'status-classifier': {
        'system': HEAD[:1] + [{'type': 'text', 'text': 'A user kicked off a Claude Code agent to do a coding task and walked away.'}],
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'Current state: working (for 0m)\nUser\'s most recent ask: "Quick check. Reply with the date."'}]}]},
}

# Native as the relay sees it: Hermes' system prompt after native's own header blocks, the replayed frames as messages,
# the per-request environment as a trailing system message. SIDE_FIRST (a body, or "copy": a foreign system over the
# turn's own messages) goes upstream before the turn, SIDE_AFTER after it.
NATIVE = r'''
import json, os, sys, urllib.request, urllib.error
argv = sys.argv
system = open(argv[argv.index('--system-prompt-file') + 1], encoding='utf-8').read()
frames = []
for line in sys.stdin:
    frame = json.loads(line)
    frames.append(frame['message'])
    if frame.get('shouldQuery') is False:
        print(json.dumps({'type':'result','num_turns':0}), flush=True)
    elif frame['type'] == 'user':
        break
head = json.loads(os.environ['HEAD'])
turn = {'system': head + ([{'type':'text','text':system,'cache_control':{'type':'ephemeral'}}] if system else []),
        'messages': frames + [{'role':'system','content':[{'type':'text','text':'# Environment\nfixture'}]}]}
extra = json.loads(os.environ.get('EXTRA') or '{}')
turn.update(extra)
url = os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages'
def post(body):
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type':'application/json'}), timeout=5).read()
        return None
    except urllib.error.HTTPError as error:
        return 'API Error: %d %s' % (error.code, error.read().decode())
def side(name):
    body = os.environ.get(name)
    if not body:
        return
    body = json.loads(body)
    if body == 'copy':
        body = dict(turn, system=head + [{'type':'text','text':'You summarize a session for a recap.'}])
    else:
        body.update(extra)
    post(body)
side('SIDE_FIRST')
error = post(turn)
side('SIDE_AFTER')
if error:
    print(json.dumps({'type':'assistant','error':'unknown','message':{'role':'assistant','model':'<synthetic>','content':[{'type':'text','text':error}]}}))
    print(json.dumps({'type':'result','subtype':'success','is_error':True,'num_turns':1}))
else:
    print(json.dumps({'type':'assistant','message':{'id':'turn','role':'assistant','content':[{'type':'text','text':'MAIN'}]}}))
    print(json.dumps({'type':'stream_event','event':{'type':'message_stop'}}))
    print(json.dumps({'type':'result','subtype':'success','usage':{'input_tokens':0,'output_tokens':0}}))
'''


def _sse(text):
    usage = {'input_tokens': 1, 'output_tokens': 1, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
    events = [{'type': 'message_start', 'message': {'id': 'm', 'role': 'assistant', 'model': 'sonnet', 'content': [], 'usage': usage}},
              {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
              {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}},
              {'type': 'content_block_stop', 'index': 0},
              {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': usage},
              {'type': 'message_stop'}]
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()


@pytest.fixture
def run(tmp_path):
    """``run(messages, side_first=None, side_after=None, extra=None, **create)`` -> (result or raised error, upstream bodies)."""
    seen = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append(body)
            titled = 'title' in (((body.get('output_config') or {}).get('format') or {}).get('schema') or {}).get('properties', {})
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(_sse('{"title":"Post-update Hermes check"}' if titled else 'MAIN'))

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    clients = []

    def go(messages, side_first=None, side_after=None, extra=None, **create):
        env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{peer.server_port}',
               'HEAD': json.dumps(HEAD), 'EXTRA': json.dumps(extra or {}),
               'SIDE_FIRST': json.dumps(side_first) if side_first else '', 'SIDE_AFTER': json.dumps(side_after) if side_after else ''}
        client = directsdk.Client(command=[sys.executable, str(native)], env=env)
        clients.append(client)
        try:
            return client.create(model='sonnet', messages=messages, **create), seen
        except directsdk.ClaudeAPIError as error:
            return error, seen

    yield go
    for client in clients:
        client.close()
    peer.shutdown()
    thread.join()
    peer.server_close()


HERMES = [{'role': 'system', 'content': 'You are Hermes, the CTO seat.'}, {'role': 'user', 'content': 'Quick check. Reply with the date.'}]
FORMAT = {'type': 'json_schema', 'json_schema': {'name': 'answer', 'schema': {'type': 'object', 'properties': {'answer': {'type': 'string'}}}}}


def test_the_hermes_turn_is_admitted(run):
    result, seen = run(HERMES)
    assert result.choices[0].message.content == 'MAIN'
    assert len(seen) == 1 and seen[0]['system'][-1]['text'] == 'You are Hermes, the CTO seat.'
    assert (result.usage.native_admission.upstream_requests, result.usage.native_admission.blocked_requests) == (1, 0)


@pytest.mark.parametrize('kind', ['session-title', 'status-classifier'])
@pytest.mark.parametrize('messages', [HERMES, HERMES[1:]], ids=['hermes-system', 'no-system'])
def test_a_side_request_before_the_turn_raises_and_never_becomes_the_reply(run, kind, messages):
    error, seen = run(messages, side_first=SIDE[kind])
    assert isinstance(error, directsdk.ClaudeAPIError), f'the {kind} request was answered as the turn: {error!r}'
    assert 'HERMES_MODEL_ADMISSION_NOT_HERMES_TURN' in str(error) and kind in str(error)
    assert seen == []  # neither the side request nor the turn behind it reached upstream


def test_hermes_response_format_on_the_title_request_does_not_pass_it_off_as_the_turn(run):
    # Native merges Hermes' extra body into its side requests: the title request then carries Hermes' own format.
    error, seen = run(HERMES, side_first=SIDE['session-title'], extra={'output_config': {'format': FORMAT}}, response_format=FORMAT)
    assert isinstance(error, directsdk.ClaudeAPIError) and 'HERMES_MODEL_ADMISSION_NOT_HERMES_TURN' in str(error)
    assert seen == []


def test_a_side_request_replaying_the_turns_messages_under_its_own_system_is_refused(run):
    error, seen = run(HERMES, side_first='copy')
    assert isinstance(error, directsdk.ClaudeAPIError) and 'HERMES_MODEL_ADMISSION_NOT_HERMES_TURN' in str(error)
    assert 'side request' in str(error)
    assert seen == []


def test_a_side_request_after_the_admitted_turn_is_denied_and_the_turn_answers(run):
    result, seen = run(HERMES, side_after=SIDE['session-title'])
    assert result.choices[0].message.content == 'MAIN'
    assert len(seen) == 1
    assert result.usage.native_admission.blocked_requests == 1


def test_a_hermes_turn_on_tool_results_is_admitted(run):
    messages = HERMES + [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'toolu_1', 'type': 'function', 'function': {'name': 'probe', 'arguments': '{}'}}]},
                         {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': 'probe output'}]
    result, seen = run(messages, tools=[{'type': 'function', 'function': {'name': 'probe', 'description': 'p', 'parameters': {'type': 'object', 'properties': {}}}}])
    assert result.choices[0].message.content == 'MAIN'
    assert len(seen) == 1


def test_a_side_request_ahead_of_a_tool_result_turn_is_refused(run):
    messages = HERMES + [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'toolu_1', 'type': 'function', 'function': {'name': 'probe', 'arguments': '{}'}}]},
                         {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': 'probe output'}]
    error, seen = run(messages, side_first=SIDE['status-classifier'],
                      tools=[{'type': 'function', 'function': {'name': 'probe', 'description': 'p', 'parameters': {'type': 'object', 'properties': {}}}}])
    assert isinstance(error, directsdk.ClaudeAPIError) and 'status-classifier' in str(error)
    assert seen == []


@pytest.mark.parametrize('payload, why', [
    # Restoration tolerates a reshaped result, but a result for another tool call is another turn (was an
    # "unprovable restoration" case forwarded unchanged before this gate).
    ({'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'wrong', 'content': 'output'}]}]},
     'does not carry the frame Hermes queried'),
    ({'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'output'}]}],
      'system': [{'type': 'text', 'text': 'Another prompt.'}]}, "system prompt is not Hermes'"),
    ({}, 'not a Messages API request'),
])
def test_the_gate_refuses_what_is_not_the_queried_turn_and_forwards_nothing(payload, why):
    import http.client
    from admission import Admission
    received = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    gate = Admission(f'http://127.0.0.1:{peer.server_port}', 5, queried=[{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'output'}],
                     system='You are Hermes.' if 'system' in payload else '')
    try:
        route = gate.url.removeprefix(f'http://127.0.0.1:{gate.server.server_port}')
        for attempt in range(2):  # the turn behind a refused request is refused too
            conn = http.client.HTTPConnection('127.0.0.1', gate.server.server_port, timeout=5)
            conn.request('POST', route + '/v1/messages', json.dumps(payload).encode(), {'Content-Type': 'application/json'})
            response = conn.getresponse()
            assert response.status == 400
            assert json.loads(response.read())['error']['message'] == 'HERMES_MODEL_ADMISSION_NOT_HERMES_TURN'
            conn.close()
        assert why in gate.refused and gate.refused.startswith('HERMES_MODEL_ADMISSION_NOT_HERMES_TURN: ')
        assert not gate.used and gate.denied == 1
        assert received == []
    finally:
        gate.close()
        peer.shutdown()
        peer.server_close()
