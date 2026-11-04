# persona.py — Auto-UV phase 2: the biometric persona (plan.md §5.2).
#
# Rewrites authenticatorGetInfo responses so the host believes the key
# has built-in biometric user verification:
#
#   options.clientPin  -> removed      (no PIN dialog in the browser)
#   options.uv         -> true         (UV "available" for required/preferred)
#
# Effect on the host (verified against Chromium device/fido sources,
# plan.md §10 item 4): Chrome takes the kNoTokenInternalUV path — it
# sends the request as-is with options {uv:true} and NO pinUvAuthParam,
# exactly the wire shape the phase-4 injector needs. The real key (no
# biometric) rejects options.uv:true with UNSUPPORTED_OPTION, which the
# injector will fix by stripping uv and injecting a real pinUvAuthParam.
#
# Fail-open by design: any parse problem returns the message unchanged
# and the relay behaves like a plain pipe for that response.

import cbor_min

CTAP2_GET_INFO = 0x04


def rewrite_getinfo(message):
    """Rewrite a GetInfo response (status byte + CBOR map) in place.

    Returns the rewritten message, or the original bytes unchanged on
    any problem (not a GetInfo success, unparseable CBOR, no options
    map, or nothing to change).
    """
    try:
        if not message or message[0] != 0x00:
            return message  # error response or empty: not ours to touch
        info = cbor_min.decode(message[1:])
        if not isinstance(info, dict):
            return message
        options = info.get(4)
        if not isinstance(options, dict):
            return message
        changed = False
        if "clientPin" in options:
            del options["clientPin"]
            changed = True
        if options.get("uv") is not True:
            options["uv"] = True
            changed = True
        if not changed:
            return message
        return b"\x00" + cbor_min.encode(info)
    except Exception:
        return message  # fail-open: never break the pipe over a rewrite
