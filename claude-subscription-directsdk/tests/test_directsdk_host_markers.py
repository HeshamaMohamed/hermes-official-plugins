"""A Hermes launched from inside a Claude Code or Agent SDK host (Hermes.app opened from the Claude app) inherits
that host's identity variables. Native reads them as "an SDK host is driving me" and sends its own side requests
first: a session title (a ``{"title": ...}`` JSON-schema call), then a status check. The admission gate admits
exactly one upstream request per step, so the title request became the turn's reply and the real turn was denied.

Hermes is native's only host: the turn's native never sees another host's markers.
"""
import pytest

from test_directsdk_telemetry import _turn_env

# Seen on the 2026-10-16 Desktop backend (Hermes.app relaunched from the Claude app) and in a Claude Code session.
HOST_MARKERS = {
    "CLAUDE_AGENT_SDK_VERSION": "0.3.299",
    "CLAUDE_AGENT_SDK_MCP_NO_PREFIX": "1",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
    "CLAUDE_CODE_SESSION_ID": "host-session",
    "CLAUDE_CODE_HOST_SESSION_ID": "host-session",
    "CLAUDE_CODE_CHILD_SESSION": "1",
    "CLAUDE_CODE_SESSION_ATTENDED": "1",
    "CLAUDE_CODE_DESKTOP_APP_VERSION": "1.0.0",
    "CLAUDE_PID": "4242",
}


@pytest.mark.parametrize("marker", sorted(HOST_MARKERS))
def test_a_turns_native_never_inherits_another_claude_hosts_identity(profile, tmp_path, marker):
    env = _turn_env(profile, tmp_path, **{marker: HOST_MARKERS[marker]})
    assert marker not in env


def test_the_users_own_claude_settings_still_reach_native(profile, tmp_path):
    """Only host identity is dropped; native configuration the user exported is not."""
    own = {"CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1", "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config")}
    env = _turn_env(profile, tmp_path, **own, **HOST_MARKERS)
    assert {k: env.get(k) for k in own} == own
