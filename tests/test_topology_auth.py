from js8mail.adapters.topology_auth import sign_request, verify_request


def test_signature_covers_body_and_expires() -> None:
    secret = b"per-install-secret"
    signature = sign_request(secret, "post", "/v1/observations", 1000, "nonce-1", b"{}")
    assert verify_request(
        secret, signature, "POST", "/v1/observations", 1000, "nonce-1", b"{}", now=1001
    )
    assert not verify_request(
        secret, signature, "POST", "/v1/observations", 1000, "nonce-1", b'{"x":1}', now=1001
    )
    assert not verify_request(
        secret, signature, "POST", "/v1/observations", 1000, "nonce-1", b"{}", now=1401
    )
