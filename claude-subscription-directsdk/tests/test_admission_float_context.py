"""A rebuilt native reads the cache of the native it replaces.

A live native keeps its launch context where it first put it: the date line inside the environment system message
(turn-1 launch) or in its own system message after the launch frame, and the account reminder before that frame.
A fresh native replaying the same history puts that context after the newest frame, and receives tool results as
Hermes replays them, not in the one-text-block form an MCP reply leaves in a live native's history. These fixtures
use native 2.1.296's layouts, measured on the wire with synthetic content.
"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admission import float_native_context, pin_message_breakpoint
import directsdk

ENV = 'ENVIRONMENT\nPrimary working directory: /tmp/x'
DATE = "Today's date is 2026-10-14."
REMINDER = {'type': 'text', 'text': '<system-reminder>userEmail: user@example.com</system-reminder>'}
ASK = {'type': 'text', 'text': 'Public question one'}
TURN = {'type': 'text', 'text': 'Public followup'}
MARK = {'type': 'ephemeral', 'ttl': '1h'}


def tool_use(n):
    return {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': f't{n}', 'name': 'mcp__hermes__probe', 'input': {}}]}


def result(n, listed=True):
    text = f'Public tool result {n}'
    return {'type': 'tool_result', 'tool_use_id': f't{n}', 'content': [{'type': 'text', 'text': text}] if listed else text}


def wire(messages):
    return json.dumps({'model': 'm', 'messages': messages}).encode()


def relay(payload, queried, recorded):
    """The warm relay's two context steps, in the Handler's order."""
    payload, recorded = float_native_context(payload, queried, recorded)
    return json.loads(pin_message_breakpoint(payload, queried)), recorded


def plain(value):
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items() if k != 'cache_control'}
    return [plain(v) for v in value] if isinstance(value, list) else value


def marks(body):
    return [(i, j) for i, m in enumerate(body['messages']) if isinstance(m['content'], list)
            for j, b in enumerate(m['content']) if 'cache_control' in b]


def prefix(body, at):
    i, j = at
    return plain(body['messages'][:i] + [dict(body['messages'][i], content=body['messages'][i]['content'][:j + 1])])


def test_a_turn_one_native_and_a_replay_share_the_prefix():
    # Live native launched on turn 1: the date line is merged into its environment message for its whole life.
    live = [{'role': 'user', 'content': [ASK]}, {'role': 'system', 'content': ENV + '\n\n' + DATE},
            tool_use(1), {'role': 'user', 'content': [dict(result(1), cache_control=MARK)]}]
    # A fresh native replaying the same history: environment alone, date after the newest frame.
    fresh = [{'role': 'user', 'content': [ASK]}, {'role': 'system', 'content': ENV}, tool_use(1),
             {'role': 'user', 'content': [dict(result(1), cache_control=MARK)]},
             {'role': 'system', 'content': [{'type': 'text', 'text': DATE}]}]
    a, _ = relay(wire(live), [result(1)], [])
    b, _ = relay(wire(fresh), [result(1)], None)
    assert a == b
    assert a['messages'][-1] == {'role': 'system', 'content': [{'type': 'text', 'text': ENV + '\n\n' + DATE}]}
    assert [m['role'] for m in a['messages']] == ['user', 'assistant', 'user', 'system']


def test_a_native_launched_from_replay_keeps_no_date_inside_the_prefix():
    # Launched on 'Public followup' (index 3); its date message stays right after that frame on later requests.
    live = [{'role': 'user', 'content': [ASK]}, {'role': 'system', 'content': ENV}, tool_use(1),
            {'role': 'user', 'content': [result(1), TURN]}, {'role': 'system', 'content': DATE},
            tool_use(2), {'role': 'user', 'content': [dict(result(2), cache_control=MARK)]}]
    fresh = [m for m in live if m['content'] != DATE] + [{'role': 'system', 'content': [{'type': 'text', 'text': DATE}]}]
    a, _ = relay(wire(live), [result(2)], [])
    b, _ = relay(wire(fresh), [result(2)], None)
    at = marks(a)[-1]
    assert prefix(a, at) == prefix(b, at)


def test_the_launch_reminder_is_recorded_once_and_moved_after_the_newest_frame_on_every_request():
    first, recorded = relay(wire([{'role': 'user', 'content': [REMINDER, TURN]},
                                  {'role': 'system', 'content': [{'type': 'text', 'text': DATE, 'cache_control': MARK}]}]), [TURN], None)
    assert recorded == [REMINDER]
    assert first['messages'][0]['content'] == [dict(TURN, cache_control=MARK), REMINDER]
    # Later request: native replays the launch turn as it built it (reminder first).
    later, recorded = relay(wire([{'role': 'user', 'content': [REMINDER, TURN]}, tool_use(1),
                                  {'role': 'user', 'content': [dict(result(1), cache_control=MARK)]}]), [result(1)], recorded)
    assert later['messages'][0]['content'] == [TURN]
    assert later['messages'][2]['content'] == [dict(result(1), cache_control=MARK), REMINDER]
    # The first request's cache entry (through TURN) recurs in the later request.
    assert prefix(later, (0, 0)) == prefix(first, (0, 0))


def test_nothing_is_added_dropped_or_edited():
    live = [{'role': 'user', 'content': [REMINDER, TURN]}, {'role': 'system', 'content': ENV}, tool_use(1),
            {'role': 'user', 'content': [result(1)]}, {'role': 'system', 'content': [{'type': 'text', 'text': DATE}]}]
    body, _ = relay(wire(live), [result(1)], [REMINDER])
    texts = lambda ms: sorted(json.dumps(plain(b), sort_keys=True) for m in ms if isinstance(m['content'], list) for b in m['content'])
    assert texts([m for m in body['messages'] if m['role'] != 'system']) == texts([m for m in live if m['role'] != 'system'])
    assert body['messages'][-1]['content'][0]['text'] == ENV + '\n\n' + DATE


def test_an_unparsed_or_non_text_system_message_forwards_as_is():
    odd = wire([{'role': 'user', 'content': [TURN]}, {'role': 'system', 'content': [{'type': 'image', 'source': {}}]}])
    assert float_native_context(odd, [TURN], None)[0] == odd
    assert float_native_context(b'not json', [TURN], None) == (b'not json', None)


def test_replayed_tool_results_take_the_form_native_keeps_from_an_mcp_reply():
    history = [{'role': 'user', 'content': 'go'},
               {'role': 'assistant', 'content': '', 'tool_calls': [
                   {'id': 'a', 'type': 'function', 'function': {'name': 'probe', 'arguments': '{}'}},
                   {'id': 'b', 'type': 'function', 'function': {'name': 'probe', 'arguments': '{}'}},
                   {'id': 'c', 'type': 'function', 'function': {'name': 'probe', 'arguments': '{}'}}]},
               {'role': 'tool', 'tool_call_id': 'a', 'content': 'out'},
               {'role': 'tool', 'tool_call_id': 'b', 'content': 'bad', 'is_error': True},
               {'role': 'tool', 'tool_call_id': 'c', 'content': ''}]
    frame = directsdk.prepare_history(history, {'probe'})[1][-1]['message']['content']
    assert frame == [{'type': 'tool_result', 'tool_use_id': 'a', 'content': [{'type': 'text', 'text': 'out'}]},
                     {'type': 'tool_result', 'tool_use_id': 'b', 'content': [{'type': 'text', 'text': 'bad'}], 'is_error': True},
                     {'type': 'tool_result', 'tool_use_id': 'c', 'content': ''}]
