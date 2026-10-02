"""
Customer Support AI Agent — Starter Code
==========================================
"""

# ── Imports ───────────────────────────────────────────────────────────────────
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── App Initialisation ────────────────────────────────────────────────────────
app = BedrockAgentCoreApp()

os.environ["BYPASS_TOOL_CONSENT"] = "true"

# ── Configuration ──────────────────────────────────────────────────────────────
GATEWAY_URL = "https://customersupportgateway-ubprttqmrc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = "OGHO6X0XIS"
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportMemory-xkPGusEUSI"

# ── Lazy Model and Clients ────────────────────────────────────────────────────
model_id = "global.amazon.nova-2-lite-v1:0"

_model = None
_memory_client = None
_bedrock_runtime = None


def get_model():
    global _model
    if _model is None:
        _model = BedrockModel(model_id=model_id)
    return _model


def get_memory_client():
    global _memory_client
    if _memory_client is None:
        _memory_client = MemoryClient(region_name=REGION)
    return _memory_client


def get_bedrock_runtime():
    global _bedrock_runtime
    if _bedrock_runtime is None:
        _bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)
    return _bedrock_runtime


# ── Namespace Helper ───────────────────────────────────────────────────────────
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)
    return {s["type"]: s["namespaces"][0] for s in strategies}


# ── Memory Hook ────────────────────────────────────────────────────────────────
class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(memory_client, memory_id)

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = event.agent.messages
        if not messages:
            return

        last_message = messages[-1]
        if last_message.get("role") != "user":
            return

        content = last_message.get("content", [])
        if any(isinstance(c, dict) and "toolResult" in c for c in content):
            return

        user_query = ""
        for c in content:
            if isinstance(c, dict) and "text" in c:
                user_query = c["text"]
                break
        if not user_query:
            return

        collected = []
        for strategy_type, namespace_template in self.namespaces.items():
            namespace = namespace_template.format(actorId=self.actor_id)
            try:
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace,
                    query=user_query,
                    top_k=5,
                )
            except Exception as e:
                logger.warning("Memory retrieval failed for %s: %s", namespace, e)
                continue

            for m in memories:
                text = m.get("content", {}).get("text", "")
                if text:
                    collected.append(f"[{strategy_type}] {text}")

        if collected:
            memory_block = "\n".join(collected)
            new_text = f"Customer Context:\n{memory_block}\n\n{user_query}"
            for c in content:
                if isinstance(c, dict) and "text" in c:
                    c["text"] = new_text
                    break

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        messages = event.agent.messages
        customer_query = None
        agent_response = None

        for msg in reversed(messages):
            role = msg.get("role")
            content = msg.get("content", [])

            if role == "assistant" and agent_response is None:
                for c in content:
                    if isinstance(c, dict) and "text" in c:
                        agent_response = c["text"]
                        break

            if role == "user" and customer_query is None:
                if not any(isinstance(c, dict) and "toolResult" in c for c in content):
                    for c in content:
                        if isinstance(c, dict) and "text" in c:
                            customer_query = c["text"]
                            break

            if customer_query and agent_response:
                break

        if customer_query and agent_response:
            try:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")],
                )
            except Exception as e:
                logger.warning("Failed to save interaction to memory: %s", e)

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── Knowledge Base Tool ────────────────────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID:
        return "Knowledge base not configured."

    try:
        resp = get_bedrock_runtime().retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )
    except Exception as e:
        return f"Error searching knowledge base: {e}"

    results = resp.get("retrievalResults", [])
    if not results:
        return "No relevant information found in the knowledge base."

    chunks = [
        r["content"]["text"]
        for r in results
        if "content" in r and "text" in r["content"]
    ]
    return "\n---\n".join(chunks)


# ── Loyalty Discount Tool (Code Interpreter) ───────────────────────────────────
@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    code = f"""
import json

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

loyalty_points = {loyalty_points}
tier = "{tier}"
order_total = {order_total}
product_category = "{product_category}"

max_redeemable_value = order_total * 0.5
points_value = loyalty_points * 0.01
points_redeemed_value = min(points_value, max_redeemable_value)
points_redeemed = int(points_redeemed_value / 0.01)
points_redeemed = (points_redeemed // 500) * 500
points_redeemed_value = points_redeemed * 0.01

subtotal_after_points = order_total - points_redeemed_value

tier_discount_rate = tier_rates.get(tier, 0.00)
tier_discount = subtotal_after_points * tier_discount_rate

final_total = subtotal_after_points - tier_discount
total_savings = points_redeemed_value + tier_discount

earn_rate = earn_rates.get(product_category, 1)
points_earned = int(final_total * earn_rate)

remaining_points = loyalty_points - points_redeemed + points_earned

result = {{
    "points_redeemed": points_redeemed,
    "points_redeemed_value": round(points_redeemed_value, 2),
    "tier_discount_rate": tier_discount_rate,
    "tier_discount": round(tier_discount, 2),
    "final_total": round(final_total, 2),
    "total_savings": round(total_savings, 2),
    "points_earned": points_earned,
    "remaining_points": remaining_points,
}}

print(json.dumps(result))
"""

    try:
        with code_session(REGION) as session:
            response = session.invoke(
                "executeCode",
                {"code": code, "language": "python", "clearContext": True},
            )
            for event in response.get("stream", []):
                return json.dumps(event)
        return json.dumps({"error": "No result returned from Code Interpreter"})

    except Exception as e:
        logger.warning("Code Interpreter unavailable, using fallback: %s", e)
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_discount_rate = tier_rates.get(tier, 0.00)
        tier_discount = order_total * tier_discount_rate
        final_total = order_total - tier_discount
        return json.dumps({
            "tier_discount_rate": tier_discount_rate,
            "tier_discount": round(tier_discount, 2),
            "final_total": round(final_total, 2),
            "note": "Fallback calculation only — Code Interpreter unavailable; points redemption not applied",
        })


# ── Agent Entrypoint ────────────────────────────────────────────────────────────
@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    try:
        from strands_tools.browser import AgentCoreBrowser

        user_input = payload.get("prompt", "")
        actor_id = payload.get("customer_id", "anonymous")
        session_id = payload.get("session_id") or str(uuid.uuid4())

        memory_hook = MemoryHook(actor_id, session_id, get_memory_client(), MEMORY_ID)
        agent_core_browser = AgentCoreBrowser(region=REGION)

        tools = [search_knowledge_base, calculate_loyalty_discount, agent_core_browser.browser]

        system_prompt = (
            "You are a helpful customer support assistant for an e-commerce "
            "platform. You can track orders, process refunds, answer product "
            "and policy questions using the knowledge base, calculate loyalty "
            "discounts, and browse the web when needed. Always use the "
            "appropriate tool rather than guessing — for example, always use "
            "the loyalty discount tool for numeric discount calculations. "
            "Be concise, friendly, and accurate."
        )

        mcp_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))
        with mcp_client:
            gateway_tools = mcp_client.list_tools_sync()
            tools.extend(gateway_tools)

            agent = Agent(
                model=get_model(),
                tools=tools,
                hooks=[memory_hook],
                system_prompt=system_prompt,
            )

            response = agent(user_input)
            return response.message["content"][0]["text"]

    except Exception as e:
        logger.error("Error during invocation: %s", e)
        return f"I'm sorry, I encountered an error processing your request: {e}"


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()