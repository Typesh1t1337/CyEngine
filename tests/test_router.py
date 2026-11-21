import pytest

from cyengine.core._router import Router
from cyengine.core._types import MethodNotAllowed, RouterNotFound


async def h(request): ...
async def h2(request): ...


def test_static_and_typed_params():
    r = Router()
    r.add("GET", "/users/<int:id>", h)
    assert r.match("GET", "/users/42") == (h, {"id": 42})
    with pytest.raises(RouterNotFound):
        r.match("GET", "/users/abc")


def test_static_beats_param_with_backtracking():
    r = Router()
    r.add("GET", "/users/me", h)
    r.add("GET", "/users/<int:id>/posts", h2)
    assert r.match("GET", "/users/me")[0] is h
    assert r.match("GET", "/users/5/posts") == (h2, {"id": 5})


def test_path_wildcard_takes_rest():
    r = Router()
    r.add("GET", "/a/<path:rest>", h)
    assert r.match("GET", "/a/b/a/c")[1] == {"rest": "b/a/c"}


def test_method_not_allowed_vs_not_found():
    r = Router()
    r.add("GET", "/x", h)
    with pytest.raises(MethodNotAllowed):
        r.match("POST", "/x")
    with pytest.raises(RouterNotFound):
        r.match("GET", "/y")


def test_conflicting_param_rejected():
    r = Router()
    r.add("GET", "/u/<int:id>", h)
    with pytest.raises(ValueError):
        r.add("GET", "/u/<name>/x", h)
