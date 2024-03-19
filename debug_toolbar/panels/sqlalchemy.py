import typing as t
from contextlib import AsyncExitStack
from time import perf_counter

from fastapi import HTTPException, Request, Response
from fastapi.dependencies.utils import solve_dependencies
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine, ExecutionContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from debug_toolbar.panels.sql import SQLPanel


class SQLAlchemyPanel(SQLPanel):
    title = "SQLAlchemy"

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        super().__init__(*args, **kwargs)
        self.engines: t.Set[Engine] = set()

    def register(self, engine: Engine) -> None:
        event.listen(engine, "before_cursor_execute", self.before_execute, named=True)
        event.listen(engine, "after_cursor_execute", self.after_execute, named=True)

    def unregister(self, engine: Engine) -> None:
        event.remove(engine, "before_cursor_execute", self.before_execute)
        event.remove(engine, "after_cursor_execute", self.after_execute)

    def before_execute(self, context: ExecutionContext, **kwargs: t.Any) -> None:
        context._start_time = perf_counter()  # type: ignore[attr-defined]

    def after_execute(self, context: ExecutionContext, **kwargs: t.Any) -> None:
        query = {
            "duration": (
                perf_counter() - context._start_time  # type: ignore[attr-defined]
            )
            * 1000,
            "sql": context.statement,
            "params": context.parameters,
        }
        self.add_query(str(context.engine.url), query)

    async def add_engines(self, request: Request):  # noqa: C901
        def add_bind_to_engines(bind: t.Union[Connection, Engine]):
            if isinstance(bind, Connection):
                self.engines.add(bind.engine)
            else:
                self.engines.add(bind)

        route = request["route"]

        if hasattr(route, "dependant"):
            try:
                solved_result = await solve_dependencies(
                    request=request,
                    dependant=route.dependant,
                    dependency_overrides_provider=route.dependency_overrides_provider,
                    async_exit_stack=AsyncExitStack(),
                )
            except HTTPException:
                pass
            else:
                for value in solved_result[0].values():
                    if isinstance(value, AsyncSession):
                        value = getattr(value, "sync_session", None)
                    if isinstance(value, Session):
                        binds = getattr(value, "_Session__binds", None)
                        if binds:
                            for bind in binds.values():
                                add_bind_to_engines(bind)
                        else:
                            bind = value.get_bind()
                            add_bind_to_engines(bind)

    async def process_request(self, request: Request) -> Response:
        await self.add_engines(request)

        for engine in self.engines:
            self.register(engine)
        try:
            response = await super().process_request(request)
        finally:
            for engine in self.engines:
                self.unregister(engine)
        return response
