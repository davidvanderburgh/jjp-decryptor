"""Deprecation redirector for JJP Asset Decryptor.

This standalone app has been retired in favour of the unified
**Pinball Asset Decryptor**.  On startup the GUI now polls the
unified app's release feed and surfaces a one-time prompt asking the
user to migrate — the standalone repo isn't getting new releases,
so a "version comparison" between this app's v1.x and the unified
app's v0.x would be misleading anyway.

Uses only the standard library (urllib, json).  All errors are
silently swallowed — a migration prompt is helpful but must never
interfere with the user finishing a job in this build.
"""

import json
import urllib.request

# The unified app — where every new feature, bug fix, and release
# lives from now on.  Includes JJP's full ISO + Direct-SSD flows
# (the Direct-SSD path even got upgrades this build can't carry).
UPSTREAM_REPO = "davidvanderburgh/pinball-asset-decryptor"
RELEASES_URL = (
    f"https://api.github.com/repos/{UPSTREAM_REPO}/releases/latest")
REQUEST_TIMEOUT = 5  # seconds


def check_for_update(current_version):  # noqa: ARG001
    """Return (latest_version, download_url) for the unified app.

    ``current_version`` is accepted for backward compatibility with
    the previous "is there a newer JJP build?" semantics but is no
    longer compared — the answer is unconditionally "yes, switch to
    the unified app" because *this* repo is frozen.

    Returns None only if the GitHub API call fails (rate-limited,
    offline, etc.) — the caller treats that as "skip the prompt
    this launch" and tries again next time.
    """
    try:
        req = urllib.request.Request(
            RELEASES_URL,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent":
                    "JJP-Asset-Decryptor-DeprecationCheck",
            })
        with urllib.request.urlopen(
                req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        tag = (data.get("tag_name") or "").lstrip("v")
        html_url = data.get("html_url") or ""
        if tag and html_url:
            return (tag, html_url)
    except Exception:
        pass
    return None
