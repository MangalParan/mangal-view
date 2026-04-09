"""Manage users for the Nifty Chart tool.

Usage:
    python scripts/manage_users.py add <mobileno> <password>
    python scripts/manage_users.py list
    python scripts/manage_users.py delete <mobileno>
"""
import os
import sys
import sqlite3
import secrets
import hashlib

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "users.db")


def hash_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000)
    return salt + ":" + h.hex()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mobileno TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    if cmd == "add":
        if len(sys.argv) < 4:
            print("Usage: python scripts/manage_users.py add <mobileno> <password>")
            return
        mobileno = sys.argv[2]
        password = sys.argv[3]
        if len(mobileno) != 10 or not mobileno.isdigit():
            print("Error: Mobile number must be 10 digits.")
            return
        if len(password) < 6:
            print("Error: Password must be at least 6 characters.")
            return
        try:
            db.execute("INSERT INTO users (mobileno, password_hash) VALUES (?, ?)",
                       (mobileno, hash_password(password)))
            db.commit()
            print(f"User {mobileno} added successfully.")
        except sqlite3.IntegrityError:
            print(f"Error: User {mobileno} already exists.")

    elif cmd == "list":
        rows = db.execute("SELECT id, mobileno, created_at FROM users ORDER BY id").fetchall()
        if not rows:
            print("No users found.")
        else:
            print(f"{'ID':<5} {'Mobile':<15} {'Created'}")
            print("-" * 45)
            for r in rows:
                print(f"{r[0]:<5} {r[1]:<15} {r[2]}")

    elif cmd == "delete":
        if len(sys.argv) < 3:
            print("Usage: python scripts/manage_users.py delete <mobileno>")
            return
        mobileno = sys.argv[2]
        cur = db.execute("DELETE FROM users WHERE mobileno = ?", (mobileno,))
        db.commit()
        if cur.rowcount:
            print(f"User {mobileno} deleted.")
        else:
            print(f"User {mobileno} not found.")

    else:
        print(__doc__)

    db.close()


if __name__ == "__main__":
    main()
