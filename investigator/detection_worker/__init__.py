"""AVAT SAM 3 텍스트 프롬프트 객체 검출 worker (프로토타입).

PySide6를 불러오지 않는다. AVAT와 다른 가상환경(torch·transformers 설치)에서
``python -m investigator.detection_worker``로 실행한다.

GUI 프로세스와 분리된 별도 프로세스로 실행되며, 표준 출력으로 JSONL 진행
이벤트를 내보내고 성공했을 때만 결과 JSON 문서를 원자적으로 저장한다.
"""

WORKER_NAME = "sam3_prompt_detector"
WORKER_VERSION = "0.2.0"
RESULT_SCHEMA_VERSION = 2
