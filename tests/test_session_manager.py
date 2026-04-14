import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from bot.config import Config
from bot.db import init_db
from bot.session_manager import SessionManager
from bot.providers.base import ProviderResponse


@pytest.fixture
def config():
    return Config(
        telegram_token="test",
        claude_path="claude",
        subprocess_timeout_minutes=1,
    )


@pytest_asyncio.fixture
async def conn(tmp_path):
    connection = await init_db(str(tmp_path / "test.db"))
    yield connection
    await connection.close()


@pytest.fixture
def mock_response():
    return ProviderResponse(
        session_id="mock-sid-1",
        text="Hello from Claude",
        cost=0.001,
        duration_seconds=3.0,
        error=None,
    )


@pytest.fixture
def mock_provider(mock_response):
    """Create a mock provider that returns mock_response."""
    provider = MagicMock()
    provider.name = "claude"
    provider.run = AsyncMock(return_value=mock_response)
    return provider


@pytest.mark.asyncio
async def test_create_session(conn, config, mock_provider):
    mgr = SessionManager(config, conn)
    mgr.register_provider(mock_provider)
    resp = await mgr.create_session("test-session", "/tmp", "hello")
    assert resp.session_id == "mock-sid-1"

    sessions = await mgr.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["name"] == "test-session"
    assert sessions[0]["status"] == "waiting"


@pytest.mark.asyncio
async def test_create_duplicate_name(conn, config, mock_provider):
    mgr = SessionManager(config, conn)
    mgr.register_provider(mock_provider)
    await mgr.create_session("dup-name", "/tmp", "hello")

    with pytest.raises(ValueError, match="already active"):
        await mgr.create_session("dup-name", "/tmp", "hello again")


@pytest.mark.asyncio
async def test_resume_session(conn, config, mock_response):
    resume_response = ProviderResponse(
        session_id="mock-sid-1",
        text="Resumed response",
        cost=0.002,
        duration_seconds=5.0,
        error=None,
    )

    provider = MagicMock()
    provider.name = "claude"
    provider.run = AsyncMock(side_effect=[mock_response, resume_response])

    mgr = SessionManager(config, conn)
    mgr.register_provider(provider)
    await mgr.create_session("resume-test", "/tmp", "initial")
    resp = await mgr.resume_session("mock-sid-1", "continue")
    assert resp.text == "Resumed response"


@pytest.mark.asyncio
async def test_stop_session(conn, config, mock_provider):
    mgr = SessionManager(config, conn)
    mgr.register_provider(mock_provider)
    await mgr.create_session("stop-test", "/tmp", "hello")
    await mgr.stop_session("mock-sid-1")

    sessions = await mgr.list_sessions()
    assert len(sessions) == 0


@pytest.mark.asyncio
async def test_stop_by_name(conn, config, mock_provider):
    mgr = SessionManager(config, conn)
    mgr.register_provider(mock_provider)
    await mgr.create_session("named-stop", "/tmp", "hello")
    await mgr.stop_session_by_name("named-stop")

    sessions = await mgr.list_sessions()
    assert len(sessions) == 0


@pytest.mark.asyncio
async def test_stop_nonexistent(conn, config):
    mgr = SessionManager(config, conn)
    with pytest.raises(ValueError, match="not found"):
        await mgr.stop_session_by_name("ghost")
