"""Auth endpoint — returns the current user's profile from their Supabase JWT."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.get("/me", summary="Get current authenticated user")
async def get_me(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Return the authenticated user's ID, email, and metadata."""
    return {
        "id": current_user.get("id"),
        "email": current_user.get("email"),
        "role": current_user.get("role"),
        "user_metadata": current_user.get("user_metadata", {}),
        "app_metadata": current_user.get("app_metadata", {}),
    }
