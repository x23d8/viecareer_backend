from typing import Literal

from pydantic import BaseModel


class AdminUserStatusRequest(BaseModel):
    status: Literal["active", "suspended"]
