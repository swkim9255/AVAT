# Windows portable layout

```text
AVAT/
├── 01_사고동영상/
│   ├── 2019년/
│   ├── 2023년/
│   └── 작업관리/
│       └── 작업관리.db
├── 02_csv내보내기/
├── 03_Report내보내기/
└── systems/
    ├── AVAT.exe
    ├── detection-runtime.json
    ├── settings.ini
    ├── workspace.json
    ├── models/
    ├── logs/
    ├── cache/
    ├── backups/
    ├── 삭제사고/
    └── Python·Qt·추론 라이브러리 및 앱 리소스
```

상단은 프로젝트 탭이 아니라 `01_사고동영상` 바로 아래 폴더 탭입니다.
모든 탭은 `01_사고동영상/작업관리/작업관리.db` 하나를 공유합니다. DB 이름은 고정이며,
현행 형식 DB를 배치할 때는 앱을 종료한 상태에서 이 위치와 이름으로 직접 배치합니다.
앱은 이전 폴더의 DB를 자동 이동하거나 임의의 `.db` 파일을 선택하지 않습니다.
원본 영상도 자동 이동하지 않습니다.
`작업관리`는 영상 탭·영상 자동 검색·폴더 감시에서 제외합니다.
영상 상대경로는 여전히 작업 폴더 루트를 기준으로 저장합니다.

앱은 `schema_version: 4`, `path_storage: workspace-relative-v1`인 DB를 전제로
동작합니다. 구버전 DB 형식 변환·중복 사고 자동 정리·초기 `001` 사고의 자동
번호 수정과 영상 이관은 제공하지 않습니다. 별도 수동 도구
`recover_video_paths.py`·`recover_investigation_csv.py`는 앱에서 실행하지 않습니다.
일반 영상 경로 복구와 새 영상 검색·감시는 유지합니다. 수동 DB 백업 및 영상
삭제 전·영상 경로 변경 전 자동 백업도 유지하며, 구버전 변환용 `pre-relative`·
`pre-object` 백업만 제거합니다.

CSV·속도 CSV·명세서는 `02_csv내보내기`, Word 보고서는
`03_Report내보내기`를 저장 창의 기본 위치로 사용합니다. 다른 저장 위치를
사용자가 직접 선택하는 것은 계속 가능합니다.

빌드에는 Windows와 실행용 requirements 전체, PyInstaller 6 이상이 필요합니다.
로컬 Grounding DINO 모델을 `runtime/models/grounding-dino-base`에 준비한 뒤 실행합니다.
실제 사고 데이터가 들어 있는 배포 폴더에 직접 빌드하지 말고 새 출력 폴더를 사용하세요.

```powershell
python -m PyInstaller --distpath dist/windows-systems --workpath runtime/builds/windows-systems packaging/AVAT.spec
```

`systems/AVAT.exe`를 실행합니다. 작업 폴더의 기준은 `systems`의 상위 폴더입니다.
`contents_directory="."`로 EXE 옆에서 라이브러리를 읽도록 빌드하고, EXE와 지원
파일 전체를 `systems`에 함께 배치합니다. 기존 EXE만 수동으로 옮기는 방식은
지원하지 않으므로 새 설정으로 재빌드해야 합니다. 설정과 로그를 기록할 수 있도록
배포 폴더에 쓰기 권한이 필요합니다. 프로그램 업데이트 시 `systems`를 통째로
삭제하거나 덮어쓰지 마세요. 기존 설정·백업·작업 기록도 이 폴더에 들어 있습니다.
조사 DB는 `01_사고동영상/작업관리/작업관리.db`에 별도로 보존해야 합니다.

이 spec은 코드로 추가한 빌드 설정이며, 이 작업에서 기존 배포 EXE를 덮어쓰지
않았습니다. 완성 배포본은 대상 Windows PC에서 시작·영상 탐색·검출 worker·
CSV 및 보고서 내보내기를 별도로 확인해야 합니다.
