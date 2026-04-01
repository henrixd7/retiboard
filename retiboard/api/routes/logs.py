"""
Logs API for RetiBoard.
"""

from fastapi import APIRouter

from retiboard.logging_buffer import get_log_buffer

router = APIRouter(prefix="/logs", tags=["logs"])

@router.get("")
async def get_logs():
    """Retrieve the in-memory log buffer."""
    return get_log_buffer().get_logs()
