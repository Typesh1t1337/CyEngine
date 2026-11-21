"""Одинаковые приложения для сравнения. Запуск: python apps.py <variant> <port>"""
import sys

PAYLOAD = {"message": "hello", "ok": True, "items": [1, 2, 3]}


def cyengine_app(variant):
    from cyengine import App, Limits, Response
    from cyengine.middleware import recover

    async def hello(request):
        return Response.json(PAYLOAD)

    async def user(request):
        return Response.json({"id": request.path_params["id"]})

    limits = Limits()
    if variant == "cy-nolimits":
        limits = Limits(None, None, None, None, None)
    elif variant.startswith("cy-off-"):          # cy-off-<поле>: отключить один лимит
        limits = Limits(**{variant[len("cy-off-"):]: None})
    app = App(limits=limits)
    if variant == "cy-recover":
        app.use(recover)
    app.handle("GET", "/json", hello)
    app.handle("GET", "/users/<int:id>", user)
    return app


def starlette_app():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def hello(request):
        return JSONResponse(PAYLOAD)

    async def user(request):
        return JSONResponse({"id": request.path_params["id"]})

    return Starlette(routes=[Route("/json", hello), Route("/users/{id:int}", user)])


if __name__ == "__main__":
    variant, port = sys.argv[1], int(sys.argv[2])
    if variant.startswith("cy"):
        import uvloop
        uvloop.run(cyengine_app(variant).serve(host="127.0.0.1", port=port))
    else:
        import uvicorn
        uvicorn.run(starlette_app(), host="127.0.0.1", port=port,
                    log_level="error", http="httptools", loop="uvloop")
