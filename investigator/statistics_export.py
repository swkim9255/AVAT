"""Atomic CSV, PNG, and PDF exports of computed statistics snapshots."""

from __future__ import annotations

import csv
import html
import math
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QMarginsF, QRect, QSizeF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QTextDocument,
)
from PySide6.QtWidgets import QApplication, QWidget

_KOREAN_TIMEZONE = timezone(timedelta(hours=9), name="KST")
_MAX_IMAGE_PIXELS = 32_000_000
_SUMMARY_LABELS = {
    "selected": "대상 사고 수 (건)",
    "total": "전체 사고 수 (건)",
    "completed": "완료 사고 (건)",
    "draft": "임시저장 사고 (건)",
    "videos": "연결 영상 수 (개)",
    "severe_cases": "중상·사망 사고 (건)",
    "injury_cases": "인명피해 사고 (건)",
    "missing_cases": "필수 미입력이 있는 사고 (건)",
    "completion_rate": "완료율 (%)",
    "mean_interval_seconds": "평균 트리거 → 충돌 (초)",
    "median_interval_seconds": "중앙값 트리거 → 충돌 (초)",
}


def _generated_at() -> str:
    return datetime.now(_KOREAN_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S KST")


@contextmanager
def _atomic_destination(path: str | Path, suffix: str):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=destination.parent, prefix=".avat-statistics-", suffix=suffix, delete=False
        ) as handle:
            temporary = Path(handle.name)
        yield destination, temporary
        # Windows FlushFileBuffers requires a descriptor opened for writing.
        with temporary.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _safe_csv_cell(value: Any) -> Any:
    """Neutralize formulas in user text while retaining signed numeric cells."""

    if value is None:
        return ""
    if isinstance(value, str) and (
        value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))
    ):
        return "'" + value
    return value


def _validate_table(headers: list[str], rows: list[list[Any]]) -> None:
    if not headers:
        raise ValueError("내보낼 표에 칼럼이 없습니다.")
    if any(len(row) != len(headers) for row in rows):
        raise ValueError("표의 칼럼 수와 행의 값 개수가 일치하지 않습니다.")


def export_statistics_csv(
    path: Path,
    headers: list[str],
    rows: list[list[Any]],
    *,
    title: str,
    filters: str,
) -> Path:
    """Export a regular UTF-8-BOM table with one header, without preamble rows.

    ``title`` and ``filters`` share the export interface with image and report
    exports. CSV contains exactly the caller's table; metadata can be provided
    as explicit columns when a downstream workflow needs it.
    """

    del title, filters
    _validate_table(headers, rows)
    with (
        _atomic_destination(path, ".csv") as (destination, temporary),
        temporary.open("w", encoding="utf-8-sig", newline="") as handle,
    ):
        writer = csv.writer(handle)
        writer.writerow([_safe_csv_cell(header) for header in headers])
        writer.writerows([_safe_csv_cell(cell) for cell in row] for row in rows)
        handle.flush()
        os.fsync(handle.fileno())
    return destination


def export_statistics_png(
    path: Path,
    widget: QWidget,
    title: str,
    filters: str,
    sample_count: int,
) -> Path:
    """Capture the supplied chart or report view with visible export context."""

    if QApplication.instance() is None:
        raise ValueError("이미지를 내보내려면 실행 중인 앱이 필요합니다.")
    pixel_ratio = widget.devicePixelRatioF()
    if widget.width() * widget.height() * pixel_ratio**2 > _MAX_IMAGE_PIXELS:
        raise ValueError("이미지로 내보낼 내용이 너무 큽니다. 표시할 항목을 줄여 주세요.")
    snapshot = widget.grab()
    if snapshot.isNull():
        raise ValueError("이미지로 내보낼 그래프가 없습니다.")
    snapshot.setDevicePixelRatio(1)
    margin = 28
    width = max(640, min(1400, snapshot.width() + margin * 2))
    if snapshot.width() > width - margin * 2:
        snapshot = snapshot.scaledToWidth(
            width - margin * 2, Qt.TransformationMode.SmoothTransformation
        )
    title_font = QFont(QApplication.font())
    title_font.setPixelSize(22)
    title_font.setWeight(QFont.Weight.DemiBold)
    metadata_font = QFont(QApplication.font())
    metadata_font.setPixelSize(12)
    text_width = width - margin * 2
    title_height = (
        QFontMetrics(title_font)
        .boundingRect(
            QRect(0, 0, text_width, 100000),
            Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere,
            title,
        )
        .height()
    )
    filter_text = f"필터: {filters or '전체'}"
    filter_height = (
        QFontMetrics(metadata_font)
        .boundingRect(
            QRect(0, 0, text_width, 100000),
            Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere,
            filter_text,
        )
        .height()
    )
    content_top = margin + title_height + filter_height + 48
    height = content_top + snapshot.height() + margin
    if width * height > _MAX_IMAGE_PIXELS:
        raise ValueError("이미지로 내보낼 내용이 너무 큽니다. 표시할 항목을 줄여 주세요.")
    generated = _generated_at()
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    if image.isNull():
        raise OSError("통계 이미지를 생성할 메모리가 부족합니다.")
    image.fill(QColor("#FFFFFF"))
    image.setText("Title", title)
    image.setText("Filters", filters or "전체")
    image.setText("SampleCount", str(max(0, int(sample_count))))
    image.setText("GeneratedAt", generated)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(title_font)
        painter.setPen(QColor("#1B1F24"))
        painter.drawText(
            QRect(margin, margin, text_width, title_height),
            Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere,
            title,
        )
        painter.setFont(metadata_font)
        painter.setPen(QColor("#5B636E"))
        painter.drawText(
            QRect(margin, margin + title_height + 10, text_width, filter_height),
            Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere,
            filter_text,
        )
        painter.drawText(
            QRect(margin, margin + title_height + filter_height + 18, text_width, 22),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"대상 {max(0, int(sample_count)):,}건 · 생성 {generated}",
        )
        painter.drawPixmap(margin, content_top, snapshot)
    finally:
        painter.end()
    with _atomic_destination(path, ".png") as (destination, temporary):
        if not image.save(str(temporary), "PNG"):
            raise OSError("통계 이미지를 저장할 수 없습니다.")
    return destination


def _report_text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        text = f"{value:.6g}" if math.isfinite(value) else "—"
    else:
        text = str(value)
    return html.escape(text).replace("\n", "<br>")


def _report_html(
    title: str,
    filters: str,
    summary: dict,
    tables: list[tuple[str, list[str], list[list[Any]]]],
    generated: str,
) -> str:
    parts = [
        "<html><head><style>"
        "body { color: #1B1F24; font-size: 9pt; }"
        "h1 { font-size: 19pt; margin-bottom: 10px; }"
        "h2 { font-size: 12pt; margin-top: 20px; }"
        "p.meta { color: #5B636E; }"
        "table { border-collapse: collapse; width: 100%; margin-top: 8px; }"
        "th { background-color: #EEF0F3; font-weight: bold; text-align: left; }"
        "td, th { border: 1px solid #DDE1E6; padding: 5px; vertical-align: top; }"
        "</style></head><body>",
        f"<h1>{_report_text(title)}</h1>",
        f'<p class="meta">필터: {_report_text(filters or "전체")}<br>생성: {_report_text(generated)}</p>',
    ]
    if summary:
        parts.append(
            '<h2>요약</h2><table width="100%"><thead><tr><th>지표</th><th>값</th></tr></thead><tbody>'
        )
        parts.extend(
            f"<tr><td>{_report_text(_SUMMARY_LABELS.get(key, key))}</td><td>{_report_text(value)}</td></tr>"
            for key, value in summary.items()
        )
        parts.append("</tbody></table>")
    for table_title, headers, rows in tables:
        parts.append(f'<h2>{_report_text(table_title)}</h2><table width="100%"><thead><tr>')
        parts.extend(f"<th>{_report_text(header)}</th>" for header in headers)
        parts.append("</tr></thead><tbody>")
        if rows:
            for row in rows:
                parts.append("<tr>")
                parts.extend(f"<td>{_report_text(cell)}</td>" for cell in row)
                parts.append("</tr>")
        else:
            parts.append(f'<tr><td colspan="{len(headers)}">표시할 데이터가 없습니다.</td></tr>')
        parts.append("</tbody></table>")
    parts.append(
        '<h2>집계 기준</h2><p class="meta">'
        "비율은 각 표에 표시한 분모를 기준으로 계산합니다. 조건부 항목은 해당 사고를 기준으로 하며, "
        "다중 선택에서는 한 사고가 여러 값에 포함되어 비율 합계가 100%를 넘을 수 있습니다.<br>"
        "영상 시간 지표는 현재 선택 영상의 T·C 마커와 FPS를 확인할 수 있는 유효한 사고를 기준으로 합니다.<br>"
        "이 보고서는 관측된 자료를 기술한 개요 보고서이며 인과관계를 판정하지 않습니다. "
        "교차 분석의 검정 결과를 해석할 때에는 표본 수와 각 검정의 가정을 확인해야 합니다."
        "</p></body></html>"
    )
    return "".join(parts)


def export_overview_pdf(
    path: Path,
    title: str,
    filters: str,
    summary: dict,
    tables: list[tuple[str, list[str], list[list[Any]]]],
) -> Path:
    """Write a paginated A4 report containing only supplied computed values."""

    if QApplication.instance() is None:
        raise ValueError("보고서를 내보내려면 실행 중인 앱이 필요합니다.")
    for _, headers, rows in tables:
        _validate_table(headers, rows)
    report = _report_html(title, filters, summary, tables, _generated_at())
    with _atomic_destination(path, ".pdf") as (destination, temporary):
        writer = QPdfWriter(str(temporary))
        try:
            writer.setTitle(title)
            writer.setCreator("AVAT")
            writer.setResolution(144)
            writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
            writer.setPageOrientation(QPageLayout.Orientation.Portrait)
            writer.setPageMargins(QMarginsF(14, 14, 14, 14), QPageLayout.Unit.Millimeter)
            document = QTextDocument()
            document.documentLayout().setPaintDevice(writer)
            document.setDocumentMargin(0)
            document.setPageSize(
                QSizeF(writer.pageLayout().paintRectPixels(writer.resolution()).size())
            )
            font = QFont(QApplication.font())
            font.setPointSizeF(9)
            document.setDefaultFont(font)
            document.setHtml(report)
            document.print_(writer)
        finally:
            del writer
        with temporary.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise OSError("통계 보고서를 생성할 수 없습니다.")
            handle.seek(max(0, temporary.stat().st_size - 32))
            if b"%%EOF" not in handle.read():
                raise OSError("통계 보고서가 완전히 저장되지 않았습니다.")
    return destination


__all__ = ["export_statistics_csv", "export_statistics_png", "export_overview_pdf"]
