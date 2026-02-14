import csv
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import instaloader
import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "db.sqlite3"
MEDIA_ROOT = BASE_DIR / "media"
STATIC_ROOT = BASE_DIR / "static"
PROFILE_PIC_DIR = STATIC_ROOT / "profile_pics"
THUMBNAIL_DIR = STATIC_ROOT / "thumbnails"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
PROFILE_PIC_DIR.mkdir(parents=True, exist_ok=True)
THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "app.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret")

login_manager = LoginManager(app)
login_manager.login_view = "login"


class User(UserMixin):
    def __init__(self, id_: int, username: str):
        self.id = str(id_)
        self.username = username


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    schema = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS creators (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        display_name TEXT,
        profile_pic_path TEXT,
        added_at TEXT
    );
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_id INTEGER,
        shortcode TEXT UNIQUE,
        post_date TEXT,
        caption TEXT,
        audio_track TEXT,
        media_type TEXT,
        original_url TEXT,
        media_path TEXT,
        thumbnail_path TEXT,
        downloaded INTEGER DEFAULT 0,
        created_at TEXT,
        FOREIGN KEY(creator_id) REFERENCES creators(id)
    );
    CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        action TEXT,
        creator_username TEXT,
        post_shortcode TEXT,
        status TEXT,
        details TEXT
    );
    """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executescript(schema)
        conn.commit()

        cur = conn.execute("SELECT * FROM users WHERE username='admin'")
        if not cur.fetchone():
            admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                ("admin", generate_password_hash(admin_password)),
            )

        defaults = {
            "max_posts_first_fetch": "50",
            "default_download_directory": str(MEDIA_ROOT),
            "thumbnail_generation": "1",
            "auto_refresh_metadata": "1",
            "instagram_username": os.getenv("INSTAGRAM_USERNAME", ""),
            "instagram_password": os.getenv("INSTAGRAM_PASSWORD", ""),
            "show_downloaded_default": "1",
            "show_not_downloaded_default": "1",
            "pagination_size": "24",
            "theme": "light",
            "bulk_operations_enabled": "1",
        }
        for key, val in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()


def log_action(action: str, status: str, creator_username: str = "", post_shortcode: str = "", details: str = ""):
    db = get_db()
    db.execute(
        """INSERT INTO activity_logs (ts, action, creator_username, post_shortcode, status, details)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (datetime.utcnow().isoformat(), action, creator_username, post_shortcode, status, details),
    )
    db.commit()


@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    row = db.execute("SELECT id, username FROM users WHERE id=?", (user_id,)).fetchone()
    if row:
        return User(row["id"], row["username"])
    return None


def get_instaloader_client() -> instaloader.Instaloader:
    L = instaloader.Instaloader(download_pictures=False, download_videos=False, download_video_thumbnails=False)
    username = get_setting("instagram_username")
    password = get_setting("instagram_password")
    if username and password:
        try:
            L.login(username, password)
        except Exception as e:
            logger.exception("Instagram login failed: %s", e)
    return L


def optimize_thumbnail(src_path: Path, dest_path: Path):
    try:
        with Image.open(src_path) as img:
            img.thumbnail((500, 500))
            img.save(dest_path, "WEBP", optimize=True, quality=80)
    except Exception as e:
        logger.exception("Thumbnail generation failed: %s", e)


def extract_audio_track_name(post) -> str:
    return getattr(post, "title", "") or getattr(post, "accessibility_caption", "") or ""


def save_profile_pic(url: str, username: str) -> str:
    if not url:
        return ""
    path = PROFILE_PIC_DIR / f"{username}.jpg"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    path.write_bytes(r.content)
    return f"profile_pics/{path.name}"


def fetch_creator_posts(creator_id: int, username: str):
    db = get_db()
    L = get_instaloader_client()
    profile = instaloader.Profile.from_username(L.context, username)

    max_first = int(get_setting("max_posts_first_fetch", "50"))
    existing_count = db.execute("SELECT COUNT(*) c FROM posts WHERE creator_id=?", (creator_id,)).fetchone()["c"]
    limit = max_first if existing_count == 0 else None

    count = 0
    for post in profile.get_posts():
        if limit and count >= limit:
            break
        count += 1
        shortcode = post.shortcode
        row = db.execute("SELECT id FROM posts WHERE shortcode=?", (shortcode,)).fetchone()
        if row:
            if existing_count > 0:
                break
            continue
        caption = post.caption or ""
        audio_track = extract_audio_track_name(post)
        media_type = "video" if post.is_video else "image"
        thumbnail_rel = ""
        if get_setting("thumbnail_generation", "1") == "1":
            temp_path = THUMBNAIL_DIR / f"{shortcode}.jpg"
            post.get_thumbnail_url()
            resp = requests.get(post.url, timeout=30)
            if resp.ok:
                temp_path.write_bytes(resp.content)
                webp_path = THUMBNAIL_DIR / f"{shortcode}.webp"
                optimize_thumbnail(temp_path, webp_path)
                if temp_path.exists():
                    temp_path.unlink()
                thumbnail_rel = f"thumbnails/{webp_path.name}"

        db.execute(
            """INSERT INTO posts (creator_id, shortcode, post_date, caption, audio_track, media_type, original_url,
             thumbnail_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                creator_id,
                shortcode,
                post.date_utc.isoformat(),
                caption,
                audio_track,
                media_type,
                f"https://www.instagram.com/p/{shortcode}/",
                thumbnail_rel,
                datetime.utcnow().isoformat(),
            ),
        )
    db.commit()


def download_post_media(post_row: sqlite3.Row, creator_username: str):
    L = get_instaloader_client()
    shortcode = post_row["shortcode"]
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    out_dir = Path(get_setting("default_download_directory", str(MEDIA_ROOT))) / creator_username / shortcode
    out_dir.mkdir(parents=True, exist_ok=True)

    media_file = ""
    if post.is_video:
        video_url = post.video_url
        ext = Path(video_url).suffix or ".mp4"
        media_path = out_dir / f"{shortcode}{ext}"
        r = requests.get(video_url, timeout=60)
        r.raise_for_status()
        media_path.write_bytes(r.content)
        media_file = str(media_path.relative_to(BASE_DIR))
    else:
        image_url = post.url
        ext = Path(image_url).suffix or ".jpg"
        media_path = out_dir / f"{shortcode}{ext}"
        r = requests.get(image_url, timeout=60)
        r.raise_for_status()
        media_path.write_bytes(r.content)
        media_file = str(media_path.relative_to(BASE_DIR))

    txt_path = out_dir / f"{shortcode}.txt"
    txt_path.write_text(f"Caption:\n{post.caption or ''}\n\nAudio Track:\n{extract_audio_track_name(post)}\n", encoding="utf-8")

    db = get_db()
    db.execute("UPDATE posts SET downloaded=1, media_path=?, audio_track=? WHERE id=?", (media_file, extract_audio_track_name(post), post_row["id"]))
    db.commit()

    tags = generate_hashtag_pack(post.caption or "")
    return media_file, tags


def extract_keywords(caption: str) -> List[str]:
    words = re.findall(r"[a-zA-Z]{4,}", caption.lower())
    stop = {"this", "that", "with", "from", "have", "your", "just", "about", "into", "then", "them"}
    return [w for w in words if w not in stop][:20]


def fetch_live_trending_tags(keywords: List[str]) -> List[str]:
    tags = []
    try:
        resp = requests.get("https://best-hashtags.com/hashtag/viral/", timeout=15)
        found = re.findall(r"#([a-zA-Z0-9_]+)", resp.text)
        tags.extend(found[:20])
    except Exception as e:
        logger.warning("Live hashtag source failed: %s", e)
    for kw in keywords[:5]:
        tags.append(kw)
    return tags


def detect_niche(keywords: List[str]) -> str:
    mapping = {
        "fitness": {"workout", "gym", "fitness", "health", "training"},
        "travel": {"travel", "trip", "adventure", "beach", "flight"},
        "pets": {"dog", "cat", "pet", "puppy", "kitten"},
        "humor": {"funny", "joke", "comedy", "laugh", "meme"},
        "relationships": {"love", "dating", "relationship", "couple", "heart"},
    }
    for niche, keys in mapping.items():
        if any(k in keys for k in keywords):
            return niche
    return "creator"


def generate_hashtag_pack(caption: str) -> str:
    keywords = extract_keywords(caption)
    viral_fallback = ["viral", "trending", "explorepage", "fyp", "reels", "instagood", "instagram", "viralreels"]
    engagement = ["likeforlikes", "commentbelow", "sharethis", "savethis", "watchtillend", "engagement", "community"]
    niche_map = {
        "fitness": ["fitnessmotivation", "workout", "fitlife", "gymtok", "healthylifestyle"],
        "travel": ["travelgram", "wanderlust", "travelreels", "exploremore", "bucketlist"],
        "pets": ["petsofinstagram", "doglover", "catlover", "petreels", "cuteanimals"],
        "humor": ["funnyreels", "comedy", "relatable", "memesdaily", "laughoutloud"],
        "relationships": ["relationshipgoals", "couplegoals", "loveadvice", "datingtips", "heartfelt"],
        "creator": ["contentcreator", "creatoreconomy", "socialmedia", "videocreator", "digitalcreator"],
    }
    viral_live = fetch_live_trending_tags(keywords)
    niche = detect_niche(keywords)

    def uniq(items):
        out = []
        seen = set()
        for i in items:
            s = i.lower().lstrip("#")
            if s and s not in seen:
                seen.add(s)
                out.append(f"#{s}")
        return out

    viral = uniq(viral_live + viral_fallback)[:5]
    eng = uniq(engagement)[:3]
    niche_tags = uniq(niche_map.get(niche, niche_map["creator"]) + keywords)[:4]
    combined = viral + eng + niche_tags
    return "Tags: " + " ".join(combined)


@app.route("/static/media/<path:filename>")
@login_required
def media_file(filename):
    return send_from_directory(BASE_DIR, filename)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            login_user(User(user["id"], user["username"]))
            log_action("login", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid credentials", "danger")
        log_action("login", "fail", details="Invalid credentials")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    log_action("logout", "success")
    logout_user()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
def dashboard():
    db = get_db()
    creator_id = request.args.get("creator_id", type=int)
    search = request.args.get("search", "")
    audio = request.args.get("audio", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    show_downloaded = request.args.get("show_downloaded", get_setting("show_downloaded_default", "1")) == "1"
    show_not_downloaded = request.args.get("show_not_downloaded", get_setting("show_not_downloaded_default", "1")) == "1"

    creators = db.execute("SELECT * FROM creators ORDER BY added_at DESC").fetchall()
    posts = []
    if creator_id:
        q = "SELECT * FROM posts WHERE creator_id=?"
        params: List = [creator_id]
        if search:
            q += " AND caption LIKE ?"
            params.append(f"%{search}%")
        if audio:
            q += " AND audio_track LIKE ?"
            params.append(f"%{audio}%")
        if start:
            q += " AND date(post_date) >= date(?)"
            params.append(start)
        if end:
            q += " AND date(post_date) <= date(?)"
            params.append(end)
        if show_downloaded and not show_not_downloaded:
            q += " AND downloaded=1"
        elif show_not_downloaded and not show_downloaded:
            q += " AND downloaded=0"
        q += " ORDER BY post_date DESC LIMIT 500"
        posts = db.execute(q, params).fetchall()

    return render_template("dashboard.html", creators=creators, posts=posts, creator_id=creator_id)


@app.route("/creator/add", methods=["POST"])
@login_required
def add_creator():
    username = request.form.get("username", "").strip().lower()
    db = get_db()
    try:
        L = get_instaloader_client()
        profile = instaloader.Profile.from_username(L.context, username)
        pic_path = save_profile_pic(profile.profile_pic_url, username)
        db.execute(
            "INSERT INTO creators (username, display_name, profile_pic_path, added_at) VALUES (?, ?, ?, ?)",
            (username, profile.full_name or username, pic_path, datetime.utcnow().isoformat()),
        )
        db.commit()
        creator_id = db.execute("SELECT id FROM creators WHERE username=?", (username,)).fetchone()["id"]
        fetch_creator_posts(creator_id, username)
        flash("Creator added", "success")
    except Exception as e:
        logger.exception("Add creator failed")
        flash(f"Error adding creator: {e}", "danger")
        log_action("add_creator", "fail", creator_username=username, details=str(e))
    return redirect(url_for("dashboard"))


@app.route("/creator/<int:creator_id>/remove", methods=["POST"])
@login_required
def remove_creator(creator_id):
    delete_media = request.form.get("delete_media") == "1"
    db = get_db()
    creator = db.execute("SELECT * FROM creators WHERE id=?", (creator_id,)).fetchone()
    if creator:
        if delete_media:
            creator_media = MEDIA_ROOT / creator["username"]
            if creator_media.exists():
                import shutil

                shutil.rmtree(creator_media, ignore_errors=True)
        db.execute("DELETE FROM posts WHERE creator_id=?", (creator_id,))
        db.execute("DELETE FROM creators WHERE id=?", (creator_id,))
        db.commit()
        flash("Creator removed", "success")
    return redirect(url_for("dashboard"))


@app.route("/creator/<int:creator_id>/refresh", methods=["POST"])
@login_required
def refresh_creator(creator_id):
    db = get_db()
    creator = db.execute("SELECT * FROM creators WHERE id=?", (creator_id,)).fetchone()
    if creator:
        try:
            fetch_creator_posts(creator_id, creator["username"])
            flash("Posts refreshed", "success")
        except Exception as e:
            flash(f"Refresh failed: {e}", "danger")
    return redirect(url_for("dashboard", creator_id=creator_id))


@app.route("/post/<int:post_id>/download", methods=["POST"])
@login_required
def download_post(post_id):
    db = get_db()
    post = db.execute("SELECT p.*, c.username creator_username FROM posts p JOIN creators c ON c.id=p.creator_id WHERE p.id=?", (post_id,)).fetchone()
    if not post:
        return jsonify({"error": "not found"}), 404
    try:
        media_file, tags = download_post_media(post, post["creator_username"])
        log_action("download", "success", post["creator_username"], post["shortcode"])
        return jsonify({"ok": True, "media_path": media_file, "hashtags": tags})
    except Exception as e:
        logger.exception("download failed")
        log_action("download", "fail", post["creator_username"], post["shortcode"], str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/posts/bulk_download", methods=["POST"])
@login_required
def bulk_download():
    ids = request.json.get("post_ids", [])
    db = get_db()
    posts = db.execute(
        f"SELECT p.*, c.username creator_username FROM posts p JOIN creators c ON c.id=p.creator_id WHERE p.id IN ({','.join('?'*len(ids))})",
        ids,
    ).fetchall() if ids else []
    count = 0
    for post in posts:
        if not post["downloaded"]:
            try:
                download_post_media(post, post["creator_username"])
                count += 1
            except Exception:
                pass
    return jsonify({"ok": True, "downloaded": count})


@app.route("/posts/delete_downloads", methods=["POST"])
@login_required
def delete_downloads():
    creator_id = request.form.get("creator_id", type=int)
    db = get_db()
    posts = db.execute("SELECT p.*, c.username creator_username FROM posts p JOIN creators c ON c.id=p.creator_id WHERE creator_id=? AND downloaded=1", (creator_id,)).fetchall()
    for p in posts:
        if p["media_path"]:
            fp = BASE_DIR / p["media_path"]
            if fp.exists():
                fp.unlink()
        folder = MEDIA_ROOT / p["creator_username"] / p["shortcode"]
        if folder.exists():
            import shutil

            shutil.rmtree(folder, ignore_errors=True)
        db.execute("UPDATE posts SET downloaded=0, media_path='' WHERE id=?", (p["id"],))
        log_action("delete", "success", p["creator_username"], p["shortcode"])
    db.commit()
    flash("Downloads deleted", "success")
    return redirect(url_for("dashboard", creator_id=creator_id))


@app.route("/download_complete", methods=["POST"])
@login_required
def download_complete():
    caption = (request.json or {}).get("caption", "")
    return jsonify({"hashtags": generate_hashtag_pack(caption)})


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        fields = [
            "max_posts_first_fetch", "default_download_directory", "thumbnail_generation", "auto_refresh_metadata",
            "instagram_username", "instagram_password", "show_downloaded_default", "show_not_downloaded_default",
            "pagination_size", "theme", "bulk_operations_enabled",
        ]
        for f in fields:
            value = request.form.get(f, "0" if "_generation" in f or "_refresh_" in f or "show_" in f or "bulk_" in f else "")
            set_setting(f, value)
        new_password = request.form.get("admin_password", "")
        if new_password:
            db = get_db()
            db.execute("UPDATE users SET password_hash=? WHERE username='admin'", (generate_password_hash(new_password),))
            db.commit()
        flash("Settings updated", "success")
        return redirect(url_for("settings"))

    db = get_db()
    data = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings").fetchall()}
    return render_template("settings.html", settings=data)


@app.route("/logs")
@login_required
def logs_page():
    page = request.args.get("page", 1, type=int)
    per = 25
    db = get_db()
    total = db.execute("SELECT COUNT(*) c FROM activity_logs").fetchone()["c"]
    rows = db.execute("SELECT * FROM activity_logs ORDER BY ts DESC LIMIT ? OFFSET ?", (per, (page - 1) * per)).fetchall()
    return render_template("logs.html", logs=rows, page=page, total=total, per=per)


@app.route("/logs/csv")
@login_required
def logs_csv():
    db = get_db()
    rows = db.execute("SELECT * FROM activity_logs ORDER BY ts DESC").fetchall()
    file_path = LOG_DIR / "activity_logs.csv"
    with file_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "action", "username", "shortcode", "status", "details"])
        for r in rows:
            writer.writerow([r["ts"], r["action"], r["creator_username"], r["post_shortcode"], r["status"], r["details"]])
    return send_file(file_path, as_attachment=True)


@app.route("/logs/clear", methods=["POST"])
@login_required
def clear_logs():
    db = get_db()
    db.execute("DELETE FROM activity_logs")
    db.commit()
    flash("Logs cleared", "success")
    return redirect(url_for("logs_page"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
