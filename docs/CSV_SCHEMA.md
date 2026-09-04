# CSV 스키마 메모

CSV는 조사 사고 1건당 1행입니다. 앞의 7개 워크플로 메타데이터 뒤에 요청된 조사 칼럼이 선언 순서대로 이어집니다.

## 워크플로 메타데이터

| 칼럼 | 내용 |
| --- | --- |
| `case_id` | `SAMSUNG-001` 등 `기관-번호` 형식의 표시 식별자 |
| `workflow_status` | `draft` 또는 `completed` |
| `video_files` | 연결 영상의 절대경로 (`|` 구분) |
| `active_video_file` | 현재 선택된 영상의 절대경로 |
| `trigger_frame` | 현재 선택 영상의 0-based 트리거 프레임 |
| `collision_frames` | 현재 선택 영상의 0-based 단일 충돌 프레임. 기존 호환성을 위해 복수형 칼럼명을 유지하며, 미지정 시 빈 값 |
| `video_markers` | 모든 영상의 단일 T/C 마커를 보존하는 JSON 객체. `collision_frames` 배열에는 최대 1개만 저장 |

## 다중 선택 표현

- 옵션별 CSV 칼럼이 있는 필드: 선택 `1`, 비선택 `0`, 질문 자체가 미입력이면 빈 값
- 단일 CSV 칼럼 필드: 화면의 고정 옵션 순서로 `|` 연결
- `collision_target`: 범주 접두어 없이 요청서 선택값을 `승용|어른`처럼 연결
- 장애물 `무`: 장애물 하위 boolean 칼럼은 모두 `0`
- 피해 `확인불가`: `incident_damage_Unknown=1`, 네 피해 수치 칼럼은 빈 값

CSV 칼럼 순서와 변환 구현은 `investigator.schema.csv_headers()` 및 `values_to_csv()`가 단일 기준입니다.
