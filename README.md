# Instagram Creator Download Manager

A production-ready Flask web application for managing Instagram creator content downloads when you have explicit permission.

Owned by LostMedia Studios

## Features
- Secure admin authentication with hashed password, Flask-Login sessions.
- Creator management with profile metadata fetching via Instaloader.
- Post browser with thumbnails, caption/audio metadata, downloadable status, and modal viewer.
- Single and bulk download operations for undownloaded content.
- Search/filter by caption, audio text, and date.
- Settings panel with persistent SQLite-backed configuration.
- Logs panel with pagination, CSV export, and clearing.
- Reel hashtag pack generation after download and `/download_complete` API for caption-based generation.
- Docker Compose deployment with persistent media and logs.

## Disclaimer
You must have explicit permission from each creator before downloading or storing their content.

## Project Layout
- `app.py`: Flask entrypoint and application logic.
- `templates/`: Login, dashboard, settings, logs.
- `static/`: CSS, JavaScript, profile pics, thumbnails.
- `media/`: Downloaded posts in `/media/{username}/{shortcode}/`.
- `logs/`: `app.log` and exported CSV files.
- `db.sqlite3`: SQLite app database.

## Local Installation
1. Create virtualenv and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Configure `.env` values.
3. Start app:
   ```bash
   python app.py
   ```
4. Open `http://localhost:5000` and login as `admin` using `ADMIN_PASSWORD`.

## Docker Installation
1. Set `.env` values.
2. Run:
   ```bash
   docker compose up --build
   ```
3. Access the app on `http://localhost:${PORT}`.

## How to use the app
1. Login as admin.
2. Add a creator username in the sidebar.
3. Browse fetched posts and use view modal.
4. Download single post, selected posts, or all undownloaded posts.
5. Generated hashtags appear under preview after download.
6. Configure behavior in `/settings` and audit activity in `/logs`.

## Environment Variables
- `FLASK_SECRET_KEY`
- `ADMIN_PASSWORD`
- `INSTAGRAM_USERNAME`
- `INSTAGRAM_PASSWORD`
- `PORT`
