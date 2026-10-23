from investigator.case_numbering import build_case_number_map, case_number_sort_key
from investigator.models import InvestigationCase


def case(number, uid, institution="samsung"):
    return InvestigationCase(uid=uid, values={"accident_classification": number, "institution": institution})


def test_numbers_are_numeric_chronological_and_not_input_or_display_order():
    cases = [case("2023010100001", "d"), case("2020011309999", "c"),
             case("2020011302572", "b"), case("2019010100001", "a")]
    before = [item.to_dict() for item in cases]
    numbers = build_case_number_map(cases)
    assert numbers == {"a": "20190101_001", "b": "20200113_001", "c": "20200113_002", "d": "20230101_001"}
    assert numbers == build_case_number_map(reversed(cases))
    assert [item.to_dict() for item in cases] == before


def test_invalid_ids_do_not_reserve_ordinals_and_ties_are_deterministic():
    cases = [case("bad", "a"), case("2020023000001", "b"), case("2020011300001", "c", "katri"),
             case("2020011300001", "d", "ajou"), case("", "e"), case("123", "f")]
    assert build_case_number_map(cases) == {"d": "20200113_001", "c": "20200113_002"}
    assert [item.uid for item in sorted(cases, key=case_number_sort_key)][:1] == ["f"]


def test_leap_dates_and_more_than_999_accidents():
    cases = [case(f"20200229{index:05d}", str(index)) for index in range(1001)]
    cases.append(case("2019022900001", "invalid"))
    mapping = build_case_number_map(reversed(cases))
    assert mapping["0"] == "20200229_001"
    assert mapping["1000"] == "20200229_1001"
    assert "invalid" not in mapping
