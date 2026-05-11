"""
app/agent.py
────────────────────────────────────────────────────────────────────────────
Core agent logic:
  1. Build semantic query from full conversation history (all user turns)
  2. Retrieve top-30 catalog candidates via FAISS
  3. Extract any previously named assessments (compare/refine continuity)
  4. Call Gemini Flash with grounded catalog context + conversation
  5. Parse + validate the structured JSON response
  6. Return ChatResponse (all recommendations validated against catalog)

Uses the new google-genai SDK (google.genai).
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List

from dotenv import load_dotenv
load_dotenv()  # loads GEMINI_API_KEY from .env if present

from google import genai
from google.genai import types as genai_types
from google.genai.errors import ClientError

from .models import ChatResponse, Message, Recommendation
from .prompts import SYSTEM_PROMPT
from . import retriever

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable not set")
        _client = genai.Client(api_key=api_key)
    return _client


# ── Query construction ────────────────────────────────────────────────────────

def _build_query(messages: List[Message]) -> str:
    """
    Build a rich semantic search query from the full conversation.
    Uses ALL user turns (not just last 3) so multi-turn conversations
    like a 7-turn engineer JD discussion get full retrieval signal.
    The most recent user message is triple-weighted for recency bias.
    """
    user_msgs = [m.content for m in messages if m.role == "user"]
    if not user_msgs:
        return ""
    # All messages contribute; latest message weighted 3x
    all_context = " ".join(user_msgs)
    latest = user_msgs[-1]
    query = f"{latest} {latest} {latest} {all_context}"
    return query.strip()


def _extract_mentioned_names(messages: List[Message]) -> List[str]:
    """
    Extract assessment names from assistant turns for compare/refine continuity.
    Ensures previously recommended items stay in the catalog context.
    """
    names = []
    for m in messages:
        if m.role == "assistant":
            # Markdown bold: **Name**
            bold = re.findall(r"\*\*([^*]+)\*\*", m.content)
            # Markdown table cells: | Name |
            cells = re.findall(r"\|\s*([A-Z][^\|]{3,60}?)\s*\|", m.content)
            names.extend(bold)
            names.extend(cells)
    # De-duplicate, filter obvious non-names
    seen, result = set(), []
    for n in names:
        n = n.strip()
        if len(n) > 4 and n not in seen:
            seen.add(n)
            result.append(n)
    return result[:15]


# ── Context formatting ────────────────────────────────────────────────────────

def _format_catalog_context(items: List[Dict[str, Any]]) -> str:
    lines = [
        "### CATALOG CONTEXT",
        "You may ONLY recommend items from this list. Use exact names and URLs.\n",
    ]
    for i, item in enumerate(items, 1):
        types_str  = ", ".join(item.get("test_types", []))
        levels_str = ", ".join(item.get("job_levels", [])) or "All levels"
        langs      = item.get("languages", [])
        lang_str   = (
            ", ".join(langs[:4]) + (f" (+{len(langs)-4} more)" if len(langs) > 4 else "")
            if langs else "N/A"
        )
        duration = item.get("duration_raw") or "—"
        desc     = (item.get("description") or "")[:250]
        code     = item.get("test_type", "?")

        lines.append(
            f"[{i}] {item['name']}\n"
            f"    URL: {item['url']}\n"
            f"    Type Code: {code} | Types: {types_str}\n"
            f"    Levels: {levels_str}\n"
            f"    Duration: {duration} | Languages: {lang_str}\n"
            f"    Description: {desc}\n"
        )
    return "\n".join(lines)


def _format_conversation(messages: List[Message]) -> str:
    lines = ["### CONVERSATION HISTORY\n"]
    for m in messages:
        role = "User" if m.role == "user" else "Assistant"
        lines.append(f"{role}: {m.content}\n")
    return "\n".join(lines)


# ── Response parsing & validation ─────────────────────────────────────────────

def _parse_json(text: str) -> Dict:
    """Extract JSON from LLM output (handles accidental code fences and whitespace)."""
    text = text.strip()
    # Strip markdown code fences: ```json ... ``` or ``` ... ```
    text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text, flags=re.IGNORECASE)
    # Strip single backtick wrapping (edge case)
    text = text.strip("`").strip()

    # First attempt: parse the cleaned text directly
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: extract the first JSON object using brace matching
    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])

    raise ValueError("No valid JSON object found in LLM output")


def _validate_recs(raw: List) -> List[Recommendation]:
    """
    Ensures recommendations match the catalog exactly (both name and URL).
    This guards against hallucinations and ensures 100% hard-eval compliance.
    """
    valid: List[Recommendation] = []
    # Get the raw catalog list for name verification
    catalog_items = retriever.get_all()
    # Create lookups for fast verification
    url_to_name = {i["url"]: i["name"] for i in catalog_items}

    for rec in raw:
        if not isinstance(rec, dict):
            continue
        url  = (rec.get("url") or "").strip()
        # Ensure URL exists and name matches the catalog exactly
        if url in url_to_name:
            valid.append(Recommendation(
                name=url_to_name[url], # Use catalog name to prevent hallucinated titles
                url=url,
                test_type=(rec.get("test_type") or "?").strip()
            ))
        if len(valid) == 10:
            break
    return valid


# ── Main entry point ──────────────────────────────────────────────────────────

async def chat(messages: List[Message]) -> ChatResponse:
    # 1. Semantic retrieval — k=40 to maximize Recall@10
    query     = _build_query(messages)
    retrieved = retriever.search(query, k=40)

    # 2. Fetch previously mentioned items (compare/refine continuity)
    mentioned_names = _extract_mentioned_names(messages)
    extra_items     = retriever.get_by_names(mentioned_names)

    # 3. Merge, de-duplicate by URL (cap at 50 to provide deep context)
    seen_urls: set = set()
    context_items: List[Dict] = []
    for item in retrieved + extra_items:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            context_items.append(item)
        if len(context_items) >= 50:
            break

    # 4. Build user prompt
    # Strict Turn Cap: Evaluator stops at 8 total messages. 
    # If len(messages) == 7, this is the 8th message (the final turn).
    is_last_turn = len(messages) >= 7
    budget_note = (
        "\nCRITICAL: This is the FINAL turn allowed by the conversation budget. "
        "You MUST provide your final recommendations NOW and set 'end_of_conversation': true.\n"
        if is_last_turn else ""
    )

    user_prompt = (
        f"{_format_catalog_context(context_items)}\n\n"
        f"{_format_conversation(messages)}\n"
        f"{budget_note}\n"
        "Respond with a JSON object only — no markdown fences, no extra text."
    )

    # 5. Call Gemini 1.5 Flash (run blocking SDK call in thread pool to avoid blocking the event loop)
    client = _get_client()

    def _call_gemini():
        return client.models.generate_content(
            model="gemini-2.5-flash",  # available with quota in this environment
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.2,
                max_output_tokens=4096,
            ),
        )

    try:
        response = await asyncio.to_thread(_call_gemini)
    except ClientError as e:
        status = getattr(e, 'status_code', None) or getattr(e, 'code', None)
        if status == 429:
            logger.warning("Gemini rate limit hit (429): %s", e)
            return ChatResponse(
                reply="The AI service is temporarily rate-limited. Please wait a moment and try again.",
                recommendations=[],
                end_of_conversation=False,
            )
        logger.error("Gemini API error (status=%s): %s", status, e)
        return ChatResponse(
            reply="The AI service returned an error. Please try again shortly.",
            recommendations=[],
            end_of_conversation=False,
        )
    except Exception as e:
        logger.exception("Unexpected error calling Gemini: %s", e)
        return ChatResponse(
            reply="An unexpected error occurred. Please try again.",
            recommendations=[],
            end_of_conversation=False,
        )

    # 6. Safely extract text — response.text raises if Gemini returned no valid candidates
    #    (e.g. safety filter, quota exceeded, empty candidates list)
    try:
        raw_text = response.text
    except Exception as e:
        # Log finish reason if available for diagnostics
        finish_reason = None
        try:
            finish_reason = response.candidates[0].finish_reason if response.candidates else "NO_CANDIDATES"
        except Exception:
            pass
        logger.error("Gemini returned no usable text. finish_reason=%s error=%s", finish_reason, e)
        return ChatResponse(
            reply="I'm having trouble generating a response right now. Please try again.",
            recommendations=[],
            end_of_conversation=False,
        )

    if not raw_text or not raw_text.strip():
        logger.error("Gemini returned empty text. Full response: %s", response)
        return ChatResponse(
            reply="I received an empty response. Please try again.",
            recommendations=[],
            end_of_conversation=False,
        )

    # 7. Parse JSON
    try:
        parsed = _parse_json(raw_text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("JSON parse failed: %s. Raw output (first 800 chars): %s", e, raw_text[:800])
        return ChatResponse(
            reply="I encountered an internal error. Could you rephrase your question?",
            recommendations=[],
            end_of_conversation=False,
        )

    reply       = parsed.get("reply", "")
    recs_raw    = parsed.get("recommendations", []) or []
    end_of_conv = bool(parsed.get("end_of_conversation", False))

    # 8. Validate recommendations against catalog (Name and URL verification)
    recommendations = _validate_recs(recs_raw)

    # Safety: never end conversation with empty recommendations unless it's a refusal
    if end_of_conv and not recommendations:
        # If forced to end by turn cap but no recs, don't end yet unless it's turn 8
        if not is_last_turn:
            end_of_conv = False
    
    # Final Turn Hard Override (Turn 4 / Message 8)
    if is_last_turn:
        end_of_conv = True

    return ChatResponse(
        reply=reply,
        recommendations=recommendations,
        end_of_conversation=end_of_conv,
    )
