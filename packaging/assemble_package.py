"""PyInstaller 결과(dist/AVAT)를 폐쇄망 반입용 배포 폴더로 구성한다.

    python packaging/assemble_package.py --dist dist/AVAT --out package/AVAT --variant cpu
        [--vc-redist vc_redist.x64.exe] [--licenses licenses.txt] [--freeze freeze.txt]
        [--model-dir <검증에 쓴 grounding-dino-base 폴더>]

결과 폴더 구성:
    AVAT.exe, _internal/                   실행 파일과 내장 라이브러리(Python·Qt·OpenCV·PyTorch 등)
    detection-runtime.json                 Grounding DINO·로컬 모델·오프라인 설정
    runtime/models/grounding-dino-base/    모델 7개 파일을 넣을 자리(가중치는 포함하지 않음)
    prerequisites/vc_redist.x64.exe        Microsoft Visual C++ 재배포 패키지(필요한 PC만 설치)
    licenses/                              제3자 고지·설치 패키지 라이선스·빌드 패키지 목록
    배포_안내.txt, BUILD-INFO.txt, SHA256SUMS.txt, verify-package.ps1
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
MODEL_FILES = (
    "config.json",
    "model.safetensors",
    "preprocessor_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
)
# 대상 PC에 Visual C++ 재배포 패키지가 없어도 PyTorch·Qt DLL이 로드되도록 함께 있어야 한다.
REQUIRED_RUNTIME_DLLS = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _check_runtime_dlls(app: Path) -> list[str]:
    internal = app / "_internal"
    found = {path.name.lower() for path in internal.rglob("*.dll")}
    return [name for name in REQUIRED_RUNTIME_DLLS if name not in found]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_checksums(app: Path) -> int:
    lines = []
    for path in sorted(p for p in app.rglob("*") if p.is_file()):
        relative = path.relative_to(app).as_posix()
        if relative in {"SHA256SUMS.txt", "verify-package.ps1"}:
            continue
        lines.append(f"{_sha256(path)}  {relative}")
    (app / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def _copy_text_for_windows(source: Path, destination: Path) -> None:
    """저장소의 LF·BOM 없는 텍스트를 메모장·Windows PowerShell 5.1이 바로 읽는 CRLF·UTF-8 BOM으로."""
    text = source.read_text(encoding="utf-8").replace("\r\n", "\n")
    destination.write_bytes(text.replace("\n", "\r\n").encode("utf-8-sig"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--variant", required=True, choices=["cpu", "cuda"])
    parser.add_argument("--vc-redist", type=Path)
    parser.add_argument("--licenses", type=Path)
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--model-dir", type=Path)
    args = parser.parse_args()

    dist = args.dist.resolve()
    app = args.out.resolve()
    if not (dist / "AVAT.exe").is_file():
        raise SystemExit(f"PyInstaller 결과가 없습니다: {dist / 'AVAT.exe'}")
    missing = _check_runtime_dlls(dist)
    if missing:
        raise SystemExit(f"_internal에 Visual C++ 런타임 DLL이 없습니다: {', '.join(missing)}")
    if app.exists():
        shutil.rmtree(app)
    app.parent.mkdir(parents=True, exist_ok=True)
    # CUDA 배포본은 수 GB이므로 복사하지 않고 옮긴다.
    shutil.move(str(dist), str(app))

    shutil.copyfile(PACKAGING / "detection-runtime.json", app / "detection-runtime.json")
    model_dir = app / "runtime" / "models" / "grounding-dino-base"
    model_dir.mkdir(parents=True)
    (model_dir / "여기에_모델_파일_7개를_복사.txt").write_text(
        "이 폴더에 IDEA-Research/grounding-dino-base의 아래 7개 파일을 그대로 복사합니다.\n"
        "(다운로드용 .cache 폴더는 필요하지 않습니다. 이 안내 파일은 지워도 됩니다.)\n\n"
        + "\n".join(MODEL_FILES) + "\n",
        encoding="utf-8-sig",
    )
    if args.model_dir is not None:
        # 빌드 검증에 쓴 모델의 체크섬. 반입한 모델 파일이 같은지 verify-package.ps1이 확인한다.
        sums = [f"{_sha256(args.model_dir / name)}  {name}" for name in MODEL_FILES]
        (model_dir / "MODEL-SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    _copy_text_for_windows(PACKAGING / "배포_안내.txt", app / "배포_안내.txt")

    licenses = app / "licenses"
    licenses.mkdir()
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", licenses / "THIRD_PARTY_NOTICES.md")
    for source, name in ((args.licenses, "python-packages-licenses.txt"),
                         (args.freeze, "build-packages.txt")):
        if source is not None and source.is_file():
            shutil.copyfile(source, licenses / name)
    if args.vc_redist is not None and args.vc_redist.is_file():
        prerequisites = app / "prerequisites"
        prerequisites.mkdir()
        shutil.copyfile(args.vc_redist, prerequisites / "vc_redist.x64.exe")

    torch_note = "CUDA 12.8 (NVIDIA GPU 사용, GPU가 없으면 CPU로 실행)" if args.variant == "cuda" else "CPU 전용"
    (app / "BUILD-INFO.txt").write_text(
        "AVAT Windows 배포본\n"
        f"빌드 시각(UTC): {datetime.now(UTC):%Y-%m-%d %H:%M:%S}\n"
        f"소스 커밋: {_git('rev-parse', 'HEAD')}\n"
        f"소스 브랜치: {_git('rev-parse', '--abbrev-ref', 'HEAD')}\n"
        f"PyTorch 종류: {torch_note}\n"
        "검출 모델: IDEA-Research/grounding-dino-base (별도 반입, runtime/models/grounding-dino-base)\n",
        encoding="utf-8-sig",
    )
    _copy_text_for_windows(PACKAGING / "verify-package.ps1", app / "verify-package.ps1")
    count = _write_checksums(app)
    print(f"배포 폴더 구성 완료: {app} (체크섬 {count}개 파일)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
