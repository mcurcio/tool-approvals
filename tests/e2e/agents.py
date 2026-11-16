"""The agents' identity provider for the e2e stack: one signing key, its public key set for the gateways, and a
JWT per principal (`sub`). Both gateways verify these tokens; the gate sees only the `sub` they attest."""

import json
import time
from pathlib import Path
from typing import Final

from joserfc import jwt
from joserfc.jwk import ECKey

ISSUER: Final = "https://agents.e2e.test"
AUDIENCE: Final = "tool-approvals-e2e"
_KID: Final = "e2e-agents"
_LIFETIME_S: Final = 3600


class AgentKeys:
    def __init__(self) -> None:
        self._key = ECKey.generate_key("P-256", parameters={"kid": _KID, "alg": "ES256", "use": "sig"})

    def write_jwks(self, path: Path) -> None:
        path.write_text(json.dumps({"keys": [self._key.as_dict(private=False)]}))

    def token(self, principal: str) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": principal, "iat": now, "exp": now + _LIFETIME_S}
        return jwt.encode({"alg": "ES256", "kid": _KID}, claims, self._key)
