import functools

from fastapi import APIRouter, Depends, FastAPI, WebSocket
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

API_VERSION = "v1"


class Item(BaseModel):
    id: int
    name: str


def require_token():
    return "token"


def audited(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


items = APIRouter(prefix="/items", tags=["items"])
parts = APIRouter(prefix="/{item_id}/parts")
root = APIRouter(prefix=f"/{API_VERSION}")
admin = APIRouter(prefix=f"/{API_VERSION}/admin")


@items.get("/{item_id}", response_model=Item)
def get_item(item_id: int):
    return Item(id=item_id, name="thing")


@items.post("")
async def create_item(item: Item) -> Item:
    return item


@parts.get("")
def list_parts(item_id: int):
    return []


@admin.get("/audit")
@audited
def audit_log():
    return []


@items.websocket("/{item_id}/live")
async def live(websocket: WebSocket, item_id: int):
    await websocket.close()


def embedded_status(request):
    return PlainTextResponse("ok")


items.mount("/embedded", Starlette(routes=[Route("/status", embedded_status)]))
items.include_router(parts)
root.include_router(items)

app = FastAPI(title="sample")
app.include_router(root, dependencies=[Depends(require_token)])
app.include_router(admin)

legacy = FastAPI()


@legacy.get("/ping")
def ping():
    return "pong"


app.mount("/legacy", legacy)
