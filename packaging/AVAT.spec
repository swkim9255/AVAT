# Build from a Windows environment containing AVAT's complete requirements.
# EXE·라이브러리·모델은 systems 안에 함께 배치한다.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

root = Path(SPECPATH).parent
datas = collect_data_files("investigator")
datas.append((str(root / "packaging" / "detection-runtime.json"), "."))
binaries = []
legacy_modules = {"investigator.detection_worker.efficient_backend", "investigator.detection_worker.yoloe_backend"}
hiddenimports = collect_submodules("investigator", filter=lambda name: name not in legacy_modules)
hiddenimports += ["_socket", "_ssl", "_sqlite3", "_multiprocessing", "_overlapped"]

# Dynamic model imports are not all visible to static dependency analysis.
for package in ("torch", "torchvision", "transformers", "safetensors", "tokenizers", "huggingface_hub"):
    package_data, package_binaries, package_imports = collect_all(package)
    datas.extend(package_data)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_imports)

# torch의 C++ 헤더(torch/include, 약 9,300개)는 확장 모듈을 컴파일할 때만 쓰이고 실행에는
# 필요 없다. 배포본 파일 수의 절반이 넘고 경로가 길어 zip 압축·해제를 느리게 하므로 뺀다.
def _without_torch_headers(entries):
    return [entry for entry in entries if Path(entry[1]).parts[:2] != ("torch", "include")]


datas = _without_torch_headers(datas)
binaries = _without_torch_headers(binaries)

model = root / "runtime" / "models" / "grounding-dino-base"
if not model.is_dir():
    raise SystemExit("Missing local model: runtime/models/grounding-dino-base")
datas.append((str(model), "models/grounding-dino-base"))
datas.append((str(root / "THIRD_PARTY_NOTICES.md"), "licenses"))

a = Analysis([str(root / "main.py")], pathex=[str(root)], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports, hookspath=[],
             runtime_hooks=[], excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", *legacy_modules])
pyz = PYZ(a.pure)
# EXE 파일 아이콘. 창·작업표시줄 아이콘도 같은 파일을 쓴다(investigator/theme.py의 APP_ICON_PATH).
app_icon = root / "investigator" / "assets" / "app_icon" / "avat.ico"
if not app_icon.is_file():
    raise SystemExit("Missing app icon: investigator/assets/app_icon/avat.ico")
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AVAT",
          debug=False, strip=False, upx=False, console=False,
          icon=str(app_icon), contents_directory=".")
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="AVAT")

# COLLECT의 name은 마지막 경로명만 사용하므로 먼저 평면 배포본을 만든 뒤 함께 이동한다.
bundle_root = Path(DISTPATH).resolve() / "AVAT"
systems_root = bundle_root / "systems"
systems_root.mkdir(exist_ok=True)
for entry in list(bundle_root.iterdir()):
    if entry != systems_root:
        entry.rename(systems_root / entry.name)
for folder in ("01_사고동영상", "02_csv내보내기", "03_Report내보내기"):
    (bundle_root / folder).mkdir(exist_ok=True)
# 최종 사용자용 안내서를 배포본 최상위에 둔다(업데이트·백업·복원 절차 포함).
# 구형 Windows 메모장에서도 한글이 깨지지 않도록 UTF-8 BOM과 CRLF로 쓴다.
guide = root / "packaging" / "사용안내.txt"
if guide.is_file():
    (bundle_root / guide.name).write_text(
        guide.read_text(encoding="utf-8"), encoding="utf-8-sig", newline="\r\n",
    )
