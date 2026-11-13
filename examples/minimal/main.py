"""The floor: one app, one module, one route.

uv run python examples/minimal/main.py
curl localhost:8080/api/v1/greeter/hello/Ada
"""

from __future__ import annotations

import inceptools
from inceptools import App, Controller, Get, Module
from inceptools.core.di import Injectable


@Injectable
class GreeterService:
    def greet(self, name: str) -> dict:
        return {"greeting": f"Hello, {name}"}


@Controller("/hello", tags=["greeter"])
class HelloController:
    def __init__(self, service: GreeterService) -> None:
        self.service = service

    @Get("/{name}")
    async def greet(self, name: str) -> dict:
        """Greet somebody."""
        return self.service.greet(name)


@Module(controllers=[HelloController], providers=[GreeterService])
class HelloModule: ...


@App(modules=[HelloModule], name="greeter")
class GreeterApp: ...


app = inceptools.create_app(service="minimal", version="1.0.0", prefix="/api/v1")
app.mount(GreeterApp)


if __name__ == "__main__":
    app.run(port=8080)
