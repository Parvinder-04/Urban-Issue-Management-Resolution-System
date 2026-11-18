"""
Image Verification Service
----------------------------
Citizens attach a photo as proof when reporting an issue. Before it reaches
a municipal officer's queue, we run automated quality/sanity checks so
officers aren't wasting time on unusable submissions:

  1. BLUR CHECK       - Laplacian variance (a standard, well-established CV
                         technique). Sharp images have high-frequency edges
                         -> high variance. Blurry images are smoothed out
                         -> low variance.
  2. BRIGHTNESS CHECK  - rejects images that are too dark/overexposed to be
                         useful as evidence.
  3. RESOLUTION CHECK  - rejects thumbnails too small to show real detail.
  4. DUPLICATE-IMAGE CHECK - perceptual hash, to catch citizens re-uploading
                         the exact same photo across multiple complaints
                         (accidental double-submits from a mobile app).

This is intentionally NOT a "is this actually a pothole" classifier — that
needs a trained CNN on thousands of labelled civic-issue photos, which is a
strong Phase 2 extension (transfer learning on MobileNetV2 / ResNet, see
System_Design_Document.md "Future Work"). What's implemented here is the
realistic, buildable-today layer: making sure the photo is USABLE evidence.
"""
import cv2
import numpy as np
from dataclasses import dataclass, field

BLUR_THRESHOLD = 100.0          # Laplacian variance below this = too blurry
MIN_RESOLUTION = (300, 300)     # width, height in pixels
BRIGHTNESS_RANGE = (25, 230)    # mean pixel brightness (0-255) acceptable range


@dataclass
class ImageCheckResult:
    passed: bool
    blur_score: float
    brightness: float
    resolution: tuple
    issues: list = field(default_factory=list)
    phash: str = ""


def _perceptual_hash(gray_img, hash_size=8):
    """Simple average-hash (aHash) — fast, good enough to catch exact/near
    duplicate re-uploads without needing an external library."""
    resized = cv2.resize(gray_img, (hash_size, hash_size))
    avg = resized.mean()
    bits = (resized > avg).flatten()
    return "".join("1" if b else "0" for b in bits)


def hamming_distance(hash1: str, hash2: str) -> int:
    return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))


def _failed_read() -> ImageCheckResult:
    return ImageCheckResult(False, 0, 0, (0, 0), ["Could not read image file"])


def _analyse_image(img) -> ImageCheckResult:
    """Run the blur, brightness, resolution and perceptual-hash checks."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape

    blur_score = cv2.Laplacian(gray, cv2.CV_64F).var()
    brightness = float(gray.mean())
    phash = _perceptual_hash(gray)

    issues = []
    if blur_score < BLUR_THRESHOLD:
        issues.append(f"Image too blurry (sharpness score {blur_score:.1f}, need >= {BLUR_THRESHOLD})")
    if width < MIN_RESOLUTION[0] or height < MIN_RESOLUTION[1]:
        issues.append(f"Resolution too low ({width}x{height}, need >= {MIN_RESOLUTION})")
    if not (BRIGHTNESS_RANGE[0] <= brightness <= BRIGHTNESS_RANGE[1]):
        issues.append(f"Image too {'dark' if brightness < BRIGHTNESS_RANGE[0] else 'bright'} "
                       f"(brightness {brightness:.0f}, acceptable range {BRIGHTNESS_RANGE})")

    return ImageCheckResult(
        passed=len(issues) == 0,
        blur_score=round(blur_score, 1),
        brightness=round(brightness, 1),
        resolution=(width, height),
        issues=issues,
        phash=phash,
    )


def verify_image_bytes(data: bytes) -> ImageCheckResult:
    """Verify an in-memory image without writing it to disk first.

    Decoding through OpenCV is also the validity check: a file whose bytes do
    not decode is rejected rather than stored as unreadable evidence.
    """
    if not data:
        return _failed_read()
    buffer = np.frombuffer(data, dtype=np.uint8)
    if buffer.size == 0:
        return _failed_read()
    img = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if img is None:
        return _failed_read()
    return _analyse_image(img)


def verify_image(image_path: str) -> ImageCheckResult:
    """Verify an image already on disk (kept for existing callers/tests)."""
    try:
        with open(image_path, "rb") as handle:
            data = handle.read()
    except OSError:
        return _failed_read()
    return verify_image_bytes(data)


if __name__ == "__main__":
    import os
    os.makedirs("/tmp/test_images", exist_ok=True)

    # Build a synthetic "sharp" test image: high-contrast grid on a mid-grey
    # background (mid-grey keeps mean brightness in a realistic photo range)
    sharp = np.full((400, 400), 130, dtype=np.uint8)
    sharp[::20, :] = 255
    sharp[:, ::20] = 255
    sharp[10::20, :] = 0
    sharp[:, 10::20] = 0
    cv2.imwrite("/tmp/test_images/sharp.jpg", sharp)

    # Blurry version of the same image
    blurry = cv2.GaussianBlur(sharp, (31, 31), 15)
    cv2.imwrite("/tmp/test_images/blurry.jpg", blurry)

    # Too-dark image
    dark = np.full((400, 400), 10, dtype=np.uint8)
    cv2.imwrite("/tmp/test_images/dark.jpg", dark)

    # Too-small image
    tiny = np.random.randint(0, 255, (50, 50), dtype=np.uint8)
    cv2.imwrite("/tmp/test_images/tiny.jpg", tiny)

    for name in ["sharp.jpg", "blurry.jpg", "dark.jpg", "tiny.jpg"]:
        result = verify_image(f"/tmp/test_images/{name}")
        status = "PASS" if result.passed else "FAIL"
        print(f"\n{name}: [{status}]")
        print(f"  blur_score={result.blur_score}  brightness={result.brightness}  "
              f"resolution={result.resolution}")
        for issue in result.issues:
            print(f"  - {issue}")

    # Duplicate-image detection demo
    h1 = verify_image("/tmp/test_images/sharp.jpg").phash
    h2 = verify_image("/tmp/test_images/blurry.jpg").phash
    print(f"\nHamming distance between sharp.jpg and its blurred copy's hash: "
          f"{hamming_distance(h1, h2)} (low distance = likely same underlying photo)")
