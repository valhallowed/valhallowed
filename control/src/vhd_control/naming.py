"""The name a world's container and generation directory share."""

import hashlib
import re


def child_id(save_name: str) -> str:
    """A name Compose accepts and no other save name shares: the save name in lower case, with
    each run of other characters as one hyphen, then six hex digits of its hash."""
    slug = re.sub(r"[^a-z0-9]+", "-", save_name.lower()).strip("-")
    digest = hashlib.sha256(save_name.encode()).hexdigest()[:6]
    return f"world-{slug}-{digest}"
