from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.db.session_scope() as session:
        yield session


# The session context commits (or rolls back) after the path function but
# before FastAPI sends the response.  Request-scoped teardown would let a 2xx
# response race a following request or independent reader that still cannot
# observe the mutation.
SessionDep = Annotated[Session, Depends(get_session, scope="function")]
