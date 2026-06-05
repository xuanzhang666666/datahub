"""独立字段级血缘流水线的数据模型。

这些模型刻意不复用表级血缘的 TableLineage / FieldLineage，避免字段级试验
耦合现有生产表级血缘链路。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List


class FieldLineageReviewStatus(str, Enum):
    """Excel 人工审核状态。"""

    PENDING = "PENDING"
    AUTO_APPROVED = "AUTO_APPROVED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NEEDS_FIX = "NEEDS_FIX"


def review_status_from_confidence(confidence: str) -> FieldLineageReviewStatus:
    """导出 Excel 时：LLM 标记为 HIGH 的候选默认进入自动通过队列。"""
    if (confidence or "").strip().upper() == "HIGH":
        return FieldLineageReviewStatus.AUTO_APPROVED
    return FieldLineageReviewStatus.PENDING


@dataclass(frozen=True)
class FieldLineageInput:
    """LLM 字段血缘解析的输入。"""

    dataset_urn: str
    table_name: str
    etl_script: str
    execute_shell: str
    target_schema_fields: List[str] = field(default_factory=list)
    target_table_aliases: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class FieldLineageCandidate:
    """LLM 生成、等待人工审核的一条字段血缘候选。"""

    target_table: str
    target_field: str
    source_table: str
    source_field: str
    transform_expression: str = ""
    transform_explanation: str = ""  # 中文：来源表/字段 + 加工含义（见 field_lineage_llm 提示词）
    evidence_sql: str = ""
    confidence: str = ""
    llm_notes: str = ""
    reviewer_notes: str = ""
    import_error: str = ""
    review_status: FieldLineageReviewStatus = FieldLineageReviewStatus.PENDING


@dataclass(frozen=True)
class UnresolvedField:
    """LLM 无法确认来源的目标字段。"""

    target_field: str
    reason: str


@dataclass(frozen=True)
class FieldLineageParseResult:
    """LLM 字段血缘 JSON 的结构化结果。"""

    target_table: str
    mappings: List[FieldLineageCandidate]
    unresolved_fields: List[UnresolvedField]
