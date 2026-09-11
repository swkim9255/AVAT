# CSV 스키마 메모

CSV는 조사 사고 1건당 1행입니다. 앞의 7개 워크플로 메타데이터 뒤에 요청된 조사 칼럼이 선언 순서대로 이어집니다.

## 워크플로 메타데이터

| 칼럼 | 내용 |
| --- | --- |
| `case_id` | `SAMSUNG-001` 등 `기관-번호` 형식의 표시 식별자 |
| `workflow_status` | `draft` 또는 `completed` |
| `video_files` | 연결 영상의 절대경로 (`|` 구분), 미지정 시 `null` |
| `active_video_file` | 현재 선택된 영상의 절대경로, 미지정 시 `null` |
| `trigger_frame` | 현재 선택 영상의 0-based 트리거 프레임, 미지정 시 `null` |
| `collision_frames` | 현재 선택 영상의 0-based 단일 충돌 프레임. 기존 호환성을 위해 복수형 칼럼명을 유지하며, 미지정 시 `null` |
| `video_markers` | 모든 영상의 단일 T/C 마커를 보존하는 JSON 객체. `collision_frames` 배열에는 최대 1개만 저장하며, 마커가 없으면 `null` |

## 다중 선택 표현

- 옵션별 CSV 칼럼이 있는 필드: 선택된 값 `1`, 미선택·미입력·비적용 값은 `null`
- 단일 CSV 칼럼 필드: 화면의 고정 옵션 순서로 `|` 연결
- `collision_target`: 범주 접두어 없이 요청서 선택값을 `승용|어른`처럼 연결
- 장애물 `무`: 장애물 객체 하위 칼럼은 `null`. 최초장애물 거리 3개 칼럼은 선택 입력값을 보존
- 블랙박스 근거: 기존 `data_list_Front_cam`, `data_list_Rear_cam`은 각각 자차 전방·후방에 사용하고, 타차 전방·후방은 `data_list_Other_Front_cam`, `data_list_Other_Rear_cam`에 기록
- 피해 `확인불가`: `incident_damage_Unknown=1`, 네 피해 수치 칼럼은 `null`
- 비고 탭의 사고 추가정보: `remarks` 칼럼에 글자 수 제한 없이 기록
- 부상급수: 경상 12~14급은 `incident_minor_injury_grade`, 중상 1~11급은 `incident_severe_injury_grade`에 각각 오름차순으로 기록. 각 인원수를 자동 집계하고 최솟값은 `incident_minor_representative_injury_grade`, `incident_severe_representative_injury_grade`에 분리 기록

CSV 파일에 기록되기 직전 모든 빈 셀은 소문자 문자열 `null`로 변환됩니다. 명시적으로 입력되거나 단일 이진 판정에서 계산된 `0`은 `null`로 바꾸지 않습니다.

CSV 칼럼 순서와 변환 구현은 `investigator.schema.csv_headers()` 및 `values_to_csv()`가 단일 기준입니다.
