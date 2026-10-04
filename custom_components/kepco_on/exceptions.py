"""Exception hierarchy for the KEPCO ON integration."""


class KepcoOnError(Exception):
    """Base exception for KEPCO ON integration errors."""


class KepcoOnAuthError(KepcoOnError):
    """Authentication failed or cannot be completed."""


class KepcoOnSessionExpired(KepcoOnAuthError):
    """The KEPCO ON session is expired or no longer accepted."""


class KepcoOnMfaRequired(KepcoOnAuthError):
    """The account requires an interactive authentication challenge."""


class KepcoOnUnsupportedAccount(KepcoOnError):
    """The authenticated account type is outside the supported INDI scope."""


class KepcoOnNoCustomersError(KepcoOnError):
    """The account has no supported customer contracts."""


class KepcoOnConnectionError(KepcoOnError):
    """A temporary KEPCO ON connection failure occurred."""


class KepcoOnRateLimitError(KepcoOnConnectionError):
    """KEPCO ON rate-limited the request."""


class KepcoOnProtocolError(KepcoOnError):
    """KEPCO ON returned an unexpected protocol shape."""


class KepcoOnPartialUpdateError(KepcoOnError):
    """One or more selected customers failed while others updated."""
