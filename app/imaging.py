"""Conservative QR/registration-mark validation and grayscale A4 rectification.

Geometry is supplied by the study manifest; test geometry is deliberately labelled
uncalibrated. No page codes or page counts are inferred from a photographed page.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from itertools import combinations
import math

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError


class ImageValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


@dataclass(frozen=True)
class ProcessedImage:
    qr_code: str
    page_number: int
    png_bytes: bytes
    metrics: dict


def _decode_qr(gray: np.ndarray):
    detector = cv2.QRCodeDetector()
    h, w = gray.shape
    # Full resolution matters for the very small QR on the supplied photograph.
    rois = [(0, 0, w, h)]
    # Crop all four edges so rotated phone photographs work too.
    rois += [(0, h // 2, w, h), (0, 0, w, h // 2),
             (0, 0, w // 2, h), (w // 2, 0, w, h)]
    for x0, y0, x1, y1 in rois:
        region = gray[y0:y1, x0:x1]
        for scale in (1.0, 1.7):
            work = cv2.resize(region, None, fx=scale, fy=scale,
                              interpolation=cv2.INTER_CUBIC) if scale != 1 else region
            variants = [work, cv2.addWeighted(work, 1.6, cv2.GaussianBlur(work, (0, 0), 1), -.6, 0)]
            for variant in variants:
                try:
                    code, points, _ = detector.detectAndDecode(variant)
                except cv2.error:
                    continue
                if code and points is not None:
                    return code, points.reshape(4, 2).astype(np.float32) / scale + [x0, y0]
    raise ImageValidationError("qr_unreadable", "QRコードを読み取れません。用紙下部のQRにピントを合わせ、用紙全体を明るく撮り直してください。")


def _marker_candidates(gray: np.ndarray, qr: np.ndarray):
    h, w = gray.shape
    # A local threshold keeps marks detectable under mild uneven lighting.
    masks = [cv2.inRange(gray, 0, 85),
             cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 51, 24)]
    found = []
    qr_center = qr.mean(axis=0)
    qr_diameter = max(np.linalg.norm(qr[2] - qr[0]), 10)
    for mask in masks:
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = cv2.contourArea(contour)
            if not h*w*.000012 <= area <= h*w*.0012:
                continue
            x, y, cw, ch = cv2.boundingRect(contour)
            perimeter = cv2.arcLength(contour, True)
            circularity = 4*math.pi*area/max(perimeter*perimeter, 1)
            if not (.65 < cw/ch < 1.55 and circularity > .67 and area/(cw*ch) > .58):
                continue
            moments = cv2.moments(contour)
            center = np.array([moments["m10"]/moments["m00"], moments["m01"]/moments["m00"]], np.float32)
            if np.linalg.norm(center-qr_center) < qr_diameter*1.15:
                continue
            # Solid black marks have a dark interior; hollow handwriting is not a marker.
            radius = max(1, int(min(cw, ch)*.22))
            cx, cy = map(int, center)
            if float(np.mean(gray[max(0,cy-radius):cy+radius+1,max(0,cx-radius):cx+radius+1])) > 95:
                continue
            if not any(np.linalg.norm(center-item[0]) < max(cw,ch)*.5 for item in found):
                found.append((center, area, circularity))
    return sorted(found, key=lambda item: item[1]*item[2], reverse=True)[:28]


def _select_markers(gray: np.ndarray, qr: np.ndarray, geometry: dict):
    candidates = _marker_candidates(gray, qr)
    if len(candidates) < 4:
        raise ImageValidationError("markers_missing", "四隅の黒丸をすべて確認できません。黒丸と用紙の外側に余白を入れて撮り直してください。")
    paper = np.array(geometry["paper_mm"], np.float32)
    target = np.array(geometry["marker_centers_mm"], np.float32)
    qr_region = geometry.get("qr_center_region", [.36, .83, .64, .985])
    qr_min_mm, qr_max_mm = geometry.get("qr_size_mm", [2, 25])
    expected_qr = np.array([(qr_region[0]+qr_region[2])/2,
                            (qr_region[1]+qr_region[3])/2])
    # QR corner order provides an orientation anchor, even for 90/180-degree photos.
    ex = qr[1]-qr[0]
    ey = qr[3]-qr[0]
    ex /= max(np.linalg.norm(ex), 1)
    ey /= max(np.linalg.norm(ey), 1)
    best = None
    h, w = gray.shape
    for group in combinations(candidates, 4):
        centers = np.array([g[0] for g in group], np.float32)
        proj = np.column_stack((centers @ ex, centers @ ey))
        indexes = [int(np.argmin(proj.sum(axis=1))),
                   int(np.argmax(proj[:,0]-proj[:,1])),
                   int(np.argmax(proj.sum(axis=1))),
                   int(np.argmin(proj[:,0]-proj[:,1]))]
        if len(set(indexes)) != 4:
            continue
        ordered = centers[indexes]
        if not cv2.isContourConvex(ordered.astype(np.int32)):
            continue
        area = abs(cv2.contourArea(ordered))
        if area < .2*h*w:
            continue
        mark_areas = np.array([g[1] for g in group])
        if mark_areas.max()/mark_areas.min() > 3.5:
            continue
        transform = cv2.getPerspectiveTransform(ordered, target)
        mapped_qr = cv2.perspectiveTransform(qr[None,:,:].astype(np.float32), transform)[0]
        normalized_qr = mapped_qr.mean(axis=0)/paper
        if not (qr_region[0] < normalized_qr[0] < qr_region[2]
                and qr_region[1] < normalized_qr[1] < qr_region[3]):
            continue
        qr_size = np.linalg.norm(mapped_qr[1]-mapped_qr[0])
        if not qr_min_mm < qr_size < qr_max_mm:
            continue
        # Marker bands should be nearly orthogonal in the camera projection.
        top = np.linalg.norm(ordered[1]-ordered[0])
        bottom = np.linalg.norm(ordered[2]-ordered[3])
        left = np.linalg.norm(ordered[3]-ordered[0])
        right = np.linalg.norm(ordered[2]-ordered[1])
        if not (.45 < top/bottom < 2.2 and .45 < left/right < 2.2):
            continue
        score = area / (1 + abs(normalized_qr[0]-expected_qr[0])*8
                        + abs(normalized_qr[1]-expected_qr[1])*3)
        if best is None or score > best[0]:
            best = score, ordered, transform
    if best is None:
        raise ImageValidationError("markers_missing", "四隅の黒丸の位置を確認できません。用紙を平らに置き、4つの黒丸と用紙全体を撮り直してください。")
    return best[1], best[2]


def process_image(data: bytes, manifest: dict) -> ProcessedImage:
    try:
        with Image.open(BytesIO(data)) as source:
            if source.width*source.height > 32_000_000:
                raise ImageValidationError("image_too_large", "画像が大きすぎます。3200万画素以下の画像を選んでください。")
            rgb = np.array(ImageOps.exif_transpose(source).convert("RGB"))
    except ImageValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageValidationError("invalid_image", "画像を開けません。JPEG・PNG・WebPで保存した写真を選んでください。") from exc
    h, w = rgb.shape[:2]
    if min(h,w) < 700:
        raise ImageValidationError("low_resolution", "画像が小さすぎます。短辺700ピクセル以上で、用紙全体を撮影してください。")
    gray_full = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    code, qr_full = _decode_qr(gray_full)
    page = next((p for p in manifest["pages"] if p["qr_code"] == code), None)
    if page is None:
        raise ImageValidationError("unknown_page", f"この用紙のQR（{code[:80]}）は今回の研究の対象外です。配布された用紙を確認してください。")
    scale = min(1.0, 2000/max(h,w))
    gray = cv2.resize(gray_full, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    qr = qr_full*scale
    geometry = manifest["geometry"]
    markers, mm_transform = _select_markers(gray, qr, geometry)
    paper_w, paper_h = map(float, geometry["paper_mm"])
    paper_corners = np.array([[0,0],[paper_w,0],[paper_w,paper_h],[0,paper_h]],np.float32)
    projected = cv2.perspectiveTransform(paper_corners[None,:,:], np.linalg.inv(mm_transform))[0]
    gh, gw = gray.shape
    margin = 2.0
    if np.any(projected[:,0] < margin) or np.any(projected[:,0] > gw-1-margin) or np.any(projected[:,1] < margin) or np.any(projected[:,1] > gh-1-margin):
        raise ImageValidationError("paper_cropped", "用紙の端が写真から欠けています。四隅の黒丸だけでなく、用紙の外側にも余白を入れて撮り直してください。")
    width = int(geometry.get("output_width",1654))
    height = round(width*paper_h/paper_w)
    pixel_target = np.array(geometry["marker_centers_mm"], np.float32)*[width/paper_w,height/paper_h]
    matrix = cv2.getPerspectiveTransform(markers.astype(np.float32),pixel_target.astype(np.float32))
    # Use original camera pixels for the final warp (marker detection alone is reduced).
    matrix_full = matrix @ np.diag([scale,scale,1.0])
    rectified = cv2.warpPerspective(gray_full,matrix_full,(width,height),flags=cv2.INTER_CUBIC,borderMode=cv2.BORDER_REPLICATE)
    # Coarse background estimate suppresses shading without thresholding pencil strokes.
    small = cv2.resize(rectified,(max(16,width//32),max(16,height//32)),interpolation=cv2.INTER_AREA)
    background_small = cv2.GaussianBlur(small.astype(np.float32),(0,0),2)
    background = cv2.resize(background_small,(width,height),interpolation=cv2.INTER_CUBIC)
    core = background[int(height*.07):int(height*.94),int(width*.08):int(width*.92)]
    p05, p95 = np.percentile(core,[5,95])
    shadow_ratio = float(p05/max(p95,1))
    if p05 < 65 or shadow_ratio < .46:
        raise ImageValidationError("strong_shadow", "強い影で文字を確認しにくい部分があります。影が入らない明るい場所で撮り直してください。")
    # Focus is checked at a fixed output scale, with blank margins excluded.
    # Do not let sharp registration marks/QR conceal blurred handwriting.
    focus = float(cv2.Laplacian(rectified[int(height*.10):int(height*.85),int(width*.12):int(width*.88)],cv2.CV_64F).var())
    if focus < float(geometry.get("min_focus", 80)):
        raise ImageValidationError("blurred", "画像がぼけています。スマホを固定し、文字にピントを合わせて撮り直してください。")
    corrected = np.clip(rectified.astype(np.float32)*248/np.maximum(background,20),0,255).astype(np.uint8)
    ok, png = cv2.imencode(".png", corrected,[cv2.IMWRITE_PNG_COMPRESSION,4])
    if not ok:
        raise ImageValidationError("encoding_failed", "補正画像を作れませんでした。別の写真で再試行してください。")
    return ProcessedImage(code,int(page["page_number"]),png.tobytes(),{
        "input_width":w,"input_height":h,"output_width":width,"output_height":height,
        "focus":round(focus,2),"background_ratio":round(shadow_ratio,3),
        "marker_centers_px":np.round(markers/scale,1).tolist(),
        "estimated_paper_corners_px":np.round(projected/scale,1).tolist(),
        "geometry_calibrated":bool(geometry.get("calibrated",False))})
