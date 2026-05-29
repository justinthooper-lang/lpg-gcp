"""Smoke test for db.py connection.

Run: python smoke_db.py
"""

from db import get_connection

with get_connection() as conn:
    cur = conn.cursor()
    try:
        cur.execute("SELECT current_database(), current_user, version()")
        row = cur.fetchone()
        print("Database:", row[0])
        print("User:    ", row[1])
        print("Version: ", row[2][:50] + "...")

        cur.execute("SELECT COUNT(*) FROM shift4.orders")
        print("Orders in shift4.orders:", cur.fetchone()[0])
    finally:
        cur.close()
        