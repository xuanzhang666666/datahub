"""Excel review files for field-level lineage candidates."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .field_lineage_models import (
    FieldLineageCandidate,
    FieldLineageInput,
    FieldLineageReviewStatus,
    UnresolvedField,
)

CANDIDATE_HEADERS = [
    "review_status",
    "target_table",
    "target_field",
    "source_table",
    "source_field",
    "transform_expression",
    "evidence_sql",
    "confidence",
    "llm_notes",
    "reviewer_notes",
    "import_error",
]

UNRESOLVED_HEADERS = ["target_field", "reason"]
CONTEXT_HEADERS = ["key", "value"]


def _style_sheet(ws) -> None:
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9E1F2")
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for idx, _ in enumerate(ws[1], start=1):
        letter = get_column_letter(idx)
        max_len = 12
        for cell in ws[letter]:
            if cell.value:
                max_len = min(max(max_len, len(str(cell.value))), 80)
        ws.column_dimensions[letter].width = max_len + 2
    ws.freeze_panes = "A2"


def _candidate_to_row(candidate: FieldLineageCandidate) -> List[str]:
    return [
        candidate.review_status.value,
        candidate.target_table,
        candidate.target_field,
        candidate.source_table,
        candidate.source_field,
        candidate.transform_expression,
        candidate.evidence_sql,
        candidate.confidence,
        candidate.llm_notes,
        candidate.reviewer_notes,
        candidate.import_error,
    ]


def write_candidate_workbook(
    path: Path,
    *,
    source_input: FieldLineageInput,
    candidates: Iterable[FieldLineageCandidate],
    unresolved_fields: Iterable[UnresolvedField],
    llm_model: str,
) -> None:
    """Write reviewable field-lineage candidates to an Excel workbook."""
    wb = Workbook()
    ws = wb.active
    ws.title = "candidate_lineage"
    ws.append(CANDIDATE_HEADERS)
    for candidate in candidates:
        ws.append(_candidate_to_row(candidate))
    _style_sheet(ws)

    unresolved_ws = wb.create_sheet("unresolved_fields")
    unresolved_ws.append(UNRESOLVED_HEADERS)
    for unresolved in unresolved_fields:
        unresolved_ws.append([unresolved.target_field, unresolved.reason])
    _style_sheet(unresolved_ws)

    context_ws = wb.create_sheet("source_context")
    context_ws.append(CONTEXT_HEADERS)
    context_ws.append(["table_name", source_input.table_name])
    context_ws.append(["dataset_urn", source_input.dataset_urn])
    context_ws.append(["etl_script_chars", str(len(source_input.etl_script))])
    context_ws.append(["execute_shell_chars", str(len(source_input.execute_shell))])
    context_ws.append(["execute_shell", source_input.execute_shell])
    context_ws.append(["llm_model", llm_model])
    context_ws.append(["generated_at", datetime.now(timezone.utc).isoformat()])
    _style_sheet(context_ws)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _row_dict(headers: List[str], values: List[object]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for idx, header in enumerate(headers):
        value = values[idx] if idx < len(values) else ""
        out[header] = "" if value is None else str(value).strip()
    return out


def load_approved_review_rows(path: Path) -> List[FieldLineageCandidate]:
    """Read only APPROVED rows from a human-reviewed workbook."""
    wb = load_workbook(path)
    if "candidate_lineage" not in wb.sheetnames:
        raise RuntimeError("Excel 缺少 candidate_lineage 工作表")
    ws = wb["candidate_lineage"]
    headers = [str(cell.value or "").strip() for cell in ws[1]]
    approved: List[FieldLineageCandidate] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        rec = _row_dict(headers, list(row))
        status = rec.get("review_status", "").upper()
        if status != FieldLineageReviewStatus.APPROVED.value:
            continue
        approved.append(
            FieldLineageCandidate(
                target_table=rec.get("target_table", "").lower(),
                target_field=rec.get("target_field", "").lower(),
                source_table=rec.get("source_table", "").lower(),
                source_field=rec.get("source_field", "").lower(),
                transform_expression=rec.get("transform_expression", ""),
                evidence_sql=rec.get("evidence_sql", ""),
                confidence=rec.get("confidence", "").upper(),
                llm_notes=rec.get("llm_notes", ""),
                reviewer_notes=rec.get("reviewer_notes", ""),
                import_error=rec.get("import_error", ""),
                review_status=FieldLineageReviewStatus.APPROVED,
            )
        )
    return approved
