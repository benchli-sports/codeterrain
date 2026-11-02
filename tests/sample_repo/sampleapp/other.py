from fastapi import FastAPI

other = FastAPI()


@other.get("/health")
def health():
    return {"ok": True}
