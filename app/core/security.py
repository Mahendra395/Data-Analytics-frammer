"""Supabase JWT verification helper for FastAPI."""
from __future__ import annotations

from typing import Any, Dict

import httpx
from fastapi import HTTPException, status

from app.core.config import get_settings

settings = get_settings()


async def verify_supabase_token(token: str) -> Dict[str, Any]:
    """
    Call Supabase Auth REST API to validate a JWT and return the user object.

    Raises HTTP 401 if the token is missing, expired, or invalid.
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_ANON_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service not configured.",
        )

    url = f"{settings.SUPABASE_URL}/auth/v1/user"
    headers = {
        "Authorization": f"Bearer {token}",
        "apikey": settings.SUPABASE_ANON_KEY,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Auth service unreachable: {exc}",
            )

    if response.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return response.json()
