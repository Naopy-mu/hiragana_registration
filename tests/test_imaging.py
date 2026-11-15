import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.imaging import ImageValidationError, process_image
from scripts.generate_fixtures import generate

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def manifest():
    return json.loads((ROOT/"config/pages.test.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def photos(tmp_path_factory,manifest):
    return generate(tmp_path_factory.mktemp("photos"),manifest)


def test_rectifies_perspective_and_preserves_gray(photos,manifest):
    result=process_image(photos[0].read_bytes(),manifest)
    assert result.qr_code=="HIRA1-01"
    corrected=cv2.imdecode(np.frombuffer(result.png_bytes,np.uint8),cv2.IMREAD_GRAYSCALE)
    assert corrected.shape==(2339,1654)
    assert np.count_nonzero((corrected>80)&(corrected<220))>30000
    assert result.metrics["geometry_calibrated"] is False


def test_selected_pdf_uses_its_own_geometry(photos, manifest):
    from copy import deepcopy
    config = deepcopy(manifest)
    code = config['pages'][1]['qr_code']
    config['page_geometries'] = {code: deepcopy(config['geometry'])}
    config['geometry']['marker_centers_mm'] = [[30, 30], [180, 30], [180, 267], [30, 267]]
    config['geometry']['output_width'] = 1000
    result = process_image(photos[1].read_bytes(), config)
    assert result.qr_code == code
    assert result.metrics['output_width'] == 1654


@pytest.mark.parametrize("rotation",[cv2.ROTATE_90_CLOCKWISE,cv2.ROTATE_180,cv2.ROTATE_90_COUNTERCLOCKWISE])
def test_rotated_photo_has_same_orientation(photos,manifest,rotation):
    image=cv2.imdecode(np.frombuffer(photos[1].read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    rotated=cv2.rotate(image,rotation)
    data=cv2.imencode(".jpg",rotated)[1].tobytes()
    result=process_image(data,manifest)
    assert result.qr_code=="TEST-02"
    assert result.page_number==2


def test_qr_failure(manifest):
    data=cv2.imencode(".png",np.full((1200,900,3),255,np.uint8))[1].tobytes()
    with pytest.raises(ImageValidationError,match="QR") as caught:
        process_image(data,manifest)
    assert caught.value.code=="qr_unreadable"


def test_unknown_qr_rejected(photos,manifest):
    config={**manifest,"pages":manifest["pages"][1:]}
    with pytest.raises(ImageValidationError) as caught:
        process_image(photos[0].read_bytes(),config)
    assert caught.value.code=="unknown_page"


def test_cropped_page_rejected(photos,manifest):
    image=cv2.imdecode(np.frombuffer(photos[0].read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    # Crop the paper's top edge, retaining all four registration marks.
    data=cv2.imencode(".png",image[210:])[1].tobytes()
    with pytest.raises(ImageValidationError) as caught:
        process_image(data,manifest)
    assert caught.value.code=="paper_cropped"


def test_missing_marker_rejected(photos,manifest):
    image=cv2.imdecode(np.frombuffer(photos[0].read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    image[250:390,280:410]=245
    data=cv2.imencode(".png",image)[1].tobytes()
    with pytest.raises(ImageValidationError) as caught:
        process_image(data,manifest)
    assert caught.value.code=="markers_missing"


def test_strong_interior_shadow_rejected(photos,manifest):
    image=cv2.imdecode(np.frombuffer(photos[0].read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    image[420:2250,250:950]=(image[420:2250,250:950]*.18).astype(np.uint8)
    data=cv2.imencode(".png",image)[1].tobytes()
    with pytest.raises(ImageValidationError) as caught:
        process_image(data,manifest)
    assert caught.value.code=="strong_shadow"


def test_blurred_handwriting_rejected_even_with_clear_qr(photos,manifest):
    image=cv2.imdecode(np.frombuffer(photos[0].read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    image[450:2230,400:1600]=cv2.GaussianBlur(image[450:2230,400:1600],(45,45),10)
    data=cv2.imencode(".png",image)[1].tobytes()
    with pytest.raises(ImageValidationError) as caught:
        process_image(data,manifest)
    assert caught.value.code=="blurred"


def test_supplied_real_photo(manifest,tmp_path):
    path=Path("c:/Users/Naopy/Pictures/Screenshots/IMG_1089.jpg")
    if not path.is_file():
        pytest.skip("Supplied private photograph is not present on this machine")
    result=process_image(path.read_bytes(),manifest)
    assert result.qr_code=="HIRA1-01"
    assert result.metrics["input_width"]==2160
    (tmp_path/"real-corrected.png").write_bytes(result.png_bytes)
