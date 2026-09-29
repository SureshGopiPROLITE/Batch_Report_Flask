"""Production entry point (gunicorn wsgi:app).

gunicorn never runs app.py's `if __name__ == "__main__":` block, so the
startup work it does (DB indexes + PLC auto-connect) is repeated here.

Run with exactly ONE worker: the PLC monitor thread lives inside the worker
process, and a second worker would start a second monitor that logs every
batch twice. Use --threads for request concurrency instead.
"""
import logging

from app import app
from database import postgres
from modules import monitor

try:
    postgres.ensure_indexes()
except Exception:
    logging.exception("Could not create database indexes")

monitor.start_auto_connect()
