# Sylly — Backend (FastAPI)

A REST API that lets students upload course syllabi (PDFs), uses Anthropic Claude to extract dated events + grade categories, and syncs events to Google Calendar. Frontend-agnostic; the same API powers a Vite/React frontend at `~/timely-front-end` (separate repo).

## High-level architecture

- **Backend** (this repo): Python 3.11+, FastAPI, async SQLAlchemy 2.x, Postgres, Anthropic Claude. Deployed on Railway at `https://timeinowenbrownie-production.up.railway.app`.
- **Frontend** (sibling): React + Vite + TanStack Router/Query + Zustand + Tailwind. Lives at `~/timely-front-end`. Deployed to Lovable (`huggable-frame-finder.lovable.app`) and runs locally on `http://localhost:8080`.
- **Auth**: Google OAuth → backend mints a JWT → frontend stores it in localStorage and sends `Authorization: Bearer <jwt>` on every protected request.
- **CORS**: backend allows `huggable-frame-finder.lovable.app`, `localhost:3000`, `localhost:5173`. Add new origins to the `ALLOWED_ORIGINS` env var if Lovable URLs change.

## Repo layout

```
app/
  main.py             FastAPI entrypoint, CORS, error handlers, router registration
  config.py           Pydantic Settings, fails fast on missing env. Auto-upgrades DATABASE_URL to asyncpg driver.
  database.py         Async engine + get_db dependency
  deps.py             get_current_user (JWT auth)
  exceptions.py       AppError + 9 typed subclasses, mapped to consistent { error: { code, message, details } } shape

  models/             SQLAlchemy ORM models (UUID PKs, timestamptz columns)
    user.py
    syllabus.py       has events + grade_categories relationships
    event.py          syllabus_id is nullable so chat-created events can stand alone
    grade_category.py weight/drop_lowest/notes/sort_order

  schemas/            Pydantic v2 request/response schemas
    auth.py
    syllabus.py       ParsedSyllabus + ParsedEvent + ParsedGradeCategory (Claude output schemas)
    event.py          EventRead/Create/Update
    grade_category.py
    chat.py           ChatMessage / ChatSendRequest / ChatSendResponse / ChatProposedEvent

  routers/
    auth.py           /auth/google/callback, /auth/me, /auth/logout
    syllabi.py        /syllabi/parse (PDF upload → Claude → DB), /syllabi (CRUD), /syllabi/{id}/sync (Google Calendar push)
    events.py         /events (range query, create), /events/{id} (patch, delete)
    grade_categories.py  full CRUD scoped under syllabus
    chat.py           /chat/plan — the AI assistant endpoint

  services/
    claude_parser.py  syllabus PDF → JSON (one of the heavier prompts)
    chat_agent.py     conversational + active-recall + event-proposal agent (the focus area)
    google_oauth.py
    google_calendar.py
    crypto.py         Fernet-encrypted refresh tokens at rest
    jwt_tokens.py
    rate_limit.py     in-memory sliding window. parse_limiter (10/hr) and chat_limiter (60/hr).

alembic/versions/      DB migrations: 0001 initial, 0002 grade_categories, 0003 event_syllabus_optional
tests/                 pytest with mocked Anthropic + Google clients, in-memory SQLite for the DB
```

## Conventions to follow

- **Type hints everywhere.** No untyped function signatures.
- **Docstrings on every router endpoint** — they show up in `/docs`.
- **Async all the way down** for DB and external HTTP calls. Use `loop.run_in_executor` only when wrapping a sync SDK call (e.g. `googleapiclient`).
- **Errors are typed.** Raise an `AppError` subclass from `app/exceptions.py`. Don't return error JSON manually from a handler.
- **Auth.** Every protected endpoint gets `current_user: User = Depends(get_current_user)`. Owner-scope all queries with `WHERE user_id = current_user.id`.
- **Settings.** Read env via `get_settings()`. New env vars go in `app/config.py` AND `.env.example`.
- **No new packages without a strong reason.** The current `requirements.txt` is intentionally lean. Adding a dependency is a deliberate choice.
- **Migrations.** Any model change requires an Alembic migration. Mirror the defensive cleanup pattern in `0001_initial.py` for new tables (DROP IF EXISTS at top of `upgrade()`).

## The chat agent (likely focus of upcoming work)

`app/services/chat_agent.py` is the brain. Key things to know:

- It's stateless on the server: the frontend posts the entire conversation history each turn (`ChatSendRequest.messages`).
- Before calling Claude, it loads:
  - The user's syllabi + grade categories
  - The user's events overlapping the visible date range (or next 30 days if none provided)
  - Filters for upcoming exams specifically
- Builds a system prompt that includes those + Socratic-tutoring guidance + the JSON output contract.
- Calls Claude with `system=<prompt>` and `messages=<conversation>`, `max_tokens=2048`.
- Parses a `<response>{ message, proposed_events? }</response>` envelope from Claude's output. Falls back to plain-text message if the envelope is missing.
- Returns a `ChatMessage` (assistant) and a list of `ChatProposedEvent` (may be empty).

The endpoint is `POST /chat/plan` in `app/routers/chat.py`. Rate-limited to 60 calls/hr per user.

### Common improvements likely on the roadmap

- Streaming responses (SSE) so the user sees the assistant typing
- Persistent chat history (currently the frontend keeps it in a Zustand store, lost on reload)
- Better quizzing: track which concepts have been asked, weight by exam proximity, hint laddering (no hint → small hint → bigger hint → answer)
- Tool-use API instead of `<response>` envelope parsing — Anthropic supports proper tool calls now
- Concept extraction from notes (separate from syllabus events) — would need a new `concepts` table
- Spaced repetition: track quiz attempts + correctness, surface concepts on a schedule

Don't build all of these at once. Pick one, scope it tightly, and ship.

## Testing & deployment

- `pytest` runs locally against in-memory SQLite. No external services needed (Anthropic + Google are mocked via fixtures).
- `alembic upgrade head` in deploy (Railway runs it as part of the start command).
- Railway deploys on every push to `main`. Build: `pip install -r requirements.txt`. Start: `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

## Frontend repo (sibling)

Located at `~/timely-front-end` if cloned next to this repo. Owned by a different developer; pull with `git pull`, don't push. Frontend integration plan for the calendar + chat panel lives in their notes; the relevant types it expects from this backend are documented in `app/schemas/chat.py` and the OpenAPI spec at `/docs` on the deployed Railway URL.

## Quick links

- Live API docs: https://timeinowenbrownie-production.up.railway.app/docs
- Local dev (this repo): not typically run locally — backend is exercised against the deployed instance. If you need to: `pip install -r requirements.txt && alembic upgrade head && uvicorn app.main:app --reload` with a `.env` populated.
