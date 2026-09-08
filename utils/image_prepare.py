from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from PIL import Image, ImageOps


MAX_SHOPIFY_IMAGE_BYTES = 20 * 1024 * 1024


class ImagePreparationError(RuntimeError):
    """Raised when the exact Baserow source cannot be transferred unchanged."""


@dataclass
class PreparedImage:
    source_url: str
    upload_mode: str
    local_path: str = ""
    mime_type: str = ""
    original_size_bytes: int = 0
    final_size_bytes: int = 0
    compressed: bool = False
    warning: str = ""
    filename: str = ""
    sha256: str = ""
    original_dimensions: str = ""
    final_dimensions: str = ""
    compression_reason: str = ""
    quality_used: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _filename_from_response(url: str, response: requests.Response) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    quoted = re.search(r'filename="?([^";]+)"?', disposition, re.I)
    if encoded:
        filename = unquote(encoded.group(1))
    elif quoted:
        filename = quoted.group(1).strip()
    else:
        filename = unquote(Path(urlparse(url).path).name)
    return Path(filename or "image").name


def get_remote_file_info(url: str) -> dict:
    info = {
        "url": url,
        "content_length": 0,
        "content_type": "",
        "filename": Path(unquote(urlparse(url).path)).name or "image",
        "can_use_original_url": True,
    }
    try:
        response = requests.head(url, allow_redirects=True, timeout=30)
        if response.ok:
            try:
                info["content_length"] = int(
                    response.headers.get("Content-Length") or 0
                )
            except ValueError:
                info["content_length"] = 0
            info["content_type"] = (
                response.headers.get("Content-Type", "").split(";", 1)[0].strip()
            )
            info["filename"] = _filename_from_response(url, response)
        else:
            info["warning"] = f"HEAD returned HTTP {response.status_code}"
        response.close()
    except requests.RequestException as exc:
        info["warning"] = f"HEAD unavailable: {type(exc).__name__}"
    info["can_use_original_url"] = not exceeds_shopify_limit(
        int(info["content_length"]), MAX_SHOPIFY_IMAGE_BYTES
    )
    return info


def exceeds_shopify_limit(file_size_bytes: int, max_bytes: int) -> bool:
    return file_size_bytes > max_bytes


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_exact_original(
    url: str,
    local_dir: str | Path,
    filename_hint: str = "",
) -> tuple[Path, str]:
    directory = Path(local_dir)
    directory.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, stream=True, timeout=(20, 180))
    response.raise_for_status()
    filename = (
        _filename_from_response(url, response)
        or Path(filename_hint).name
        or "image"
    )
    target = directory / Path(filename).name
    with target.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)
    mime_type = (
        response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        or mimetypes.guess_type(target.name)[0]
        or "application/octet-stream"
    )
    response.close()
    return target, mime_type


def download_baserow_source_file(
    source_url: str,
    destination_path: Path,
) -> Path:
    if "/thumbnails/" in source_url.casefold():
        raise ImagePreparationError("thumbnail_url_blocked")
    if "/user_files/" not in source_url.casefold():
        raise ImagePreparationError("baserow_user_files_url_required")
    with requests.get(
        source_url,
        stream=True,
        timeout=(20, 180),
    ) as response:
        response.raise_for_status()
        with destination_path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
    return destination_path


def resize_for_shopify(
    input_path: Path,
    output_path: Path,
    target_width: int = 2304,
    target_height: int = 4096,
    *,
    jpeg_quality: int = 95,
    aspect_tolerance: float = 0.01,
) -> dict:
    if target_width <= 0 or target_height <= 0:
        raise ImagePreparationError("target_dimensions_must_be_positive")
    target_ratio = target_width / target_height
    with Image.open(input_path) as opened:
        image = ImageOps.exif_transpose(opened)
        source_width, source_height = image.size
        if source_height <= 0:
            raise ImagePreparationError("front_view_aspect_ratio_invalid")
        source_ratio = source_width / source_height
        relative_difference = abs(source_ratio - target_ratio) / target_ratio
        if relative_difference > aspect_tolerance:
            raise ImagePreparationError("front_view_aspect_ratio_invalid")
        source_format = (opened.format or "").upper()
        icc_profile = opened.info.get("icc_profile")
        resized = image.resize(
            (target_width, target_height),
            Image.Resampling.LANCZOS,
        )
        save_options: dict = {}
        if icc_profile:
            save_options["icc_profile"] = icc_profile
        if source_format == "PNG":
            output_mime = "image/png"
            resized.save(output_path, format="PNG", **save_options)
        elif source_format in {"JPEG", "JPG"}:
            output_mime = "image/jpeg"
            if resized.mode not in {"RGB", "L"}:
                resized = resized.convert("RGB")
            resized.save(
                output_path,
                format="JPEG",
                quality=jpeg_quality,
                subsampling=0,
                **save_options,
            )
        else:
            raise ImagePreparationError(
                f"unsupported_resize_format:{source_format or 'unknown'}"
            )
    with Image.open(output_path) as verified:
        output_width, output_height = verified.size
        output_format = (verified.format or "").upper()
    if (output_width, output_height) != (target_width, target_height):
        raise ImagePreparationError("resized_output_dimension_mismatch")
    return {
        "source_width": source_width,
        "source_height": source_height,
        "source_format": source_format,
        "output_width": output_width,
        "output_height": output_height,
        "output_format": output_format,
        "output_mime": output_mime,
        "output_size_bytes": output_path.stat().st_size,
        "resize_filter": "LANCZOS",
        "output_path": str(output_path),
    }


def prepare_image_for_shopify(
    url: str,
    local_dir: str,
    max_bytes: int = MAX_SHOPIFY_IMAGE_BYTES,
    *,
    force_stage: bool = False,
) -> PreparedImage:
    """Return the original URL or download its exact bytes for staged upload."""
    if "/thumbnails/" in url.casefold():
        raise ImagePreparationError("thumbnail_url_blocked")
    info = get_remote_file_info(url)
    known_size = int(info.get("content_length") or 0)
    over_limit = known_size and exceeds_shopify_limit(known_size, max_bytes)
    if over_limit:
        force_stage = True
    if not force_stage:
        return PreparedImage(
            source_url=url,
            upload_mode="external_original_url",
            mime_type=str(info.get("content_type") or ""),
            original_size_bytes=known_size,
            final_size_bytes=known_size,
            warning=str(info.get("warning") or ""),
            filename=str(info.get("filename") or "image"),
        )

    original, mime_type = download_exact_original(
        url, local_dir, str(info.get("filename") or "image")
    )
    actual_size = original.stat().st_size
    original_dimensions = ""
    if exceeds_shopify_limit(actual_size, max_bytes):
        with Image.open(original) as opened:
            original_dimensions = f"{opened.width}x{opened.height}"
            source_format = (opened.format or "").upper()
            has_meaningful_alpha = (
                opened.mode in {"RGBA", "LA"}
                and opened.getchannel("A").getextrema() != (255, 255)
            )
        prepared_path: Path | None = None
        quality_used: int | None = None
        compression_reason = "source_over_shopify_limit"
        if source_format in {"JPEG", "JPG"}:
            with Image.open(original) as opened:
                image = ImageOps.exif_transpose(opened)
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                for quality in (95, 93, 91, 89, 87):
                    candidate = original.with_name(
                        f"{original.stem}-shopify-q{quality}.jpg"
                    )
                    image.save(
                        candidate,
                        format="JPEG",
                        quality=quality,
                        optimize=True,
                        subsampling=0,
                    )
                    if candidate.stat().st_size <= max_bytes:
                        prepared_path = candidate
                        quality_used = quality
                        break
                    candidate.unlink(missing_ok=True)
        elif source_format == "PNG":
            with Image.open(original) as opened:
                image = ImageOps.exif_transpose(opened)
                candidate = original.with_name(
                    f"{original.stem}-shopify-optimized.png"
                )
                image.save(candidate, format="PNG", optimize=True, compress_level=9)
                if candidate.stat().st_size <= max_bytes:
                    prepared_path = candidate
                else:
                    candidate.unlink(missing_ok=True)
                    if not has_meaningful_alpha:
                        rgb = image.convert("RGB")
                        for quality in (95, 93, 91, 89, 87):
                            jpeg = original.with_name(
                                f"{original.stem}-shopify-q{quality}.jpg"
                            )
                            rgb.save(
                                jpeg,
                                format="JPEG",
                                quality=quality,
                                optimize=True,
                                subsampling=0,
                            )
                            if jpeg.stat().st_size <= max_bytes:
                                prepared_path = jpeg
                                quality_used = quality
                                compression_reason = (
                                    "source_over_shopify_limit_png_without_transparency"
                                )
                                break
                            jpeg.unlink(missing_ok=True)
        if prepared_path is None or prepared_path.stat().st_size > max_bytes:
            original.unlink(missing_ok=True)
            raise ImagePreparationError("image_over_shopify_limit")
        with Image.open(prepared_path) as verified:
            final_dimensions = f"{verified.width}x{verified.height}"
        prepared_mime = (
            "image/jpeg"
            if prepared_path.suffix.casefold() in {".jpg", ".jpeg"}
            else "image/png"
        )
        return PreparedImage(
            source_url=url,
            upload_mode="staged_over_limit_prepared",
            local_path=str(prepared_path),
            mime_type=prepared_mime,
            original_size_bytes=actual_size,
            final_size_bytes=prepared_path.stat().st_size,
            compressed=True,
            warning=str(info.get("warning") or ""),
            filename=prepared_path.name,
            sha256=_sha256(prepared_path),
            original_dimensions=original_dimensions,
            final_dimensions=final_dimensions,
            compression_reason=compression_reason,
            quality_used=quality_used,
        )
    return PreparedImage(
        source_url=url,
        upload_mode="staged_original_file",
        local_path=str(original),
        mime_type=mime_type,
        original_size_bytes=actual_size,
        final_size_bytes=actual_size,
        compressed=False,
        warning=str(info.get("warning") or ""),
        filename=original.name,
        sha256=_sha256(original),
        original_dimensions=original_dimensions,
        final_dimensions=original_dimensions,
    )


def compress_image_for_shopify(input_path: str, output_path: str) -> str:
    """Compatibility guard: Upload Saree image processing is permanently disabled."""
    source = Path(input_path)
    if exceeds_shopify_limit(source.stat().st_size, MAX_SHOPIFY_IMAGE_BYTES):
        raise ImagePreparationError("compression_disabled_exact_original_required")
    return str(source)
