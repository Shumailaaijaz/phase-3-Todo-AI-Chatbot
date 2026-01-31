"""OpenAI-powered Agent Runner.

Uses GPT-4 with function calling to:
1. Answer general questions naturally
2. Execute task operations through MCP tools
3. Support multilingual responses (English, Urdu, Roman Urdu)
"""

import json
import logging
import os
from typing import Callable, Dict, Any, List, Optional

from sqlmodel import Session

from agent.context import AgentContext, AgentResult, ToolCallRecord
from mcp.server import MCPToolServer, get_tool_definitions

logger = logging.getLogger(__name__)

# System prompt for the AI assistant - Multilingual support
SYSTEM_PROMPT = """You are a helpful, friendly AI assistant that can answer ANY question and also manage tasks.

## IMPORTANT - Language Rules:
- Detect the language the user is speaking (English, Urdu, or Roman Urdu)
- ALWAYS respond in the SAME language the user used
- If user writes in Urdu script (اردو), respond in Urdu script
- If user writes in Roman Urdu (like "kya haal hai"), respond in Roman Urdu
- If user writes in English, respond in English

## Your Capabilities:
1. **Answer ANY Question**: Science, history, coding, math, life advice, jokes, stories, explanations - ANYTHING!
2. **Task Management**: Help users manage their to-do list using the available tools.

## Task Management Tools (use ONLY for task operations):
- `add_task`: Create a new task (use when user wants to add/create a task)
- `list_tasks`: Show all tasks (use when user wants to see their tasks)
- `complete_task`: Mark a task as done (use when user says task is done/complete)
- `delete_task`: Remove a task (use when user wants to delete/remove a task)
- `update_task`: Modify a task (use when user wants to change a task)

## Guidelines:
- Be conversational, helpful, and friendly
- For general questions (who am I, what is X, explain Y), answer DIRECTLY - do NOT use tools
- For task operations, use the appropriate tool
- When user asks about themselves (who am I), explain you're an AI assistant
- Support conversations in any language

## Examples:
- "What is Python?" → Answer directly about Python programming language
- "who am I" → Explain that you're an AI and can help them
- "Add task buy milk" → Use add_task tool
- "mujhe apne tasks dikhao" → Use list_tasks tool, respond in Roman Urdu
- "آپ کون ہیں" → Answer in Urdu script
- "Tell me a joke" → Tell a joke directly
- "2+2 kya hota hai" → Answer "4 hota hai" in Roman Urdu
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
        self._client = None

    def _get_client(self):
        """Lazy initialization of OpenAI client."""
        if self._client is None:
            # Import here to avoid issues if openai not installed
            from openai import OpenAI

            # Try multiple env var names
            api_key = os.environ.get("OPENAI_API_KEY", "")

            if not api_key:
                raise ValueError(
                    "OPENAI_API_KEY environment variable is not set. "
                    "Please add it in Vercel Project Settings > Environment Variables."
                )

            logger.info("Initializing OpenAI client...")
            self._client = OpenAI(api_key=api_key)
        return self._client

    def _get_model(self) -> str:
        """Get the model name from environment."""
        return os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

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
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role in ["user", "assistant"] and content:
                    messages.append({"role": role, "content": content})

            # Get OpenAI tools from MCP definitions
            openai_tools = convert_tools_to_openai_format(get_tool_definitions())

            # Call OpenAI
            logger.info(f"Calling OpenAI with message: {message[:100]}...")
            client = self._get_client()
            model = self._get_model()
            logger.info(f"Using model: {model}")

            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=openai_tools,
                tool_choice="auto",
                max_tokens=1024,
                temperature=0.7
            )

            assistant_message = response.choices[0].message
            logger.info(f"OpenAI response received. Tool calls: {bool(assistant_message.tool_calls)}")

            # Check if model wants to call tools
            if assistant_message.tool_calls:
                # Execute all tool calls
                tool_results = []
                for tool_call in assistant_message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments)

                    # Inject user_id into params
                    tool_args["user_id"] = context.user_id

                    logger.info(f"Executing tool: {tool_name}")

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
                        logger.info(f"Tool {tool_name} succeeded")
                    except Exception as e:
                        logger.exception(f"Tool {tool_name} failed: {e}")
                        tool_results.append({
                            "tool_call_id": tool_call.id,
                            "output": json.dumps({"error": str(e)})
                        })

                # Send tool results back to get final response
                # Convert assistant_message to dict properly
                assistant_dict = {
                    "role": "assistant",
                    "content": assistant_message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        }
                        for tc in assistant_message.tool_calls
                    ]
                }
                messages.append(assistant_dict)

                for tool_result in tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_result["tool_call_id"],
                        "content": tool_result["output"]
                    })

                # Get final response
                logger.info("Getting final response after tool execution...")
                final_response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=1024,
                    temperature=0.7
                )

                response_text = final_response.choices[0].message.content or "Task completed successfully!"
            else:
                # No tool calls - direct response
                response_text = assistant_message.content or "I'm here to help! What would you like to know?"

            logger.info(f"Returning response: {response_text[:100]}...")
            return AgentResult(
                success=True,
                response=response_text,
                tool_calls=tool_calls_record,
                language="en"
            )

        except ValueError as e:
            # Configuration errors - show to user
            error_msg = str(e)
            logger.error(f"Configuration error: {error_msg}")
            return AgentResult(
                success=False,
                response=error_msg,
                tool_calls=tool_calls_record,
                error=error_msg,
                language="en"
            )
        except Exception as e:
            logger.exception(f"OpenAI agent error: {e}")
            error_msg = str(e)

            # Provide helpful error message based on error type
            if "api_key" in error_msg.lower() or "authentication" in error_msg.lower() or "invalid_api_key" in error_msg.lower():
                response_text = "OpenAI API key is invalid or not configured. Please check OPENAI_API_KEY in environment variables."
            elif "rate_limit" in error_msg.lower():
                response_text = "Too many requests. Please wait a moment and try again."
            elif "model" in error_msg.lower():
                response_text = f"Model error: {error_msg}. Please check OPENAI_MODEL setting."
            else:
                response_text = f"Error: {error_msg}"

            return AgentResult(
                success=False,
                response=response_text,
                tool_calls=tool_calls_record,
                error=error_msg,
                language="en"
            )
