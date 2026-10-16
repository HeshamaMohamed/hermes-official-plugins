"""Real native executable, synthetic HTTP peer; no vendor calls or credentials.

Hermes composes every user turn itself, so native must deliver its text as written. A turn whose text starts with
a built-in command name ("/status please") was answered by native's own headless command dispatch ("/status isn't
available in this environment.", model "<synthetic>") with no upstream request, even under
--disable-slash-commands; the step then failed as an incomplete native response and the user got no reply.

Run: .venv/bin/python evals/directsdk_composed_text.py /path/to/claude
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import directsdk

REPLY = 'FIXTURE REPLY'
TEXTS = ('/status please', '/help', '/compact now', '/model opus')
# Warm mode serves only calls with tools; without one both rows would run per-call.
TOOLS = [{'type':'function', 'function':{'name':'read_file', 'description':'Read a fixture', 'parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}}]


def sse(text):
    rows = [
        {'type':'message_start', 'message':{'id':'msg_fixture', 'type':'message', 'role':'assistant', 'model':'fixture', 'content':[], 'stop_reason':None, 'stop_sequence':None, 'usage':{'input_tokens':1, 'output_tokens':0}}},
        {'type':'content_block_start', 'index':0, 'content_block':{'type':'text', 'text':''}},
        {'type':'content_block_delta', 'index':0, 'delta':{'type':'text_delta', 'text':text}},
        {'type':'content_block_stop', 'index':0},
        {'type':'message_delta', 'delta':{'stop_reason':'end_turn', 'stop_sequence':None}, 'usage':{'output_tokens':1}},
        {'type':'message_stop'}]
    return ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in rows).encode()


class Peer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.wfile.write(sse(REPLY))
        self.wfile.flush()


def user_texts(body):
    return [b['text'] for m in body['messages'] if m['role'] == 'user' and isinstance(m['content'], list) for b in m['content'] if b.get('type') == 'text']


def run(binary):
    binary = str(Path(binary).absolute())
    rows = []
    # (case, opening history, next user text or None): the next text is sent as a second step on the same client,
    # which in warm mode goes to the parked native session instead of a fresh one.
    cases = [(text, [{'role':'user', 'content':text}], None) for text in TEXTS]
    # A command-shaped turn earlier in the history replays as written too.
    cases.append(('replayed', [{'role':'user', 'content':'/status'}, {'role':'assistant', 'content':'Status is fine.'}, {'role':'user', 'content':'And now?'}], None))
    cases.append(('next step', [{'role':'user', 'content':'Hello'}], '/status please'))
    for warm in ('0', '1'):
        for case, messages, following in cases:
            with tempfile.TemporaryDirectory(prefix='directsdk-composed-eval-') as home:
                peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
                peer.bodies = []
                thread = threading.Thread(target=peer.serve_forever, daemon=True)
                thread.start()
                env = {'PATH':os.defpath, 'HOME':home, 'HERMES_HOME':home, 'XDG_CONFIG_HOME':home, 'CLAUDE_CONFIG_DIR':home,
                       'ANTHROPIC_API_KEY':'fixture-not-a-real-key', 'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}',
                       'DISABLE_TELEMETRY':'1', 'DISABLE_ERROR_REPORTING':'1', 'CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM':warm}
                client = directsdk.Client(command=binary, env=env, timeout=10)
                row = {'warm':warm, 'case':case, 'replies':[], 'warm_modes':[]}
                steps = [messages]
                try:
                    try:
                        while len(row['replies']) < (1 if following is None else 2):
                            result = client.create(model='claude-sonnet-4-6', messages=steps[-1], tools=TOOLS, max_tokens=2400)
                            row['replies'].append(result.choices[0].message.content)
                            row['warm_modes'].append(result.usage.model_dump()['native_admission'].get('warm'))
                            message = result.choices[0].message.model_dump()
                            steps.append(steps[-1] + [{**message, 'content':(message['content'] or '').strip()}, {'role':'user', 'content':following}])
                    except Exception as exc:
                        row['raised'] = f'{type(exc).__name__}: {exc}'
                    row['upstream_requests'] = len(peer.bodies)
                    row['upstream_user_texts'] = [t for body in peer.bodies[-1:] for t in user_texts(body)]
                    sent = [m['content'] for m in steps[len(row['replies']) - 1] if m['role'] == 'user'] if row['replies'] else []
                    expected = 1 if following is None else 2
                    row['ok'] = row['replies'] == [REPLY] * expected and len(peer.bodies) == expected and all(t in row['upstream_user_texts'] for t in sent) \
                        and not any('<local-command-stdout>' in t for body in peer.bodies for t in user_texts(body)) \
                        and (warm == '0' or following is None or row['warm_modes'][-1] == 'reused')
                    rows.append(row)
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                finally:
                    client.close()
                    directsdk._warm_shutdown()
                    peer.shutdown()
                    thread.join()
                    peer.server_close()
    failed = [row for row in rows if not row['ok']]
    assert not failed, json.dumps(failed, indent=2, ensure_ascii=False)
    return {'native_version':subprocess.check_output([binary, '--version'], text=True, stdin=subprocess.DEVNULL).strip(),
            'synthetic_peer':True, 'cases':rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary')
    print(json.dumps(run(parser.parse_args().binary), indent=2, ensure_ascii=False))
