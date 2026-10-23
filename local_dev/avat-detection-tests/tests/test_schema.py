from __future__ import annotations

from investigator.schema import (
    AVAILABLE_DATA_CHOICES,
    DRIVER_AGE_CHOICES,
    FIELD_BY_KEY,
    IDENTITY_FIELDS,
    INVALID_FORMAT_MESSAGE,
    SECTIONS,
    TRIGGER_VEHICLE_SPEED_CHOICES,
    VEHICLE_MANUFACTURER_CHOICES,
    FieldKind,
    csv_headers,
    injury_grade_summary,
    missing_questions,
    validation_errors,
    values_to_csv,
)
from tests.helpers import valid_values


def test_identity_is_pinned_and_notes_is_the_ninth_tab() -> None:
    assert [field.key for field in IDENTITY_FIELDS] == ["institution", "accident_classification"]
    assert [section.short_title for section in SECTIONS] == [
        "운전자",
        "차량",
        "트리거",
        "객체",
        "사고",
        "환경",
        "충돌",
        "근거",
        "비고",
    ]
    remarks = FIELD_BY_KEY["remarks"]
    assert remarks.kind is FieldKind.LONG_TEXT
    assert remarks.csv_column == "remarks"
    assert not remarks.required


def test_institution_example_uses_samsung_without_removing_other_institutions() -> None:
    institution = FIELD_BY_KEY["institution"]
    assert institution.helper == "공백 없이 입력 · 예: samsung"
    assert [choice.value for choice in institution.choices] == [
        "katri", "ajou", "nfs", "samsung",
    ]


def test_requested_csv_contract_preserves_special_names() -> None:
    headers = csv_headers()
    for name in (
        "Institution",
        "vehicle_milege",
        "obstacle_Existence",
        "incident_collision_direction_front-right",
        "Lighting/Environmental_Condition",
        "incident_damage_Unknown",
        "incident_minor_injury_grade",
        "incident_minor_representative_injury_grade",
        "incident_severe_injury_grade",
        "incident_severe_representative_injury_grade",
        "data_list_Report",
        "remarks",
    ):
        assert name in headers
    assert len(headers) == len(set(headers))
    assert headers.index("incident_day") + 1 == headers.index("incident_date")

    environment = next(section for section in SECTIONS if section.key == "environment")
    environment_keys = [field.key for field in environment.fields]
    assert environment_keys.index("incident_day") + 1 == environment_keys.index("incident_date")
    assert not FIELD_BY_KEY["incident_day"].required


def test_remarks_preserves_unlimited_multiline_text_in_csv() -> None:
    text = "사고 경위 첫 줄\n" + ("추가정보" * 20_000)
    assert values_to_csv({"remarks": text})["remarks"] == text


def test_injury_grades_are_sorted_and_derive_counts_and_representative() -> None:
    assert injury_grade_summary("14, 13, 12", minimum=12, maximum=14) == (
        "12, 13, 14",
        "3",
        "12",
    )
    assert injury_grade_summary("8, 1, 7, 11, 7", minimum=1, maximum=11) == (
        "1, 7, 7, 8, 11",
        "5",
        "1",
    )

    values = {
        "incident_damage_unknown": "확인 가능",
        "incident_minor_injury_grades": "14, 13, 12",
        "incident_severe_injury_grades": "11, 7, 1",
        "incident_fatality": "0",
        "incident_property_damage": "0",
    }
    assert "incident_minor_injury" not in missing_questions(values)
    assert "incident_severe_injury" not in missing_questions(values)
    row = values_to_csv(values)
    assert row["incident_minor_injury_grade"] == "12, 13, 14"
    assert row["incident_minor_injury"] == "3"
    assert row["incident_minor_representative_injury_grade"] == "12"
    assert row["incident_severe_injury_grade"] == "1, 7, 11"
    assert row["incident_severe_injury"] == "3"
    assert row["incident_severe_representative_injury_grade"] == "1"


def test_injury_grade_validation_rejects_out_of_range_and_mismatched_derived_values() -> None:
    for invalid in ("0", "11", "15", "12.5", "12,,14", "경상"):
        assert (
            validation_errors({"incident_minor_injury_grades": invalid})[
                "incident_minor_injury_grades"
            ]
            == INVALID_FORMAT_MESSAGE
        )
    assert (
        validation_errors({"incident_severe_injury_grades": "12"})["incident_severe_injury_grades"]
        == INVALID_FORMAT_MESSAGE
    )

    values = {
        "incident_minor_injury_grades": "12, 14",
        "incident_minor_injury": "3",
        "incident_minor_representative_injury_grade": "14",
        "incident_severe_injury_grades": "1, 11",
        "incident_severe_injury": "0",
        "incident_severe_representative_injury_grade": "11",
    }
    errors = validation_errors(values)
    assert set(errors) == {
        "incident_minor_injury",
        "incident_minor_representative_injury_grade",
        "incident_severe_injury",
        "incident_severe_representative_injury_grade",
    }


def test_accident_number_accepts_up_to_thirteen_digits() -> None:
    field = FIELD_BY_KEY["accident_classification"]
    assert field.max_length == 13

    values = valid_values()
    values["accident_classification"] = "1234567890123"
    assert "accident_classification" not in validation_errors(values)
    values["accident_classification"] = "12345678901234"
    assert validation_errors(values)["accident_classification"] == INVALID_FORMAT_MESSAGE


def test_vehicle_mileage_accepts_any_number_of_digits() -> None:
    field = FIELD_BY_KEY["vehicle_mileage"]
    assert field.max_length is None

    values = valid_values()
    values["vehicle_mileage"] = "123456789012345678901234567890"
    assert "vehicle_mileage" not in validation_errors(values)
    assert values_to_csv(values)["vehicle_milege"] == values["vehicle_mileage"]

    values["vehicle_mileage"] = "123km"
    assert validation_errors(values)["vehicle_mileage"] == INVALID_FORMAT_MESSAGE


def test_driver_age_uses_five_year_ranges_from_twenty_to_ninety_nine() -> None:
    expected = [f"{start}-{start + 4}세" for start in range(20, 100, 5)]
    field = FIELD_BY_KEY["driver_age"]
    assert [choice.label for choice in DRIVER_AGE_CHOICES] == expected
    assert [choice.value for choice in field.choices] == expected

    values = valid_values()
    values["driver_age"] = "45-49세"
    assert "driver_age" not in validation_errors(values)
    assert values_to_csv(values)["driver_age"] == "45-49세"
    values["driver_age"] = "45"
    assert validation_errors(values)["driver_age"] == INVALID_FORMAT_MESSAGE


def test_vehicle_manufacturer_label_choices_and_csv_values() -> None:
    expected = [
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
    ]
    field = FIELD_BY_KEY["vehicle_manufacturer"]
    assert field.label == "제작사"
    assert field.required and field.editable
    assert [choice.label for choice in VEHICLE_MANUFACTURER_CHOICES] == expected
    assert [choice.value for choice in VEHICLE_MANUFACTURER_CHOICES] == expected
    assert len(set(expected)) == 27
    for manufacturer in expected:
        assert (
            values_to_csv({"vehicle_manufacturer": manufacturer})["vehicle_manufacturer"]
            == manufacturer
        )


def test_valid_complete_record_has_no_missing_or_errors() -> None:
    values = valid_values()
    assert missing_questions(values) == []
    assert validation_errors(values) == {}


def test_no_obstacle_makes_first_obstacle_distance_not_applicable() -> None:
    values = valid_values()
    for key in ("obstacle_dist_source", "obstacle_dist_value", "obstacle_dist_range"):
        values.pop(key, None)
    assert missing_questions(values) == []
    assert validation_errors(values) == {}


def test_requested_speed_ranges_and_available_data_choices() -> None:
    assert [choice.label for choice in TRIGGER_VEHICLE_SPEED_CHOICES] == [
        "정차(0kph)",
        "크립(0-10kph)",
        "저속(10-30kph)",
        "중속(30-60kph)",
        "고속(60kph이상)",
    ]
    assert [(choice.label, choice.csv_column) for choice in AVAILABLE_DATA_CHOICES] == [
        ("EDR", "data_list_EDR"),
        ("자차전방블랙박스", "data_list_Front_cam"),
        ("자차후방블랙박스", "data_list_Rear_cam"),
        ("타차전방블랙박스", "data_list_Other_Front_cam"),
        ("타차후방블랙박스", "data_list_Other_Rear_cam"),
        ("페달캠", "data_list_Pedal_cam"),
        ("CCTV", "data_list_CCTV"),
        ("보고서(감정서)", "data_list_Report"),
    ]


def test_no_obstacle_preserves_optional_first_obstacle_distance_in_csv() -> None:
    values = valid_values()
    values["obstacle_dist_source"] = "사고영상"
    values["obstacle_dist_value"] = "8"
    values["obstacle_dist_range"] = "1.5m~10m"

    assert missing_questions(values) == []
    assert validation_errors(values) == {}
    row = values_to_csv(values)
    assert row["obstacle_dist_source"] == "사고영상"
    assert row["obstacle_dist_value"] == "8"
    assert row["obstacle_dist_range"] == "1.5m~10m"

    values["obstacle_dist_range"] = "10m 이상"
    assert validation_errors(values)["obstacle_dist_range"] == INVALID_FORMAT_MESSAGE


def test_conditional_missing_rules() -> None:
    values = valid_values()
    values["obstacle_exists"] = "유"
    assert "obstacle_object_types" in missing_questions(values)
    assert "obstacle_state" in missing_questions(values)
    values["obstacle_vehicle_types"] = ["승용"]
    values["obstacle_state"] = "정적"
    assert "obstacle_vehicle_state" in missing_questions(values)

    values = valid_values()
    values["incident_damage_unknown"] = "확인 가능"
    missing = missing_questions(values)
    assert "incident_minor_injury" in missing
    assert "incident_property_damage" in missing

    values = valid_values()
    values.pop("site_types")
    values["site_special_areas"] = ["주차정산소"]
    assert "site_location" not in missing_questions(values)


def test_date_time_and_marker_validation() -> None:
    values = valid_values()
    values["incident_time"] = "2460"
    values["incident_date"] = "20260230"
    values["trigger_frame"] = 20
    values["collision_frame"] = 10
    errors = validation_errors(values)
    assert set(("incident_time", "incident_date", "trigger_frame")) <= errors.keys()
    assert set(errors.values()) == {INVALID_FORMAT_MESSAGE}


def test_invalid_date_uses_unified_format_message() -> None:
    values = valid_values()
    values["incident_date"] = "20260230"
    assert validation_errors(values)["incident_date"] == "형식이 잘못되었습니다"


def test_distance_value_must_match_selected_range_and_be_finite() -> None:
    values = valid_values()
    values["collision_dist_value"] = "11"
    values["collision_dist_range"] = "10m 이내"
    assert "collision_dist_range" in validation_errors(values)
    values["collision_dist_value"] = "nan"
    assert "collision_dist_value" in validation_errors(values)


def test_multi_values_expand_to_boolean_columns() -> None:
    values = valid_values()
    values["collision_directions"] = ["전면", "좌측"]
    values["available_data"] = ["EDR", "CCTV"]
    row = values_to_csv(values)
    assert row["incident_collision_direction_front"] == "1"
    assert row["incident_collision_direction_left"] == "1"
    assert row["incident_collision_direction_rear"] == ""
    assert row["data_list_EDR"] == "1"
    assert row["data_list_CCTV"] == "1"
    assert row["data_list_Report"] == ""

    values["obstacle_exists"] = "무"
    values["obstacle_pedestrians"] = ["어린이"]
    values["incident_damage_unknown"] = "확인불가"
    values["incident_minor_injury"] = "99"
    row = values_to_csv(values)
    assert row["obstacle_child"] == ""
    assert row["obstacle_adult"] == ""
    assert row["incident_minor_injury"] == ""


def test_collision_target_csv_uses_requested_leaf_values() -> None:
    values = valid_values()
    values["collision_targets"] = ["vehicle.passenger", "pedestrian.adult"]
    assert values_to_csv(values)["collision_target"] == "승용|어른"
