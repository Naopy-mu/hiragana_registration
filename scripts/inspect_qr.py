"""Read QR text from template images, without guessing page order or page count."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps


def read_codes(path: Path) -> list[str]:
    with Image.open(path) as source:
        gray = np.array(ImageOps.exif_transpose(source).convert("L"))
    detector = cv2.QRCodeDetector()
    codes: set[str] = set()
    for rotation in range(4):
        rotated = np.ascontiguousarray(np.rot90(gray, rotation))
        height, width = rotated.shape
        regions = [rotated, rotated[height // 2 :], rotated[:, width // 4 : 3 * width // 4]]
        for region in regions:
            longest = max(region.shape)
            for maximum in (1800, 3000):
                scale = min(2.0, maximum / longest)
                candidate = cv2.resize(region, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                found, decoded, _, _ = detector.detectAndDecodeMulti(candidate)
                if found:
                    codes.update(code for code in decoded if code)
                decoded_one, _, _ = detector.detectAndDecode(candidate)
                if decoded_one:
                    codes.add(decoded_one)
        if codes:
            break
    return sorted(codes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+", type=Path, help="Original pages exported as PNG/JPEG, in explicitly checked order")
    args = parser.parse_args()
    records = []
    failed = False
    for path in args.images:
        try:
            codes = read_codes(path)
            records.append({"file": str(path), "qr_codes": codes})
            if len(codes) != 1:
                failed = True
        except Exception as exc:
            records.append({"file": str(path), "error": type(exc).__name__})
            failed = True
    print(json.dumps(records, ensure_ascii=False, indent=2))
    if failed:
        raise SystemExit("QRを一意に読み取れないページがあります。原本を確認してください。ページ順や総数は自動推定しません。")


if __name__ == "__main__":
    main()
