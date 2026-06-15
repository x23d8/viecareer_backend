from fastapi import APIRouter, Depends

from core.clerk_auth import AdminUser, require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _admin_payload(admin: AdminUser) -> dict:
    return {
        "id": admin.internal_user_id,
        "external_auth_id": admin.user_id,
        "email": admin.email,
        "full_name": admin.full_name,
        "role_id": admin.role_id,
    }


@router.get("/me")
def get_current_admin(current_admin: AdminUser = Depends(require_admin)):
    return {"data": _admin_payload(current_admin)}


@router.get("/overview")
def get_admin_overview(current_admin: AdminUser = Depends(require_admin)):
    return {
        "data": {
            "current_admin": _admin_payload(current_admin),
            "dashboard": {},
        }
    }
