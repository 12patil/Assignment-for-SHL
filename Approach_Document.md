# Approach Document: SHL Assessment Recommender

## 1. Design Choices
The architecture was designed for **reliability, statelessness, and strict schema compliance** to meet the requirements of the agentic evaluation harness.
*   **FastAPI Backend**: Provides a lightweight, high-performance, stateless HTTP API.
*   **Gemini 2.5 Flash**: Chosen for its fast inference time, cost-effectiveness, and strong instruction-following capabilities, which are crucial for adhering to the strict JSON output schema.
*   **Defensive API Layer**: Rather than trusting the LLM to output perfect JSON or perfectly matching URLs, the backend includes robust post-processing. A custom JSON extractor strips markdown fences, and a validation layer (`_validate_recs`) enforces exact URL-to-catalog-name mappings, effectively eliminating hallucinations.
*   **Strict Turn Cap Management**: To ensure the 8-turn constraint is never breached, the backend intercepts the message array length. On the 7th user message (Turn 4), the system prompt is injected with a critical override forcing a final recommendation and `end_of_conversation: true`.

## 2. Retrieval Setup
The system uses a Retrieval-Augmented Generation (RAG) approach to ground recommendations in the provided catalog.
*   **Embeddings**: Used `sentence-transformers/all-MiniLM-L6-v2`. It is lightweight, fast, and runs locally without network latency, making it ideal for the 30-second API timeout limit.
*   **Vector Store**: FAISS (`IndexFlatIP`) is used with L2-normalized embeddings for fast cosine similarity search. The index is built once and persisted to disk for zero-latency cold starts.
*   **Context Strategy**: 
    1.  *Semantic Search*: The query is built by concatenating all user turns, heavily weighting the most recent turn. We retrieve $k=40$ items to maximize Recall@10.
    2.  *Conversational Continuity*: Pure semantic search often drops previous recommendations when the user asks comparison questions. We extract assessment names mentioned in previous assistant turns using regex and inject them back into the context.
    3.  *Context Window*: The final context is de-duplicated by URL and capped at 50 items. This provides the LLM a wide view of the catalog to make nuanced comparisons.

## 3. Prompt Design
The system prompt (`app/prompts.py`) is engineered to guide the agent through the four required behaviors: clarify, recommend, refine, and compare.
*   **Explicit Behavioral Rules**: The prompt explicitly maps user intents to actions (e.g., "If vague, ask ONE short clarification question").
*   **Strict Output Formatting**: The prompt reinforces that the output must be raw JSON. It provides concrete examples of clarification and recommendation schemas.
*   **Constraint Management**: Includes rules for specific catalog quirks (e.g., SVAR accents, OPQ32r default for senior roles).
*   **Dynamic Injection**: The context is dynamically injected at request time, along with a "budget note" if the conversation is at its final allowed turn.

## 4. Evaluation Approach & Iteration
*   **Automated Testing**: We developed against the 10 provided public conversation traces. We simulated the exact automated replay harness behavior to ensure schema compliance and behavior probe pass-rates.
*   **Measurement**: We tracked Mean Recall@10 and Schema Pass Rate across iterations.

### What Didn't Work & Improvements
1.  **Hallucinated Names/URLs**: Initially, the LLM would sometimes slightly alter assessment names or URLs. *Fix*: Implemented a hard validation layer that cross-references generated URLs against the catalog and overwrites the generated name with the exact catalog name. Schema compliance jumped to 100%.
2.  **Context Loss on Refinement**: When a user asked "What's the difference between the first two?", semantic search failed to retrieve them. *Fix*: Added the `_extract_mentioned_names` function to carry forward previously mentioned items in the FAISS retrieval step.
3.  **JSON Parsing Errors**: The LLM occasionally wrapped responses in ````json` markdown blocks, causing 500 errors. *Fix*: Implemented a robust brace-matching JSON parser that ignores surrounding prose or markdown.

## 5. AI Tools Used
*   **Agentic Coding Assistant**: Used an advanced agentic coding assistant to accelerate the boilerplate setup of FastAPI, write the FAISS integration script (`build_index.py`), and rapidly iterate on the JSON parsing/validation logic. It was also used to quickly format the enriched catalog JSON.
