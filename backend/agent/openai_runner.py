"""OpenAI-powered Agent Runner.

Uses GPT-4 with function calling to:
1. Answer general questions naturally
2. Execute task operations through MCP tools
"""

import json
import logging
from typing import Callable, Dict, Any, List, Optional

from openai import OpenAI
from sqlmodel import Session

from agent.context import AgentContext, AgentResult, ToolCallRecord
from mcp.server import MCPToolServer, get_tool_definitions
from core.config import settings

logger = logging.getLogger(__name__)

# System prompt for the AI assistant
SYSTEM_PROMPT = """You are a helpful AI assistant that can both answer general questions AND manage tasks.

## Your Capabilities:
1. **General Questions**: Answer any question the user asks - about science, history, coding, life advice, etc.
2. **Task Management**: Help users manage their to-do list using the available tools.

## Task Management Tools:
- `add_task`: Create a new task
- `list_tasks`: Show all tasks
- `complete_task`: Mark a task as done
- `delete_task`: Remove a task
- `update_task`: Modify a task

## Guidelines:
- Be conversational and friendly
- For task operations, use the appropriate tool
- For general questions, answer directly without using tools
- If the user wants to manage tasks, always use the tools - don't just describe what you would do
- When listing tasks, format them nicely
- Support both English and Urdu (Roman Urdu too)

## Examples:
- "What is Python?" → Answer the question directly
- "Add a task to buy groceries" → Use add_task tool
- "Show my tasks" → Use list_tasks tool
- "Who invented the telephone?" → Answer directly
- "Mark task 1 as complete" → Use complete_task tool
"""


def convert_tools_to_openai_format(tools: List[Dict]) -> List[Dict]:
    """Convert MCP tool definitions to OpenAI function format."""
    openai_tools = []
    for tool in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"]
            }
        })
    return openai_tools


class OpenAIAgentRunner:
    """Agent runner powered by OpenAI GPT with function calling."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        mcp_server: MCPToolServer
    ):
        """Initialize the OpenAI agent.

        Args:
            session_factory: Factory to create DB sessions
            mcp_server: MCP server for tool execution
        """
        self._session_factory = session_factory
        self._mcp_server = mcp_server
        self._client: Optional[OpenAI] = None

    def _get_client(self) -> OpenAI:
        """Lazy initialization of OpenAI client."""
        if self._client is None:
            if not settings.OPENAI_API_KEY:
                raise ValueError(
                    "OPENAI_API_KEY is not set. Please add it to your environment variables."
                )
            self._client = OpenAI(api_key=settings.OPENAI_API_KEY)
        return self._client

    async def run(
        self,
        context: AgentContext,
        message: str
    ) -> AgentResult:
        """Execute agent with user message.

        Args:
            context: Request context with user_id and history
            message: User's natural language input

        Returns:
            AgentResult with response and tool call records
        """
        tool_calls_record: List[ToolCallRecord] = []

        try:
            # Build messages array
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]

            # Add conversation history (last 10 messages for context)
            for msg in context.messages[-10:]:
                messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", "")
                })

            # Get OpenAI tools from MCP definitions
            openai_tools = convert_tools_to_openai_format(get_tool_definitions())

            # Call OpenAI
            client = self._get_client()
            response = client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=messages,
                tools=openai_tools,
                tool_choice="auto",
                max_tokens=1024,
                temperature=0.7
            )

            assistant_message = response.choices[0].message

            # Check if model wants to call tools
            if assistant_message.tool_calls:
                # Execute all tool calls
                tool_results = []
                for tool_call in assistant_message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments)

                    # Inject user_id into params
                    tool_args["user_id"] = context.user_id

                    logger.info(f"Executing tool: {tool_name} with args: {tool_args}")

                    # Call MCP tool
                    try:
                        result = self._mcp_server.call(
                            tool_name,
                            tool_args,
                            int(context.user_id)
                        )
                        tool_results.append({
                            "tool_call_id": tool_call.id,
                            "output": json.dumps(result)
                        })

                        # Record the tool call
                        tool_calls_record.append(ToolCallRecord(
                            tool_name=tool_name,
                            parameters={k: v for k, v in tool_args.items() if k != "user_id"},
                            result=result,
                            duration_ms=0
                        ))
                    except Exception as e:
                        logger.exception(f"Tool {tool_name} failed: {e}")
                        tool_results.append({
                            "tool_call_id": tool_call.id,
                            "output": json.dumps({"error": str(e)})
                        })

                # Send tool results back to get final response
                messages.append(assistant_message.model_dump())
                for tool_result in tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_result["tool_call_id"],
                        "content": tool_result["output"]
                    })

                # Get final response
                final_response = client.chat.completions.create(
                    model=settings.OPENAI_MODEL,
                    messages=messages,
                    max_tokens=1024,
                    temperature=0.7
                )

                response_text = final_response.choices[0].message.content or "Task completed."
            else:
                # No tool calls - direct response
                response_text = assistant_message.content or "I'm not sure how to respond to that."

            return AgentResult(
                success=True,
                response=response_text,
                tool_calls=tool_calls_record,
                language="en"
            )

        except Exception as e:
            logger.exception(f"OpenAI agent error: {e}")
            error_msg = str(e)

            # Provide helpful error message
            if "api_key" in error_msg.lower() or "authentication" in error_msg.lower():
                response_text = "The AI service is not configured. Please set the OPENAI_API_KEY environment variable."
            else:
                response_text = f"Sorry, something went wrong: {error_msg}"

            return AgentResult(
                success=False,
                response=response_text,
                tool_calls=tool_calls_record,
                error=error_msg,
                language="en"
            )
