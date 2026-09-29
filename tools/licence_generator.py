"""SKEW licence key generator - VENDOR USE ONLY. Never ship this or the private key.

One-time setup (creates the signing key pair and prints the public key to put
into auth/licence.py PUBLIC_KEY_B64):

    python tools/licence_generator.py init

Issue a key for a client machine (Machine ID is shown on the activation screen):

    python tools/licence_generator.py demo 54:05:db:cc:7b:1a --customer "ABC Feeds"
    python tools/licence_generator.py full 54:05:db:cc:7b:1a --customer "ABC Feeds"

Options:
    --days N        demo length (default 30) or an expiry for a full licence
    --key-file P    private key location (default: %USERPROFILE%\\.skew_licence\\private_key.pem)

BACK UP THE PRIVATE KEY FILE SAFELY. Without it no new keys can be issued;
anyone who has it can issue keys.
"""
import argparse
import base64
import os
import sys
from datetime import date, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_KEY_FILE = os.path.join(os.path.expanduser("~"), ".skew_licence", "private_key.pem")


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def load_private_key(path):
    if not os.path.exists(path):
        sys.exit(f"Private key not found at {path} - run 'init' first or pass --key-file")
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def public_key_b64(private_key):
    return b64(private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def cmd_init(args):
    if os.path.exists(args.key_file):
        sys.exit(f"{args.key_file} already exists - refusing to overwrite (existing keys depend on it)")
    os.makedirs(os.path.dirname(args.key_file), exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    with open(args.key_file, "wb") as f:
        f.write(private_key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    print(f"Private key written to {args.key_file}  <- BACK THIS UP, KEEP IT SECRET")
    print(f'Put this in auth/licence.py:\nPUBLIC_KEY_B64 = "{public_key_b64(private_key)}"')


def issue_key(private_key, lic_type, mac, days=None, customer="", issued=None):
    from auth.licence import KEY_PREFIX, normalize_mac
    issued = issued or date.today()
    expires = issued + timedelta(days=days) if days else None
    customer = customer.replace("|", "/")
    payload = "|".join([lic_type, normalize_mac(mac), f"{issued:%Y%m%d}",
                        f"{expires:%Y%m%d}" if expires else "", customer]).encode("utf-8")
    return KEY_PREFIX + b64(payload + private_key.sign(payload)), expires


def cmd_issue(args):
    from auth.licence import TYPE_DEMO, TYPE_PURCHASED, normalize_mac
    mac = normalize_mac(args.machine_id)
    if len(mac.split(":")) != 6:
        sys.exit(f"'{args.machine_id}' is not a MAC address like 54:05:db:cc:7b:1a")

    if args.command == "demo":
        lic_type, days = TYPE_DEMO, args.days or 30
    else:
        lic_type, days = TYPE_PURCHASED, args.days

    key, expires = issue_key(load_private_key(args.key_file), lic_type, mac, days, args.customer or "")
    kind = "Demo" if lic_type == TYPE_DEMO else "Purchased"
    print(f"{kind} licence for {mac}" + (f" ({args.customer})" if args.customer else ""))
    print(f"Expires: {expires:%d-%m-%Y}" if expires else "Expires: never")
    print()
    print(key)


def main():
    parser = argparse.ArgumentParser(description="SKEW licence key generator (vendor only)")
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the signing key pair (once)")
    for name in ("demo", "full"):
        p = sub.add_parser(name, help=f"issue a {name} licence key")
        p.add_argument("machine_id", help="Machine ID (MAC) shown on the client's activation screen")
        p.add_argument("--days", type=int, help="demo length / optional expiry for a full licence")
        p.add_argument("--customer", help="client name printed on the licence")
    args = parser.parse_args()
    cmd_init(args) if args.command == "init" else cmd_issue(args)


if __name__ == "__main__":
    main()
