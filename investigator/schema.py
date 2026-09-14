"""Declarative investigation form, validation, and CSV contract."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

MISSING_VALUE_MESSAGE = "미입력 값이 있습니다"
INVALID_FORMAT_MESSAGE = "형식이 잘못되었습니다"


class FieldKind(StrEnum):
    TEXT = "text"
    LONG_TEXT = "long_text"
    DIGITS = "digits"
    DECIMAL = "decimal"
    SINGLE = "single"
    MULTI = "multi"


@dataclass(frozen=True, slots=True)
class Choice:
    label: str
    value: str
    csv_column: str | None = None


@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str
    label: str
    kind: FieldKind
    csv_column: str | None = None
    choices: tuple[Choice, ...] = ()
    required: bool = False
    editable: bool = False
    max_length: int | None = None
    exact_length: int | None = None
    placeholder: str = ""
    helper: str = ""
    unit: str = ""
    group: str = ""


@dataclass(frozen=True, slots=True)
class SectionSpec:
    key: str
    title: str
    short_title: str
    fields: tuple[FieldSpec, ...]


def choices(*labels: str) -> tuple[Choice, ...]:
    return tuple(Choice(label, label) for label in labels)


IDENTITY_FIELDS = (
    FieldSpec(
        "institution",
        "기관코드 (Index)",
        FieldKind.SINGLE,
        "Institution",
        (
            Choice("katri", "katri"),
            Choice("ajou", "ajou"),
            Choice("nfs", "nfs"),
            Choice("samsung", "samsung"),
        ),
        required=True,
        editable=True,
        placeholder="기관코드",
        helper="공백 없이 입력 · 예: katri, ajou, nfs, samsung",
    ),
    FieldSpec(
        "accident_classification",
        "사고 구분 번호",
        FieldKind.DIGITS,
        "accident_classification",
        required=True,
        max_length=13,
        placeholder="사고 구분 번호",
        helper="숫자 최대 13자리이며 앞자리 0이 유지됩니다.",
    ),
)


DRIVER_AGE_CHOICES = choices(*(f"{start}-{start + 4}세" for start in range(20, 100, 5)))

TRIGGER_VEHICLE_SPEED_CHOICES = choices(
    "정차(0kph)",
    "크립(0-10kph)",
    "저속(10-30kph)",
    "중속(30-60kph)",
    "고속(60kph이상)",
)

LEGACY_TRIGGER_VEHICLE_SPEED_VALUES = {
    "정차": "정차(0kph)",
    "크립": "크립(0-10kph)",
    "저속": "저속(10-30kph)",
    "중속": "중속(30-60kph)",
    "고속": "고속(60kph이상)",
}

LEGACY_AVAILABLE_DATA_VALUES = {
    "전방블랙박스": "자차전방블랙박스",
    "후방블랙박스": "자차후방블랙박스",
}


def normalize_driver_age(value: Any) -> str | None:
    """Map a legacy numeric age to the corresponding five-year range."""

    if value is None:
        return None
    text = str(value).strip()
    allowed = {choice.value for choice in DRIVER_AGE_CHOICES}
    if text in allowed:
        return text
    if text.isdigit():
        age = int(text)
        if 20 <= age <= 99:
            start = 20 + ((age - 20) // 5) * 5
            return f"{start}-{start + 4}세"
    return None


def incident_day_for_date(value: Any) -> str | None:
    """Return the Korean weekday for a valid YYYYMMDD value."""

    text = str(value).strip() if value is not None else ""
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        parsed = datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return ("월", "화", "수", "목", "금", "토", "일")[parsed.weekday()]


INJURY_GRADE_RULES = {
    "incident_minor_injury_grades": (
        12,
        14,
        "incident_minor_injury",
        "incident_minor_representative_injury_grade",
    ),
    "incident_severe_injury_grades": (
        1,
        11,
        "incident_severe_injury",
        "incident_severe_representative_injury_grade",
    ),
}


def injury_grade_summary(value: Any, *, minimum: int, maximum: int) -> tuple[str, str, str] | None:
    """Normalize a comma-delimited injury-grade list within the requested range."""

    text = str(value).strip() if value is not None else ""
    if not text:
        return "", "", ""
    parts = text.split(",")
    if any(not re.fullmatch(r"\s*\d{1,2}\s*", part) for part in parts):
        return None
    grades = sorted(int(part.strip()) for part in parts)
    if any(grade < minimum or grade > maximum for grade in grades):
        return None
    normalized = ", ".join(map(str, grades))
    return normalized, str(len(grades)), str(grades[0])


DRIVER = SectionSpec(
    "driver",
    "운전자 요인",
    "운전자",
    (
        FieldSpec(
            "driver_sex",
            "운전자 성별",
            FieldKind.SINGLE,
            "driver_sex",
            choices("남성", "여성", "불명"),
            required=True,
        ),
        FieldSpec(
            "driver_exp",
            "운전 경력",
            FieldKind.DIGITS,
            "driver_exp",
            required=True,
            max_length=2,
            placeholder="00",
            helper="0~99, 최대 2자리",
            unit="년",
        ),
        FieldSpec(
            "driver_age",
            "운전 연령",
            FieldKind.SINGLE,
            "driver_age",
            choices=DRIVER_AGE_CHOICES,
            required=True,
            placeholder="연령대 선택",
        ),
    ),
)

VEHICLE_MANUFACTURER_CHOICES = choices(
    "kia",
    "hyundai(Genesis)",
    "KGM",
    "GM",
    "renault",
    "bmw",
    "mercedes-benz",
    "tesla",
    "volkswagen",
    "audi",
    "volvo",
    "toyota(Lexus)",
    "jaguar land rover",
    "polestar",
    "porsche",
    "stellantis(peugeot, Fiat, jeep, chrysler)",
    "honda",
    "byd",
    "ford(Lincoln)",
    "nissan(infiniti)",
    "bentley",
    "lamborghini",
    "ferrari",
    "maserati",
    "aston martin",
    "mclaren",
    "lotus",
)


VEHICLE = SectionSpec(
    "vehicle",
    "차량 요인",
    "차량",
    (
        FieldSpec(
            "vehicle_manufacturer",
            "제작사",
            FieldKind.SINGLE,
            "vehicle_manufacturer",
            VEHICLE_MANUFACTURER_CHOICES,
            required=True,
            editable=True,
            placeholder="선택 또는 직접 입력",
        ),
        FieldSpec(
            "vehicle_fuel",
            "사고 차량 사용연료",
            FieldKind.SINGLE,
            "vehicle_model_fuel",
            choices("휘발유", "경유", "전기", "하이브리드", "수소", "LPG", "CNG"),
            required=True,
        ),
        FieldSpec(
            "vehicle_type",
            "사고 차량 차종",
            FieldKind.SINGLE,
            "vehicle_type",
            choices("승용", "승합", "화물", "특수", "기타"),
            required=True,
        ),
        FieldSpec(
            "vehicle_model_name",
            "사고 차량 모델명",
            FieldKind.TEXT,
            "vehicle_model_name",
            required=True,
            max_length=80,
            placeholder="예: 쏘나타",
        ),
        FieldSpec(
            "vehicle_model_year",
            "사고 차량 모델연식",
            FieldKind.DIGITS,
            "vehicle_model_year",
            required=True,
            exact_length=4,
            max_length=4,
            placeholder="2023",
        ),
        FieldSpec(
            "vehicle_mileage",
            "사고 당시 주행거리",
            FieldKind.DIGITS,
            "vehicle_milege",
            required=True,
            placeholder="12345",
            helper="숫자 입력",
            unit="km",
        ),
    ),
)

TRIGGER = SectionSpec(
    "trigger",
    "트리거 시점 사고 정보",
    "트리거",
    (
        FieldSpec(
            "trigger_vehicle_speed_source",
            "차량 속도 출처",
            FieldKind.SINGLE,
            "trigger_vehicle_speed_source",
            choices("EDR", "보고서", "사고영상"),
            required=True,
        ),
        FieldSpec(
            "trigger_vehicle_speed",
            "차량 속도 (추정)",
            FieldKind.SINGLE,
            "trigger_vehicle_speed",
            TRIGGER_VEHICLE_SPEED_CHOICES,
            required=True,
        ),
        FieldSpec(
            "trigger_vehicle_driving_state",
            "차량 주행 상태",
            FieldKind.SINGLE,
            "trigger_vehicle_driving_state",
            choices("정차", "크립", "주행"),
            required=True,
        ),
        FieldSpec(
            "trigger_gear_state",
            "차량 기어 상태",
            FieldKind.SINGLE,
            "trigger_gear_state",
            choices("전진", "후진"),
            required=True,
        ),
        FieldSpec(
            "trigger_steer_state",
            "차량 조향 상태",
            FieldKind.SINGLE,
            "trigger_steer_state",
            choices(
                "직진", "곡선(차로유지)", "좌회전", "우회전", "유턴", "차선변경(좌)", "차선변경(우)"
            ),
            required=True,
        ),
        FieldSpec(
            "collision_dist_source",
            "충돌 대상까지 거리 출처",
            FieldKind.SINGLE,
            "collision_dist_source",
            choices("EDR", "보고서", "사고영상"),
            required=True,
        ),
        FieldSpec(
            "collision_dist_value",
            "충돌 대상까지 거리 수치",
            FieldKind.DECIMAL,
            "collision_dist_value",
            required=True,
            placeholder="10",
            unit="m",
        ),
        FieldSpec(
            "collision_dist_range",
            "충돌 대상까지 거리 범위",
            FieldKind.SINGLE,
            "collision_dist_range",
            choices("10m 이내", "10m~50m", "50m 이상"),
            required=True,
        ),
        FieldSpec(
            "obstacle_dist_source",
            "최초장애물까지 거리 출처",
            FieldKind.SINGLE,
            "obstacle_dist_source",
            choices("EDR", "보고서", "사고영상"),
            required=True,
            group="obstacle_distance",
        ),
        FieldSpec(
            "obstacle_dist_value",
            "최초장애물까지 거리 수치",
            FieldKind.DECIMAL,
            "obstacle_dist_value",
            placeholder="1.5",
            helper="확인 또는 계산 가능한 경우 입력",
            unit="m",
            group="obstacle_distance",
        ),
        FieldSpec(
            "obstacle_dist_range",
            "최초장애물까지 거리 범위",
            FieldKind.SINGLE,
            "obstacle_dist_range",
            choices("1.5m 이내", "1.5m~10m", "10m 이상"),
            required=True,
            group="obstacle_distance",
        ),
    ),
)

TRIGGER_OBJECTS = SectionSpec(
    "trigger_objects",
    "트리거 시점 관여 객체",
    "객체",
    (
        FieldSpec(
            "obstacle_exists",
            "장애물 유무",
            FieldKind.SINGLE,
            "obstacle_Existence",
            choices("유", "무"),
            required=True,
        ),
        FieldSpec(
            "obstacle_vehicle_types",
            "장애물(차량) · 차량 종류",
            FieldKind.MULTI,
            "obstacle_vehicle",
            choices("승용", "승합", "화물", "특수", "이륜", "자전거", "기타"),
            group="obstacle_detail",
        ),
        FieldSpec(
            "obstacle_vehicle_state",
            "장애물(차량)의 대면 상태",
            FieldKind.SINGLE,
            "obstacle_vehicle_state",
            choices("후면", "정면", "사선"),
            group="obstacle_detail",
        ),
        FieldSpec(
            "obstacle_pedestrians",
            "장애물(보행자)",
            FieldKind.MULTI,
            choices=(
                Choice("어린이", "어린이", "obstacle_child"),
                Choice("어른", "어른", "obstacle_adult"),
            ),
            group="obstacle_detail",
        ),
        FieldSpec(
            "obstacle_road_facilities",
            "장애물(도로시설물)",
            FieldKind.MULTI,
            choices=(
                Choice("차단기", "차단기", "obstacle_barrier"),
                Choice("가드레일", "가드레일", "obstacle_guardrail"),
                Choice("중앙분리대", "중앙분리대", "obstacle_median"),
                Choice("연석", "연석", "obstacle_curb"),
                Choice("표지판", "표지판", "obstacle_traffic_sign"),
                Choice("전봇대·전신주·가로등", "전봇대·전신주·가로등", "obstacle_pole_streetlight"),
                Choice("볼라드·시선유도봉", "볼라드·시선유도봉", "obstacle_bollard_delineator"),
                Choice("충격흡수시설", "충격흡수시설", "obstacle_crash_cushion"),
                Choice("과속방지턱", "과속방지턱", "obstacle_speed_bump"),
            ),
            group="obstacle_detail",
        ),
        FieldSpec(
            "obstacle_building_landscape",
            "장애물(건물/조경)",
            FieldKind.MULTI,
            choices=(
                Choice("건물 기둥", "건물 기둥", "obstacle_building_pillar"),
                Choice("건물 벽", "건물 벽", "obstacle_building_wall"),
                Choice("담벼락", "담벼락", "obstacle_wall"),
                Choice(
                    "화단·흙더미·가로수(수목)",
                    "화단·흙더미·가로수(수목)",
                    "obstacle_flowerbed_dirtpile_streettree",
                ),
            ),
            group="obstacle_detail",
        ),
        FieldSpec(
            "obstacle_other",
            "장애물(기타)",
            FieldKind.MULTI,
            choices=(
                Choice("동물", "동물", "obstacle_animal"),
                Choice("도로낙하물", "도로낙하물", "obstacle_road_debris"),
            ),
            group="obstacle_detail",
        ),
        FieldSpec(
            "obstacle_state",
            "장애물 거동 상태",
            FieldKind.SINGLE,
            "obstacle_state",
            choices("동적", "정적"),
            group="obstacle_detail",
        ),
    ),
)

COLLISION_DIRECTIONS = (
    Choice("전면", "전면", "incident_collision_direction_front"),
    Choice("전우측", "전우측", "incident_collision_direction_front-right"),
    Choice("우측", "우측", "incident_collision_direction_right"),
    Choice("후우측", "후우측", "incident_collision_direction_rear_right"),
    Choice("후면", "후면", "incident_collision_direction_rear"),
    Choice("후좌측", "후좌측", "incident_collision_direction_rear_left"),
    Choice("좌측", "좌측", "incident_collision_direction_left"),
    Choice("전좌측", "전좌측", "incident_collision_direction_front_left"),
)

ACCIDENT = SectionSpec(
    "accident",
    "사고정보",
    "사고",
    (
        FieldSpec(
            "collision_directions",
            "사고 차량 충돌 방향",
            FieldKind.MULTI,
            choices=COLLISION_DIRECTIONS,
            required=True,
            helper="다중 충돌·연속 충돌 시 여러 방향을 선택할 수 있습니다.",
        ),
    ),
)

SITE_CHOICES = (
    Choice("고속도로(3차선 이상)", "고속도로(3차선 이상)", "site_highway_3lane_over"),
    Choice("일반도로(3차선 이상)", "일반도로(3차선 이상)", "site_general_road_3lane_over"),
    Choice("고속도로(3차선 미만)", "고속도로(3차선 미만)", "site_highway_under_3lane"),
    Choice("일반도로(3차선 미만)", "일반도로(3차선 미만)", "site_general_road_under_3lane"),
    Choice("골목길(3차선 미만)", "골목길(3차선 미만)", "site_alley"),
    Choice("교차로", "교차로", "site_intersection"),
    Choice("횡단보도", "횡단보도", "site_crosswalk"),
    Choice("연결로/램프", "연결로/램프", "site_link_ramp"),
    Choice("주차장", "주차장", "site_parking_lot"),
    Choice("터널", "터널", "site_tunnel"),
)

ENVIRONMENT = SectionSpec(
    "environment",
    "환경적 조건",
    "환경",
    (
        FieldSpec(
            "site_types",
            "사고장소",
            FieldKind.MULTI,
            choices=SITE_CHOICES,
            required=True,
            helper="교차로+횡단보도처럼 복수 특성을 선택할 수 있습니다.",
        ),
        FieldSpec(
            "site_special_areas",
            "특수장소",
            FieldKind.MULTI,
            "site_special_area",
            choices(
                "고속도로 요금소",
                "주차정산소",
                "DT(드라이브스루)",
                "세차장",
                "주유소",
                "진출입 차단기",
            ),
        ),
        FieldSpec(
            "site_manual_input",
            "기타장소",
            FieldKind.TEXT,
            "site_manual_input",
            max_length=120,
            placeholder="기타 장소 특성 입력",
        ),
        FieldSpec(
            "weather",
            "기상상황",
            FieldKind.SINGLE,
            "weather",
            choices("맑음", "강수", "강설", "안개"),
            required=True,
        ),
        FieldSpec(
            "environmental_conditions",
            "기타 환경조건",
            FieldKind.MULTI,
            "Lighting/Environmental_Condition",
            choices("역광", "젖은 노면", "언 노면"),
        ),
        FieldSpec(
            "incident_time",
            "사고시간",
            FieldKind.DIGITS,
            "incident_time",
            required=True,
            exact_length=4,
            max_length=4,
            placeholder="HHMM",
            helper="24시간제 4자리 · 예: 0930",
        ),
        FieldSpec(
            "incident_day",
            "사고요일",
            FieldKind.SINGLE,
            "incident_day",
            choices("월", "화", "수", "목", "금", "토", "일"),
        ),
        FieldSpec(
            "incident_date",
            "사고일자",
            FieldKind.DIGITS,
            "incident_date",
            required=True,
            exact_length=8,
            max_length=8,
            placeholder="YYYYMMDD",
        ),
    ),
)

COLLISION_TARGETS: tuple[Choice, ...] = (
    *(
        Choice(f"차량 · {label}", f"vehicle.{value}")
        for label, value in (
            ("승용", "passenger"),
            ("승합", "van"),
            ("화물", "truck"),
            ("특수", "special"),
            ("이륜", "motorcycle"),
            ("자전거", "bicycle"),
            ("PM", "pm"),
        )
    ),
    Choice("보행자 · 어린이", "pedestrian.child"),
    Choice("보행자 · 어른", "pedestrian.adult"),
    *(
        Choice(f"도로시설물 · {label}", f"facility.{value}")
        for label, value in (
            ("차단기", "barrier"),
            ("가드레일", "guardrail"),
            ("중앙분리대", "median"),
            ("연석", "curb"),
            ("표지판", "traffic_sign"),
            ("전봇대·전신주·가로등", "pole_streetlight"),
            ("볼라드·시선유도봉", "bollard_delineator"),
            ("충격흡수시설", "crash_cushion"),
            ("과속방지턱", "speed_bump"),
        )
    ),
    *(
        Choice(f"건물/조경 · {label}", f"building.{value}")
        for label, value in (
            ("건물 기둥", "pillar"),
            ("건물 벽", "wall"),
            ("담벼락", "boundary_wall"),
            ("화단", "flowerbed"),
            ("흙더미", "dirtpile"),
            ("가로수(수목)", "streettree"),
        )
    ),
    Choice("기타 · 동물", "other.animal"),
    Choice("기타 · 도로낙하물", "other.road_debris"),
)

COLLISION_OBJECTS = SectionSpec(
    "collision_objects",
    "충돌 관여 객체",
    "충돌",
    (
        FieldSpec(
            "collision_targets",
            "충돌 대상",
            FieldKind.MULTI,
            "collision_target",
            COLLISION_TARGETS,
            required=True,
            helper="복수 충돌 대상을 모두 선택합니다.",
        ),
        FieldSpec(
            "incident_minor_injury_grades",
            "경상부상급수",
            FieldKind.TEXT,
            "incident_minor_injury_grade",
            placeholder="예: 12, 12, 14",
            group="damage_counts",
        ),
        FieldSpec(
            "incident_minor_representative_injury_grade",
            "경상대표부상급수",
            FieldKind.DIGITS,
            "incident_minor_representative_injury_grade",
            max_length=2,
            group="damage_counts",
        ),
        FieldSpec(
            "incident_minor_injury",
            "경상자 수",
            FieldKind.DIGITS,
            "incident_minor_injury",
            max_length=4,
            unit="명",
            group="damage_counts",
        ),
        FieldSpec(
            "incident_severe_injury_grades",
            "중상부상급수",
            FieldKind.TEXT,
            "incident_severe_injury_grade",
            placeholder="예: 1, 7, 11",
            group="damage_counts",
        ),
        FieldSpec(
            "incident_severe_representative_injury_grade",
            "중상대표부상급수",
            FieldKind.DIGITS,
            "incident_severe_representative_injury_grade",
            max_length=2,
            group="damage_counts",
        ),
        FieldSpec(
            "incident_severe_injury",
            "중상자 수",
            FieldKind.DIGITS,
            "incident_severe_injury",
            max_length=4,
            unit="명",
            group="damage_counts",
        ),
        FieldSpec(
            "incident_fatality",
            "사망자 수",
            FieldKind.DIGITS,
            "incident_fatality",
            max_length=4,
            unit="명",
            group="damage_counts",
        ),
        FieldSpec(
            "incident_property_damage",
            "물적피해 건수",
            FieldKind.DIGITS,
            "incident_property_damage",
            max_length=4,
            unit="건",
            group="damage_counts",
        ),
        FieldSpec(
            "incident_damage_unknown",
            "피해정도 확인불가",
            FieldKind.SINGLE,
            "incident_damage_Unknown",
            choices("확인 가능", "확인불가"),
            required=True,
        ),
    ),
)

AVAILABLE_DATA_CHOICES = (
    Choice("EDR", "EDR", "data_list_EDR"),
    Choice("자차전방블랙박스", "자차전방블랙박스", "data_list_Front_cam"),
    Choice("자차후방블랙박스", "자차후방블랙박스", "data_list_Rear_cam"),
    Choice("타차전방블랙박스", "타차전방블랙박스", "data_list_Other_Front_cam"),
    Choice("타차후방블랙박스", "타차후방블랙박스", "data_list_Other_Rear_cam"),
    Choice("페달캠", "페달캠", "data_list_Pedal_cam"),
    Choice("CCTV", "CCTV", "data_list_CCTV"),
    Choice("보고서(감정서)", "보고서(감정서)", "data_list_Report"),
)

EVIDENCE = SectionSpec(
    "evidence",
    "접근 가능 데이터 (판정 근거)",
    "근거",
    (
        FieldSpec(
            "available_data",
            "접근 가능 데이터",
            FieldKind.MULTI,
            choices=AVAILABLE_DATA_CHOICES,
            required=True,
            helper="확보된 판정 근거를 모두 선택합니다.",
        ),
    ),
)

NOTES = SectionSpec(
    "notes",
    "비고",
    "비고",
    (
        FieldSpec(
            "remarks",
            "사고 추가정보",
            FieldKind.LONG_TEXT,
            "remarks",
            placeholder="사고에 대한 추가정보를 입력하세요.",
        ),
    ),
)

SECTIONS = (
    DRIVER,
    VEHICLE,
    TRIGGER,
    TRIGGER_OBJECTS,
    ACCIDENT,
    ENVIRONMENT,
    COLLISION_OBJECTS,
    EVIDENCE,
    NOTES,
)
ALL_FIELDS = IDENTITY_FIELDS + tuple(field for section in SECTIONS for field in section.fields)
FIELD_BY_KEY = {field.key: field for field in ALL_FIELDS}


def is_blank(value: Any) -> bool:
    return value is None or value == "" or value == []


def _obstacle_object_selected(values: dict[str, Any]) -> bool:
    keys = (
        "obstacle_vehicle_types",
        "obstacle_pedestrians",
        "obstacle_road_facilities",
        "obstacle_building_landscape",
        "obstacle_other",
    )
    return any(bool(values.get(key)) for key in keys)


def missing_questions(
    values: dict[str, Any], fields: tuple[FieldSpec, ...] = ALL_FIELDS
) -> list[str]:
    """Return logical required questions that have not been answered."""

    ignored_when_no_obstacle = {
        "obstacle_dist_source",
        "obstacle_dist_value",
        "obstacle_dist_range",
    }
    missing = [
        field.key
        for field in fields
        if field.required
        and field.key != "site_types"
        and not (values.get("obstacle_exists") == "무" and field.key in ignored_when_no_obstacle)
        and is_blank(values.get(field.key))
    ]

    if not any(
        not is_blank(values.get(key))
        for key in ("site_types", "site_special_areas", "site_manual_input")
    ):
        missing.append("site_location")

    if values.get("obstacle_exists") == "유":
        if not _obstacle_object_selected(values):
            missing.append("obstacle_object_types")
        if is_blank(values.get("obstacle_state")):
            missing.append("obstacle_state")
        if values.get("obstacle_vehicle_types") and is_blank(values.get("obstacle_vehicle_state")):
            missing.append("obstacle_vehicle_state")

    damage_answer = values.get("incident_damage_unknown")
    if damage_answer == "확인 가능":
        derived_counts: dict[str, str] = {}
        for grade_key, (minimum, maximum, count_key, _) in INJURY_GRADE_RULES.items():
            injury_summary = injury_grade_summary(
                values.get(grade_key), minimum=minimum, maximum=maximum
            )
            if injury_summary and injury_summary[0]:
                derived_counts[count_key] = injury_summary[1]
        for key in (
            "incident_minor_injury",
            "incident_severe_injury",
            "incident_fatality",
            "incident_property_damage",
        ):
            if is_blank(values.get(key)) and is_blank(derived_counts.get(key)):
                missing.append(key)
    return list(dict.fromkeys(missing))


def validation_errors(
    values: dict[str, Any], fields: tuple[FieldSpec, ...] = ALL_FIELDS
) -> dict[str, str]:
    """Validate values independently of Qt widgets."""

    errors: dict[str, str] = {}
    institution = str(values.get("institution", "")).strip()
    if institution and re.search(r"\s", institution):
        errors["institution"] = INVALID_FORMAT_MESSAGE

    for field in fields:
        value = values.get(field.key)
        if is_blank(value):
            continue
        if field.kind is FieldKind.DIGITS:
            text = str(value)
            if not text.isdigit():
                errors[field.key] = INVALID_FORMAT_MESSAGE
                continue
            if field.max_length and len(text) > field.max_length:
                errors[field.key] = INVALID_FORMAT_MESSAGE
            if field.exact_length and len(text) != field.exact_length:
                errors[field.key] = INVALID_FORMAT_MESSAGE
        elif field.kind is FieldKind.DECIMAL:
            try:
                number = float(str(value))
                if not math.isfinite(number) or number < 0:
                    raise ValueError
            except ValueError:
                errors[field.key] = INVALID_FORMAT_MESSAGE
        elif field.kind is FieldKind.SINGLE and field.choices and not field.editable:
            allowed = {choice.value for choice in field.choices}
            if not isinstance(value, str) or value not in allowed:
                errors[field.key] = INVALID_FORMAT_MESSAGE
        elif field.kind is FieldKind.MULTI:
            if not isinstance(value, list):
                errors[field.key] = INVALID_FORMAT_MESSAGE
            else:
                allowed = {choice.value for choice in field.choices}
                if any(not isinstance(item, str) or item not in allowed for item in value):
                    errors[field.key] = INVALID_FORMAT_MESSAGE
        elif field.kind in {FieldKind.TEXT, FieldKind.LONG_TEXT} and not isinstance(value, str):
            errors[field.key] = INVALID_FORMAT_MESSAGE

    time_value = str(values.get("incident_time", ""))
    if (
        time_value
        and len(time_value) == 4
        and time_value.isdigit()
        and (int(time_value[:2]) > 23 or int(time_value[2:]) > 59)
    ):
        errors["incident_time"] = INVALID_FORMAT_MESSAGE

    date_value = str(values.get("incident_date", ""))
    parsed_date = None
    if date_value and len(date_value) == 8 and date_value.isdigit():
        try:
            parsed_date = datetime.strptime(date_value, "%Y%m%d")
        except ValueError:
            errors["incident_date"] = INVALID_FORMAT_MESSAGE
    if parsed_date and values.get("incident_day"):
        korean_days = ("월", "화", "수", "목", "금", "토", "일")
        if values["incident_day"] != korean_days[parsed_date.weekday()]:
            errors["incident_day"] = INVALID_FORMAT_MESSAGE

    for grade_key, (minimum, maximum, count_key, representative_key) in INJURY_GRADE_RULES.items():
        injury_grade_value = values.get(grade_key)
        injury_summary = injury_grade_summary(injury_grade_value, minimum=minimum, maximum=maximum)
        if not is_blank(injury_grade_value) and injury_summary is None:
            errors[grade_key] = INVALID_FORMAT_MESSAGE
        elif injury_summary and injury_summary[0]:
            for key, expected in (
                (count_key, injury_summary[1]),
                (representative_key, injury_summary[2]),
            ):
                actual = values.get(key)
                if not is_blank(actual) and str(actual) != expected:
                    errors[key] = INVALID_FORMAT_MESSAGE

    def expected_distance_range(raw_value: Any, *, collision: bool) -> str | None:
        if is_blank(raw_value):
            return None
        try:
            number = float(str(raw_value))
        except ValueError:
            return None
        if not math.isfinite(number) or number < 0:
            return None
        if collision:
            return "10m 이내" if number <= 10 else ("10m~50m" if number < 50 else "50m 이상")
        return "1.5m 이내" if number <= 1.5 else ("1.5m~10m" if number < 10 else "10m 이상")

    expected_collision_range = expected_distance_range(
        values.get("collision_dist_value"), collision=True
    )
    if expected_collision_range and values.get("collision_dist_range") not in (
        None,
        "",
        expected_collision_range,
    ):
        errors["collision_dist_range"] = INVALID_FORMAT_MESSAGE
    expected_obstacle_range = expected_distance_range(
        values.get("obstacle_dist_value"), collision=False
    )
    if expected_obstacle_range and values.get("obstacle_dist_range") not in (
        None,
        "",
        expected_obstacle_range,
    ):
        errors["obstacle_dist_range"] = INVALID_FORMAT_MESSAGE

    trigger = values.get("trigger_frame")
    collision = values.get("collision_frame")
    if collision is None:
        legacy_collisions = values.get("collision_frames", [])
        collision = min(map(int, legacy_collisions)) if legacy_collisions else None
    if trigger is not None and collision is not None and int(trigger) > int(collision):
        errors["trigger_frame"] = INVALID_FORMAT_MESSAGE
    return errors


def field_label(key: str) -> str:
    special = {
        "obstacle_object_types": "장애물 객체 종류",
        "site_location": "사고장소",
        "trigger_frame": "타임라인 트리거",
    }
    if key in special:
        return special[key]
    field = FIELD_BY_KEY.get(key)
    return field.label if field else key


def csv_headers() -> list[str]:
    """Return stable CSV columns in UI/schema order."""

    headers: list[str] = []
    for field in ALL_FIELDS:
        if field.csv_column and field.csv_column not in headers:
            headers.append(field.csv_column)
        for choice in field.choices:
            if choice.csv_column and choice.csv_column not in headers:
                headers.append(choice.csv_column)
    return headers


def values_to_csv(values: dict[str, Any]) -> dict[str, str]:
    """Flatten one record according to the exact requested CSV contract."""

    effective_values = dict(values)
    for grade_key, (minimum, maximum, count_key, representative_key) in INJURY_GRADE_RULES.items():
        injury_summary = injury_grade_summary(
            effective_values.get(grade_key), minimum=minimum, maximum=maximum
        )
        if injury_summary and injury_summary[0]:
            effective_values.update(
                {
                    grade_key: injury_summary[0],
                    count_key: injury_summary[1],
                    representative_key: injury_summary[2],
                }
            )
    row = {header: "" for header in csv_headers()}
    for field in ALL_FIELDS:
        value = effective_values.get(field.key)
        obstacle_answer = effective_values.get("obstacle_exists")
        if field.group == "obstacle_detail" and obstacle_answer == "무":
            value = None
        if field.group == "damage_counts" and values.get("incident_damage_unknown") == "확인불가":
            value = None
        if field.choices and any(choice.csv_column for choice in field.choices):
            selected = set(
                value if isinstance(value, list) else ([] if is_blank(value) else [value])
            )
            for choice in field.choices:
                if choice.csv_column:
                    row[choice.csv_column] = "1" if choice.value in selected else ""
            continue
        if not field.csv_column or is_blank(value):
            continue
        if field.key == "incident_damage_unknown":
            row[field.csv_column] = "1" if value == "확인불가" else "0"
        elif isinstance(value, list):
            label_by_value = {choice.value: choice.label for choice in field.choices}
            labels = [label_by_value.get(item, str(item)) for item in value]
            if field.key == "collision_targets":
                labels = [label.split(" · ", maxsplit=1)[-1] for label in labels]
            row[field.csv_column] = "|".join(labels)
        else:
            row[field.csv_column] = str(value)
    return row


__all__ = [
    "ALL_FIELDS",
    "DRIVER_AGE_CHOICES",
    "EVIDENCE",
    "FIELD_BY_KEY",
    "FieldKind",
    "FieldSpec",
    "IDENTITY_FIELDS",
    "INJURY_GRADE_RULES",
    "INVALID_FORMAT_MESSAGE",
    "MISSING_VALUE_MESSAGE",
    "NOTES",
    "SECTIONS",
    "SectionSpec",
    "VEHICLE_MANUFACTURER_CHOICES",
    "csv_headers",
    "field_label",
    "incident_day_for_date",
    "injury_grade_summary",
    "missing_questions",
    "normalize_driver_age",
    "validation_errors",
    "values_to_csv",
]
