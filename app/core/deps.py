"""
core/deps.py
────────────
Dependencies for FastAPI endpoints.
Includes vendor session retrieval.
"""

from __future__ import annotations

import logging
from typing import Annotated
from dataclasses import dataclass

from fastapi import Header, HTTPException, status

logger = logging.getLogger(__name__)

@dataclass
class VendorSession:
    id:            str
    nin:           str
    first_name:    str
    last_name:     str
    date_of_birth: str


async def get_current_vendor_session(
    x_session_id: Annotated[str | None, Header()] = None
) -> VendorSession:
    """
    Retrieves the current vendor session.
    In a real app, this would look up the session_id in Redis/DB.
    For now, it extracts from header or throws 401.
    """
    if not x_session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Session-Id header",
        )
    
    # TODO: In production, fetch this from Redis/DB
    # For integration testing, we return a mock object if it's a test ID
    # or implement the actual lookup logic here.
    
    # Mock for now so the code runs
    return VendorSession(
        id=x_session_id,
        nin="12345678901",
        first_name="Mock",
        last_name="User",
        date_of_birth="1990-01-01"
    )
