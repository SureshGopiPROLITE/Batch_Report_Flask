"""SKEW licence check.

A licence key is issued by the vendor with tools/licence_generator.py and is
bound to one machine (the host's MAC address):

    type 0 = Demo       - stops working on its expiry date (30 days by default)
    type 1 = Purchased  - no expiry (unless issued with one)

Keys are signed with the vendor's PRIVATE key (never shipped). The app only
holds the PUBLIC key below, so it can verify keys but cannot create them.

Key layout: "SKEW-" + base64url(payload + 64-byte Ed25519 signature)
payload   : "<type>|<mac>|<issued YYYYMMDD>|<expires YYYYMMDD or empty>|<customer>"
"""
import base64
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from database import postgres

# Vendor public key (tools/licence_generator.py init prints it).
PUBLIC_KEY_B64 = "9uw-v2O0VvTNkp--YUHe513NickRQFC-XD33G0PYe40"

KEY_PREFIX = "SKEW-"
TYPE_DEMO, TYPE_PURCHASED = "0", "1"
TYPE_NAMES = {TYPE_DEMO: "Demo", TYPE_PURCHASED: "Purchased"}

INFO_KEY = "Activation_Key"          # Info_db row holding the key
INFO_LAST_SEEN = "Licence_Last_Seen" # guards a demo against winding the clock back
CLOCK_TOLERANCE = timedelta(days=1)
CACHE_SECONDS = 60


class LicenceError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Machine identity
# ---------------------------------------------------------------------------
def normalize_mac(mac):
    return str(mac or "").strip().lower().replace("-", ":")


def machine_id():
    """The host MAC this installation is bound to.

    In Docker the container cannot see the host's network card, so the
    installer (deploy/install.ps1) writes the host MAC into .env as HOST_MAC.
    Running directly on Windows (development) it is read from the NIC."""
    mac = normalize_mac(os.environ.get("HOST_MAC"))
    if mac:
        return mac
    from getmac import get_mac_address
    return normalize_mac(get_mac_address(interface="Ethernet") or get_mac_address())


# ---------------------------------------------------------------------------
# Key parsing / verification
# ---------------------------------------------------------------------------
def _b64decode(text):
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def parse_key(key):
    """Verifies the signature and returns the licence fields. Raises LicenceError."""
    key = "".join(str(key or "").split())          # tolerate spaces / line breaks
    if not key.upper().startswith(KEY_PREFIX):
        raise LicenceError("Not a SKEW licence key")
    try:
        raw = _b64decode(key[len(KEY_PREFIX):])
    except Exception:
        raise LicenceError("Licence key is damaged - copy it again")
    if len(raw) <= 64:
        raise LicenceError("Licence key is damaged - copy it again")

    payload, signature = raw[:-64], raw[-64:]
    try:
        Ed25519PublicKey.from_public_bytes(_b64decode(PUBLIC_KEY_B64)).verify(signature, payload)
    except (InvalidSignature, ValueError):
        raise LicenceError("Invalid licence key")

    try:
        lic_type, mac, issued, expires, customer = payload.decode("utf-8").split("|", 4)
        return {
            "type": lic_type,
            "type_name": TYPE_NAMES[lic_type],
            "mac": normalize_mac(mac),
            "issued": datetime.strptime(issued, "%Y%m%d").date(),
            "expires": datetime.strptime(expires, "%Y%m%d").date() if expires else None,
            "customer": customer,
        }
    except Exception:
        raise LicenceError("Invalid licence key")


def evaluate(key, mac=None, today=None, last_seen=None):
    """Licence status for a key on this machine (pure - no database)."""
    mac = normalize_mac(mac or machine_id())
    today = today or date.today()
    status = {"valid": False, "machine_id": mac, "type": None, "type_name": "Not activated",
              "expires": None, "days_left": None, "customer": "", "message": "No licence key entered"}
    if not key:
        return status

    try:
        lic = parse_key(key)
    except LicenceError as e:
        status["message"] = str(e)
        return status

    status.update(type=lic["type"], type_name=lic["type_name"], expires=lic["expires"],
                  customer=lic["customer"])

    if lic["mac"] != mac:
        status["message"] = "This licence key belongs to a different machine"
        return status

    if lic["expires"]:
        if last_seen and today < last_seen - CLOCK_TOLERANCE:
            status["message"] = "System date has been changed - correct the date to continue"
            return status
        status["days_left"] = (lic["expires"] - today).days
        if today > lic["expires"]:
            status["days_left"] = 0
            status["message"] = f"{lic['type_name']} licence expired on {lic['expires']:%d-%m-%Y}"
            return status

    status["valid"] = True
    status["message"] = f"{lic['type_name']} licence active"
    return status


# ---------------------------------------------------------------------------
# Stored licence (Info_db) + cache
# ---------------------------------------------------------------------------
_cache = {"at": 0.0, "status": None}
_cache_lock = threading.Lock()


def _read_info(cur):
    cur.execute('SELECT "Particulars", "Info" FROM "Info_db" WHERE "Particulars" IN (%s, %s)',
                (INFO_KEY, INFO_LAST_SEEN))
    return dict(cur.fetchall())


def _write_info(cur, particular, value):
    cur.execute('UPDATE "Info_db" SET "Info" = %s WHERE "Particulars" = %s', (value, particular))
    if cur.rowcount == 0:
        cur.execute('INSERT INTO "Info_db" ("Id", "Particulars", "Info") '
                    'SELECT COALESCE(MAX("Id"), 0) + 1, %s, %s FROM "Info_db"', (particular, value))


def status(force=False):
    """Current licence status (cached for CACHE_SECONDS)."""
    with _cache_lock:
        if not force and _cache["status"] and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["status"]

    try:
        conn = postgres.connect()
        try:
            with conn, conn.cursor() as cur:
                info = _read_info(cur)
                today = date.today()
                try:
                    last_seen = datetime.strptime(info.get(INFO_LAST_SEEN) or "", "%Y-%m-%d").date()
                except ValueError:
                    last_seen = None

                result = evaluate(info.get(INFO_KEY), today=today, last_seen=last_seen)

                # Remember the latest date seen, so winding the clock back is noticed
                if last_seen is None or today > last_seen:
                    _write_info(cur, INFO_LAST_SEEN, today.isoformat())
        finally:
            conn.close()
    except Exception as e:
        logging.exception("Licence check failed")
        # Keep the last good answer through a short database hiccup
        with _cache_lock:
            if _cache["status"]:
                return _cache["status"]
        result = evaluate(None)
        result["message"] = f"Licence check failed: {e}"

    with _cache_lock:
        _cache.update(at=time.monotonic(), status=result)
    return result


def activate(key):
    """Validates and stores a key. Returns (success, message)."""
    key = "".join(str(key or "").split())
    result = evaluate(key)
    if not result["valid"]:
        return False, result["message"]

    conn = postgres.connect()
    try:
        with conn, conn.cursor() as cur:
            _write_info(cur, INFO_KEY, key)
    finally:
        conn.close()

    status(force=True)
    days = f" - {result['days_left']} days left" if result["days_left"] is not None else ""
    logging.info(f"Licence activated: {result['type_name']}{days} (machine {result['machine_id']})")
    return True, f"{result['type_name']} licence activated{days}"
