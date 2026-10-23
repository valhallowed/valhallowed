"""The name a world's container and generation directory share."""

import hashlib
import re

# A DNS label holds 63 characters, and Docker's DNS answers to the service name:
# "world-", the slug, "-" and six hex digits.
MAX_SLUG = 63 - len("world-") - 1 - 6


def child_id(save_name: str) -> str:
    """The world's Compose service and container name: "world-", the save name in lower case
    with each run of other characters as one hyphen and cut to fit a DNS label, then six hex
    digits of the save name's hash, which tells apart names that read alike. Reconcile refuses
    the rare two names that still share one."""
    slug = re.sub(r"[^a-z0-9]+", "-", save_name.lower()).strip("-")[:MAX_SLUG].rstrip("-")
    digest = hashlib.sha256(save_name.encode()).hexdigest()[:6]
    return f"world-{slug}-{digest}"
