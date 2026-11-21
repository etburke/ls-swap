import pytest


@pytest.fixture(autouse=True)
def _mcp_free_port(monkeypatch):
    monkeypatch.setenv("CRANK_MCP_PORT", "0")      # any server a test starts picks a free port
