# -*- mode: python ; coding: utf-8 -*-
# AVAT Windows 배포본(PyInstaller onedir) 빌드 설정.
#
# 프로젝트 루트에서: pyinstaller packaging/avat.spec --noconfirm
# 결과: dist/AVAT/AVAT.exe + dist/AVAT/_internal/ (Python·Qt·OpenCV·PyTorch·Transformers 포함)
#
# - 한 파일(onefile)이 아니라 폴더(onedir)로 묶는다. 실행할 때마다 수 GB를 임시 폴더에
#   풀지 않고, Qt(LGPL) DLL을 교체할 수 있는 형태를 유지한다.
# - 모델 가중치는 넣지 않는다. AVAT.exe 옆 runtime/models/grounding-dino-base/에 둔다.
# - 디코더·검출 worker는 같은 AVAT.exe를 --avat-worker 인자로 다시 실행한다
#   (investigator/runtime_paths.py).

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent

# transformers는 모델·토크나이저 모듈을 이름으로 지연 import하므로 정적 분석에 보이지 않는다.
hiddenimports = [
    "investigator.video_decode_worker",
    "investigator.detection_worker",
    "investigator.detection_worker.__main__",
    "investigator.detection_worker.grounding_dino_backend",
    "torchvision",
]
for package in (
    "transformers.models.auto",
    "transformers.models.grounding_dino",
    "transformers.models.bert",
):
    hiddenimports += collect_submodules(package)

datas = [
    (str(ROOT / "investigator" / "assets"), "investigator/assets"),
]
# transformers는 실행 중 importlib.metadata로 의존 패키지 버전을 확인한다.
for distribution in ("transformers", "torch", "torchvision", "pillow", "numpy", "opencv-python"):
    datas += copy_metadata(distribution, recursive=distribution == "transformers")

# 보존된 비교용 백엔드(YOLOE·EfficientSAM3)의 선택 의존성과 개발 도구는 넣지 않는다.
excludes = [
    "ultralytics",
    "clip",
    "mobileclip",
    "tkinter",
    "matplotlib",
    "IPython",
    "pytest",
    "pytestqt",
    "tensorboard",
    "notebook",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    # torch·transformers는 inspect로 소스 코드를 읽는 경로가 있어 .py도 함께 넣는다.
    module_collection_mode={
        "torch": "pyz+py",
        "torchvision": "pyz+py",
        "transformers": "pyz+py",
    },
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    # worker의 JSON 이벤트가 버퍼에 머물지 않도록 표준 입출력을 버퍼링하지 않는다.
    [("u", None, "OPTION")],
    exclude_binaries=True,
    name="AVAT",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AVAT",
)
