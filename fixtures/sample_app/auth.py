"""Auth mainline (fixture).

Material flow: an authorization header → token → claims → stable User state.

  login(req)
    └─ verify_token(token)
         └─ parse_jwt(token)        ← INTERMEDIATE processing step (claims)
              └─ _decode(token)
    └─ get_current_user(user_id)    ← STABLE SINK (the settled User state)

`parse_jwt` is deliberately an *intermediate* node: it returns half-processed
claims, not a settled domain object. `get_current_user` is the *stable state*.
billing.py crosses both — see that file for why one crossing is healthy and the
other is responsibility pollution.
"""


def _decode(token):
    # Pretend-decode: the part before the first '.' is the subject.
    return {"sub": token.split(".", 1)[0], "raw": token}


def parse_jwt(token):
    """Intermediate: raw token -> claims dict. NOT a stable domain object."""
    claims = _decode(token)
    return claims


def verify_token(token):
    """Auth processing: validate claims, yield the user id."""
    claims = parse_jwt(token)
    return claims["sub"]


def get_current_user(user_id):
    """Stable sink: the settled User state other features should depend on."""
    return {"id": user_id, "name": "Alice", "role": "member"}


def login(req):
    token = req["authorization"]
    user_id = verify_token(token)
    return get_current_user(user_id)
