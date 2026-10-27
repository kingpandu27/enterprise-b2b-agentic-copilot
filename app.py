import os
import json
import time
from typing import List, Dict, Optional, TypedDict
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel
import redis
from google import genai
from langgraph.graph import StateGraph, END
from langchain_text_splitters import RecursiveCharacterTextSplitter
from handbook import SIAM_FOOD_HANDBOOK

load_dotenv()

# ======================================================================
# 1. FASTAPI APP SETUP
# ======================================================================
app = FastAPI(
    title="Siam Food Services — B2B Operations & Logistics AI Copilot",
    description="Enterprise B2B copilot powered by LangGraph, In-Memory RAG, and Redis multi-turn state.",
    version="1.0.0"
)

# ======================================================================
# 2. CLIENT CONNECTIONS (Gemini & Redis)
# ======================================================================
api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
gemini_client = genai.Client(api_key=api_key) if api_key else None

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=0,
    decode_responses=True,
    socket_connect_timeout=0.5,
    socket_timeout=0.5
)

# ======================================================================
# 3. IN-MEMORY RAG KNOWLEDGE BASE (Product-Aware Hybrid Retrieval)
# ======================================================================
splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=80)
handbook_chunks = splitter.split_text(SIAM_FOOD_HANDBOOK)

def retrieve_handbook_chunks(query: str, top_k: int = 4) -> str:
    """RAG retrieval with domain entity boosting for B2B products and logistics."""
    q_lower = query.lower()
    query_words = [w for w in q_lower.split() if len(w) > 2]
    product_keywords = ["wagyu", "striploin", "ribeye", "tenderloin", "salmon", "butter", "angus"]
    
    scored = []
    for chunk in handbook_chunks:
        c_lower = chunk.lower()
        score = sum(1 for w in query_words if w in c_lower)
        for p in product_keywords:
            if p in q_lower and p in c_lower:
                score += 10
        scored.append((score, chunk))
        
    scored.sort(key=lambda x: x[0], reverse=True)
    selected = [c for score, c in scored[:top_k] if score > 0]
    if not selected:
        selected = handbook_chunks[:top_k]
    return "\n\n".join(selected)

# ======================================================================
# 4. LANGGRAPH STATE & NODES
# ======================================================================
COMMERCIAL_VIOLATIONS = [
    "verbally",
    "without approval",
    "special discount",
    "cash rebate",
    "waive invoice",
    "dump all customer pricing",
    "ignore temperature",
    "bypass quarantine"
]

class CopilotState(TypedDict):
    user_query: str
    session_id: str
    retrieved_chunks: str
    agent_response: str
    is_blocked: bool
    block_reason: str
    conversation_history: List[Dict[str, str]]

def guardrail_node(state: CopilotState) -> CopilotState:
    """Checks for commercial policy violations and unauthorized discount requests."""
    q = state["user_query"].lower()
    for signature in COMMERCIAL_VIOLATIONS:
        if signature in q:
            state["is_blocked"] = True
            state["block_reason"] = f"Commercial policy violation detected: '{signature}'"
            return state
    state["is_blocked"] = False
    state["block_reason"] = ""
    return state

def security_blocked_node(state: CopilotState) -> CopilotState:
    """Returns strict commercial and food safety firewall refusal."""
    state["agent_response"] = (
        "[COMMERCIAL POLICY BLOCKED: 403 FORBIDDEN]\n"
        f"[REASON]: {state.get('block_reason', 'Unauthorized Action')}\n"
        "[ACTION]: Request Terminated - Price deviations require written Commercial Director sign-off."
    )
    return state

def retrieve_node(state: CopilotState) -> CopilotState:
    """Retrieves relevant product specs, temperatures, and SLAs."""
    state["retrieved_chunks"] = retrieve_handbook_chunks(state["user_query"])
    return state

def generate_node(state: CopilotState) -> CopilotState:
    """Generates professional B2B foodservice response using Gemini 3.5 Flash Lite."""
    formatted_history = ""
    for turn in state["conversation_history"]:
        role = "CLIENT" if turn["role"] == "user" else "SIAM_FOOD_COPILOT"
        formatted_history += f"{role}: {turn['text']}\n"

    prompt = f"""You are Nong Som, the Senior B2B Customer & Logistics Copilot at Siam Food Services Limited (CP Axtra).
You assist executive chefs, hotel procurement managers, and restaurant buyers across Southeast Asia.

CORE OPERATIONAL RULES:
1. Ground answers strictly in the verified Product & Logistics Catalog below.
2. Quote exact Material Codes (SAP MATNR), prices, storage temperatures, shelf-life, and order cutoffs.
3. Be professional, polite, and service-oriented ("Sawadee Khrap / Sawadee Kha").

VERIFIED CATALOG:
{state['retrieved_chunks']}

CONVERSATION HISTORY:
{formatted_history}

CLIENT INQUIRY:
{state['user_query']}

Nong Som:"""

    try:
        res = gemini_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt
        )
        reply = res.text.strip()
    except Exception as e:
        reply = f"[LLM ERROR]: {str(e)}"

    state["agent_response"] = reply
    return state

def security_router(state: CopilotState) -> str:
    return "blocked" if state["is_blocked"] else "retrieve"

# Compile LangGraph Workflow
workflow = StateGraph(CopilotState)
workflow.add_node("guardrail", guardrail_node)
workflow.add_node("security", security_blocked_node)
workflow.add_node("retrieve", retrieve_node)
workflow.add_node("generate", generate_node)

workflow.set_entry_point("guardrail")
workflow.add_conditional_edges("guardrail", security_router, {
    "blocked": "security",
    "retrieve": "retrieve"
})
workflow.add_edge("security", END)
workflow.add_edge("retrieve", "generate")
workflow.add_edge("generate", END)

siam_copilot_graph = workflow.compile()

# ======================================================================
# 5. SCHEMAS & API ENDPOINTS
# ======================================================================
class ChatRequest(BaseModel):
    question: str
    session_id: str = "default_b2b_session"

class ChatResponse(BaseModel):
    question: str
    reply: str
    session_id: str
    turn_count: int
    security_status: str
    container_status: str

@app.get("/health")
def health():
    """Liveness and readiness probe for Kubernetes."""
    redis_alive = False
    try:
        redis_alive = bool(redis_client.ping())
    except Exception:
        redis_alive = False

    return {
        "status": "healthy" if redis_alive else "degraded",
        "service": "siam_food_b2b_copilot",
        "redis_connected": redis_alive,
        "catalog_chunks_indexed": len(handbook_chunks),
        "organization": "Siam Food Services (CP Axtra)"
    }

@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    """B2B Customer & Order Inquiry Chat Endpoint."""
    if not gemini_client:
        return ChatResponse(
            question=request.question,
            reply="Gemini API key not configured.",
            session_id=request.session_id,
            turn_count=0,
            security_status="CONFIG_ERROR",
            container_status="DEGRADED"
        )

    # Step A: Retrieve past turns from Redis
    redis_key = f"session:{request.session_id}"
    history: List[Dict[str, str]] = []
    try:
        cached = redis_client.get(redis_key)
        if cached:
            history = json.loads(cached)
    except Exception as e:
        pass

    # Step B: Run LangGraph Agent
    initial_state: CopilotState = {
        "user_query": request.question,
        "session_id": request.session_id,
        "retrieved_chunks": "",
        "agent_response": "",
        "is_blocked": False,
        "block_reason": "",
        "conversation_history": history
    }

    result = siam_copilot_graph.invoke(initial_state)
    reply_text = result["agent_response"]
    is_blocked = result["is_blocked"]

    # Step C: Save updated turns to Redis (1-hour TTL)
    history.append({"role": "user", "text": request.question})
    history.append({"role": "assistant", "text": reply_text})

    try:
        redis_client.set(redis_key, json.dumps(history), ex=3600)
    except Exception as e:
        pass

    return ChatResponse(
        question=request.question,
        reply=reply_text,
        session_id=request.session_id,
        turn_count=len(history) // 2,
        security_status="BLOCKED (403)" if is_blocked else "CLEAN (PASS)",
        container_status="ONLINE"
    )
