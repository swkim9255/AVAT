"""속도 패널 결과 줄·경고·안내 문구는 'T가 들어 있는 두 지점의 평균 속도' 규칙을 따른다."""

from __future__ import annotations

import pytest

from investigator.speed_estimation import SpeedPoint, estimate_speed
from investigator.widgets.speed_panel import SpeedPanel

POINTS = [SpeedPoint(100), SpeedPoint(130, 3.0), SpeedPoint(160, 5.0)]


@pytest.fixture
def panel(qtbot):
    widget = SpeedPanel()
    qtbot.addWidget(widget)
    return widget


def show(panel, points, trigger, fps=30.0):
    panel.set_data(points, estimate_speed(points, fps, trigger), fps)
    return panel.result_label.text()


def test_computed_result_names_the_two_points_of_the_t_segment(panel):
    text = show(panel, POINTS, 145)
    assert "T 시점 약 <b style='font-size:16px'>18 km/h</b>" in text
    assert "(17–19 km/h · 구간 평균 속도 · 2→3번 지점)" in text
    assert "저속(10-30kph)" in text
    assert panel.apply_button.isEnabled()
    # 계산에 쓴 두 지점 번호만 굵게 표시한다.
    assert [panel.table.item(row, 0).font().bold() for row in range(3)] == [False, True, True]


@pytest.mark.parametrize(
    ("points", "trigger", "fps", "message"),
    [
        (POINTS, None, 30.0, "T 트리거를 지정하면 T 시점의 속도를 계산해요."),
        (POINTS, 99, 30.0, "T 앞뒤에 지점을 하나씩 두면 T 시점 속도를 계산해요."),
        (POINTS, 161, 30.0, "T 앞뒤에 지점을 하나씩 두면 T 시점 속도를 계산해요."),
        ([SpeedPoint(100)], 100, 30.0, "T 앞뒤에 지점을 하나씩 두면 T 시점 속도를 계산해요."),
        ([SpeedPoint(100), SpeedPoint(130, 3.0), SpeedPoint(160)], 145, 30.0,
         "T가 들어 있는 구간(2→3번 지점)의 거리를 입력하면 T 시점 속도를 계산해요."),
        (POINTS, 145, 0.0, "영상 FPS를 알 수 없어 T 시점 속도를 계산할 수 없어요."),
    ],
)
def test_uncomputed_result_explains_what_is_missing(panel, points, trigger, fps, message):
    assert message in show(panel, points, trigger, fps)
    assert not panel.apply_button.isEnabled()


def test_boundary_warning_and_sensitivity_note_use_new_wording(panel):
    # 1프레임 구간은 범위 상한이 무한대라 속도 구분 경계에 걸친다.
    show(panel, [SpeedPoint(0), SpeedPoint(1, 1.0)], 0)
    assert panel.warning_label.text().startswith(
        "· T 시점 속도 범위가 속도 구간 경계에 걸쳐 있어요. T 앞뒤 지점의 위치와 거리를 다시 확인해 주세요."
    )
    assert panel.sensitivity_note.text() == (
        "T가 들어 있는 두 지점 사이의 평균 속도입니다(일정 FPS 가정). "
        "범위는 ±0.5프레임 배치 민감도이며 거리 오차는 포함하지 않습니다."
    )


def test_chart_draws_without_the_removed_fit_line(panel, qtbot):
    show(panel, POINTS, 145)
    panel.resize(900, 420)
    panel.show()
    qtbot.waitExposed(panel)
    assert panel.chart._estimate.velocity_line is None
    assert not panel.chart.grab().isNull()
