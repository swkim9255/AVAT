# Third-Party Notices

이 프로젝트는 아래의 제3자 소프트웨어를 포함하거나 사용합니다.

현재 실행·배포 구성의 객체 검출 모델은 **Grounding DINO**입니다. PyTorch·Torchvision·Transformers·Pillow와 그 전이 의존성은 `requirements.txt`를 통해 별도로 설치합니다. 실제 배포물에는 설치한 각 패키지와 모델의 원본 라이선스·고지 파일을 함께 확인하고 보존해야 합니다.

아래 YOLOE·EfficientSAM3 고지는 저장소에 보존된 기존 통합 코드에 대한 참고 기록입니다. 해당 패키지·가중치·연구 소스는 현재 Grounding DINO 실행·배포 구성에 포함하지 않으며, 코드나 기존 로컬 가중치를 이번 변경에서 삭제하지 않습니다.

## Material Symbols Outlined

- Copyright Google LLC
- Licensed under the Apache License 2.0
- Bundled files and the full license are in `investigator/assets/material_symbols/`.
- Source provenance is recorded in `investigator/assets/material_symbols/SOURCE.md`.

## Noto Sans KR

- Copyright 2014-2021 Adobe (http://www.adobe.com/), with Reserved Font Name "Source".
- Licensed under the SIL Open Font License 1.1.
- The bundled variable font and full license are in `investigator/assets/fonts/`.
- Source: Google Fonts `ofl/notosanskr` distribution.

## Runtime libraries

- PySide6 / Qt for Python: LGPLv3/GPLv3/commercial terms as distributed by The Qt Company.
- OpenCV: Apache License 2.0.
- NumPy: BSD 3-Clause License.

These packages are installed separately through Python package management and are not copied into this source tree.

## 기존 YOLOE 통합 고지 — 현재 배포 대상 아님

- Ultralytics package tested/pinned at `8.3.253`: https://github.com/ultralytics/ultralytics
- Upstream offers AGPL-3.0 and Enterprise licensing: https://www.ultralytics.com/license
  Review applicable terms before proprietary/internal EXE distribution.
- YOLOE checkpoint `yoloe-11l-seg.pt` and text encoder `mobileclip_blt.ts` are
  obtained separately from https://github.com/ultralytics/assets/releases/tag/v8.3.0
- CLIP tokenizer source: https://github.com/ultralytics/CLIP, tested commit
  `7ffa84b3bfa40c42ecc1c77147a855e69cb2dd40`. Preserve its upstream notices and
  those of the MobileCLIP encoder when preparing an offline installation bundle.
- Local weights, embedding caches and test environments are under ignored
  runtime directories and are not bundled into AVAT's source package.

## 현재 배포 구성: Grounding DINO

- 배포 모델: `IDEA-Research/grounding-dino-base`.
- `runtime/models/grounding-dino-base/`의 `config.json`, `model.safetensors`,
  `preprocessor_config.json`, `special_tokens_map.json`, `tokenizer.json`,
  `tokenizer_config.json`, `vocab.txt`를 별도로 준비합니다. 다운로드용 `.cache/`는
  실행에 필요하지 않으며, 가중치는 Git 저장소에 포함하지 않습니다.
- Ultralytics·CLIP·MobileCLIP 및 다른 모델의 연구 소스는 이 구성의 실행 의존성이 아닙니다.

- Transformers implementation: https://huggingface.co/docs/transformers/model_doc/grounding-dino
- Checkpoints: https://huggingface.co/IDEA-Research/grounding-dino-base and
  https://huggingface.co/IDEA-Research/grounding-dino-tiny (model cards declare Apache-2.0).
- Model/tokenizer/processor files are downloaded separately under ignored
  `runtime/models/`; they are not bundled into AVAT's source package.
- This integration uses bounding boxes only and does not produce segmentation masks.

## 기존 EfficientSAM3 통합 고지 — 현재 배포 대상 아님

- Research source: https://github.com/SimonZeng7108/efficientsam3
- Tested source commit: `bd0936c788fed8d51fa799437f05abd97b401b06`.
- The repository declares Apache 2.0 and references the respective licenses of
  SAM/SAM2/SAM3 and its student backbones. Its copied Meta source files retain
  their original notices. See upstream README and license files for full terms.
- Optional TV-M checkpoint: `Simon7108528/EfficientSAM3`,
  `efficientsam3_ft/efficientsam3_tinyvit.pt` on Hugging Face.
- The source and weights are downloaded separately under ignored
  `runtime/efficientsam3/` and `runtime/models/`;
  they are not bundled into AVAT's source package by this change.
