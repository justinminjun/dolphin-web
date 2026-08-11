"""
Chadwick Lost & Found — Flask backend
Mirrors the CI Dolphin app's Lost & Found tab.

Local development:
  1. pip3 install flask firebase-admin google-generativeai gunicorn
  2. Place dolphin-service-account.json in this folder
  3. Fill in FIREBASE_CONFIG below
  4. python3 app.py  →  http://localhost:5001

Production (Render):
  - Set FIREBASE_SERVICE_ACCOUNT env var = contents of dolphin-service-account.json
  - Set FLASK_SECRET_KEY env var = any long random string
  - Set GEMINI_API_KEY env var (optional, for AI image analysis)
  - Start command: gunicorn app:app
"""

import json
import os
import re
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore, auth as firebase_auth
from flask import (Flask, jsonify, redirect, render_template,
                   request, session, url_for)

# ── CONFIGURATION ──────────────────────────────────────────────────────────────
# Paste your Firebase web config here (one place to update, used in all templates)
FIREBASE_CONFIG = {
    "apiKey":            "AIzaSyCriHavoReJxEHHWnEuIcI8FfvpTlrIHOo",
    "authDomain":        "chadwicklostfound-justinminjun.firebaseapp.com",
    "projectId":         "chadwicklostfound-justinminjun",
    "storageBucket":     "chadwicklostfound-justinminjun.firebasestorage.app",
    "messagingSenderId": "538017118563",
    "appId":             "1:538017118563:web:2b3b91a773439408c57b56",
    "measurementId":     "G-6L6PDM054L",
}

# Gemini API key — optional, enables AI image analysis on the post form
# Get one at https://aistudio.google.com/app/apikey
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Emails with admin access to /admin panel
ADMIN_EMAILS = [
    # "yourname@chadwickschool.org",
]

# Non-Chadwick emails explicitly allowed to log in (leave empty to allow all @chadwickschool.org only)
ALLOWED_EXTRA_EMAILS: list[str] = []
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production-abc123")

# ── Production: cookies only over HTTPS ────────────────────────────────────────
if os.environ.get("RENDER"):          # Render sets this env var automatically
    app.config["SESSION_COOKIE_SECURE"]   = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

# ── Firebase Admin SDK ─────────────────────────────────────────────────────────
# Production: load service account from FIREBASE_SERVICE_ACCOUNT env var
# Local dev: fall back to dolphin-service-account.json file
_sa_env = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
if _sa_env:
    cred = credentials.Certificate(json.loads(_sa_env))
else:
    cred = credentials.Certificate("dolphin-service-account.json")

firebase_admin.initialize_app(cred)
fdb = firestore.client()


# ── HELPERS ────────────────────────────────────────────────────────────────────

def get_role_label(email: str) -> str:
    """Faculty/Staff if no trailing 4-digit year, else Student."""
    local = (email or "").split("@")[0]
    return "Student" if re.search(r"\d{4}$", local) else "Faculty / Staff"


def current_user():
    if "uid" not in session:
        return None
    return {
        "uid":      session["uid"],
        "email":    session["email"],
        "name":     session["name"],
        "photo":    session.get("photo"),
        "role":     get_role_label(session["email"]),
        "is_admin": session["email"] in ADMIN_EMAILS,
    }


def _ts_to_iso(ts):
    if ts is None:
        return None
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    if hasattr(ts, "seconds"):
        return datetime.fromtimestamp(ts.seconds, tz=timezone.utc).isoformat()
    return str(ts)


def serialize_post(doc):
    data = doc.to_dict() or {}
    data["id"] = doc.id
    data["createdAt"] = _ts_to_iso(data.get("createdAt"))
    data["updatedAt"] = _ts_to_iso(data.get("updatedAt"))
    return data


def firebase_cfg():
    return json.dumps(FIREBASE_CONFIG)


# ── ROUTES ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", user=current_user(), firebase_cfg=firebase_cfg())


@app.route("/login")
def login():
    if current_user():
        return redirect(url_for("index"))
    return render_template("login.html", firebase_cfg=firebase_cfg())


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/auth/google", methods=["POST"])
def auth_google():
    """Client sends Firebase ID token → Flask verifies → creates session."""
    data = request.get_json() or {}
    id_token = data.get("idToken")
    if not id_token:
        return jsonify({"error": "No token provided"}), 400

    try:
        decoded = firebase_auth.verify_id_token(id_token)
    except Exception as e:
        return jsonify({"error": f"Token verification failed: {e}"}), 401

    uid   = decoded["uid"]
    email = decoded.get("email", "")
    name  = decoded.get("name") or email.split("@")[0]
    photo = decoded.get("picture")

    # Domain check
    is_chadwick = email.endswith("@chadwickschool.org")
    is_allowed  = email in ALLOWED_EXTRA_EMAILS
    if not is_chadwick and not is_allowed:
        return jsonify({"error": "Only Chadwick International School accounts are permitted."}), 403

    # Upsert user document
    fdb.collection("users").document(uid).set(
        {"uid": uid, "email": email, "displayName": name, "photoURL": photo},
        merge=True,
    )

    session["uid"]   = uid
    session["email"] = email
    session["name"]  = name
    session["photo"] = photo
    return jsonify({"success": True})


# ── POST DETAIL ────────────────────────────────────────────────────────────────

@app.route("/post/<post_id>")
def detail(post_id):
    user = current_user()
    try:
        doc = fdb.collection("posts").document(post_id).get()
    except Exception as e:
        return f"Firestore error: {e}", 500
    if not doc.exists:
        return "Post not found", 404
    post = serialize_post(doc)
    return render_template(
        "detail.html",
        user=user,
        post=post,
        post_json=json.dumps(post),
        firebase_cfg=firebase_cfg(),
    )


@app.route("/post/<post_id>/resolve", methods=["POST"])
def resolve_post(post_id):
    user = current_user()
    if not user:
        return jsonify({"error": "Not logged in"}), 401
    try:
        ref = fdb.collection("posts").document(post_id)
        doc = ref.get()
        if not doc.exists:
            return jsonify({"error": "Not found"}), 404
        data = doc.to_dict()
        if data.get("authorId") != user["uid"] and not user["is_admin"]:
            return jsonify({"error": "Unauthorized"}), 403
        new_status = "active" if data.get("status") == "resolved" else "resolved"
        ref.update({"status": new_status})
        return jsonify({"success": True, "status": new_status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/post/<post_id>/delete", methods=["POST"])
def delete_post(post_id):
    user = current_user()
    if not user:
        return jsonify({"error": "Not logged in"}), 401
    try:
        ref = fdb.collection("posts").document(post_id)
        doc = ref.get()
        if not doc.exists:
            return jsonify({"error": "Not found"}), 404
        data = doc.to_dict()
        if data.get("authorId") != user["uid"] and not user["is_admin"]:
            return jsonify({"error": "Unauthorized"}), 403
        ref.delete()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── CREATE / EDIT POST ─────────────────────────────────────────────────────────

@app.route("/post/new")
def new_post():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("post.html", user=user, post=None, firebase_cfg=firebase_cfg())


@app.route("/post/<post_id>/edit")
def edit_post(post_id):
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    try:
        doc = fdb.collection("posts").document(post_id).get()
    except Exception:
        return redirect(url_for("index"))
    if not doc.exists:
        return redirect(url_for("index"))
    post = serialize_post(doc)
    if post.get("authorId") != user["uid"] and not user["is_admin"]:
        return redirect(url_for("detail", post_id=post_id))
    return render_template("post.html", user=user, post=post,
                           post_json=json.dumps(post),
                           firebase_cfg=firebase_cfg())


# ── PROFILE ────────────────────────────────────────────────────────────────────

@app.route("/profile")
def profile():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    try:
        docs = (
            fdb.collection("posts")
            .where("authorId", "==", user["uid"])
            .order_by("createdAt", direction=firestore.Query.DESCENDING)
            .get()
        )
        posts = [serialize_post(d) for d in docs]
    except Exception:
        # Fallback if composite index not yet created
        try:
            docs = fdb.collection("posts").where("authorId", "==", user["uid"]).get()
            posts = sorted(
                [serialize_post(d) for d in docs],
                key=lambda p: p.get("createdAt") or "",
                reverse=True,
            )
        except Exception:
            posts = []

    stats = {
        "total": len(posts),
        "lost":  sum(1 for p in posts if p.get("postType") == "lost"),
        "found": sum(1 for p in posts if p.get("postType") == "found"),
        "resolved": sum(1 for p in posts if p.get("status") == "resolved"),
    }
    return render_template("profile.html", user=user, posts=posts, stats=stats,
                           firebase_cfg=firebase_cfg())


# ── ADMIN ──────────────────────────────────────────────────────────────────────

@app.route("/admin")
def admin():
    user = current_user()
    if not user or not user["is_admin"]:
        return redirect(url_for("index"))
    try:
        docs = (
            fdb.collection("posts")
            .order_by("createdAt", direction=firestore.Query.DESCENDING)
            .get()
        )
        posts = [serialize_post(d) for d in docs]
    except Exception:
        posts = []

    stats = {
        "total":    len(posts),
        "lost":     sum(1 for p in posts if p.get("postType") == "lost"),
        "found":    sum(1 for p in posts if p.get("postType") == "found"),
        "active":   sum(1 for p in posts if p.get("status") != "resolved"),
        "resolved": sum(1 for p in posts if p.get("status") == "resolved"),
        "users":    len({p.get("authorId") for p in posts if p.get("authorId")}),
    }
    return render_template("admin.html", user=user, posts=posts, stats=stats,
                           firebase_cfg=firebase_cfg())


# ── AI IMAGE ANALYSIS (Gemini proxy) ──────────────────────────────────────────

@app.route("/api/analyze", methods=["POST"])
def analyze_image():
    """Receives an uploaded image, calls Gemini, returns JSON with item details."""
    if not GEMINI_API_KEY:
        return jsonify({"error": "Gemini API key not configured on server"}), 503

    file = request.files.get("image")
    if not file:
        return jsonify({"error": "No image provided"}), 400

    try:
        import base64
        import google.generativeai as genai

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")

        image_bytes = file.read()
        mime = file.mimetype or "image/jpeg"

        prompt = (
            "You are an AI assistant for a school Lost & Found. Analyze this image.\n\n"
            "Return ONLY a raw JSON object (no markdown, no code block) with these keys:\n"
            '- "title": short clear item name (e.g. "AirPods Pro")\n'
            '- "description": 2-3 sentence visual description\n'
            '- "color": primary color\n'
            '- "category": one of: Electronics, Clothing, Accessories, Stationery, Bag, Sports, Books, Water Bottle, Keys, ID Card, Other\n'
            '- "tags": array of exactly 4 descriptive keywords (no # symbol)'
        )

        result = model.generate_content(
            [prompt, {"mime_type": mime, "data": base64.b64encode(image_bytes).decode()}]
        )
        text = result.text.replace("```json", "").replace("```", "").strip()
        return jsonify(json.loads(text))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5001)
