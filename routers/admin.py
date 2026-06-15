from fastapi import APIRouter, Depends

from core.clerk_auth import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/overview")
def get_admin_overview():
    return {}
