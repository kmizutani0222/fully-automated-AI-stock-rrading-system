from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ModelRecord:
    version: str
    task: str
    status: str
    test_metrics: dict | None = None
    path: str | None = None


@dataclass
class ModelRegistry:
    records: dict[str, ModelRecord] = field(default_factory=dict)

    def add(self, record: ModelRecord) -> None:
        self.records[record.version] = record

    def activate(self, version: str) -> ModelRecord:
        record = self.records.get(version)
        if record is None:
            raise KeyError(version)
        reason = activation_block(record)
        if reason:
            raise ValueError(reason)
        for other in self.records.values():
            if other.task == record.task and other.status == "ACTIVE" and other.version != version:
                other.status = "INACTIVE"
        record.status = "ACTIVE"
        return record


def activation_block(record: ModelRecord) -> str | None:
    if record.status == "FAILED":
        return "a failed model cannot be activated"
    if record.status not in {"TRAINED", "ACTIVE"}:
        return "only a succeeded model can be activated"
    if not record.path:
        return "model artifact is missing"
    metrics = record.test_metrics or {}
    if not metrics:
        return "test metrics are missing"
    for value in metrics.values():
        if isinstance(value, dict) and (value.get("test_rmse") is None or value.get("test_samples", 0) <= 0):
            return "test metrics are incomplete"
    return None
