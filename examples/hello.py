import asyncio

try:
    import uvloop
    run = uvloop.run          # заметно быстрее стандартного loop
except ImportError:
    run = asyncio.run

from cyengine import App, Request, Response
from cyengine.middleware import recover, access_log


class Database:
    """Пример зависимости: создаётся в main, закрывается в main."""

    @classmethod
    async def connect(cls, dsn):
        print("db: connected")
        return cls()

    async def get_user(self, user_id: int):
        return {"id": user_id, "name": f"user{user_id}"}

    async def close(self):
        print("db: closed")


class UserHandler:
    def __init__(self, db: Database):
        self.db = db

    async def handle(self, request: Request) -> Response:
        return Response.json(await self.db.get_user(request.path_params["id"]))


async def health(request: Request) -> Response:
    request.body()
    return Response.text("ok")


async def main():
    db = await Database.connect("postgresql://...")
    try:
        app = App()
        app.use(access_log(), recover)
        app.handle("GET", "/users/<int:id>", UserHandler(db))
        app.handle("GET", "/health", health)
        await app.serve(port=8000)
    finally:
        await db.close()


if __name__ == "__main__":
    run(main())
