"""Test bootstrap and shared fixture.

The standalone plugin imports Hermes core (`providers`, `agent`) from a checkout: set
``HERMES_AGENT_REPO`` to your hermes-agent clone (default ``~/.hermes/hermes-agent``).

``profile`` loads this repo exactly as `hermes plugins install` leaves it —
``$HERMES_HOME/plugins/<name>/`` with ``kind: model-provider`` — through core's provider discovery.
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

HERMES_AGENT_REPO = Path(os.environ.get("HERMES_AGENT_REPO") or Path.home() / ".hermes" / "hermes-agent").expanduser()
REPO_ROOT = Path(__file__).resolve().parent
PLUGIN_NAME = "claude-subscription-directsdk-experimental"
# Importing core's agent.conversation_loop imports hermes_bootstrap, which on a self-updating install calls
# hermes_cli.venv_sync.prepare_launch and os.execv's this process into the store Python (hermes_bootstrap.py
# `os.execv(str(_launch_python), _command)`). That interpreter has no pytest, so the run dies with no report.
# prepare_launch skips the relaunch when this is set (hermes_cli/venv_sync.py); core's own tests set it too.
os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
for path in (str(HERMES_AGENT_REPO), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

collect_ignore = ["__init__.py", "directsdk.py", "directsdk_setup.py", "admission.py", "model_catalog.py",
                  "inert_mcp.py", "evals"]


@pytest.fixture
def profile(tmp_path, monkeypatch):
    import providers

    home = tmp_path / "hermes-home"
    installed = home / "plugins" / PLUGIN_NAME
    shutil.copytree(REPO_ROOT, installed, ignore=shutil.ignore_patterns(".git", "tests", "evals", "__pycache__"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    # A machine with Claude Code installed must not leak it into offline tests.
    monkeypatch.setenv("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND", str(tmp_path / "no-such-claude"))
    for name in tuple(sys.modules):
        if name.startswith("_hermes_user_provider_"):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(providers, "_REGISTRY", {})
    monkeypatch.setattr(providers, "_ALIASES", {})
    monkeypatch.setattr(providers, "_PROVIDER_LIST_CACHE", None)
    monkeypatch.setattr(providers, "_discovered", False)
    providers._discover_providers()
    result = providers.get_provider_profile(PLUGIN_NAME)
    assert result is not None, "installed-plugin discovery (step 2b) did not register the provider"
    return result


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path_factory, monkeypatch):
    """Plugin settings (warm_session, telemetry) are read from $HERMES_HOME/config.yaml on every call.

    Without this the suite inherits the caller's profile: a profile with ``warm_session: true`` sends the
    one-shot fakes down the warm path, where each waits out its full timeout. A test that needs its own home
    sets it with monkeypatch after this runs (``profile`` does)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path_factory.mktemp("isolated-hermes-home")))


@pytest.fixture(autouse=True, scope="session")
def _fast_server_stop():
    """Cap every ``serve_forever()`` poll at 10 ms in the test process.

    The fake upstreams used the default 0.5 s poll and admission's relay uses 0.05 s, so every ``shutdown()``
    waited up to that long for the loop to notice. On these servers (plain HTTPServer / ThreadingHTTPServer,
    no ``service_actions`` override) the poll paces only that stop check: requests are served on socket
    readiness, so no request path changes."""
    import socketserver

    original = socketserver.BaseServer.serve_forever

    def serve_forever(self, poll_interval=0.5):
        return original(self, min(poll_interval, 0.01))

    socketserver.BaseServer.serve_forever = serve_forever
    yield
    socketserver.BaseServer.serve_forever = original
