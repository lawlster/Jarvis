
import requests

from agent.memory import (
    initialise_memory,
    save_message,
    load_recent_messages
)

from tools.calculator import calculate
from tools.research import search_web
from tools.finance import get_company_financials
from tools.valuation import calculate_dcf


MODEL = "qwen3:8b"
OLLAMA_URL = "http://localhost:11434/api/chat"


SYSTEM_PROMPT = """
You are AiAssistant, a personal AI agent.

You help with investment research, business analysis,
personal tasks and general questions.

AVAILABLE CAPABILITIES:

1. Calculator
2. Internet research
3. Company financial data
4. Discounted cash flow valuation

RULES:

1. Use calculate for mathematical calculations.
2. Use search_web for current internet research.
3. Use get_company_financials for company financial data.
4. Use calculate_dcf when the user requests DCF valuation.
5. Never invent financial figures.
6. Never invent company information.
7. Distinguish facts from assumptions.
8. Prioritise official company sources.
9. Explain financial metrics clearly.
10. Identify missing or unavailable financial information.
11. Do not treat Yahoo Finance data as verified filings.
12. Never claim you searched the internet unless you did.
13. Do not make investment decisions for the user.

DCF RULES:

1. Explain the assumptions used.
2. Use free cash flow as the starting point.
3. Clearly distinguish enterprise value and equity value.
4. Explain the discount rate and terminal growth rate.
5. Never present a DCF estimate as an exact stock price.
6. If the user has not provided assumptions, ask for them.
7. Do not invent missing financial information.
8. Explain how sensitive the valuation is to assumptions.

INVESTMENT PHILOSOPHY:

Focus on business quality, competitive advantages,
financial strength, owner earnings, free cash flow,
intrinsic value and margin of safety.

Be analytical, conservative and transparent.
"""


TOOLS = [

    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Perform mathematical calculations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string"}
                },
                "required": ["expression"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the internet for recent company information and financial news.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"}
                },
                "required": ["query"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "get_company_financials",
            "description": "Retrieve financial information and financial statements for a company using its ticker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"}
                },
                "required": ["ticker"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "calculate_dcf",
            "description": "Calculate discounted cash flow valuation using explicitly supplied assumptions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "free_cash_flow": {"type": "number"},
                    "growth_rate": {"type": "number"},
                    "discount_rate": {"type": "number"},
                    "terminal_growth_rate": {"type": "number"},
                    "years": {"type": "integer"},
                    "net_debt": {"type": "number"},
                    "shares_outstanding": {"type": "number"}
                },
                "required": [
                    "free_cash_flow",
                    "growth_rate",
                    "discount_rate",
                    "terminal_growth_rate"
                ]
            }
        }
    }

]


conversation_history = []


def execute_tool(tool_name, arguments):

    print(f"\n[Tool called: {tool_name}]")

    if tool_name == "calculate":

        return calculate(
            arguments.get("expression", "")
        )

    elif tool_name == "search_web":

        return str(search_web(
            arguments.get("query", "")
        ))

    elif tool_name == "get_company_financials":

        return str(get_company_financials(
            arguments.get("ticker", "")
        ))

    elif tool_name == "calculate_dcf":

        return str(calculate_dcf(
            free_cash_flow=arguments["free_cash_flow"],
            growth_rate=arguments["growth_rate"],
            discount_rate=arguments["discount_rate"],
            terminal_growth_rate=arguments["terminal_growth_rate"],
            years=arguments.get("years", 5),
            net_debt=arguments.get("net_debt", 0),
            shares_outstanding=arguments.get("shares_outstanding")
        ))

    return "Error: Unknown tool."


def ask_ai(user_message):

    global conversation_history

    conversation_history.append({
        "role": "user",
        "content": user_message
    })

    save_message("user", user_message)

    conversation_history = conversation_history[-20:]

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ] + conversation_history.copy()

    for _ in range(5):

        payload = {
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
            "stream": False
        }

        try:

            response = requests.post(
                OLLAMA_URL,
                json=payload,
                timeout=180
            )

            response.raise_for_status()

            result = response.json()

            assistant_message = result["message"]

            tool_calls = assistant_message.get(
                "tool_calls", []
            )

            if not tool_calls:

                reply = assistant_message.get(
                    "content", ""
                )

                if not reply:
                    reply = "I couldn't generate a response."

                conversation_history.append({
                    "role": "assistant",
                    "content": reply
                })

                conversation_history = conversation_history[-20:]

                save_message("assistant", reply)

                return reply

            messages.append(assistant_message)

            for tool_call in tool_calls:

                function_data = tool_call["function"]

                tool_name = function_data["name"]

                arguments = function_data.get(
                    "arguments", {}
                )

                tool_result = execute_tool(
                    tool_name,
                    arguments
                )

                messages.append({
                    "role": "tool",
                    "tool_name": tool_name,
                    "content": str(tool_result)
                })

        except requests.exceptions.RequestException as error:

            return f"Connection error: {error}"

        except Exception as error:

            return f"Unexpected error: {error}"

    return "I reached the maximum number of tool calls."


def main():

    global conversation_history

    initialise_memory()

    conversation_history = load_recent_messages(20)

    print("=" * 45)
    print("          AiAssistant v0.7")
    print("=" * 45)

    print("Persistent memory: Active")
    print("Calculator: Active")
    print("Web research: Active")
    print("Financial data: Active")
    print("DCF valuation: Active")
    print("Type 'exit' to end.\n")

    while True:

        user_input = input("You: ").strip()

        if user_input.lower() in ["exit", "quit"]:

            print("AiAssistant: Goodbye!")

            break

        if not user_input:
            continue

        print("\nAiAssistant is thinking...\n")

        reply = ask_ai(user_input)

        print(f"AiAssistant: {reply}\n")


if __name__ == "__main__":
    main()
