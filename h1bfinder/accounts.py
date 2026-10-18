"""Account sessions and private resume library."""
import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
import time
from datetime import timedelta
from flask import g, session, request, jsonify, redirect, render_template, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from . import resume as rs

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
 password_hash TEXT NOT NULL, active_resume_id INTEGER);
CREATE TABLE IF NOT EXISTS resumes(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
 name TEXT NOT NULL, text TEXT NOT NULL, original BLOB, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_resumes_user ON resumes(user_id);
CREATE TABLE IF NOT EXISTS auth_sessions(token TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
CREATE TABLE IF NOT EXISTS auth_attempts(ip TEXT, at REAL);
CREATE INDEX IF NOT EXISTS ix_auth_attempts ON auth_attempts(ip, at);
"""

def active_resume(conn):
    return conn.execute("SELECT r.* FROM resumes r JOIN users u ON u.active_resume_id=r.id "
                        "WHERE u.id=? AND r.user_id=u.id", (g.user['id'],)).fetchone()

def profile(row):
    return dict(rs.build_profile(row['text']), id=row['id'], name=row['name'], created_at=row['created_at'])

def install(app, conn):
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('session_secret',?)", (secrets.token_hex(32),))
    conn.commit()
    app.config.update(SECRET_KEY=os.environ.get('H1B_SECRET_KEY') or conn.execute(
        "SELECT v FROM meta WHERE k='session_secret'").fetchone()[0],
        MAX_CONTENT_LENGTH=10*1024*1024, SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('CODESPACES')=='true'
        or os.environ.get('H1B_COOKIE_SECURE')=='1', PERMANENT_SESSION_LIFETIME=timedelta(days=7))

    @app.before_request
    def authenticate():
        g.user = None
        token = session.get('token', '')
        if token:
            g.user = conn.execute('SELECT u.* FROM users u JOIN auth_sessions s ON s.user_id=u.id '
                'WHERE s.token=? AND s.expires>?', (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        public = request.path in ('/login', '/api/auth/session', '/api/auth/login', '/api/auth/register')
        if not public and not g.user:
            return (jsonify(error='Please sign in.'), 401) if request.path.startswith('/api/') else redirect('/login')
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not hmac.compare_digest(
                request.headers.get('X-CSRF-Token', ''), session['csrf']):
            return jsonify(error='Your session changed. Refresh the page and try again.'), 403

    @app.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    @app.get('/login')
    def login_page():
        return redirect('/') if g.user else render_template('login.html')

    @app.get('/api/auth/session')
    def auth_session():
        return jsonify(user={'username':g.user['username']} if g.user else None, csrf=session['csrf'])

    @app.post('/api/auth/register')
    @app.post('/api/auth/login')
    def sign_in():
        now = time.time()
        ip = request.remote_addr or 'unknown'
        conn.execute('DELETE FROM auth_attempts WHERE at<?', (now-600,))
        count = conn.execute('SELECT COUNT(*) FROM auth_attempts WHERE ip=?', (ip,)).fetchone()[0]
        if count >= 30:
            conn.commit()
            return jsonify(error='Too many attempts. Try again in 10 minutes.'), 429
        conn.execute('INSERT INTO auth_attempts VALUES(?,?)', (ip, now))
        conn.commit()
        b = request.get_json(silent=True) or {}
        username, password = b.get('username', ''), b.get('password', '')
        if not isinstance(username, str) or not isinstance(password, str):
            abort(400, 'Enter a username and password.')
        username = username.strip().lower()
        if not re.fullmatch(r'[a-z0-9_.-]{3,32}', username) or not 12 <= len(password) <= 256:
            return jsonify(error='Use a 3–32 character username (letters, numbers, dots, dashes or underscores) and a password of 12–256 characters.'), 400
        if request.path.endswith('/register'):
            hashed = generate_password_hash(password)
            try:
                conn.execute('INSERT INTO users(username,password_hash) VALUES(?,?)', (username, hashed))
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                return jsonify(error='That username is unavailable.'), 409
        user = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if not user or not check_password_hash(user['password_hash'], password):
            return jsonify(error='Incorrect username or password.'), 401
        if session.get('token'):
            conn.execute('DELETE FROM auth_sessions WHERE token=?', (hashlib.sha256(session['token'].encode()).hexdigest(),))
        token = secrets.token_urlsafe(32)
        conn.execute('DELETE FROM auth_sessions WHERE expires<?', (now,))
        conn.execute('INSERT INTO auth_sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user['id'], now+604800))
        conn.commit()
        session.clear()
        session.update(token=token, csrf=secrets.token_urlsafe(32))
        session.permanent = True
        return jsonify(ok=True)

    @app.post('/api/auth/logout')
    def logout():
        conn.execute('DELETE FROM auth_sessions WHERE token=?', (hashlib.sha256(session.get('token','').encode()).hexdigest(),))
        conn.commit()
        session.clear()
        return jsonify(ok=True)

    def owned(resume_id):
        row = conn.execute('SELECT * FROM resumes WHERE id=? AND user_id=?', (resume_id, g.user['id'])).fetchone()
        if row is None:
            abort(404, 'Resume not found.')
        return row

    @app.get('/api/resumes')
    def library():
        rows = conn.execute('SELECT * FROM resumes WHERE user_id=? ORDER BY created_at DESC, id DESC', (g.user['id'],)).fetchall()
        return jsonify(resumes=[dict(profile(r), active=r['id']==g.user['active_resume_id']) for r in rows])

    @app.get('/api/resumes/<int:resume_id>')
    def detail(resume_id):
        r = owned(resume_id)
        return jsonify(dict(profile(r), text=r['text']))

    @app.get('/api/resumes/<int:resume_id>/download')
    def download(resume_id):
        r = owned(resume_id)
        return send_file(io.BytesIO(r['original'] if r['original'] is not None else r['text'].encode()),
                         as_attachment=True, download_name=r['name'], mimetype='application/octet-stream')

    @app.post('/api/resumes/<int:resume_id>/activate')
    def activate(resume_id):
        r = owned(resume_id)
        conn.execute('UPDATE users SET active_resume_id=? WHERE id=?', (resume_id,g.user['id']))
        conn.commit()
        return jsonify(profile(r))

    @app.post('/api/resumes/<int:resume_id>/delete')
    def delete(resume_id):
        owned(resume_id)
        conn.execute('DELETE FROM resumes WHERE id=? AND user_id=?', (resume_id,g.user['id']))
        conn.execute('UPDATE users SET active_resume_id=NULL WHERE id=? AND active_resume_id=?', (g.user['id'],resume_id))
        conn.commit()
        return jsonify(ok=True)

def save_resume(conn):
    f = request.files.get('file')
    original = None
    name = 'Pasted resume.txt'
    if f and f.filename:
        name = secure_filename(f.filename)[:160] or 'resume.txt'
        if name.rsplit('.',1)[-1].lower() not in ('pdf','docx','txt'):
            abort(400, 'Choose a PDF, DOCX or TXT file.')
        original = f.read()
        try:
            text = rs.extract_text(original, name)
        except Exception:
            abort(400, 'This file could not be read. Try another file or paste the resume text.')
    else:
        text = (request.get_json(silent=True) or {}).get('text','')
    if not isinstance(text,str) or not 50 <= len(text.strip()) <= 500000:
        abort(400, 'The resume must contain between 50 and 500,000 readable characters. You can paste text if the file is scanned.')
    try:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT COUNT(*) FROM resumes WHERE user_id=?',(g.user['id'],)).fetchone()[0] >= 20:
            abort(400, 'Your library holds up to 20 resumes. Delete an old resume first.')
        cur = conn.execute('INSERT INTO resumes(user_id,name,text,original,created_at) VALUES(?,?,?,?,?)',
                           (g.user['id'],name,text,original,time.time()))
        conn.execute('UPDATE users SET active_resume_id=? WHERE id=?',(cur.lastrowid,g.user['id']))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify(profile(conn.execute('SELECT * FROM resumes WHERE id=?',(cur.lastrowid,)).fetchone()))
