"""Agent interface for chat processing.

Wires the real AgentRunner with MCP tools for task management.
Imports are deferred to avoid double-loading SQLModel tables.
"""
from typing import List, Dict, Any, Optional


class ChatAgentRunner:
    """Wraps the real AgentRunner for use by the chat endpoint."""

    def __init__(self):
        # Lazy import to avoid circular / double-registration issues
        from mcp.server import MCPToolServer
        from agent.runner import AgentRunner
        from database.session import engine

        # get_session is a generator (FastAPI dep), MCP tools need a plain factory
        def session_factory():
            from sqlmodel import Session
            return Session(engine)

        self._mcp_server = MCPToolServer(session_factory=session_factory)
        self._runner = AgentRunner(
            session_factory=session_factory,
            mcp_server=self._mcp_server,
        )

    def process(self, messages: List[Dict[str, Any]], user_id: str = "", conversation_id: str = "") -> str:
        """Sync wrapper - runs the async agent and returns response text."""
        import asyncio
        from agent.context import AgentContext, AgentResult

        context = AgentContext.from_request(
            user_id=user_id,
            conversation_id=conversation_id,
            messages=messages,
        )

        last_message = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_message = msg.get("content", "")
                break

        if not last_message:
            return "I didn't understand your request. Could you please try again?"

        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result: AgentResult = pool.submit(
                    asyncio.run, self._runner.run(context, last_message)
                ).result()
        except Exception as e:
            return f"Sorry, something went wrong: {str(e)}"

        return result.response


_agent_instance: Optional[ChatAgentRunner] = None


def get_agent_runner() -> ChatAgentRunner:
    """Get the agent runner instance (singleton)."""
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = ChatAgentRunner()
    return _agent_instance
