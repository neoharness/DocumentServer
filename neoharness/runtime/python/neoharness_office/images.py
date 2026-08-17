from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable

from .paths import file_fact
from .process import UtilityError, run_utility
from .quality import artifact_provenance


def _mime_type(path: Path) -> str:
    result = run_utility(
        ["file", "--brief", "--mime-type", "--", str(path)], timeout=10
    )
    value = str(result["stdout"]).strip()
    return value or "application/octet-stream"


def _pillow_info(path: Path) -> dict[str, object]:
    try:
        from PIL import Image
    except ImportError:
        return {"available": False}

    try:
        with Image.open(path) as image:
            exif = image.getexif()
            return {
                "available": True,
                "format": image.format,
                "width": image.width,
                "height": image.height,
                "mode": image.mode,
                "frames": int(getattr(image, "n_frames", 1)),
                "animated": bool(getattr(image, "is_animated", False)),
                "exif_orientation": exif.get(274),
                "has_icc_profile": bool(image.info.get("icc_profile")),
                "has_transparency": (
                    "transparency" in image.info or "A" in image.getbands()
                ),
            }
    except Exception as exc:  # Pillow's plugin exceptions are format-specific.
        return {"available": True, "error": str(exc)}


def _exif_info(path: Path) -> dict[str, object]:
    if shutil.which("exiftool") is None:
        return {"available": False}
    result = run_utility(["exiftool", "-json", "-n", "--", str(path)], timeout=30)
    try:
        records = json.loads(str(result["stdout"]))
    except json.JSONDecodeError as exc:
        return {"available": True, "error": str(exc)}
    record = records[0] if records else {}
    selected = {
        key: record[key]
        for key in (
            "FileType",
            "FileTypeExtension",
            "MIMEType",
            "ImageWidth",
            "ImageHeight",
            "Orientation",
            "ColorSpace",
            "ColorSpaceData",
            "ICC_ProfileName",
            "ProfileDescription",
            "BitDepth",
            "Compression",
            "AnimationIterations",
            "FrameCount",
        )
        if key in record
    }
    return {"available": True, "fields": selected}


def image_info(path: Path) -> dict[str, object]:
    mime_type = _mime_type(path)
    result = file_fact(path, mime_type=mime_type)
    result["pillow"] = _pillow_info(path)
    result["exif"] = _exif_info(path)
    if shutil.which("vipsheader") is not None:
        header = run_utility(
            ["vipsheader", "-a", str(path)], timeout=30, check=False
        )
        result["vips"] = {
            "available": True,
            "readable": header["returncode"] == 0,
            "header": str(header["stdout"]).strip(),
            "diagnostic": str(header["stderr"]).strip(),
        }
    else:
        result["vips"] = {"available": False}
    return result


def _temporary_sibling(output: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=output.suffix, dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    return temporary


def _materialize(
    input_path: Path,
    output_path: Path,
    command: Callable[[Path], list[str]],
    *,
    operation: str,
    timeout: float = 180.0,
) -> dict[str, object]:
    temporary = _temporary_sibling(output_path)
    try:
        execution = run_utility(command(temporary), timeout=timeout)
        if not temporary.is_file() or temporary.is_symlink():
            raise UtilityError(f"{operation} did not produce a regular output file")
        os.replace(temporary, output_path)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    producer = Path(str(execution["command"][0])).name
    return {
        "operation": operation,
        "input": file_fact(input_path, mime_type=_mime_type(input_path)),
        "output": image_info(output_path),
        "provenance": artifact_provenance(
            output_path,
            finalizer="image_transform",
            producer=producer,
            operation=operation,
        ),
        "execution": {
            "command": execution["command"],
            "returncode": execution["returncode"],
            "duration_ms": execution["duration_ms"],
        },
    }


def crop(
    input_path: Path,
    output_path: Path,
    *,
    left: int,
    top: int,
    width: int,
    height: int,
) -> dict[str, object]:
    if min(left, top) < 0 or min(width, height) <= 0:
        raise ValueError("crop coordinates must be non-negative with positive size")
    return _materialize(
        input_path,
        output_path,
        lambda temporary: [
            "vips",
            "crop",
            str(input_path),
            str(temporary),
            str(left),
            str(top),
            str(width),
            str(height),
        ],
        operation="crop",
    )


def auto_orient(input_path: Path, output_path: Path) -> dict[str, object]:
    return _materialize(
        input_path,
        output_path,
        lambda temporary: ["vips", "autorot", str(input_path), str(temporary)],
        operation="auto_orient",
    )


def convert(input_path: Path, output_path: Path) -> dict[str, object]:
    return _materialize(
        input_path,
        output_path,
        lambda temporary: ["vips", "copy", str(input_path), str(temporary)],
        operation="convert",
    )


def _magick() -> str:
    executable = shutil.which("magick") or shutil.which("convert")
    if executable is None:
        raise UtilityError("ImageMagick is not installed")
    return executable


def crop_to_content(
    input_path: Path,
    output_path: Path,
    *,
    fuzz_percent: float = 0.0,
) -> dict[str, object]:
    if not 0 <= fuzz_percent <= 100:
        raise ValueError("fuzz percent must be between 0 and 100")

    def command(temporary: Path) -> list[str]:
        result = [_magick(), str(input_path)]
        if fuzz_percent:
            result.extend(["-fuzz", f"{fuzz_percent:g}%"])
        result.extend(["-trim", "+repage", str(temporary)])
        return result

    return _materialize(
        input_path,
        output_path,
        command,
        operation="crop_to_content",
    )


def fit(
    input_path: Path,
    output_path: Path,
    *,
    width: int,
    height: int,
    mode: str,
    background: str = "none",
) -> dict[str, object]:
    if min(width, height) <= 0:
        raise ValueError("fit dimensions must be positive")
    if mode not in {"contain", "cover"}:
        raise ValueError("fit mode must be contain or cover")

    def command(temporary: Path) -> list[str]:
        geometry = f"{width}x{height}" + ("^" if mode == "cover" else "")
        result = [
            _magick(),
            str(input_path),
            "-auto-orient",
            "-filter",
            "Lanczos",
            "-thumbnail",
            geometry,
            "-gravity",
            "center",
        ]
        if mode == "contain":
            result.extend(["-background", background])
        result.extend(["-extent", f"{width}x{height}", str(temporary)])
        return result

    return _materialize(input_path, output_path, command, operation=f"fit_{mode}")


def flatten(
    input_path: Path,
    output_path: Path,
    *,
    background: str = "white",
) -> dict[str, object]:
    return _materialize(
        input_path,
        output_path,
        lambda temporary: [
            _magick(),
            str(input_path),
            "-background",
            background,
            "-alpha",
            "remove",
            "-alpha",
            "off",
            str(temporary),
        ],
        operation="flatten",
    )
