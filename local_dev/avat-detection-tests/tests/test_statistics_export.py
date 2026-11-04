import csv

import pytest
from PySide6.QtGui import QImage
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QLabel

from investigator.statistics_export import (
    _report_html,
    export_overview_pdf,
    export_statistics_csv,
    export_statistics_png,
)
from investigator.theme import apply_theme


def test_statistics_csv_keeps_regular_table_bom_and_numeric_values(tmp_path):
    path = tmp_path / "statistics.csv"
    result = export_statistics_csv(
        path,
        ["분류", "건수", "비율"],
        [["승용", 3, 75.0], ["미입력", 1, 25.0], [None, -2, -1.5]],
        title="충돌 대상",
        filters="상태: 완료",
    )
    assert result == path.resolve()
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows == [
        ["분류", "건수", "비율"],
        ["승용", "3", "75.0"],
        ["미입력", "1", "25.0"],
        ["", "-2", "-1.5"],
    ]


@pytest.mark.parametrize(
    ("headers", "rows"),
    [
        (
            ["행 값", "열 값", "건수", "행 기준 비율(%)", "행 사고 수"],
            [["승용", "경상", 2, 66.67, 3], ["승용", "미입력", 1, 33.33, 3]],
        ),
        (
            ["그룹", "사고번호", "수치", "단위"],
            [
                ["화물", "SAMSUNG-0000000000001", 12.5, "m"],
                ["승용", "SAMSUNG-0000000000002", 0.0, "m"],
            ],
        ),
        (
            ["사고번호", "가로축", "세로축"],
            [["SAMSUNG-0000000000001", -1.25, 0.5], ["SAMSUNG-0000000000002", 0.0, 0.0]],
        ),
    ],
    ids=["categorical-cross-table", "grouped-numeric", "numeric-pairs"],
)
def test_csv_accepts_each_cross_view_snapshot_without_reaggregating(tmp_path, headers, rows):
    destination = export_statistics_csv(
        tmp_path / "cross-view.csv", headers, rows, title="교차 분석", filters="완료만"
    )
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        exported = list(csv.reader(handle))
    assert exported == [headers, *[[str(value) for value in row] for row in rows]]


@pytest.mark.parametrize(
    "text", ["=1+1", "+cmd", "-cmd", "@SUM(A1)", "  =1+1", "\tname", "\rname", "\nname"]
)
def test_statistics_csv_neutralizes_user_formula_text_in_labels_and_headers(tmp_path, text):
    path = export_statistics_csv(
        tmp_path / "statistics.csv", [text], [[text]], title="Title", filters="All"
    )
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows == [["'" + text], ["'" + text]]


def test_png_contains_visible_context_and_machine_readable_metadata(qtbot, tmp_path):
    widget = QLabel("Actual chart contents")
    qtbot.addWidget(widget)
    widget.resize(1800, 500)
    widget.show()
    path = export_statistics_png(
        tmp_path / "statistics.png", widget, "사고 통계", "상태: 완료 · 차량: 승용", 23
    )
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    image = QImage(str(path))
    assert not image.isNull()
    assert image.width() <= 1400
    assert image.text("Title") == "사고 통계"
    assert image.text("Filters") == "상태: 완료 · 차량: 승용"
    assert image.text("SampleCount") == "23"
    assert image.text("GeneratedAt").endswith(" KST")
    assert image.pixelColor(0, 0).name() == "#ffffff"


def test_pdf_paginated_report_contains_filters_data_and_literal_user_text(qapp, tmp_path):
    apply_theme(qapp)
    title = "AVAT 통계 보고서"
    path = export_overview_pdf(
        tmp_path / "overview.pdf",
        title,
        "Status completed",
        {"Accidents": 120, "Human injury": 7},
        [
            (
                "Actual categories",
                ["Category", "Count"],
                [[f"Category {index}", index] for index in range(160)],
            ),
            ("User text", ["Value"], [["<script>literal & text</script>"]]),
        ],
    )
    assert path.read_bytes().startswith(b"%PDF-")
    document = QPdfDocument()
    assert document.load(str(path)) == QPdfDocument.Error.None_
    assert document.pageCount() >= 2
    assert document.metaData(QPdfDocument.MetaDataField.Title) == title
    text = "\n".join(document.getAllText(index).text() for index in range(document.pageCount()))
    # Some PDF font subsets return per-glyph line breaks when text is extracted.
    compact = "".join(text.split())
    assert "Statuscompleted" in compact
    assert "Actualcategories" in compact
    assert "Category159" in compact
    assert "literal&text" in compact
    assert "KST" in compact
    document.close()


@pytest.mark.parametrize("kind", ["csv", "png", "pdf"])
def test_export_replace_failure_preserves_existing_file_and_cleans_temp(
    kind, qtbot, tmp_path, monkeypatch
):
    destination = tmp_path / f"statistics.{kind}"
    destination.write_bytes(b"previous export")
    widget = QLabel("Chart")
    qtbot.addWidget(widget)

    def locked_file(source, target):
        raise PermissionError("The export is open in another program")

    monkeypatch.setattr("investigator.statistics_export.os.replace", locked_file)
    with pytest.raises(PermissionError):
        if kind == "csv":
            export_statistics_csv(destination, ["Value"], [[1]], title="Stats", filters="All")
        elif kind == "png":
            export_statistics_png(destination, widget, "Stats", "All", 1)
        else:
            export_overview_pdf(destination, "Stats", "All", {"Accidents": 1}, [])
    assert destination.read_bytes() == b"previous export"
    assert not list(tmp_path.glob(".avat-statistics-*"))


def test_malformed_export_table_does_not_replace_existing_file(tmp_path):
    destination = tmp_path / "statistics.csv"
    destination.write_bytes(b"previous export")
    with pytest.raises(ValueError, match="칼럼 수"):
        export_statistics_csv(destination, ["Value"], [[1, 2]], title="Stats", filters="All")
    assert destination.read_bytes() == b"previous export"
    assert not list(tmp_path.glob(".avat-statistics-*"))


def test_malformed_pdf_table_does_not_replace_existing_report(qapp, tmp_path):
    destination = tmp_path / "overview.pdf"
    destination.write_bytes(b"previous report")
    with pytest.raises(ValueError, match="칼럼 수"):
        export_overview_pdf(
            destination,
            "개요 보고서",
            "완료만",
            {"selected": 1},
            [("항목", ["값", "분모"], [["승용"]])],
        )
    assert destination.read_bytes() == b"previous report"
    assert not list(tmp_path.glob(".avat-statistics-*"))


def test_report_translates_known_summary_keys_and_preserves_custom_labels():
    report = _report_html(
        "Report <title>",
        "All & complete",
        {
            "selected": 10,
            "total": 12,
            "completed": 8,
            "draft": 2,
            "videos": 14,
            "severe_cases": 1,
            "injury_cases": 2,
            "missing_cases": 2,
            "completion_rate": 80.0,
            "mean_interval_seconds": 1.25,
            "median_interval_seconds": 1.0,
            "사용자 정의 지표": "<b>5</b>",
        },
        [("Distribution", ["Value", "분모"], [["<script>literal</script>", 10]])],
        "2026-10-18 12:00:00 KST",
    )
    for label in (
        "대상 사고 수 (건)",
        "전체 사고 수 (건)",
        "완료 사고 (건)",
        "임시저장 사고 (건)",
        "연결 영상 수 (개)",
        "중상·사망 사고 (건)",
        "인명피해 사고 (건)",
        "필수 미입력이 있는 사고 (건)",
        "완료율 (%)",
        "평균 트리거 → 충돌 (초)",
        "중앙값 트리거 → 충돌 (초)",
        "사용자 정의 지표",
    ):
        assert label in report
    assert "<td>selected</td>" not in report
    assert "&lt;script&gt;literal&lt;/script&gt;" in report
    assert "&lt;b&gt;5&lt;/b&gt;" in report
    assert "각 표에 표시한 분모" in report
    assert "개요 보고서이며 인과관계를 판정하지 않습니다" in report
    assert "표본 수와 각 검정의 가정을 확인해야 합니다" in report
