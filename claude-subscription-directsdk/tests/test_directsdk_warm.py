"""Warm mode: one live native session across Hermes steps; parked hermes tool calls; rebuild on divergence."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk

# A native that speaks the warm protocol: SDK MCP over control requests, one tool call at a time,
# a new upstream request after every answered batch, and an idle wait for the next user frame.
NATIVE = r'''
import json, os, re, sys, urllib.request
with open(os.environ['SPAWNS'], 'a') as f:
    f.write(json.dumps({'pid': os.getpid(), 'argv': sys.argv[1:]}) + '\n')
history, queue, rpc, answered, limits = [], [], [0], {}, {}
def out(row):
    print(json.dumps(row), flush=True)
def persist(block, result, force=False):
    # 2.1.285: an MCP result past the tool's limit (UTF-16 units) is written under the config dir and replaced by a stub
    # in native's history; an error result past 11,024 chars is middle-truncated. FAKE_PERSIST_LIMIT stands in for a CLI
    # that persists below what tools/list advertised.
    text = ''.join(b['text'] for b in result['content'] if b['type'] == 'text')
    size = len(text.encode('utf-16-le', 'surrogatepass')) // 2
    if result.get('isError') and size > 11024:
        return text[:5000] + '\n... [%d characters truncated] ...\n' % (len(text) - 10000) + text[-5000:]
    if not force and size <= int(os.environ.get('FAKE_PERSIST_LIMIT') or limits.get(block['name'], 50000)):
        return result['content']
    config = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(os.environ['HOME'], '.claude')
    path = os.path.join(config, 'projects', re.sub(r'[^A-Za-z0-9]', '-', os.getcwd()), 'session-%d' % os.getpid(), 'tool-results', block['id'] + '.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(result['content'], f)
    return '<persisted-output>\nOutput too large (%dKB). Full output saved to: %s\n\nPreview (first 2KB):\n%s\n...\n</persisted-output>' % (size // 1000, path, text[:2000])
def round_budget():
    budget = os.environ.get('FAKE_ROUND_BUDGET')
    if not budget:
        return
    results = []
    for message in reversed(history):
        if message['role'] != 'user':
            break
        results.extend(b for b in message['content'] if b['type'] == 'tool_result')
    def text(result):
        content = result['content']
        return content if isinstance(content, str) else ''.join(b['text'] for b in content if b['type'] == 'text')
    def size(result):
        return len(text(result).encode('utf-16-le', 'surrogatepass')) // 2
    total = sum(size(result) for result in results)
    for result in sorted(results, key=size, reverse=True):
        if total <= int(budget):
            break
        before = size(result)
        result['content'] = persist({'id':result['tool_use_id']}, {'content':[{'type':'text','text':text(result)}]}, force=True)
        total += size(result) - before
def call_next():
    block = queue[0]
    rpc[0] += 1
    out({'type':'control_request','request_id':'c%d' % rpc[0],'request':{'subtype':'mcp_message','server_name':'hermes','message':{'jsonrpc':'2.0','id':rpc[0],'method':'tools/call','params':{'name':block['name'][len('mcp__hermes__'):],'arguments':block['input'],'_meta':{'claudecode/toolUseId':block['id']}}}}})
def wire():
    # Real native merges consecutive user messages; NATIVE_CONTEXT adds its per-request context the way 2.1.287+
    # does: a reminder before the first turn's frame or after (inside the last tool result of) a later one, a mark
    # on the last assistant block and a marked trailing system message the next request never replays.
    messages = []
    for message in json.loads(json.dumps(history)):
        if messages and message['role'] == messages[-1]['role'] == 'user':
            messages[-1]['content'] += message['content']
        else:
            messages.append(message)
    if os.environ.get('FAKE_REVERSE'):
        messages[-1]['content'].reverse()  # intact results, forwarded in another order
    if not os.environ.get('NATIVE_CONTEXT'):
        return messages
    reminder, newest = '\n<system-reminder>userEmail: user@example.com</system-reminder>', messages[-1]['content']
    if len(messages) == 1:
        newest.insert(0, {'type':'text','text':reminder})
        if len(history) == 1 and history[0]['role'] == 'user':
            # A live native keeps the first turn as it built it: later requests replay the reminder first.
            history[0] = {'role':'user','content':[{'type':'text','text':reminder}] + (history[0]['content'] if isinstance(history[0]['content'], list) else [{'type':'text','text':history[0]['content']}])}
    elif newest[-1]['type'] == 'tool_result':
        if isinstance(newest[-1]['content'], str):
            newest[-1]['content'] += reminder
        else:
            newest[-1]['content'].append({'type':'text','text':reminder})
    else:
        newest.append({'type':'text','text':reminder})
    if len(messages) > 1:
        messages[-2]['content'][-1]['cache_control'] = {'type':'ephemeral'}
    return messages + [{'role':'system','content':[{'type':'text','text':'Today is 2026-10-05.','cache_control':{'type':'ephemeral'}}]}]
def query():
    body = json.dumps({'messages': wire()}).encode()
    try:
        raw = urllib.request.urlopen(urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages', data=body, headers={'Content-Type':'application/json'}), timeout=30).read().decode()
    except urllib.error.HTTPError as error:
        out({'type':'assistant','error':'rate_limit' if error.code == 429 else 'unknown','message':{'role':'assistant','model':'<synthetic>','content':[{'type':'text','text':'API Error: %d' % error.code}]}})
        out({'type':'result','subtype':'success','is_error':True,'num_turns':1})
        return
    message = None
    for frame in raw.split('\n\n'):
        if frame.startswith('data: '):
            event = json.loads(frame[6:])
            if event['type'] == 'message_start':
                message = event['message']
            elif event['type'] == 'content_block_start':
                message['content'].append(event['content_block'])
            elif event['type'] == 'message_delta':
                message.update(event['delta'])
    for block in message['content']:
        if block['type'] == 'text':
            out({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':block['text']}}})
        out({'type':'assistant','message':dict(message, content=[block])})
    out({'type':'stream_event','event':{'type':'message_stop'}})
    history.append({'role':'assistant','content':message['content']})
    tools = [b for b in message['content'] if b['type'] == 'tool_use'] if message.get('stop_reason') == 'tool_use' else []
    if tools:
        queue.extend(tools)
        call_next()
    else:
        out({'type':'result','subtype':'success','is_error':False,'num_turns':1,'usage':message['usage']})
for line in sys.stdin:
    row = json.loads(line)
    if row['type'] == 'control_request':
        out({'type':'control_response','response':{'subtype':'success','request_id':row['request_id'],'response':{}}})
        out({'type':'control_request','request_id':'list','request':{'subtype':'mcp_message','server_name':'hermes','message':{'jsonrpc':'2.0','id':0,'method':'tools/list'}}})
    elif row['type'] == 'control_response' and row['response']['request_id'] == 'list':
        # Without _meta an MCP tool persists past min(100,000, 50,000); _meta counts up to the 500,000 ceiling.
        for tool in row['response']['response']['mcp_response']['result']['tools']:
            meta = (tool.get('_meta') or {}).get('anthropic/maxResultSizeChars')
            limits['mcp__hermes__' + tool['name']] = min(meta, 500000) if isinstance(meta, (int, float)) and meta > 0 else 50000
    elif row['type'] == 'control_response':
        result = row['response']['response']['mcp_response']['result']
        block = queue.pop(0)
        content = persist(block, result)
        history.append({'role':'user','content':[{'type':'tool_result','tool_use_id':block['id'],'content':content, **({'is_error':True} if result.get('isError') else {})}]})
        out({'type':'user','message':{'role':'user','content':[{'type':'tool_result','tool_use_id':block['id'],'content':content}]},'tool_use_result':result['content']})
        if queue:
            call_next()
        else:
            round_budget()  # Native's aggregate rewrite happens AFTER its stdout echoes.
            query()
    elif row['type'] in ('user', 'assistant'):
        history.append(row['message'])
        if row.get('shouldQuery') is False:
            out({'type':'result','num_turns':0,'is_error':False})
        elif row['type'] == 'user' and os.environ.get('AUTH_FAIL'):
            # Logged out: native answers itself before any upstream request.
            out({'type':'assistant','error':'authentication_failed','message':{'role':'assistant','model':'<synthetic>','content':[{'type':'text','text':'Not logged in'}]}})
            out({'type':'result','subtype':'success','is_error':True,'num_turns':1,'result':'Not logged in'})
        elif row['type'] == 'user' and os.environ.get('NATIVE_ERROR'):
            # A plan limit native answers itself, with no upstream request.
            out({'type':'assistant','error':os.environ['NATIVE_ERROR'],'message':{'role':'assistant','model':'<synthetic>','content':[{'type':'text','text':'limit reached'}]}})
            out({'type':'result','subtype':'success','is_error':True,'num_turns':1})
        elif row['type'] == 'user':
            query()
'''

USAGE = {'input_tokens': 1, 'output_tokens': 2, 'cache_read_input_tokens': 3, 'cache_creation_input_tokens': 4}


def sse(message_id, blocks, stop, details=None):
    events = [{'type': 'message_start', 'message': {'id': message_id, 'role': 'assistant', 'model': 'sonnet', 'content': [], 'usage': dict(USAGE)}}]
    for index, block in enumerate(blocks):
        if block['type'] == 'tool_use':
            events += [{'type': 'content_block_start', 'index': index, 'content_block': dict(block, input={})},
                       {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}}]
        else:
            events += [{'type': 'content_block_start', 'index': index, 'content_block': {'type': 'text', 'text': ''}},
                       {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'text_delta', 'text': block['text']}}]
        events.append({'type': 'content_block_stop', 'index': index})
    events += [{'type': 'message_delta', 'delta': {'stop_reason': stop, **({'stop_details': details} if details else {})}, 'usage': dict(USAGE)}, {'type': 'message_stop'}]
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()


class Upstream:
    """Answers the newest native message: a user text asks for two parallel probe calls, tool results get echoed."""
    def __init__(self, hang=None):
        self.bodies, self.hang = [], hang or threading.Event()
        outer = self
        class Peer(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.bodies.append(body)
                if outer.hang.is_set():
                    time.sleep(30)
                    return
                # Native's trailing per-request context (NATIVE_CONTEXT) is not part of the conversation.
                messages = [m for m in body['messages'] if m['role'] != 'system']
                last = messages[-1]
                content = last['content'] if isinstance(last['content'], list) else [{'type': 'text', 'text': last['content']}]
                if 'slow' in content[0].get('text', ''):
                    time.sleep(1.5)
                results = [b for m in messages[-2:] for b in (m['content'] if isinstance(m['content'], list) else []) if b.get('type') == 'tool_result']
                n = len(outer.bodies)
                text = ''.join(b.get('text', '') for b in content if b.get('type') == 'text')
                if 'limit' in text:
                    error = json.dumps({'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'slow down'}}).encode()
                    self.send_response(429)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(error)))
                    self.end_headers()
                    self.wfile.write(error)
                    return
                if results and last['role'] == 'user' and content[0].get('type') == 'tool_result':
                    texts = [r['content'][0]['text'] if isinstance(r['content'], list) else r['content'] for r in results]
                    payload = sse(f'm{n}', [{'type': 'text', 'text': 'got ' + '+'.join(texts)}], 'end_turn')
                elif 'refuse' in text:
                    payload = sse(f'm{n}', [{'type': 'tool_use', 'id': f'toolu_{n}r', 'name': 'mcp__hermes__probe', 'input': {}}], 'refusal',
                                  {'category': 'cyber', 'explanation': 'The request was declined.'})
                elif 'ghost' in text or 'bare' in text:
                    name = 'mcp__hermes__ghost' if 'ghost' in text else 'probe'
                    payload = sse(f'm{n}', [{'type': 'tool_use', 'id': f'toolu_{n}g', 'name': name, 'input': {'key': 'g'}}], 'tool_use')
                elif 'parallel' in text:
                    payload = sse(f'm{n}', [{'type': 'tool_use', 'id': f'toolu_{n}a', 'name': 'mcp__hermes__probe', 'input': {'key': 'a'}},
                                            {'type': 'tool_use', 'id': f'toolu_{n}b', 'name': 'mcp__hermes__probe', 'input': {'key': 'b'}}], 'tool_use')
                else:
                    payload = sse(f'm{n}', [{'type': 'text', 'text': 'echo ' + content[0].get('text', '')}], 'end_turn')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(payload)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


TOOLS = [{'type': 'function', 'function': {'name': 'probe', 'description': 'probe', 'parameters': {'type': 'object', 'properties': {'key': {'type': 'string'}}}}}]


@pytest.fixture
def warm(tmp_path):
    upstream = Upstream()
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    spawns = tmp_path / 'spawns.jsonl'
    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{upstream.server.server_port}',
           'CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM': '1', 'SPAWNS': str(spawns)}
    clients = []
    def client(drop=()):
        clients.append(directsdk.Client(command=[sys.executable, str(native)], env={k: v for k, v in env.items() if k not in drop}, timeout=20))
        return clients[-1]
    def spawned():
        return [json.loads(line) for line in spawns.read_text().splitlines()] if spawns.exists() else []
    yield SimpleFixture(client, spawned, upstream)
    for c in clients:
        c.close()
    directsdk._warm_shutdown()
    upstream.close()


class SimpleFixture:
    def __init__(self, client, spawned, upstream):
        self.client, self.spawned, self.upstream = client, spawned, upstream


def step(client, messages, stream=False):
    result = client.create(model='sonnet', messages=messages, tools=TOOLS, stream=stream)
    if stream:
        chunks = list(result)
        final = chunks[-1]
        return final._response
    return result


def as_history(response):
    message = response.choices[0].message.model_dump()
    message['content'] = (message.get('content') or '').strip()
    message.pop('reasoning_content', None)
    return message


def alive(pid):
    import psutil
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_one_native_serves_tool_rounds_and_turns_with_hermes_results(warm):
    messages = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    calls = first.choices[0].message.tool_calls
    assert [json.loads(c.function.arguments)['key'] for c in calls] == ['a', 'b']
    assert first.usage.model_dump()['native_admission']['warm'] == 'cold'
    # Native already asked the hermes MCP server for the first call; it stays parked while Hermes runs tools.
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': calls[1].id, 'content': 'BANANA'},
                 {'role': 'tool', 'tool_call_id': calls[0].id, 'content': 'APPLE'}]
    # Hermes builds request-local clients: a fresh client must still find the session by history.
    second = step(warm.client(), messages, stream=True)
    assert second.choices[0].message.content == 'got APPLE+BANANA'
    assert second.usage.model_dump()['native_admission'] == {'upstream_requests': 1, 'blocked_requests': 0, 'request_id': None, 'warm': 'reused'}
    messages += [as_history(second), {'role': 'user', 'content': 'next turn'}]
    third = step(warm.client(), messages)
    assert third.choices[0].message.content == 'echo next turn'
    assert third.usage.model_dump()['native_admission']['warm'] == 'reused'
    spawned = warm.spawned()
    assert len(spawned) == 1 and len(warm.upstream.bodies) == 3
    argv = spawned[0]['argv']
    assert '--max-turns' not in argv and argv[argv.index('--allowedTools') + 1] == 'mcp__hermes'
    assert json.loads(argv[argv.index('--mcp-config') + 1]) == {'mcpServers': {'hermes': {'type': 'sdk', 'name': 'hermes'}}}
    # The carrier is the baseline one, so a later rebuild (or a cold client) replays it unchanged.
    assert second.choices[0].message.reasoning_details[0]['type'] == directsdk.CARRIER


def test_divergence_kills_the_session_and_replays_full_history(warm):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    calls = first.choices[0].message.tool_calls
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': f'R{i}' * 50} for i, c in enumerate(calls)]
    old = warm.spawned()[0]['pid']
    second = step(warm.client(), messages)
    messages += [as_history(second)]
    # Host compaction rewrites a tool result: the warm session no longer prefixes the request.
    messages[2] = dict(messages[2], content='[compacted] R0')
    messages.append({'role': 'user', 'content': 'after compaction'})
    third = step(warm.client(), messages)
    assert third.usage.model_dump()['native_admission']['warm'] == 'rebuild:prefix'
    assert third.choices[0].message.content == 'echo after compaction'
    spawned = warm.spawned()
    assert len(spawned) == 2 and not alive(old)
    # The rebuild replayed the full (edited) history: every tool result's text, not an excerpt.
    replayed = warm.upstream.bodies[-1]['messages']
    results = [b for m in replayed if isinstance(m['content'], list) for b in m['content'] if b.get('type') == 'tool_result']
    assert [r['content'] for r in results] == ['[compacted] R0', 'R1' * 50]
    # The rebuilt session is warm again for the next step.
    messages += [as_history(third), {'role': 'user', 'content': 'again'}]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'reused'
    assert len(warm.spawned()) == 2


def test_unexpected_suffix_rebuilds(warm):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    calls = first.choices[0].message.tool_calls
    # An interrupted step: Hermes answers one call only and appends a user row.
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': calls[0].id, 'content': 'A'},
                 {'role': 'tool', 'tool_call_id': calls[1].id, 'content': 'B'}, {'role': 'user', 'content': 'steer'}]
    result = step(warm.client(), messages)
    assert result.usage.model_dump()['native_admission']['warm'] == 'rebuild:suffix'
    assert len(warm.spawned()) == 2


def test_cancel_kills_the_warm_session(warm):
    warm.upstream.hang.set()
    client = warm.client()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(step, client, [{'role': 'user', 'content': 'call probe in parallel'}])
        deadline = time.monotonic() + 10
        while not warm.upstream.bodies and time.monotonic() < deadline:
            time.sleep(.05)
        client.cancel()
        with pytest.raises(RuntimeError, match='cancelled'):
            future.result(timeout=10)
    pid = warm.spawned()[0]['pid']
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(pid) and not directsdk._warm_pool


def test_tool_free_calls_and_unflagged_clients_stay_per_call(warm, tmp_path):
    client = warm.client()
    # Auxiliary one-shots carry no tools: they never start a warm session.
    result = client.create(model='sonnet', messages=[{'role': 'user', 'content': 'title please'}])
    assert result.choices[0].message.content == 'echo title please'
    assert result.usage.model_dump()['native_admission'] == {'upstream_requests': 1, 'blocked_requests': 0, 'request_id': None}
    assert '--max-turns' in warm.spawned()[0]['argv']
    assert not directsdk._warm_pool


def test_shutdown_leaves_no_native_behind(warm):
    step(warm.client(), [{'role': 'user', 'content': 'call probe in parallel'}])
    pid = warm.spawned()[0]['pid']
    assert alive(pid) and len(directsdk._warm_pool) == 1
    directsdk._warm_shutdown()
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(pid) and not directsdk._warm_pool


def test_interleaved_conversations_keep_separate_natives(warm):
    left = [{'role': 'user', 'content': 'call probe in parallel'}]
    right = [{'role': 'user', 'content': 'call probe in parallel, please'}]
    for messages in (left, right):
        first = step(warm.client(), messages)
        messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': c.id[-2:] + '-' + messages[0]['content'][-6:]}
                                           for c in first.choices[0].message.tool_calls]
    answers = [step(warm.client(), messages) for messages in (right, left)]
    assert [a.usage.model_dump()['native_admission']['warm'] for a in answers] == ['reused', 'reused']
    assert answers[0].choices[0].message.content == 'got 2a-please+2b-please'
    assert answers[1].choices[0].message.content == 'got 1a-rallel+1b-rallel'
    assert len(warm.spawned()) == 2 and len(directsdk._warm_pool) == 2



def test_a_review_on_another_model_coexists_with_the_chat_it_forks(warm):
    """A background review forks the chat's history on another model: neither native evicts the other."""
    chat = [{'role': 'user', 'content': 'hi'}]
    chat.append(as_history(step(warm.client(), chat)))
    chat_session = directsdk._warm_pool[0]
    modes = []
    for turn in range(2):
        # Same first message, different ident (model): the review gets its own native and the chat's survives.
        review = chat + [{'role': 'user', 'content': f'review {turn}'}]
        modes.append(warm.client().create(model='opus', messages=review, tools=TOOLS).usage.model_dump()['native_admission']['warm'])
        chat.append({'role': 'user', 'content': f'chat {turn}'})
        result = step(warm.client(), chat)
        modes.append(result.usage.model_dump()['native_admission']['warm'])
        chat.append(as_history(result))
    # The chat native served every chat turn; the second review replaced only the first review's native (same ident).
    assert modes == ['cold', 'reused', 'rebuild:prefix', 'reused']
    assert len(warm.spawned()) == 3 and chat_session.alive()



# ---- Codex review (v2): each test reproduces one finding at 4f8016d ----

LAUNCHER = 'import subprocess, sys\nsys.exit(subprocess.call([sys.executable] + sys.argv[1:]))\n'


@pytest.mark.parametrize('launcher', [False, True])
def test_r2_identical_visible_histories_never_share_a_native(warm, tmp_path, launcher):
    """R2: two conversations with the same visible text differ only in native message identity."""
    def client():
        c = warm.client()
        if launcher:
            shim = tmp_path / 'launcher.py'
            shim.write_text(LAUNCHER)
            c.command = [sys.executable, str(shim)] + c.command[1:]
        return c
    a = [{'role': 'user', 'content': 'hi'}]
    b = [{'role': 'user', 'content': 'hi'}]
    sessions = []
    for messages in (a, b):
        messages.append(as_history(step(client(), messages)))
        sessions += [s for s in directsdk._warm_pool if s not in sessions]
    assert a[1]['content'] == b[1]['content'] == 'echo hi'
    # One identity throughout: the pool's own sessions and Popen pids. Behind a launcher (a Windows venv python)
    # the pid native reports is not the one Popen holds.
    session_a, session_b = sessions
    pids = session_a.pid, session_b.pid
    b.append({'role': 'user', 'content': 'b turn two'})
    assert step(client(), b).choices[0].message.content == 'echo b turn two'
    # B's own native continued; A's native was neither used nor killed; nothing replaced either.
    assert len(directsdk._warm_pool) == 2 and set(directsdk._warm_pool) == {session_a, session_b}
    assert (session_a.pid, session_b.pid) == pids and session_a.alive() and session_b.alive() and len(warm.spawned()) == 2
    assert len(session_b.consumed) == 4 and len(session_a.consumed) == 2


def test_r3_changed_native_carrier_forces_rebuild(warm):
    """R3: same visible text, different (here: removed) native carrier must not continue the old native state."""
    messages = [{'role': 'user', 'content': 'hi'}]
    messages.append(as_history(step(warm.client(), messages)))
    messages[1] = {k: v for k, v in messages[1].items() if k != 'reasoning_details'}
    messages.append({'role': 'user', 'content': 'again'})
    result = step(warm.client(), messages)
    assert result.usage.model_dump()['native_admission']['warm'] != 'reused'
    assert len(warm.spawned()) == 2


def test_r5_cancel_after_tool_calls_kills_the_parked_native(warm):
    """R5: create() returned tool calls, Hermes is running tools, the user interrupts: client.cancel()."""
    client = warm.client()
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    pid = warm.spawned()[0]['pid']
    client.cancel()
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(pid) and not directsdk._warm_pool
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'cold'


def test_r9_pool_cap_holds_with_busy_sessions(warm, monkeypatch):
    """R9: with every warm slot busy, an extra conversation takes the per-call path; nothing exceeds the cap."""
    monkeypatch.setattr(directsdk, 'WARM_POOL_SIZE', 1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        slow = pool.submit(step, warm.client(), [{'role': 'user', 'content': 'slow hi'}])
        deadline = time.monotonic() + 10
        while not warm.upstream.bodies and time.monotonic() < deadline:
            time.sleep(.02)
        other = step(warm.client(), [{'role': 'user', 'content': 'hi'}])
        assert slow.result(timeout=20).choices[0].message.content == 'echo slow hi'
    assert other.choices[0].message.content == 'echo hi'
    assert 'warm' not in other.usage.model_dump()['native_admission']
    assert '--max-turns' in warm.spawned()[1]['argv']
    assert len(directsdk._warm_pool) <= 1


def test_rauth_launch_identity_is_part_of_reuse(warm, tmp_path):
    """R-auth: a different native config dir (or command) with identical history must not reuse the old child."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    other = warm.client()
    other.env['CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR'] = str(tmp_path / 'other-config')
    assert step(other, messages).usage.model_dump()['native_admission']['warm'] != 'reused'
    assert len(warm.spawned()) == 2


def test_rauth_conflicting_override_is_refused_on_reuse(warm, monkeypatch):
    """R-auth: the inherited-environment refusal runs on every warm step, not only at spawn."""
    url = f'http://127.0.0.1:{warm.upstream.server.server_port}'
    class Local(directsdk.Admission):
        def __init__(self, upstream, timeout, queried=None):
            super().__init__(url, timeout, queried)
    monkeypatch.setattr(directsdk, 'Admission', Local)
    fixture = warm.client()
    for key, value in fixture.env.items():
        if key != 'ANTHROPIC_BASE_URL':
            monkeypatch.setenv(key, value)
    monkeypatch.delenv('ANTHROPIC_BASE_URL', raising=False)
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_FOUNDRY_API_KEY', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        monkeypatch.delenv(key, raising=False)
    client = directsdk.Client(command=fixture.command, timeout=20)
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-not-a-key')
    with pytest.raises(ValueError, match='ANTHROPIC_API_KEY'):
        step(client, messages)
    client.close()


def test_logged_out_warm_native_raises_the_login_hint(warm):
    """NON_BLOCKING: warm keeps baseline's ClaudeCodeLoggedOut type and hint."""
    from directsdk_setup import LOGGED_OUT_HINT
    client = warm.client()
    client.env['AUTH_FAIL'] = '1'
    with pytest.raises(directsdk.ClaudeCodeLoggedOut, match='Not logged in'):
        step(client, [{'role': 'user', 'content': 'hi'}])
    assert LOGGED_OUT_HINT


# ---- Codex re-review (v3) ----

def test_r5_cancel_during_handoff_is_not_published(warm, monkeypatch):
    """R5 race: the step detached its process, then cancel() arrives before release publishes the session."""
    client = warm.client()
    release = directsdk._warm_release
    def racing(*args):
        client.cancel()  # lands while the session is still busy and no process is attached
        return release(*args)
    monkeypatch.setattr(directsdk, '_warm_release', racing)
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    monkeypatch.setattr(directsdk, '_warm_release', release)
    pid = warm.spawned()[0]['pid']
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(pid) and not directsdk._warm_pool
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'cold'


def test_routine_close_after_a_step_keeps_the_session(warm):
    client = warm.client()
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages, stream=True)  # the stream is drained and closed by step()
    client.close()
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'reused'


def test_reaper_start_failure_leaks_no_reservation(warm, monkeypatch):
    """A failing reaper Thread.start must not leave a busy reservation or a dead reaper object behind."""
    import threading
    monkeypatch.setattr(directsdk, 'WARM_POOL_SIZE', 1)
    monkeypatch.setattr(directsdk, '_warm_reaper', None)
    real = threading.Thread
    class Failing(real):
        def start(self):
            if getattr(self, '_target', None) is directsdk._warm_reap:
                raise RuntimeError('injected: cannot start thread')
            return super().start()
    monkeypatch.setattr(threading, 'Thread', Failing)
    result = step(warm.client(), [{'role': 'user', 'content': 'hi'}])
    assert result.choices[0].message.content == 'echo hi'
    assert not directsdk._warm_pool and directsdk._warm_reaper is None
    monkeypatch.setattr(threading, 'Thread', real)
    again = step(warm.client(), [{'role': 'user', 'content': 'hi again'}])
    assert again.usage.model_dump()['native_admission']['warm'] == 'cold'
    assert len(directsdk._warm_pool) == 1 and directsdk._warm_reaper.is_alive()


def test_reuse_key_ignores_volatile_env_but_not_config_dir(warm, tmp_path):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = warm.client()
    first.env['CLAUDE_PID'] = '111'
    one = step(first, messages)
    messages += [as_history(one)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in one.choices[0].message.tool_calls]
    second = warm.client()
    second.env['CLAUDE_PID'] = '222'
    two = step(second, messages)
    assert two.usage.model_dump()['native_admission']['warm'] == 'reused'
    messages += [as_history(two), {'role': 'user', 'content': 'next'}]
    third = warm.client()
    third.env.update(CLAUDE_PID='333', CLAUDE_CONFIG_DIR=str(tmp_path / 'elsewhere'))
    assert step(third, messages).usage.model_dump()['native_admission']['warm'] != 'reused'
    assert len(warm.spawned()) == 2


# ---- v7: interplay with upstream main since ef73726 ----

@pytest.mark.parametrize('stream', [False, True])
def test_refusal_after_a_tool_call_is_content_filter_and_never_reused(warm, stream):
    """4c4cfbb/065edc7/31b591f: the warm step answers a refusal as baseline does, and the session is not kept."""
    messages = [{'role': 'user', 'content': 'please refuse'}]
    client = warm.client()
    if stream:
        chunks = list(client.create(model='sonnet', messages=messages, tools=TOOLS, stream=True))
        last = chunks[-1].choices[0]
        assert last.finish_reason == 'content_filter' and last.delta.refusal == 'The request was declined.'
        assert not any(c.choices[0].delta.tool_calls for c in chunks)
        response = chunks[-1]._response
    else:
        response = step(client, messages)
    message = response.choices[0].message
    assert response.choices[0].finish_reason == 'content_filter' and message.refusal == 'The request was declined.'
    assert not message.tool_calls
    # The carrier keeps native's turn as sent, cut-off call included.
    assert [b['type'] for b in message.reasoning_details[0]['messages'][0]['content']] == ['tool_use']
    assert not directsdk._warm_pool
    messages += [as_history(response), {'role': 'user', 'content': 'try again'}]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'cold'


def test_failures_carry_the_status_hermes_routes_on(warm):
    """144bd1d (#58): a relayed upstream status and a native-answered limit both reach Hermes as status_code."""
    with pytest.raises(directsdk.ClaudeAPIError, match='Incomplete upstream response') as upstream:
        step(warm.client(), [{'role': 'user', 'content': 'hit the limit'}])
    assert upstream.value.status_code == 429
    client = warm.client()
    client.env['NATIVE_ERROR'] = 'rate_limit'
    with pytest.raises(directsdk.ClaudeAPIError, match='limit reached') as native:
        step(client, [{'role': 'user', 'content': 'hi'}])
    assert native.value.status_code == 429
    assert not directsdk._warm_pool


@pytest.mark.parametrize('trigger, offered', [('ghost', 'mcp__hermes__ghost'), ('bare', 'mcp__hermes__probe')])
def test_unparked_tool_names_go_to_hermes_and_rebuild(warm, trigger, offered):
    """4336364 (#39) / 218d5e4 (#62): Hermes gets the call; native never parked it, so the next step replays."""
    messages = [{'role': 'user', 'content': 'call ' + trigger}]
    first = step(warm.client(), messages)
    (call,) = first.choices[0].message.tool_calls
    assert first.choices[0].finish_reason == 'tool_calls' and call.function.name == offered[len(directsdk.PREFIX):]
    assert first.choices[0].message.reasoning_details[0]['messages'][0]['content'][0]['name'] == offered
    assert not directsdk._warm_pool
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': call.id, 'content': 'unknown tool'}]
    second = step(warm.client(), messages)
    assert second.usage.model_dump()['native_admission']['warm'] == 'cold'
    assert second.choices[0].message.content == 'got unknown tool'


def test_traffic_policy_and_proxy_are_part_of_launch_identity(warm, monkeypatch):
    """72249fa / 7d2ae80: a session launched under one telemetry setting or proxy route is not reused under another."""
    import directsdk_setup
    monkeypatch.setattr(directsdk_setup, 'telemetry_enabled', lambda: True)
    messages = [{'role': 'user', 'content': 'hi'}]
    messages.append(as_history(step(warm.client(), messages)))
    messages.append({'role': 'user', 'content': 'same policy'})
    messages.append(as_history(step(warm.client(), messages)))
    assert len(warm.spawned()) == 1
    monkeypatch.setattr(directsdk_setup, 'telemetry_enabled', lambda: False)
    messages.append({'role': 'user', 'content': 'telemetry off'})
    response = step(warm.client(), messages)
    assert response.usage.model_dump()['native_admission']['warm'] != 'reused' and len(warm.spawned()) == 2
    messages += [as_history(response), {'role': 'user', 'content': 'via proxy'}]
    proxied = warm.client()
    proxied.env['HTTPS_PROXY'] = 'http://127.0.0.1:9'  # the loopback http upstream is never proxied
    response = step(proxied, messages)
    assert response.usage.model_dump()['native_admission']['warm'] != 'reused'
    assert len(warm.spawned()) == 3
    # A user-exported DO_NOT_TRACK reaches the child unchanged (apply_traffic_policy never removes it): a change starts a new native.
    messages += [as_history(response), {'role': 'user', 'content': 'do not track'}]
    untracked = warm.client()
    untracked.env.update(HTTPS_PROXY='http://127.0.0.1:9', DO_NOT_TRACK='1')
    assert step(untracked, messages).usage.model_dump()['native_admission']['warm'] != 'reused'
    assert len(warm.spawned()) == 4


def test_each_step_runs_under_its_own_timeout_and_trace_header(warm):
    """67a6b05 / 522b25e (#85): rearm takes the step's timeout; a per-step traceparent never forces a rebuild."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = warm.client().create(model='sonnet', messages=messages, tools=TOOLS, timeout=20,
                                 extra_headers={'traceparent': '00-' + '1' * 32 + '-' + '1' * 16 + '-01'})
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    second = warm.client().create(model='sonnet', messages=messages, tools=TOOLS, timeout=17,
                                  extra_headers={'traceparent': '00-' + '2' * 32 + '-' + '2' * 16 + '-01'})
    assert second.usage.model_dump()['native_admission']['warm'] == 'reused'
    (session,) = directsdk._warm_pool
    assert session.admission.timeout == 17


def test_killing_a_warm_session_leaves_the_shared_cwd(warm):
    """f57ddbb/8a8338b: the cwd is shared across clients; a warm kill removes only its own per-session root."""
    client = warm.client()
    step(client, [{'role': 'user', 'content': 'call probe in parallel'}])
    (session,) = directsdk._warm_pool
    root, cwd = session.root, Path(client._workdir())
    assert root.name.startswith('claude-directsdk-warm-') and root != cwd
    directsdk._warm_shutdown()
    assert not root.exists() and cwd.is_dir()


REMINDER = '\n<system-reminder>userEmail: user@example.com</system-reminder>'
MARK = {'type': 'ephemeral'}


def context_client(warm):
    client = warm.client()
    client.env['NATIVE_CONTEXT'] = '1'
    return client


def message_marks(body):
    return [(i, j) for i, m in enumerate(body['messages']) for j, b in enumerate(m['content']) if 'cache_control' in b]


def prefix(body, at):
    """The messages through block ``at`` as the cache reads them; markers are not prompt content."""
    def plain(value):
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items() if k != 'cache_control'}
        return [plain(v) for v in value] if isinstance(value, list) else value
    i, j = at
    return json.dumps(plain(body['messages'][:i] + [dict(body['messages'][i], content=body['messages'][i]['content'][:j + 1])]))


def test_warm_steps_get_mains_restoration_and_breakpoint_pinning(warm):
    """456fdb0/fda48c3/f8630df: every warm request passes through main's admission rewrites, anchored on the frame
    native replays next (Hermes' frame on a launch or user turn, native's own MCP result shape on a tool round)."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(context_client(warm), messages)
    calls = first.choices[0].message.tool_calls
    # A warm native keeps its prepended reminder first on every later request, so the relay leaves that order.
    assert [b['text'] for b in warm.upstream.bodies[0]['messages'][0]['content']] == [REMINDER, 'call probe in parallel']
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': k} for c, k in zip(calls, ('APPLE', 'BANANA'))]
    second = step(context_client(warm), messages)
    assert second.usage.model_dump()['native_admission']['warm'] == 'reused'
    tool_round = warm.upstream.bodies[1]
    # Native's reminder is not forwarded as tool output; the trailing context mark moves onto the last result (#33).
    assert tool_round['messages'][2]['content'] == [
        {'type': 'tool_result', 'tool_use_id': calls[0].id, 'content': [{'type': 'text', 'text': 'APPLE'}]},
        {'type': 'tool_result', 'tool_use_id': calls[1].id, 'content': [{'type': 'text', 'text': 'BANANA'}], 'cache_control': MARK}]
    assert message_marks(tool_round) == [(1, 1), (2, 1)]
    messages += [as_history(second), {'role': 'user', 'content': 'next turn'}]
    third = step(context_client(warm), messages)
    assert third.usage.model_dump()['native_admission']['warm'] == 'reused' and len(warm.spawned()) == 1
    turn = warm.upstream.bodies[2]
    assert message_marks(turn) == [(3, 0), (4, 0)]
    # Each request replays the previous one's prefix through its last breakpoint, so that entry is read.
    for before, after in ((warm.upstream.bodies[0], tool_round), (tool_round, turn)):
        mark = message_marks(before)[-1] if message_marks(before) else (0, 0)
        assert prefix(after, mark) == prefix(before, mark)


def test_a_cold_launch_on_a_tool_result_frame_restores_hermes_results(warm):
    """456fdb0/85dacbf: a warm launch replaying Hermes' tool results forwards them without native's reminder."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(context_client(warm), messages)
    calls = first.choices[0].message.tool_calls
    directsdk._warm_shutdown()  # reaped meanwhile: the next step launches on the full history
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': k} for c, k in zip(calls, ('APPLE', 'BANANA'))]
    second = step(context_client(warm), messages)
    assert second.usage.model_dump()['native_admission']['warm'] == 'cold' and len(warm.spawned()) == 2
    assert warm.upstream.bodies[-1]['messages'][2]['content'] == [
        {'type': 'tool_result', 'tool_use_id': calls[0].id, 'content': 'APPLE'},
        {'type': 'tool_result', 'tool_use_id': calls[1].id, 'content': 'BANANA', 'cache_control': MARK}]


# ---- v8: review of PR #99 ----

# A native that refuses the warm launch before any upstream request; its per-call launch is the usual fake.
NO_WARM = "import sys\nif '--allowedTools' in sys.argv: print('fixture: warm protocol unavailable', flush=True); sys.exit(0)\n" + NATIVE


def no_warm_client(warm, tmp_path):
    native = tmp_path / 'no_warm.py'
    native.write_text(NO_WARM)
    client = warm.client()
    client.command = [sys.executable, str(native)]
    return client


@pytest.mark.parametrize('stream', [False, True])
def test_warm_protocol_failure_before_admission_falls_back_per_call(warm, tmp_path, stream):
    """A warm launch that dies before admission serves this step per-call: one upstream request, no kept session."""
    response = step(no_warm_client(warm, tmp_path), [{'role': 'user', 'content': 'hello'}], stream=stream)
    assert response.choices[0].message.content == 'echo hello'
    assert response.usage.model_dump()['native_admission'] == {'upstream_requests': 1, 'blocked_requests': 0, 'request_id': None}
    assert len(warm.upstream.bodies) == 1 and not directsdk._warm_pool
    assert [('--max-turns' in s['argv']) for s in warm.spawned()] == [True]
    # A healthy warm native beside it still reuses its process.
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'reused'
    assert len(warm.spawned()) == 2 and len(directsdk._warm_pool) == 1


def test_cancelled_warm_failure_never_falls_back(warm, tmp_path, monkeypatch, caplog):
    """A cancel that lands while the warm launch fails ends the step: no per-call replay, no upstream request."""
    caplog.set_level('INFO', logger=directsdk.__name__)
    client = no_warm_client(warm, tmp_path)
    spawn = directsdk.Client._warm_spawn
    def cancelling(self, *args):
        try:
            spawn(self, *args)
        finally:
            self.cancel()
    monkeypatch.setattr(directsdk.Client, '_warm_spawn', cancelling)
    with pytest.raises(RuntimeError):
        step(client, [{'role': 'user', 'content': 'hello'}])
    assert not warm.upstream.bodies and not warm.spawned() and not directsdk._warm_pool
    assert 'warm fallback' not in caplog.text and 'warm kill' in caplog.text


# ---- large tool results: native 2.1.285 persists an MCP result past the tool's limit and keeps a stub ----

def persisted(tmp_path):
    return sorted(str(p) for p in tmp_path.rglob('tool-results/*'))


def tool_round(warm, big, is_error=False):
    """A parallel round answered with one large result and one small one; returns the history and the step's response."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    calls = first.choices[0].message.tool_calls
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': calls[0].id, 'content': big, **({'is_error': True} if is_error else {})},
                 {'role': 'tool', 'tool_call_id': calls[1].id, 'content': 'small'}]
    sent = len(warm.upstream.bodies)
    response = step(warm.client(), messages)
    return messages, response, sent


def upstream_results(body):
    return [(b['content'] if isinstance(b['content'], str) else ''.join(x['text'] for x in b['content'])).split('\n<system-reminder>')[0]
            for m in body['messages'] if isinstance(m['content'], list) for b in m['content'] if b.get('type') == 'tool_result']


def test_a_tool_round_over_the_message_budget_is_replayed_not_answered_over_mcp(warm, tmp_path, caplog):
    caplog.set_level('INFO', logger=directsdk.__name__)
    client = warm.client()
    client.env['FAKE_ROUND_BUDGET'] = '200000'
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    # Each has 100,000 characters but 100,001 UTF-16 units: the fake's strict >200,000 budget fires.
    texts = [letter * 99_999 + '\U0001F600' for letter in ('A', 'B')]
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': call.id, 'content': text}
                                     for call, text in zip(first.choices[0].message.tool_calls, texts)]
    sent = len(warm.upstream.bodies)
    old = warm.spawned()[0]['pid']
    response = step(warm.client(), messages)
    admission = response.usage.model_dump()['native_admission']
    results = upstream_results(warm.upstream.bodies[-1])
    assert {'mode': admission['warm'], 'full_texts': results == texts,
            'stub': any('<persisted-output>' in result for result in results)} == {
                'mode': 'rebuild:oversize', 'full_texts': True, 'stub': False}
    assert admission['upstream_requests'] == 1 and len(warm.upstream.bodies) == sent + 1
    assert not alive(old) and len(warm.spawned()) == 2 and not persisted(tmp_path)
    assert 'reason=tool-result-over-mcp-limit' in caplog.text
    messages += [as_history(response), {'role': 'user', 'content': 'next'}]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'reused'
    assert len(warm.spawned()) == 2 and upstream_results(warm.upstream.bodies[-1]) == texts


def test_a_session_parked_on_a_long_tool_survives_the_idle_limit(warm, monkeypatch):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    old = warm.spawned()[0]['pid']
    monkeypatch.setattr(directsdk, 'WARM_IDLE_SECONDS', 0.2)
    monkeypatch.setattr(directsdk, 'WARM_PARKED_IDLE_SECONDS', 3600, raising=False)
    time.sleep(0.25)
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': call.id, 'content': 'result'}
                                     for call in first.choices[0].message.tool_calls]
    response = step(warm.client(), messages)
    assert response.usage.model_dump()['native_admission']['warm'] == 'reused'
    assert alive(old) and len(warm.spawned()) == 1
    # Settled user-idle sessions still expire at the shorter limit.
    time.sleep(0.25)
    messages += [as_history(response), {'role': 'user', 'content': 'next'}]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] != 'reused'
    assert not alive(old) and len(warm.spawned()) == 2


def test_lru_evicts_a_user_idle_session_before_a_tool_parked_one(warm, monkeypatch):
    monkeypatch.setattr(directsdk, 'WARM_POOL_SIZE', 2)
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    (parked,) = directsdk._warm_pool
    step(warm.client(), [{'role': 'user', 'content': 'idle conversation'}])
    idle = next(session for session in directsdk._warm_pool if session is not parked)
    # Explicit ordering avoids relying on clock resolution or launch timing.
    parked.last_used, idle.last_used = time.monotonic() - 1, time.monotonic()
    step(warm.client(), [{'role': 'user', 'content': 'third conversation'}])
    assert parked.alive() and not idle.alive() and len(directsdk._warm_pool) == 2
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': call.id, 'content': 'result'}
                                     for call in first.choices[0].message.tool_calls]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'reused'
    assert len(warm.spawned()) == 3


@pytest.mark.parametrize('size', [49_999, 50_001, 100_000, directsdk.WARM_ROUND_SAFE - len('small')])
def test_large_results_reach_upstream_intact_over_mcp(warm, tmp_path, size):
    """tools/list advertises anthropic/maxResultSizeChars, so a result past native's 50,000 default is not persisted."""
    big = ''.join('line %06d of a large tool result\n' % i for i in range(size // 31 + 1))[:size]
    messages, response, sent = tool_round(warm, big)
    admission = response.usage.model_dump()['native_admission']
    assert admission['warm'] == 'reused' and 'unrestored' not in admission
    assert len(warm.upstream.bodies) == sent + 1 and upstream_results(warm.upstream.bodies[-1]) == [big, 'small']
    assert response.choices[0].message.content == 'got ' + big + '+small'
    assert not persisted(tmp_path) and len(warm.spawned()) == 1
    # The session stays warm with Hermes' text in its own history.
    messages += [as_history(response), {'role': 'user', 'content': 'next'}]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'reused'
    assert upstream_results(warm.upstream.bodies[-1]) == [big, 'small']


@pytest.mark.parametrize('big, is_error', [('x' * (directsdk.WARM_RESULT_CHARS + 1), False),
                                           # 250,001 code points are 500,002 UTF-16 units, which is what native counts.
                                           ('\U0001F600' * (directsdk.WARM_RESULT_CHARS // 2 + 1), False),
                                           ('E' * 20_000, True)], ids=['over-ceiling', 'over-ceiling-in-utf16', 'long-error'])
def test_a_result_native_would_rewrite_is_replayed_on_a_new_native(warm, tmp_path, caplog, big, is_error):
    """Never delivered over MCP: the parked session is killed and the step replays the history, one upstream request."""
    caplog.set_level('INFO', logger=directsdk.__name__)
    messages, response, sent = tool_round(warm, big, is_error)
    admission = response.usage.model_dump()['native_admission']
    assert admission == {'upstream_requests': 1, 'blocked_requests': 0, 'request_id': None, 'warm': 'rebuild:oversize'}
    assert len(warm.upstream.bodies) == sent + 1 and upstream_results(warm.upstream.bodies[-1]) == [big, 'small']
    old, new = [s['pid'] for s in warm.spawned()]
    assert not alive(old) and not persisted(tmp_path)
    assert 'reason=tool-result-over-mcp-limit' in caplog.text
    # The rebuilt native holds the full text and serves the next step warm.
    messages += [as_history(response), {'role': 'user', 'content': 'next'}]
    again = step(warm.client(), messages)
    assert again.usage.model_dump()['native_admission']['warm'] == 'reused' and len(warm.spawned()) == 2
    assert upstream_results(warm.upstream.bodies[-1]) == [big, 'small']


def test_a_delivered_result_native_rewrote_is_answered_then_replayed(warm, tmp_path, caplog):
    """Defense in depth: a CLI that persists below the advertised limit costs this step's text, never later steps."""
    caplog.set_level('INFO', logger=directsdk.__name__)
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    client = warm.client()
    client.env['FAKE_PERSIST_LIMIT'] = '1000'
    first = step(client, messages)
    calls = first.choices[0].message.tool_calls
    big = 'B' * 2_000
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': calls[0].id, 'content': big}, {'role': 'tool', 'tool_call_id': calls[1].id, 'content': 'small'}]
    response = step(warm.client(), messages)
    admission = response.usage.model_dump()['native_admission']
    assert admission['warm'] == 'reused' and admission['unrestored']
    assert response.choices[0].message.content.startswith('got <persisted-output>') and len(persisted(tmp_path)) == 1
    assert not directsdk._warm_pool and 'unrestored=1' in caplog.text and 'reason=native-rewrote-tool-result' in caplog.text
    messages += [as_history(response), {'role': 'user', 'content': 'next'}]
    again = step(warm.client(), messages)
    assert again.usage.model_dump()['native_admission']['warm'] == 'cold'
    assert upstream_results(warm.upstream.bodies[-1]) == [big, 'small']


def test_intact_results_forwarded_in_another_order_stay_warm(warm):
    """Unrestored alone is no rewrite: the same texts in another order keep the session."""
    client = warm.client()
    client.env['FAKE_REVERSE'] = '1'
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    calls = first.choices[0].message.tool_calls
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': calls[0].id, 'content': 'APPLE'}, {'role': 'tool', 'tool_call_id': calls[1].id, 'content': 'BANANA'}]
    response = step(warm.client(), messages)
    admission = response.usage.model_dump()['native_admission']
    assert admission['warm'] == 'reused' and admission['unrestored'] and response.choices[0].message.content == 'got BANANA+APPLE'
    messages += [as_history(response), {'role': 'user', 'content': 'next'}]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'reused'
    assert len(warm.spawned()) == 1


def _set_warm(value):
    """Write the setting where `hermes config set plugins.entries.<id>.settings.warm_session` puts it."""
    import directsdk_setup
    from hermes_cli.config import get_config_path
    get_config_path().write_text("plugins:\n  enabled: []\n  entries:\n    %s:\n      settings:\n        warm_session: %s\n"
                                 % (directsdk_setup.PLUGIN_ID, value), encoding='utf-8')


@pytest.mark.parametrize('setting, env, expected', [
    (None, {}, False), ('true', {}, True), ('false', {}, False), ('"on"', {}, True), ("''", {}, False), ('null', {}, False),
    ('true', {'CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM': '0'}, False), ('false', {'CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM': '1'}, True),
    ('true', {'CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM': ''}, True)])
def test_warm_follows_the_profile_setting_unless_the_env_flag_is_set(profile, setting, env, expected):
    if setting is not None:
        _set_warm(setting)
    assert directsdk._warm_enabled(env) is expected


def test_each_profile_of_a_multi_profile_process_reads_its_own_warm_setting(profile, tmp_path):
    """A desktop backend or gateway serving several profiles loads only its launch home's .env: the setting is per profile."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    warm_home = tmp_path / 'warm-profile'
    warm_home.mkdir()
    token = set_hermes_home_override(warm_home)
    try:
        _set_warm('true')
        assert directsdk._warm_enabled({}) is True
    finally:
        reset_hermes_home_override(token)
    assert directsdk._warm_enabled({}) is False


def test_the_profile_setting_alone_turns_warm_sessions_on(profile, warm):
    _set_warm('true')
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(drop=('CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM',)), messages)
    assert first.usage.model_dump()['native_admission']['warm'] == 'cold'
    calls = first.choices[0].message.tool_calls
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': c.id[-4:]} for c in calls]
    second = step(warm.client(drop=('CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM',)), messages)
    assert second.usage.model_dump()['native_admission']['warm'] == 'reused'
    (spawned,) = warm.spawned()
    assert spawned['argv'][spawned['argv'].index('--allowedTools') + 1] == 'mcp__hermes'


def test_warm_request_two_replays_the_first_turn_as_request_one_forwarded_it(warm):
    """PR #29 review: the #77 first-turn reorder must not run on a warm session. A live native replays its first
    turn with the reminder first; reordering request 1 alone made request 2 miss the cache and re-write it all."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(context_client(warm), messages)
    calls = first.choices[0].message.tool_calls
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': k} for c, k in zip(calls, ('APPLE', 'BANANA'))]
    second = step(context_client(warm), messages)
    assert second.usage.model_dump()['native_admission']['warm'] == 'reused'
    one, two = warm.upstream.bodies[0], warm.upstream.bodies[1]
    plain = lambda m: [{k: v for k, v in b.items() if k != 'cache_control'} for b in m['content']]
    assert plain(two['messages'][0]) == plain(one['messages'][0])
