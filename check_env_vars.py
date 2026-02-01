"""Quick diagnostic script to verify required notification environment variables."""

from __future__ import annotations

import os

from dotenv import load_dotenv


REQUIRED_KEYS = [
    "SENDGRID_API_KEY",
    "SENDGRID_SENDER_EMAIL",
    "APPOINTMENT_HASH_KEY",
]

OPTIONAL_KEYS = [
    "DOXY_ROOM_URL",
    "ON_CALL_DOCTOR_NAME",
]


def main() -> None:
    load_dotenv()
    for key in REQUIRED_KEYS:
        value = os.getenv(key)
        status = "SET" if value else "MISSING"
        print(f"{key}: {status}")

    for key in OPTIONAL_KEYS:
        value = os.getenv(key)
        status = "SET" if value else "MISSING (optional)"
        print(f"{key}: {status}")


if __name__ == "__main__":
    main()
