# injector.py — Auto-UV phase 4 (plan.md §5.3): the pinUvAuthParam injector.
#
# Rewrites PC -> key makeCredential/getAssertion requests so the YubiKey
# performs genuine PIN-based user verification, while the PC believes it
# talked to a biometric key (the persona):
#
#   - options.uv is REMOVED (fw 5.4.3 rejects the uv option key with
#     UNSUPPORTED_OPTION even when a valid pinUvAuthParam is present -
#     phase-0 probes P5a/P5f)
#   - pinUvAuthParam = HMAC-SHA-256(pinUvAuthToken, clientDataHash) is
#     added (full 32 bytes on protocol 2, 16 on protocol 1), replacing
#     any existing (empty, pre-flight) value
#   - pinUvAuthProtocol is added
#
# The key itself verifies the HMAC and sets UV=true in the signed
# assertion - nothing is forged at the assertion level.
#
# Policy (UV_POLICY in code.py):
#   "follow"  inject only when the request's options carry uv:true
#             (Chrome's UV-required / discoverable-credential shape)
#   "always"  inject on every makeCredential/getAssertion
#
# Fail-open by design: no token, no clientDataHash, unparseable CBOR ->
# the request is forwarded unmodified (UV=preferred sites still work
# touch-only; UV=required sites fail visibly at the RP).

import cbor_min
import sha256

CTAP2_MAKE_CREDENTIAL = 0x01
CTAP2_GET_ASSERTION = 0x02

# CTAP2 parameter keys are positional (python-fido2 args()); they differ
# between the two commands:
#   makeCredential: 1=clientDataHash 7=options 8=pinUvAuthParam 9=proto
#   getAssertion:   2=clientDataHash 5=options 6=pinUvAuthParam 7=proto
_PARAM_KEYS = {
    CTAP2_MAKE_CREDENTIAL: (1, 7, 8, 9),
    CTAP2_GET_ASSERTION: (2, 5, 6, 7),
}


def _xor_pad(key, pad):
    k = bytes(key) + bytes(64 - len(key))
    out = bytearray(64)
    for i in range(64):
        out[i] = k[i] ^ pad
    return bytes(out)


def hmac_sha256(key, message):
    """HMAC-SHA-256 (RFC 2104, block size 64; key is a 32-byte token)."""
    ipad = _xor_pad(key, 0x36)
    opad = _xor_pad(key, 0x5C)
    inner = sha256.sha256(ipad + bytes(message))
    return sha256.sha256(opad + inner)


def inject(message, token, token_proto, uv_policy="follow"):
    """Rewrite one request; returns (new_message, injected).

    new_message is the original bytes unchanged whenever anything is off
    (fail-open). injected is True only when a pinUvAuthParam was spliced.
    """
    if not message:
        return message, False
    cmd = message[0]
    keys = _PARAM_KEYS.get(cmd)
    if keys is None or not token:
        return message, False
    cdh_key, opt_key, param_key, proto_key = keys
    try:
        params = cbor_min.decode(message[1:])
        if not isinstance(params, dict):
            return message, False

        options = params.get(opt_key)
        has_uv_opt = isinstance(options, dict) and options.get("uv") is True
        if uv_policy == "follow" and not has_uv_opt:
            # Chrome did not ask for UV (discouraged/preferred-unresolved):
            # pass through untouched. Still strip a uv key if present so the
            # option-phobic firmware never sees it (uv:false means "no UV",
            # removing it means the same thing).
            if isinstance(options, dict) and "uv" in options:
                del options["uv"]
                if options:
                    params[opt_key] = options
                else:
                    del params[opt_key]
                return bytes([cmd]) + cbor_min.encode(params), False
            return message, False

        cdh = params.get(cdh_key)
        if not isinstance(cdh, (bytes, bytearray)) or len(cdh) != 32:
            return message, False

        # strip uv from the options map (mandatory, see module docstring)
        if isinstance(options, dict) and "uv" in options:
            del options["uv"]
            if options:
                params[opt_key] = options
            else:
                del params[opt_key]

        param = hmac_sha256(token, bytes(cdh))
        if token_proto == 1:
            param = param[:16]
        params[param_key] = param
        params[proto_key] = token_proto
        return bytes([cmd]) + cbor_min.encode(params), True
    except Exception:
        return message, False  # fail-open: never break the pipe
