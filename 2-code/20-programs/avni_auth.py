#!/usr/bin/env python3
"""
avni_auth.py
------------
A small, reusable Avni API client with automatic Cognito login, token caching
and refresh. Import it from any script:

    from avni_auth import AvniClient

    client = AvniClient()                 # reads config from env (see below)
    subjects = client.get_all_pages("/api/subjects", subjectType="Pump")
    one = client.get("/api/subject/<uuid>")

CONFIGURATION (constructor args, each falling back to an env var)
----------------------------------------------------------------
    server      AVNI_SERVER              default https://app.avniproject.org
    username    AVNI_USERNAME            (needed for Cognito login)
    password    AVNI_PASSWORD            (needed for Cognito login)
    client_id   AVNI_COGNITO_CLIENT_ID   default 1d6rgvitjsfoonlkbm07uivgmg
    region      AVNI_COGNITO_REGION      default ap-south-1
    auth_token  AVNI_AUTH_TOKEN          optional pre-supplied Cognito *id* token
    version     ("2")                    Avni API version query param

AUTH NOTES
----------
Avni's `auth-token` header MUST be a Cognito *id* token (a JWT), NOT the access
token. A wrong token (access-vs-id, or a token minted for a different Avni
environment) makes the server throw a verification error surfaced as HTTP 500.
An expired / malformed token yields HTTP 401. Those hints are baked into the
error messages raised by get()/get_all_pages().

Dependencies: standard library only, plus boto3 (imported lazily inside login()).
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SERVER = "https://app.avniproject.org"
DEFAULT_CLIENT_ID = "1d6rgvitjsfoonlkbm07uivgmg"
DEFAULT_REGION = "ap-south-1"
DEFAULT_SINCE = "1900-01-01T00:00:00.001Z"

# Refresh a bit before the token actually expires so an in-flight request does
# not hit the exact expiry boundary.
EXPIRY_SAFETY_MARGIN_SECONDS = 60


def decode_jwt_claims(token):
    """Best-effort decode of a JWT payload (no signature check).

    Returns the claims dict, or None if the token is not a decodable JWT.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # pad base64url
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


class AvniError(Exception):
    """Raised for auth / HTTP problems talking to the Avni API."""


class AvniClient:
    """Reusable Avni API client with automatic Cognito login + token caching."""

    def __init__(self, server=None, username=None, password=None,
                 client_id=None, region=None, auth_token=None, version="2"):
        self.server = (server or os.environ.get("AVNI_SERVER") or DEFAULT_SERVER).rstrip("/")
        self.username = username or os.environ.get("AVNI_USERNAME")
        self.password = password or os.environ.get("AVNI_PASSWORD")
        self.client_id = (client_id or os.environ.get("AVNI_COGNITO_CLIENT_ID")
                          or DEFAULT_CLIENT_ID)
        self.region = region or os.environ.get("AVNI_COGNITO_REGION") or DEFAULT_REGION
        self.version = str(version) if version is not None else "2"

        # A pre-supplied id token (constructor arg or env) primes the cache.
        pre = auth_token or os.environ.get("AVNI_AUTH_TOKEN")
        if pre:
            pre = pre.strip().strip('"').strip("'")  # strip stray quotes/whitespace
        self._token = pre or None

    # ------------------------------------------------------------------ #
    # Token handling
    # ------------------------------------------------------------------ #
    def _is_expired(self, token):
        """True if the JWT is missing/undecodable/expired (with safety margin)."""
        if not token:
            return True
        claims = decode_jwt_claims(token)
        if not claims:
            # Not decodable -> treat as unusable so we log in afresh.
            return True
        exp = claims.get("exp")
        if not exp:
            # No exp claim we can trust -> force a login rather than risk a stale token.
            return True
        return float(exp) <= (time.time() + EXPIRY_SAFETY_MARGIN_SECONDS)

    def token(self):
        """Return a valid id token, reusing the cached one unless it is expired."""
        if self._token and not self._is_expired(self._token):
            return self._token
        return self.login()

    def login(self):
        """Force a fresh Cognito USER_PASSWORD_AUTH login; cache and return the id token."""
        try:
            import boto3
        except ImportError:
            raise AvniError(
                "Cognito login requires boto3, which is not installed. "
                "Install it with: pip install boto3"
            )

        if not self.username or not self.password:
            raise AvniError(
                "Cannot log in to Cognito: missing username/password. Set "
                "AVNI_USERNAME and AVNI_PASSWORD (or pass username=/password=), "
                "or supply a pre-minted id token via AVNI_AUTH_TOKEN / auth_token=."
            )
        if not self.client_id or not self.region:
            raise AvniError(
                "Cannot log in to Cognito: missing client id / region "
                "(AVNI_COGNITO_CLIENT_ID / AVNI_COGNITO_REGION)."
            )

        client = boto3.client("cognito-idp", region_name=self.region)
        try:
            resp = client.initiate_auth(
                ClientId=self.client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": self.username, "PASSWORD": self.password},
            )
        except Exception as e:
            raise AvniError(f"Cognito login failed: {e}")

        if "ChallengeName" in resp:
            raise AvniError(
                "Cognito returned a challenge: "
                f"{resp['ChallengeName']}. This usually means a forced password "
                "reset or MFA. Log in once via the web app to clear it, then retry."
            )

        result = resp.get("AuthenticationResult") or {}
        token = result.get("IdToken")
        if not token:
            raise AvniError(
                "Cognito login returned no IdToken (unexpected response shape). "
                f"Keys present: {sorted(result.keys())}"
            )
        self._token = token
        return token

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _build_url(self, path, params):
        clean = {}
        for k, v in params.items():
            if v is None or v == "":
                continue
            clean[k] = v
        clean.setdefault("version", self.version)
        query = urllib.parse.urlencode(clean)
        base = self.server + "/" + path.lstrip("/")
        return base + ("?" + query if query else "")

    def get(self, path, **params):
        """Authenticated GET. Returns parsed JSON. Raises AvniError on HTTP errors."""
        url = self._build_url(path, params)
        req = urllib.request.Request(
            url,
            headers={"auth-token": self.token(), "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            msg = [f"HTTP {e.code} from {url}", f"Response body: {body or '(empty)'}"]
            if e.code == 500:
                msg.append(
                    ">>> A 500 here is almost certainly the token "
                    "(access-vs-id or wrong environment): it is a valid JWT but its "
                    "issuer/audience/token_use do not match this server. Use the "
                    "Cognito *id* token minted for EXACTLY this AVNI_SERVER."
                )
            elif e.code == 401:
                msg.append(">>> 401 = token missing/expired. Grab or mint a fresh id token.")
            elif e.code == 403:
                msg.append(">>> 403 = authenticated but lacks the required role/privilege.")
            raise AvniError("\n".join(msg))
        except urllib.error.URLError as e:
            raise AvniError(f"Network error reaching {url}: {e.reason}")

    # ------------------------------------------------------------------ #
    # Cursor pagination
    # ------------------------------------------------------------------ #
    @staticmethod
    def _last_modified_of(item):
        return (item.get("audit") or {}).get("Last modified at")

    def get_all_pages(self, path, **params):
        """Walk Avni's cursor pagination and return every content item.

        Pagination advances lastModifiedDateTime to the last record's
        audit['Last modified at']; it stops when a page has no content or when
        the cursor fails to advance (guards against an infinite loop). Do NOT
        rely on totalElements (it is per-page, not a grand total).
        """
        cursor = params.pop("lastModifiedDateTime", None) or DEFAULT_SINCE
        results = []
        seen_cursors = set()
        while True:
            page_params = dict(params)
            page_params["lastModifiedDateTime"] = cursor
            data = self.get(path, **page_params)
            content = data.get("content") or []
            if not content:
                break
            results.extend(content)

            next_cursor = self._last_modified_of(content[-1])
            if not next_cursor or next_cursor in seen_cursors:
                # Cursor did not advance -> stop to avoid an infinite loop.
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return results


# --------------------------------------------------------------------------- #
# Demo / smoke test (safe on import: nothing runs unless executed directly)
# --------------------------------------------------------------------------- #
def _demo():
    client = AvniClient()
    print(f"Server     : {client.server}")
    print(f"Client id  : {client.client_id}")
    print(f"Region     : {client.region}")
    print("-" * 60)

    # 1) Token check -- decode whatever token we can obtain WITHOUT crashing.
    token = None
    if client._token:
        token = client._token
        print("Have a pre-supplied / cached token (AVNI_AUTH_TOKEN or auth_token=).")
    else:
        try:
            token = client.token()  # may trigger a Cognito login
        except AvniError as e:
            print("No usable token yet (this is fine for a structural check):")
            print(f"  {e}")

    if token:
        claims = decode_jwt_claims(token) or {}
        exp = claims.get("exp")
        expired = bool(exp) and float(exp) <= time.time()
        print("Token check:")
        print(f"  iss       : {claims.get('iss', '(?)')}")
        print(f"  aud       : {claims.get('aud', '(?)')}")
        print(f"  token_use : {claims.get('token_use', '(?)')}")
        print(f"  expired   : {expired}")
        if claims.get("token_use") and claims.get("token_use") != "id":
            print("  ! token_use is not 'id' -> this is the ACCESS token; Avni wants "
                  "the id token (wrong one -> 500).")

    print("-" * 60)

    # 2) If we have credentials/token, do a tiny live call -- but never crash.
    if not token:
        print("Skipping live /api/subjects call (no token / credentials available).")
        return
    try:
        data = client.get("/api/subjects", lastModifiedDateTime=DEFAULT_SINCE)
        content = data.get("content") or []
        print(f"/api/subjects page 1: {len(content)} record(s).")
    except AvniError as e:
        print("Live /api/subjects call did not succeed:")
        print(f"  {e}")


if __name__ == "__main__":
    _demo()
