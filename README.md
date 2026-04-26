# Syllabus-to-Calendar API

A production-ready REST API that lets students upload course syllabi (PDFs), uses Anthropic's Claude to extract dated events (lectures, assignments, exams, holidays, etc.), and syncs those events to the user's Google Calendar. The API is frontend-agnostic: any web, mobile, or browser-extension client can integrate by calling these endpoints with a bearer token.

## Prerequisites

- Python 3.11+
- PostgreSQL 14+
- A Google Cloud project with the Calendar API enabled and an OAuth 2.0 Client ID
- An Anthropic API key with access to `claude-haiku-4-5`

## Google Cloud Console setup

1. Go to https://console.cloud.google.com/ and create or select a project.
2. **Enable APIs**: APIs & Services -> Library -> enable:
   - Google Calendar API
   - Google People API (for `userinfo`)
3. **OAuth consent screen**: APIs & Services -> OAuth consent screen.
   - User Type: External (for public use) or Internal (Workspace).
   - Scopes to add:
     - `openid`
     - `email`
     - `profile`
     - `https://www.googleapis.com/auth/calendar`
   - Add test users if the app is in "Testing" mode.
4. **Create OAuth client**: APIs & Services -> Credentials -> Create Credentials -> OAuth client ID.
   - Application type: Web application.
   - Authorized redirect URIs: include exactly the value you put in `GOOGLE_REDIRECT_URI` (e.g. `http://localhost:3000/auth/callback`).
   - Save and copy the Client ID and Client Secret.
5. The frontend should redirect users to Google's auth URL with these query params: `client_id`, `redirect_uri`, `response_type=code`, `scope=openid email profile https://www.googleapis.com/auth/calendar`, `access_type=offline`, `prompt=consent` (the last two ensure a refresh_token is returned every time).

## Local setup

```bash
git clone <this repo>
cd syllabus-calendar-api

python3.11 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# Then fill in .env. Generate values for the secrets:
python -c "import secrets; print(secrets.token_urlsafe(64))"   # for JWT_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # for REFRESH_TOKEN_ENCRYPTION_KEY

# Create the database, then run migrations
createdb syllabus_calendar
alembic upgrade head

# Run the server
uvicorn app.main:app --reload
```

The OpenAPI docs are then available at http://localhost:8000/docs.

## Running tests

```bash
pytest
```

Tests use an in-memory SQLite database and mock the Anthropic and Google clients, so no network access or external services are required.

## API overview

All endpoints return JSON. Errors use the shape:
```json
{ "error": { "code": "INVALID_INPUT", "message": "...", "details": {} } }
```

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/google/callback` | none | Exchange Google auth code for session JWT |
| GET | `/auth/me` | bearer | Current user profile |
| POST | `/auth/logout` | bearer | No-op on the server (drop the token client-side) |
| POST | `/syllabi/parse` | bearer | Upload PDF, extract events with Claude (NOT synced) |
| GET | `/syllabi` | bearer | List user's syllabi |
| GET | `/syllabi/{id}` | bearer | Get a syllabus and its events |
| DELETE | `/syllabi/{id}` | bearer | Delete syllabus + cascade events (also from Google) |
| POST | `/syllabi/{id}/sync` | bearer | Push events to a Google Calendar |
| PATCH | `/events/{id}` | bearer | Update an event (also in Google if synced) |
| DELETE | `/events/{id}` | bearer | Delete event (also from Google if synced) |
| GET | `/health` | none | Liveness probe |

Full request/response schemas are at `/docs` (Swagger UI) and `/openapi.json`.

## Limits & rate limiting

- PDFs must be `application/pdf`, <= 20 MB, <= 100 pages.
- Each user is limited to 10 `/syllabi/parse` calls per hour. The limiter is in-memory only — counts do **not** survive process restarts and do not work across multiple workers. For production, swap in a Redis backend.

## Deployment notes

### Railway / Render

Set the following environment variables in the dashboard:

```
DATABASE_URL                       (their managed Postgres URL, asyncpg format)
ANTHROPIC_API_KEY
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
GOOGLE_REDIRECT_URI                (your frontend's callback URL)
JWT_SECRET                         (token_urlsafe(64))
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=10080
REFRESH_TOKEN_ENCRYPTION_KEY       (Fernet.generate_key())
ALLOWED_ORIGINS                    (comma-separated list of frontend origins)
APP_ENV=production
LOG_LEVEL=INFO
```

Build command:
```
pip install -r requirements.txt && alembic upgrade head
```

Start command:
```
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

The `DATABASE_URL` must use the `postgresql+asyncpg://` scheme. If your provider gives you a `postgres://` URL, replace the scheme.

## Project layout

```
app/
  main.py            FastAPI app, CORS, exception handlers, request logging
  config.py          Pydantic Settings (loads .env)
  database.py        Async SQLAlchemy engine + get_db dependency
  deps.py            get_current_user dependency
  exceptions.py      AppError + subclasses
  models/            SQLAlchemy ORM models
  schemas/           Pydantic request/response schemas
  routers/           FastAPI routers
  services/          Claude parser, Google OAuth/Calendar, crypto, JWT, rate limit
alembic/             Migrations
tests/               pytest suite (mocks Anthropic + Google)
```
