"""The Web Push channel (milestone 3): approval.waiting and approval.resolved, encrypted per RFC 8291 (aes128gcm)
and authorised with VAPID (RFC 8292), to every subscription the event's approver groups route to; sent only to
endpoints under the configured push origins."""


def application_server_key(vapid_private_key_pem: bytes) -> str:
    """The VAPID public key a browser subscribes with (the Push API's applicationServerKey): the uncompressed P-256
    point of the key's public half, base64url without padding. Raises ValueError for a key that is not a PEM P-256
    private key."""
    raise NotImplementedError
