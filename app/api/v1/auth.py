from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_backend, get_directory
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services.backend import BackendClient, BackendError
from app.services.directory import UserDirectory, identity_from_profile

router = APIRouter(prefix="/auth", tags=["auth"])


def _raise_backend_error(exc: BackendError) -> None:
    status_code = exc.status_code if 400 <= exc.status_code <= 499 else 502
    raise HTTPException(status_code=status_code, detail=exc.detail) from exc


def _remember(directory: UserDirectory, response: dict) -> None:
    user = response.get("user")
    if isinstance(user, dict):
        try:
            directory.remember(identity_from_profile(user))
        except Exception:
            pass


@router.post("/login")
async def login(
    payload: LoginRequest,
    backend: BackendClient = Depends(get_backend),
    directory: UserDirectory = Depends(get_directory),
) -> dict:
    try:
        response = await backend.login(payload.username, payload.password)
    except BackendError as exc:
        _raise_backend_error(exc)
    _remember(directory, response)
    return response


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    backend: BackendClient = Depends(get_backend),
    directory: UserDirectory = Depends(get_directory),
) -> dict:
    try:
        response = await backend.register(payload.username, payload.password, payload.display_name)
    except BackendError as exc:
        _raise_backend_error(exc)
    _remember(directory, response)
    return response
