# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
"""
CyFrame Router — Trie-based HTTP router.

  - Статические сегменты:   /users/profile
  - Typed параметры:        /users/<int:id>  /posts/<str:slug>
  - Wildcard:               /static/<path:rest>
  - HTTP методы
"""


from cyengine.core._types import MethodNotAllowed, RouterNotFound


cdef int PARAM_STR  = 0
cdef int PARAM_INT  = 1
cdef int PARAM_PATH = 2 # lazy


cdef inline object _coerce(int kind, str value):
    if kind == PARAM_INT:
        try:
            return int(value)
        except ValueError:
            return None

    return value


cdef int _parse_param_type(str type_str) except -1:
    if type_str == "int":
        return PARAM_INT
    if type_str == "path":
        return PARAM_PATH
    return PARAM_STR



cdef class TrieNode:
    cdef public dict children
    cdef public TrieNode param_child
    cdef public str param_name
    cdef public int param_type
    cdef public dict handlers

    def __cinit__(self):
        self.children    = {}
        self.param_child = None
        self.param_name  = ""
        self.param_type  = PARAM_STR
        self.handlers    = None   # None → промежуточный узел


cdef class Router:
    """
    Использование (Python-сторона):
        router = Router()
        router.add("GET",  "/users/<int:id>", my_handler)
        router.add("POST", "/users",          create_handler)

        handler, params = router.match("GET", "/users/42")
        # handler = my_handler
        # params  = {"id": 42}
    """
    cdef TrieNode _root

    def __cinit__(self):
        self._root = TrieNode()

    cpdef void add(self, str method, str path, object handler):
        cdef TrieNode node = self._root
        cdef TrieNode child
        cdef list segments = _split_path(path)
        cdef str seg, pname, ptype_str
        cdef int ptype

        for seg in segments:
            if seg.startswith("<") and seg.endswith(">"):
                inner = seg[1:-1]
                if ":" in inner:
                    ptype_str, pname = inner.split(":", 1)
                    ptype = _parse_param_type(ptype_str)
                else:
                    pname  = inner
                    ptype  = PARAM_STR

                if node.param_child is None:
                    child = TrieNode()
                    child.param_name = pname
                    child.param_type = ptype
                    node.param_child = child
                else:
                    child = node.param_child
                node = child
            else:
                if seg not in node.children:
                    node.children[seg] = TrieNode()
                node = node.children[seg]

        if node.handlers is None:
            node.handlers = {}
        node.handlers[method.upper()] = handler


    cpdef tuple match(self, str method, str path):
        cdef TrieNode node = self._root
        cdef list segments = _split_path(path)
        cdef dict params = {}
        cdef str seg
        cdef object handler

        for seg in segments:
            if seg in node.children:
                node = node.children[seg]
                continue

            if node.param_child is not None:
                child = node.param_child

                if child.param_type == PARAM_PATH:
                    idx = segments.index(seg)
                    params[child.param_name] = "/".join(segments[idx:])
                    node = child
                    break

                coerced = _coerce(child.param_type, seg)
                if coerced is None:
                    raise RouterNotFound(method, path)
                params[child.param_name] = coerced
                node = child
                continue

            raise RouterNotFound(method, path)

        if node.handlers is None:
            raise RouterNotFound(method, path)

        method_upper = method.upper()
        handler = node.handlers.get(method_upper)
        if handler is None:
            if node.handlers:
                raise MethodNotAllowed(method, path)
            raise RouterNotFound(method, path)

        return (handler, params)


    cpdef void get(self, str path, object handler):
        self.add("GET", path, handler)

    cpdef void post(self, str path, object handler):
        self.add("POST", path, handler)

    cpdef void put(self, str path, object handler):
        self.add("PUT", path, handler)

    cpdef void delete(self, str path, object handler):
        self.add("DELETE", path, handler)

    cpdef void patch(self, str path, object handler):
        self.add("PATCH", path, handler)

    # ── Дебаг ────────────────────────────────────────────────────────────────

    def routes(self):
        result = []
        _collect(self._root, "", result)
        return result


cdef list _split_path(str path):
    cdef str stripped = path.strip("/")
    if not stripped:
        return []
    return stripped.split("/")


def _collect(TrieNode node, str prefix, list result):
    if node.handlers:
        for method, handler in node.handlers.items():
            result.append((method, "/" + prefix if prefix else "/", handler))
    for seg, child in node.children.items():
        new_prefix = (prefix + "/" + seg) if prefix else seg
        _collect(child, new_prefix, result)
    if node.param_child is not None:
        child = node.param_child
        pname = f"<{child.param_name}>"
        new_prefix = (prefix + "/" + pname) if prefix else pname
        _collect(child, new_prefix, result)
