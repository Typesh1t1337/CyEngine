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


@pytest.mark.parametrize("segment", [
    "4_2",              # подчёркивание: int("4_2") == 42
    "%2042",            # ведущий пробел после декодирования
    "42%20",
    "4%202",
    "+5", "-5",         # знак
    "%D9%A4%D9%A2",     # арабо-индийские цифры «٤٢»
    "%C2%B2",           # «²»: isdigit() True, int() падает
    "%EF%BC%94%EF%BC%92",   # полноширинные «４２»
    "",
    "9" * 5000,         # длиннее лимита цифр int() — не 500, а просто не совпадает
    "abc",
])
def test_int_param_accepts_only_ascii_digits(segment):
    r = Router()
    r.add("GET", "/users/<int:id>", h)
    with pytest.raises(RouterNotFound):
        r.match("GET", f"/users/{segment}")


def test_int_param_valid_values():
    r = Router()
    r.add("GET", "/users/<int:id>", h)
    assert r.match("GET", "/users/42")[1] == {"id": 42}
    assert r.match("GET", "/users/0")[1] == {"id": 0}
    assert r.match("GET", "/users/007")[1] == {"id": 7}
    assert r.match("GET", "/users/%34%32")[1] == {"id": 42}      # «%34%32» = ASCII "42"
