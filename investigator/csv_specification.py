"""Column order of the investigation CSV export.

The companion data specification is not generated from the form: exports copy
the bundled ``AVAT 데이터 명세서.xlsx`` unchanged (see ``CaseRepository.export_csv``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from investigator.case_numbering import REPORT_ACCIDENT_NUMBER_COLUMN

if TYPE_CHECKING:
    from investigator.form_definition import FormDefinition

METADATA_HEADERS = (
    "case_id", "workflow_status", "video_files", "active_video_file",
    "trigger_frame", "collision_frames", "video_markers",
)


def csv_export_headers(definition: FormDefinition) -> list[str]:
    return [*METADATA_HEADERS, *definition.csv_headers(), REPORT_ACCIDENT_NUMBER_COLUMN]
