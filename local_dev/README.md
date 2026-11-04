# 개발 코드와 로컬 자료

테스트, 도구, 예제 설정, 시안, 패치, 개발 문서는 Git에 포함합니다. 샘플 데이터, 백업, 참고 원본, 생성된 보고서는 루트 `.gitignore`로 제외합니다. 앱 코드와 필수 번들 자산은 기존 위치에 유지합니다.

테스트 설정인 `pytest.ini`와 개발 의존성도 공유합니다. `.git/info/exclude`에 `/local_dev/`, `/avat_ai/`, `/tests/` 같은 폴더 전체 제외 규칙을 추가하면 코드가 누락되므로 사용하지 않습니다. 개인별 추가 제외 규칙은 코드가 아닌 로컬 자료에만 적용하세요.

프로젝트 루트에서 실행:

```powershell
python -m pip install -r local_dev/requirements-dev.txt
python -m pytest -c local_dev/pytest.ini local_dev/tests -q
python -m ruff check investigator main.py
python -m local_dev.tests.benchmark_storage
python local_dev/tools/benchmark_seek.py --video "D:\video.mp4" --output local_dev/reports/seek.json
```

`샘플 통계 열기.cmd`는 프로젝트 루트의 `.venv`를 사용합니다. 기본 CSV와 작업 DB는 `samples/`에 있으며 Git에는 포함하지 않습니다. 새로 복제한 환경에서 샘플 도구를 실행하려면 별도로 승인된 샘플 데이터를 준비해야 합니다. 로컬 자료는 별도 백업이 필요합니다.

```text
local_dev/
├── tests/                # 테스트와 저장 성능 측정
├── tools/                # 샘플 생성·탐색 성능 측정 도구
├── samples/              # 샘플 CSV 및 샘플 작업 DB
├── mockups/              # HTML 시안과 관련 JS
├── reports/              # 측정 결과 JSON·비교 HTML
├── patches/              # 실험·구버전용 패치
├── docs/                 # 개발 분석·스키마 문서
├── references/           # 참고용 명세서 원본
├── backups/              # 개발 샘플 백업
├── pytest.ini            # 공유 테스트 설정
├── requirements-dev.txt
└── 샘플 통계 열기.cmd
```

상세 CSV 개발 문서는 `docs/CSV_SCHEMA.md`에 보관합니다. `936c18b`(2026-09-15, UI Update)용 최소 백포트 패치는 `patches/avat-936c18b-single-thread.patch`에 있습니다. 해당 버전에는 Qt 오디오가 없고, 패치는 기본 `capture_factory`만 바꾸며 사용자 지정 팩토리와 UI는 유지합니다.
