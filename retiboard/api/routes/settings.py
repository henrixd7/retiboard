"""
Global settings API for RetiBoard.
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Dict, Any

from retiboard.settings import get_settings

router = APIRouter(prefix="/settings", tags=["settings"])

class SettingsUpdate(BaseModel):
    settings: Dict[str, Any]

@router.get("")
async def get_all_settings():
    """Retrieve all global settings."""
    return get_settings().to_dict()

@router.patch("")
async def update_settings(update: SettingsUpdate):
    """Update global settings."""
    get_settings().update(update.settings)
    return get_settings().to_dict()
