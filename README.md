# BrandBridge

FastAPI-based collaboration platform for Brands and Advertisers with:

- JWT authentication
- Profile creation + admin approval workflow
- Role-based access control
- Brand <-> Advertiser one-to-one chat
- Template message tracking
- Admin dashboard stats endpoints
- Basic HTML + Tailwind pages for registration/login/dashboard

## Quick Start

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Run server:

```bash
uvicorn app.main:app --reload
```

3. Open:

- App UI: `http://127.0.0.1:8000/`
- API docs: `http://127.0.0.1:8000/docs`

## Notes

- SQLite is used by default via `brandbridge.db`.
- Register with an email ending in `@admin.com` to create an admin user.
- Approved profile is required before chat access.
- Forgot password email uses SMTP env vars:
  - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`
  - `SMTP_FROM_EMAIL` (optional, defaults to `SMTP_USER`)
  - `SMTP_USE_TLS` (`true`/`false`, default `true`)
  - `APP_BASE_URL` (default `http://127.0.0.1:8000`)

## API Summary

### Auth

- `POST /register`
- `POST /login`
- `POST /forgot-password`
- `POST /reset-password`
- `GET /forgot-password`
- `GET /reset-password?token=...`
- `GET /me`

### Profiles

- `POST /advertiser-profile`
- `POST /brand-profile`
- `GET /profiles`

### Admin

- `GET /admin/profiles`
- `POST /admin/approve/{id}`
- `POST /admin/reject/{id}`
- `GET /admin/stats`

### Chat

- `GET /users`
- `POST /chat/send`
- `GET /chat/{user_id}`
- `GET /templates`
