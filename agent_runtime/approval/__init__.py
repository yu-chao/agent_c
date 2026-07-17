from .models import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    RuntimeIdentity,
    hash_tool_arguments,
)
from .coordinator import ApprovalCoordinator
from .ports import ApprovalRepository

__all__ = [
    'ApprovalAction',
    'ApprovalCoordinator',
    'ApprovalDecision',
    'ApprovalRepository',
    'ApprovalRequest',
    'ApprovalStatus',
    'RuntimeIdentity',
    'SQLiteApprovalStore',
    'hash_tool_arguments',
]


def __getattr__(name: str):
    if name == 'SQLiteApprovalStore':
        from .store import SQLiteApprovalStore

        return SQLiteApprovalStore
    raise AttributeError(name)
