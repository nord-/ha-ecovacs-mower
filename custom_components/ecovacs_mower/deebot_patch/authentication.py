"""Password-free renewal of the portal credentials.

Backport of the still-unreleased upstream fix (DeebotUniverse/client.py#1743,
wired into the core integration by home-assistant/core#178558) onto the pinned
deebot-client 18.5.1, which has neither an ``account_credentials`` seed nor
``login_with_account``.

Why it is needed: for some accounts Ecovacs answers ``user/login`` with code
1013 ("Please update to the latest version to continue") even for a device ID
that was verified by email minutes earlier, and the library raises that as
``DeviceVerificationRequiredError``. Home Assistant finishes the config flow,
reloads the entry, builds a fresh ``Authenticator`` that knows nothing but the
password, hits 1013 and asks for verification again — the loop of
home-assistant/core#177870, reported here as issue #21.

The way out is that the ``uid`` + ``accessToken`` pair returned by both
``user/login`` and ``user/verifyDevice`` mints fresh portal credentials through
``getAuthCode`` + ``loginByItToken``, neither of which involves the password.
Persisting that pair in the config entry is what makes a reload survive without
a new email code.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING, Any

from deebot_client.authentication import Authenticator, _AuthClient
from deebot_client.exceptions import AuthenticationError

if TYPE_CHECKING:
    from deebot_client.models import Credentials

_LOGGER = logging.getLogger(__name__)

# The name-mangled private of ``_AuthClient`` that carries the account pair.
#
# It is wrapped rather than reimplemented because it takes the raw
# ``{"uid", "accessToken"}`` dict of a login response and performs the entire
# tail of a login: getAuthCode, loginByItToken, the switch to the shorter UID
# and the expiry maths. Handing it a synthetic dict built from the stored pair
# *is* the token based login, with no copy of that logic to keep in sync.
COMPLETE_LOGIN = "_AuthClient__complete_login"

# Both members are replaced on the instance, so the library's own call paths
# (``verify_device`` and ``authenticate``) reach the wrappers through ``self``.
WRAPPED_MEMBERS = (COMPLETE_LOGIN, "login")


def missing_wrapped_members() -> tuple[str, ...]:
    """Return the members ``AccountAuthenticator`` wraps and does not find."""
    return tuple(name for name in WRAPPED_MEMBERS if not hasattr(_AuthClient, name))


class AccountAuthenticator(Authenticator):
    """Authenticator that prefers a login not involving the password endpoint."""

    # Declared on the class, not only assigned in ``__init__``, so that it is
    # part of the class' spec — ``patch(..., autospec=True)`` in the config flow
    # tests only sees what the class declares.
    account_credentials: dict[str, str] | None = None

    def __init__(
        self,
        *args: Any,
        account_credentials: dict[str, str] | None = None,
        on_account_credentials: Callable[[dict[str, str]], None] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize, optionally seeded with a persisted account pair.

        ``on_account_credentials`` is called with the new pair whenever one is
        captured that differs from what is already stored — a first capture
        during a password login, or a replacement minted by the password
        fallback below when a stored pair goes stale. It is not called for a
        token based login re-affirming the same pair, so a caller wiring it to
        ``async_update_entry`` does not rewrite the entry on every routine
        token refresh.
        """
        # Local import: the package imports this module, so this cannot be a
        # top level one. By the time an authenticator is built the package is
        # fully initialized.
        from . import _fail

        super().__init__(*args, **kwargs)
        self.account_credentials = account_credentials
        self._on_account_credentials = on_account_credentials

        if missing := missing_wrapped_members():
            _fail(
                f"deebot_client.authentication._AuthClient has no "
                f"{', '.join(missing)} for the token based login to wrap"
            )

        client = self._auth_client
        complete_login = getattr(client, COMPLETE_LOGIN)
        password_login = client.login

        async def capturing_complete_login(response: Any, error: str) -> Credentials:
            """Complete a login and keep the account pair it carried."""
            credentials = await complete_login(response, error)
            if isinstance(response, dict):
                access_token = response.get("accessToken")
                user_id = response.get("uid")
                if access_token and user_id:
                    pair = {
                        "access_token": str(access_token),
                        "user_id": str(user_id),
                    }
                    if pair != self.account_credentials:
                        self.account_credentials = pair
                        if self._on_account_credentials is not None:
                            self._on_account_credentials(pair)
            return credentials

        async def login() -> Credentials:
            """Log in with the account pair, falling back to the password."""
            account = self.account_credentials
            user_id = account.get("user_id") if account else None
            access_token = account.get("access_token") if account else None
            if user_id and access_token:
                _LOGGER.debug("Performing token based login")
                try:
                    return await capturing_complete_login(
                        {"uid": user_id, "accessToken": access_token},
                        "Invalid token based login response",
                    )
                except AuthenticationError:
                    # Narrow on purpose: a connection error has to reach the
                    # caller as such, not be retried as a password login.
                    _LOGGER.debug(
                        "Token based login failed, falling back to the password",
                        exc_info=True,
                    )
            _LOGGER.debug("Performing password login")
            return await password_login()

        setattr(client, COMPLETE_LOGIN, capturing_complete_login)
        client.login = login
