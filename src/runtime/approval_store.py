from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.json_store import load_json, save_json
from core.paths import AppPaths


@dataclass
class ApprovalRecord:
    request_id: int
    method: str
    params: dict[str, Any]
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "method": self.method,
            "params": self.params,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRecord":
        return cls(
            request_id=int(data["request_id"]),
            method=data["method"],
            params=data.get("params", {}),
            status=data.get("status", "pending"),
        )


@dataclass
class ApprovalStoreState:
    approvals: list[ApprovalRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"approvals": [approval.to_dict() for approval in self.approvals]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalStoreState":
        return cls(approvals=[ApprovalRecord.from_dict(item) for item in data.get("approvals", [])])


class ApprovalStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def load(self) -> ApprovalStoreState:
        return load_json(self.paths.approvals, ApprovalStoreState.from_dict) or ApprovalStoreState()

    def save(self, state: ApprovalStoreState) -> None:
        save_json(self.paths.approvals, state.to_dict())

    def add(self, approval: ApprovalRecord) -> None:
        state = self.load()
        state.approvals = [item for item in state.approvals if item.request_id != approval.request_id]
        state.approvals.append(approval)
        self.save(state)

    def get_pending(self, request_id: int) -> ApprovalRecord | None:
        state = self.load()
        for approval in state.approvals:
            if approval.request_id == request_id and approval.status == "pending":
                return approval
        return None

    def mark(self, request_id: int, status: str) -> ApprovalRecord | None:
        state = self.load()
        for approval in state.approvals:
            if approval.request_id == request_id:
                approval.status = status
                self.save(state)
                return approval
        return None

    def pending(self) -> list[ApprovalRecord]:
        return [approval for approval in self.load().approvals if approval.status == "pending"]
