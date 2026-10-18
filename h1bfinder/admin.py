"""Manage administrators from the server terminal, never from public registration."""
import argparse
import os
from . import db


def set_admin(conn, username, enabled):
    user = conn.execute('SELECT id FROM users WHERE username=?', (username.strip().lower(),)).fetchone()
    if user is None:
        raise ValueError('Account not found. Create the account in the app first.')
    conn.execute('CREATE TABLE IF NOT EXISTS administrators(user_id INTEGER PRIMARY KEY)')
    if enabled:
        conn.execute('INSERT OR IGNORE INTO administrators(user_id) VALUES(?)', (user['id'],))
    else:
        conn.execute('DELETE FROM administrators WHERE user_id=?', (user['id'],))
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description='Grant or revoke shared-dataset administrator access.')
    parser.add_argument('action', choices=['grant', 'revoke'])
    parser.add_argument('username', help='Existing app username (not your GitHub username unless they match)')
    args = parser.parse_args()
    conn = db.connect(os.environ.get('H1B_DB_PATH'))
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
            parser.error('Start the app and create your account first.')
        set_admin(conn, args.username, args.action == 'grant')
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        conn.close()
    print(f'Administrator access {"granted to" if args.action == "grant" else "revoked from"} {args.username}. Refresh the app.')


if __name__ == '__main__':
    main()
