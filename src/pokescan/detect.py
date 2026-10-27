"""Find a hand-held card in a frame, straighten it, and judge it.

The locator is Cardstream's RF-DETR Seg Nano card segmentation model (Ximilar,
Apache-2.0 weights and architecture; see NOTICE). It finds the card's outline
even with fingers over the corners, which plain edge detection could not on
real stream footage (10-12 of 60 frames, against 59 of 60).

`sharpness` is adapted from Cardstream's `core/sharpness.py` (Apache-2.0).
"""

import hashlib
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

MODEL_URL = "https://github.com/Ximilar-com/cardstream/releases/download/v2.0.0/cardstream-segmentation-v2.tar.gz"
MODEL_SHA256 = "c740782cdab1a65408b6fd55fe6e9a5070307189e036361597201714eb3bce61"
MODEL_MEMBER = "segmentation_model/onnx/model.onnx"

CARD_ASPECT = 63 / 88  # short side / long side of a standard card
WARP_SIZE = (630, 880)  # upright crop sent for identification
MIN_CONFIDENCE = 0.5
MIN_HEIGHT_FRACTION = 0.1  # a card shorter than this share of the frame is too small to name
ASPECT_TOLERANCE = 0.2

_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)


def ensure_model(data_dir: Path, log=print) -> Path:
    """The segmentation model, downloaded and checksum-verified on first use."""
    path = data_dir / "models" / "cardstream-segmentation-v2.onnx"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    archive = path.with_suffix(".tar.gz.part")
    log(f"downloading the card detection model (108 MB) to {path.parent}...")
    urllib.request.urlretrieve(MODEL_URL, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != MODEL_SHA256:
        archive.unlink()
        raise RuntimeError(f"card model download is corrupt (sha256 {digest})")
    with tarfile.open(archive) as tar:
        # Only ever read the one member we expect; never extract paths from the archive.
        member = tar.getmember(MODEL_MEMBER)
        with tar.extractfile(member) as src, open(path.with_suffix(".part"), "wb") as dst:
            dst.write(src.read())
    path.with_suffix(".part").rename(path)
    archive.unlink()
    return path


@dataclass(frozen=True)
class Detection:
    confidence: float
    quad: np.ndarray  # (4, 2) float32: top-left, top-right, bottom-right, bottom-left of the upright card
    frame_size: tuple[int, int]  # (width, height) of the frame the quad is in

    def scaled(self, width: int, height: int) -> "Detection":
        """The same card in a frame of another size, e.g. small poll frame -> full resolution."""
        sx, sy = width / self.frame_size[0], height / self.frame_size[1]
        return Detection(self.confidence, self.quad * np.float32([sx, sy]), (width, height))


class CardDetector:
    def __init__(self, model_path: Path):
        import onnxruntime

        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        self._session = onnxruntime.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        model_input = self._session.get_inputs()[0]
        self._input_name = model_input.name
        self._size = int(model_input.shape[2])
        self._outputs = [o.name for o in self._session.get_outputs()]

    def detect(self, frame: np.ndarray) -> Detection | None:
        """The most confident card in `frame` (BGR), or None."""
        height, width = frame.shape[:2]
        rgb = cv2.cvtColor(cv2.resize(frame, (self._size, self._size)), cv2.COLOR_BGR2RGB)
        blob = ((rgb.astype(np.float32) / 255 - _MEAN) / _STD).transpose(2, 0, 1)[None]
        out = dict(zip(self._outputs, self._session.run(None, {self._input_name: blob})))
        logits = out["labels"][0][:, :-1]  # (queries, classes); the last column is "no object"
        confidence = 1 / (1 + np.exp(-logits.max(axis=1)))
        best = int(confidence.argmax())
        if confidence[best] < MIN_CONFIDENCE:
            return None
        mask = cv2.resize(out["masks"][0][best], (width, height), interpolation=cv2.INTER_LINEAR) > 0
        quad = quad_from_mask(mask)
        if quad is None or not plausible_card(quad, height):
            return None
        return Detection(float(confidence[best]), quad, (width, height))


def quad_from_mask(mask: np.ndarray) -> np.ndarray | None:
    """Four upright-ordered corners of the largest blob in a boolean mask."""
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    hull = cv2.convexHull(contour)
    perimeter = cv2.arcLength(hull, True)
    # Loosen the simplification until the outline is four corners; a box around it if never.
    for fraction in np.linspace(0.01, 0.10, 19):
        approx = cv2.approxPolyDP(hull, fraction * perimeter, True)
        if len(approx) == 4:
            return order_upright(approx.reshape(4, 2))
    return order_upright(cv2.boxPoints(cv2.minAreaRect(contour)))


def order_upright(points: np.ndarray) -> np.ndarray:
    """Corners as top-left, top-right, bottom-right, bottom-left, long side vertical.

    Sorting by angle around the centre, not by x+y, so a card held at 45° still
    orders correctly. A card turned sideways is treated as upright-portrait.
    """
    points = np.asarray(points, np.float32)
    centre = points.mean(axis=0)
    points = points[np.argsort(np.arctan2(points[:, 1] - centre[1], points[:, 0] - centre[0]))]
    points = np.roll(points, -int(np.argmin(points.sum(axis=1))), axis=0)
    top = np.linalg.norm(points[1] - points[0])
    left = np.linalg.norm(points[3] - points[0])
    if top > left:  # landscape: start from the bottom-left so the long edges become left/right
        points = np.roll(points, 1, axis=0)
    return points


def side_lengths(quad: np.ndarray) -> tuple[float, float]:
    """(width, height) of an upright-ordered quad, averaging opposite sides."""
    width = (np.linalg.norm(quad[1] - quad[0]) + np.linalg.norm(quad[2] - quad[3])) / 2
    height = (np.linalg.norm(quad[3] - quad[0]) + np.linalg.norm(quad[2] - quad[1])) / 2
    return float(width), float(height)


def plausible_card(quad: np.ndarray, frame_height: int) -> bool:
    width, height = side_lengths(quad)
    if height < MIN_HEIGHT_FRACTION * frame_height or height == 0:
        return False
    if not cv2.isContourConvex(quad.astype(np.int32).reshape(-1, 1, 2)):
        return False
    return abs(width / height - CARD_ASPECT) <= ASPECT_TOLERANCE * CARD_ASPECT


def warp(frame: np.ndarray, quad: np.ndarray, size: tuple[int, int] = WARP_SIZE) -> np.ndarray:
    """The card cut out of `frame` and straightened to `size` (width, height)."""
    w, h = size
    target = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
    return cv2.warpPerspective(frame, cv2.getPerspectiveTransform(quad.astype(np.float32), target), size)


def sharpness(crop: np.ndarray) -> float:
    """0..1, higher is sharper: the share of edge contrast a further blur removes (re-blur ratio).

    Adapted from Cardstream. Unlike variance of the Laplacian it doesn't swing
    with the artwork or exposure, so one threshold holds across cards. Judged
    at 256 px on the long edge, on the worse of the two axes, so a card smeared
    sideways by a moving hand still reads as blurred.
    """
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    scale = 256 / max(gray.shape)
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = gray.astype(np.float32)
    scores = []
    for axis, ksize in ((0, (1, 9)), (1, (9, 1))):
        before = np.abs(np.diff(gray, axis=axis))
        after = np.abs(np.diff(cv2.blur(gray, ksize), axis=axis))
        total = float(before.sum())
        scores.append(float(np.maximum(before - after, 0).sum()) / total if total > 0 else 0.0)
    return min(scores)


def phash(crop: np.ndarray) -> int:
    """64-bit DCT perceptual hash: near-identical crops differ in few bits."""
    gray = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (32, 32), interpolation=cv2.INTER_AREA)
    low = cv2.dct(gray.astype(np.float32))[:8, :8].flatten()
    bits = low > np.median(low[1:])  # leave out the DC term, which is just brightness
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


# Same card or not, from two crops. Measured on 581 pairs of sharp crops from
# real stream footage (fingers, glare, tilt): ORB inliers are decisive when high
# (different cards never shared more than 9) but drop to ~0 for flat,
# low-texture art in a dim frame, where the pHash is still close. Neither alone
# separates the cards; together 546 of 581 pairs are judged right and 15 unsure.
SAME_INLIERS = 20
DIFFERENT_INLIERS = 10
SAME_HASH_BITS = 18
DIFFERENT_HASH_BITS = 22

_orb = cv2.ORB_create(nfeatures=800)
_matcher = cv2.BFMatcher(cv2.NORM_HAMMING)


@dataclass(frozen=True, eq=False)
class Fingerprint:
    phash: int
    points: np.ndarray  # (n, 2) keypoint positions in the 315x440 comparison image
    descriptors: np.ndarray | None


def fingerprint(crop: np.ndarray) -> Fingerprint:
    gray = cv2.cvtColor(cv2.resize(crop, (315, 440), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    keypoints, descriptors = _orb.detectAndCompute(gray, None)
    points = np.float32([k.pt for k in keypoints]).reshape(-1, 2)
    return Fingerprint(phash(crop), points, descriptors)


def matching_points(a: Fingerprint, b: Fingerprint) -> int:
    """Feature matches between two crops that agree on one geometric mapping."""
    if a.descriptors is None or b.descriptors is None or len(a.points) < 8 or len(b.points) < 8:
        return 0
    pairs = _matcher.knnMatch(a.descriptors, b.descriptors, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
    if len(good) < 8:
        return len(good) // 2
    _, inliers = cv2.findHomography(
        a.points[[m.queryIdx for m in good]], b.points[[m.trainIdx for m in good]], cv2.RANSAC, 6.0
    )
    return int(inliers.sum()) if inliers is not None else 0


def compare(a: Fingerprint, b: Fingerprint) -> str:
    """'same', 'different' or 'unsure'."""
    inliers = matching_points(a, b)
    bits = hamming(a.phash, b.phash)
    if inliers >= SAME_INLIERS or bits <= SAME_HASH_BITS:
        return "same"
    if inliers < DIFFERENT_INLIERS and bits >= DIFFERENT_HASH_BITS:
        return "different"
    return "unsure"
