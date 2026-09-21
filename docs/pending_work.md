# Pending Work & Placeholders

While the core data resolution pipelines and database schema are fully functional, several application layers are currently empty placeholder files awaiting implementation.

## 1. FastAPI Application (`backend/app/`)
The REST API structure is created but the files are empty.
- **`main.py`**: Will house the FastAPI app instance and middleware setup.
- **Routes (`backend/app/routes/`)**:
  - `check.py`: Intended for endpoints that evaluate drug interactions.
  - `resolve.py`: Intended to expose the `build_drug_record()` function as a synchronous API.
  - `ws.py`: Intended for real-time WebSocket communication, potentially streaming agentic thoughts from LangGraph.

## 2. LangGraph AI Workers (`backend/worker/`)
The asynchronous job processing and AI-driven logic are not yet built.
- **`consumer.py`**: Will act as the queue listener, picking up jobs (like those created in the `InteractionJob` database table).
- **LangGraph Pipeline (`backend/worker/langgraph_pipeline/`)**:
  - `graph.py`: Will define the nodes and edges of the state graph.
  - `nodes.py`: Will contain the specific agentic functions.
  - `prompts.py`: Will hold LLM instructions.
  - `state.py`: Will define the `TypedDict` passing data between nodes.

## Next Steps for the Developer
1. **API Development**: Implement `main.py` and the `resolve.py` route to connect the working `core.resolution` logic to the web.
2. **Database Integration**: Update the resolution logic or route handlers to check the Postgres database before falling back to the external API queries.
3. **Graph Design**: Design the LangGraph state machine to handle complex drug interaction queries that go beyond simple FDA label lookups.
