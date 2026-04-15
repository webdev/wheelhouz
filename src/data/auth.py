"""E*Trade OAuth 1.0a token management.

First-time setup requires a browser login. After that, tokens persist
in config/.etrade_tokens.json and are valid until midnight ET.

Usage:
    # First time (interactive):
    python -m src.data.auth

    # From code:
    from src.data.auth import get_session
    session = get_session()  # loads saved tokens or raises
"""

from __future__ import annotations

import json
import os
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyetrade
import structlog
from dotenv import load_dotenv

load_dotenv()

logger = structlog.get_logger()

TOKEN_FILE = Path("config/.etrade_tokens.json")

# Loaded from .env
CONSUMER_KEY = os.environ.get("ETRADE_CONSUMER_KEY", "")
CONSUMER_SECRET = os.environ.get("ETRADE_CONSUMER_SECRET", "")


@dataclass
class ETradeSession:
    """Authenticated E*Trade API session with all three clients."""
    accounts: pyetrade.ETradeAccounts
    market: pyetrade.ETradeMarket
    order: pyetrade.ETradeOrder
    oauth_token: str
    oauth_secret: str
    sandbox: bool
    authenticated_at: str


def _build_clients(
    oauth_token: str,
    oauth_secret: str,
    sandbox: bool,
) -> ETradeSession:
    """Create all three E*Trade API clients from tokens."""
    kwargs = dict(
        client_key=CONSUMER_KEY,
        client_secret=CONSUMER_SECRET,
        resource_owner_key=oauth_token,
        resource_owner_secret=oauth_secret,
        dev=sandbox,
    )
    return ETradeSession(
        accounts=pyetrade.ETradeAccounts(**kwargs),
        market=pyetrade.ETradeMarket(**kwargs),
        order=pyetrade.ETradeOrder(**kwargs),
        oauth_token=oauth_token,
        oauth_secret=oauth_secret,
        sandbox=sandbox,
        authenticated_at=datetime.now(timezone.utc).isoformat(),
    )


def save_tokens(oauth_token: str, oauth_secret: str, sandbox: bool) -> None:
    """Persist tokens to disk."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({
        "oauth_token": oauth_token,
        "oauth_secret": oauth_secret,
        "sandbox": sandbox,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    logger.info("etrade_tokens_saved", path=str(TOKEN_FILE))


def load_tokens() -> dict[str, str | bool] | None:
    """Load tokens from disk, or None if not present."""
    if not TOKEN_FILE.exists():
        return None
    return json.loads(TOKEN_FILE.read_text())  # type: ignore[no-any-return]


def authenticate_interactive(sandbox: bool = True) -> ETradeSession:
    """Run the one-time browser OAuth flow. Returns an authenticated session."""
    if not CONSUMER_KEY or not CONSUMER_SECRET:
        raise RuntimeError(
            "ETRADE_CONSUMER_KEY and ETRADE_CONSUMER_SECRET must be set in .env"
        )

    oauth = pyetrade.ETradeOAuth(CONSUMER_KEY, CONSUMER_SECRET)
    authorize_url = oauth.get_request_token()

    print(f"\nOpening browser for E*Trade authorization...")
    print(f"URL: {authorize_url}\n")
    webbrowser.open(authorize_url)

    verifier = input("Enter the verifier code from E*Trade: ").strip()
    tokens = oauth.get_access_token(verifier)

    oauth_token = tokens["oauth_token"]
    oauth_secret = tokens["oauth_token_secret"]

    save_tokens(oauth_token, oauth_secret, sandbox)

    session = _build_clients(oauth_token, oauth_secret, sandbox)
    logger.info("etrade_authenticated", sandbox=sandbox)
    return session


def renew_tokens(oauth_token: str, oauth_secret: str) -> bool:
    """Renew access tokens to extend the session (up to 2 hours).

    E*Trade allows renewal until midnight ET. After midnight, tokens are
    permanently expired and require a fresh browser-based OAuth flow.
    """
    try:
        mgr = pyetrade.ETradeAccessManager(
            client_key=CONSUMER_KEY,
            client_secret=CONSUMER_SECRET,
            resource_owner_key=oauth_token,
            resource_owner_secret=oauth_secret,
        )
        result = mgr.renew_access_token()
        if result:
            logger.info("etrade_tokens_renewed")
        return bool(result)
    except Exception as e:
        logger.warning("etrade_token_renewal_failed", error=str(e))
        return False


def get_session(sandbox: bool = True) -> ETradeSession:
    """Load saved tokens and return an authenticated session.

    Tries to renew tokens if they appear expired. Raises RuntimeError
    if tokens are missing or permanently expired (past midnight ET).
    """
    saved = load_tokens()
    if not saved:
        raise RuntimeError(
            "No saved E*Trade tokens. Run: python -m src.data.auth"
        )

    oauth_token = str(saved["oauth_token"])
    oauth_secret = str(saved["oauth_secret"])
    is_sandbox = bool(saved.get("sandbox", sandbox))

    session = _build_clients(oauth_token, oauth_secret, is_sandbox)

    # Verify tokens still work (they expire at midnight ET)
    try:
        session.accounts.list_accounts()
        logger.info("etrade_session_loaded", sandbox=is_sandbox)
        return session
    except Exception:
        # Try renewing before giving up
        logger.info("etrade_tokens_expired_attempting_renewal")
        if renew_tokens(oauth_token, oauth_secret):
            # Rebuild clients with same tokens (renewal extends server-side)
            session = _build_clients(oauth_token, oauth_secret, is_sandbox)
            try:
                session.accounts.list_accounts()
                logger.info("etrade_session_restored_via_renewal")
                return session
            except Exception:
                pass
        raise RuntimeError(
            "E*Trade tokens expired and renewal failed (past midnight ET). "
            "Run: python -m src.data.auth"
        )


# Allow running directly: python -m src.data.auth
if __name__ == "__main__":
    import sys
    sandbox = "--live" not in sys.argv

    if "--renew" in sys.argv:
        # Headless token renewal for cron use
        saved = load_tokens()
        if not saved:
            print("No saved tokens. Run: python -m src.data.auth --live")
            sys.exit(1)
        ok = renew_tokens(str(saved["oauth_token"]), str(saved["oauth_secret"]))
        if ok:
            print("Tokens renewed successfully.")
        else:
            print("Token renewal failed (expired past midnight ET?).")
            sys.exit(1)
    else:
        mode = "SANDBOX" if sandbox else "PRODUCTION"
        print(f"E*Trade OAuth — {mode} mode")
        print("=" * 40)
        session = authenticate_interactive(sandbox=sandbox)
        # Quick verification
        accounts = session.accounts.list_accounts()
        acct_list = accounts["AccountListResponse"]["Accounts"]["Account"]
        print(f"\nSuccess! Found {len(acct_list)} account(s):")
        for a in acct_list:
            print(f"  - {a.get('accountDesc', a['accountIdKey'])}")
