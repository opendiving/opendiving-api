# Open Diving API

Backend API for Open Diving app.

## Authentication

There is a single entry point into the app: email magic link or Google - no
passwords, no separate sign up flow. See `src/app/api/v1/auth.py` for the endpoints
(`/auth/email/request`, `/auth/email/verify`, `/auth/google`, `/auth/complete`) and
`DECISIONS.md` for the full design rationale.

Changing an account's email (`src/app/api/v1/users.py`) reuses the same magic-link
mechanics: `POST /user/{uuid}/email-change/request` emails a confirmation link to the
*new* address, and the change only applies once `POST /user/email-change/verify`
confirms it - see `DECISIONS.md`.

Magic-link emails are sent via [Resend](https://resend.com). Set these in `src/.env`:

```bash
# Required to actually deliver magic-link emails - without it, the link is only
# logged (useful for local development).
RESEND_API_KEY="re_..._your_key"
EMAIL_FROM_ADDRESS="onboarding@resend.dev"

# Used to build the magic-link URL (`{FRONTEND_URL}/auth/verify?token=...`).
FRONTEND_URL="http://localhost:3000"

# Optional - tune magic-link/onboarding-session expiry and rate limits. See
# `MagicLinkSettings`/`CryptSettings` in `src/app/core/config.py` for all of them
# and their defaults.
MAGIC_LINK_TOKEN_EXPIRE_MINUTES=30
ONBOARDING_TOKEN_EXPIRE_MINUTES=30
EMAIL_CHANGE_TOKEN_EXPIRE_MINUTES=30
```

## Endpoints

### Parse a Suunto dive XML file

Upload a Suunto dive XML file and receive the parsed dive data as JSON:

```bash
curl -X POST http://localhost:8000/api/v1/dive/parse-xml \
  -F "file=@Dive_2021-04-06-1231.xml"
```
