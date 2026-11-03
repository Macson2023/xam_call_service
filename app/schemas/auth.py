from pydantic import BaseModel, Field

USERNAME_PATTERN = r"^[A-Za-z0-9_]{3,32}$"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=72)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=USERNAME_PATTERN)
    password: str = Field(min_length=8, max_length=72)
    display_name: str = Field(min_length=1, max_length=64)
