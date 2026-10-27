import hashlib
import io
import tarfile

import cv2
import numpy as np
import pytest

from pokescan import detect
from pokescan.detect import (compare, ensure_model, fingerprint, hamming, order_upright, phash,
                             plausible_card, sharpness, side_lengths, warp)


def card_image(seed: int, size=(630, 880), text: bool = True) -> np.ndarray:
    """A synthetic card face: random shapes and text, unique per seed."""
    rng = np.random.default_rng(seed)
    w, h = size
    img = np.full((h, w, 3), rng.integers(40, 220, 3), np.uint8)
    for _ in range(40):
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        if rng.random() < 0.5:
            cv2.circle(img, (x, y), int(rng.integers(10, 80)), color, -1)
        else:
            cv2.rectangle(img, (x, y), (x + int(rng.integers(10, 120)), y + int(rng.integers(10, 120))), color, -1)
    letters = np.array(list("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"))
    for _ in range(8 if text else 0):
        line = "".join(rng.choice(letters, int(rng.integers(6, 14))))
        origin = (int(rng.integers(10, w // 2)), int(rng.integers(40, h - 10)))
        cv2.putText(img, line, origin, cv2.FONT_HERSHEY_SIMPLEX, float(rng.uniform(0.6, 1.4)), (0, 0, 0), 2)
    return img


def test_order_upright_from_any_starting_corner_and_rotation():
    tl, tr, br, bl = [100, 100], [172, 100], [172, 200], [100, 200]
    for pts in ([br, tl, bl, tr], [tr, br, bl, tl]):
        assert order_upright(np.float32(pts)).tolist() == [tl, tr, br, bl]
    # A card lying sideways comes back with its long edges as left and right.
    sideways = order_upright(np.float32([[100, 100], [200, 100], [200, 172], [100, 172]]))
    width, height = side_lengths(sideways)
    assert height > width


def test_plausible_card_rejects_wrong_shapes():
    card = order_upright(np.float32([[100, 100], [172, 100], [172, 200], [100, 200]]))
    assert plausible_card(card, frame_height=540)
    square = order_upright(np.float32([[100, 100], [200, 100], [200, 200], [100, 200]]))
    assert not plausible_card(square, frame_height=540)
    tiny = order_upright(np.float32([[100, 100], [107, 100], [107, 110], [100, 110]]))
    assert not plausible_card(tiny, frame_height=540)


def test_warp_straightens_a_rotated_card():
    face = card_image(1)
    frame = np.zeros((1080, 1920, 3), np.uint8)
    corners = np.float32([[800, 300], [1080, 340], [1030, 730], [750, 690]])  # tilted
    m = cv2.getPerspectiveTransform(np.float32([[0, 0], [629, 0], [629, 879], [0, 879]]), corners)
    frame = cv2.warpPerspective(face, m, (1920, 1080), dst=frame, borderMode=cv2.BORDER_TRANSPARENT)
    out = warp(frame, order_upright(corners))
    assert out.shape == (880, 630, 3)
    assert compare(fingerprint(face), fingerprint(out)) == "same"


def test_sharpness_separates_sharp_from_blurred():
    face = card_image(2)
    sharp = sharpness(face)
    soft = sharpness(cv2.GaussianBlur(face, (0, 0), 4))
    assert sharp > 0.68 > soft
    assert sharpness(np.full((100, 100, 3), 128, np.uint8)) == 0.0


def test_phash_is_stable_under_small_changes():
    face = card_image(3)
    brighter = cv2.convertScaleAbs(face, alpha=1.1, beta=10)
    assert hamming(phash(face), phash(brighter)) <= 10
    assert hamming(phash(face), phash(card_image(4))) > 10


def test_compare_tells_cards_apart_despite_noise_and_fingers():
    face = card_image(5)
    noisy = np.clip(face + np.random.default_rng(0).normal(0, 6, face.shape), 0, 255).astype(np.uint8)
    cv2.ellipse(noisy, (600, 300), (90, 40), 0, 0, 360, (150, 120, 110), -1)  # a thumb on the edge
    assert compare(fingerprint(face), fingerprint(noisy)) == "same"
    # Never merge two different cards. (Synthetic cards sharing one font can match a
    # few glyphs and come out "unsure"; real cards never shared more than 9 points.)
    for other in (6, 7, 8):
        assert compare(fingerprint(face), fingerprint(card_image(other))) != "same"
    assert compare(fingerprint(card_image(9, text=False)), fingerprint(card_image(10, text=False))) == "different"


def fake_archive(content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(detect.MODEL_MEMBER)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def serve_archive(monkeypatch, archive: bytes):
    def fake_retrieve(url, path):
        with open(path, "wb") as f:
            f.write(archive)
    monkeypatch.setattr(detect.urllib.request, "urlretrieve", fake_retrieve)


def test_ensure_model_downloads_verifies_and_extracts_once(tmp_path, monkeypatch):
    archive = fake_archive(b"onnx bytes")
    monkeypatch.setattr(detect, "MODEL_SHA256", hashlib.sha256(archive).hexdigest())
    serve_archive(monkeypatch, archive)
    path = ensure_model(tmp_path, log=lambda *_: None)
    assert path.read_bytes() == b"onnx bytes"
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]  # no leftovers
    serve_archive(monkeypatch, b"should not be fetched again")
    assert ensure_model(tmp_path, log=lambda *_: None) == path


def test_ensure_model_refuses_a_corrupt_download(tmp_path, monkeypatch):
    serve_archive(monkeypatch, fake_archive(b"tampered"))
    with pytest.raises(RuntimeError, match="corrupt"):
        ensure_model(tmp_path, log=lambda *_: None)
    assert not any((tmp_path / "models").iterdir())
