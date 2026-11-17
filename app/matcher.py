"""Decides whether a photo shows the same title as the lot's reference photos.

Two checks, both calibrated on the reference photos themselves:
1. ORB features + RANSAC homography: how many print points line up with a reference.
2. After aligning, the largest patch of the page that differs. This catches books
   from the same series whose layout is identical and only the title or picture
   changes. It is measured against the two best references and the smaller value
   kept: a real difference shows against every reference, noise does not.
Pages with too little print for features (near blank) fall back to normalised
cross-correlation of a small blurred copy.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass

import cv2
import numpy as np


class TeachError(Exception):
    pass


@dataclass
class Result:
    same: bool
    reason: str
    inliers: int = 0
    inlier_threshold: int = 0
    diff_patch: int = 0
    diff_threshold: int = 0
    ncc: float | None = None
    ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _stretch(img: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Map the 2nd..98th percentile of img (inside mask) to 0..255."""
    cdf = np.cumsum(cv2.calcHist([img], [0], mask, [256], [0, 256]).ravel())
    lo = int(np.searchsorted(cdf, 0.02 * cdf[-1]))
    hi = int(np.searchsorted(cdf, 0.98 * cdf[-1]))
    lut = np.clip((np.arange(256) - lo) * (255.0 / max(hi - lo, 1)), 0, 255).astype(np.uint8)
    return cv2.LUT(img, lut)


def _fit_similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares shift + rotation + scale mapping src -> dst points (2x3 matrix).

    Written with complex numbers: dst - mean = z * (src - mean), z = scale * e^(i*angle).
    """
    ms, md = src.mean(axis=0), dst.mean(axis=0)
    s = (src[:, 0] - ms[0]) + 1j * (src[:, 1] - ms[1])
    d = (dst[:, 0] - md[0]) + 1j * (dst[:, 1] - md[1])
    z = np.vdot(s, d) / max(np.vdot(s, s).real, 1e-9)
    M = np.array([[z.real, -z.imag, 0.0], [z.imag, z.real, 0.0]])
    M[:, 2] = md - M[:, :2] @ ms
    return M


@dataclass
class _Page:
    small: np.ndarray
    kp: tuple
    des: np.ndarray | None


class PageMatcher:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.orb = cv2.ORB_create(nfeatures=int(cfg["orb_features"]), fastThreshold=10)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        self.refs: list[_Page] = []
        self.use_ncc = False
        self.inlier_threshold = int(cfg["min_inliers"])
        self.diff_threshold = int(cfg["diff_min_patch"])
        self.ncc_threshold = float(cfg["ncc_threshold"])
        self.baseline_patch = 0     # largest changed patch between the references themselves

    def set_diff_threshold(self, cfg: dict) -> None:
        """Changed patch allowed; replay calls this to try other settings on the same scores."""
        self.diff_threshold = max(int(cfg["diff_min_patch"]), int(self.baseline_patch * float(cfg["diff_factor"])))

    # ---- building blocks -------------------------------------------------

    def _prepare(self, gray: np.ndarray) -> _Page:
        w = int(self.cfg["work_width"])
        h = int(round(gray.shape[0] * w / gray.shape[1]))
        small = cv2.resize(gray, (w, h), interpolation=cv2.INTER_AREA)
        small = self.clahe.apply(small)
        kp, des = self.orb.detectAndCompute(small, None)
        return _Page(small, kp, des)

    def _align(self, page: _Page, ref: _Page) -> tuple[int, np.ndarray | None]:
        """Number of print points that line up, and the transform page -> ref.

        Only shift + rotation + scale is allowed: a book lying flat under a fixed camera
        cannot shear, and a free homography can fit the text block while being wrong elsewhere.
        """
        if page.des is None or ref.des is None or len(page.kp) < 8 or len(ref.kp) < 8:
            return 0, None
        good = [p[0] for p in self.bf.knnMatch(page.des, ref.des, k=2)
                if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
        if len(good) < 8:
            return 0, None
        src = np.float32([page.kp[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([ref.kp[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        # OpenCV's own refinement step is slow (~80 ms); a direct least-squares fit on the inliers is not.
        M, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0,
                                              maxIters=500, confidence=0.99, refineIters=0)
        if M is None:
            return 0, None
        inl = mask.ravel().astype(bool)
        return int(inl.sum()), _fit_similarity(src.reshape(-1, 2)[inl], dst.reshape(-1, 2)[inl])

    def _changed_mask(self, page: _Page, ref: _Page, H: np.ndarray) -> np.ndarray | None:
        """Pixels that differ after aligning page onto ref; None if the overlap is too small."""
        h, w = ref.small.shape
        warped = cv2.warpAffine(page.small, H, (w, h))
        valid = cv2.warpAffine(np.full_like(page.small, 255), H, (w, h))
        valid = cv2.erode(valid, np.ones((9, 9), np.uint8)) > 0
        if valid.sum() < 0.3 * valid.size:
            return None
        mask = valid.astype(np.uint8)
        # Stretch both to the same dark..light range inside the overlap, so lighting drift is not "change".
        a = _stretch(cv2.GaussianBlur(warped, (3, 3), 0), mask)
        b = _stretch(cv2.GaussianBlur(ref.small, (3, 3), 0), mask)
        # A pixel only counts as changed if nothing within +-2 px in the other image is close to it,
        # so a slightly imperfect alignment does not light up every text edge.
        t = int(self.cfg["diff_level"])
        k = np.ones((5, 5), np.uint8)
        changed = ((cv2.subtract(a, cv2.dilate(b, k)) > t) | (cv2.subtract(cv2.erode(b, k), a) > t) |
                   (cv2.subtract(b, cv2.dilate(a, k)) > t) | (cv2.subtract(cv2.erode(a, k), b) > t)) & valid
        return cv2.morphologyEx(changed.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

    def _diff_patch(self, page: _Page, ref: _Page, H: np.ndarray) -> int:
        """Changed pixels in the largest patch of change (letters of one word join into one patch)."""
        changed = self._changed_mask(page, ref, H)
        if changed is None:
            return 10 ** 6
        joined = cv2.dilate(changed, np.ones((9, 9), np.uint8))
        n, labels = cv2.connectedComponents(joined)
        if n <= 1:
            return 0
        counts = np.bincount(labels[changed > 0], minlength=n)
        return int(counts[1:].max())

    @staticmethod
    def _ncc(page: _Page, ref: _Page) -> float:
        a = cv2.GaussianBlur(cv2.resize(page.small, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA), (5, 5), 0)
        b = cv2.GaussianBlur(cv2.resize(ref.small, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA), (5, 5), 0)
        h, w = a.shape
        my, mx = int(h * 0.15), int(w * 0.15)
        templ = a[my:h - my, mx:w - mx]
        if templ.std() < 2:  # blank page: compare brightness only
            return 1.0 if abs(float(templ.mean()) - float(b.mean())) < 15 else 0.0
        return float(cv2.matchTemplate(b, templ, cv2.TM_CCOEFF_NORMED).max())

    # ---- public ------------------------------------------------------------

    def build(self, images: list[np.ndarray]) -> dict:
        """Learn the title from reference photos. Raises TeachError if they disagree."""
        if len(images) < 3:
            raise TeachError("Need at least 3 reference photos.")
        refs = [self._prepare(img) for img in images]
        keypoints = [len(r.kp) for r in refs]
        use_ncc = float(np.median(keypoints)) < float(self.cfg["min_keypoints"])

        if use_ncc:
            scores = [[self._ncc(refs[i], refs[j]) if i != j else 1.0 for j in range(len(refs))]
                      for i in range(len(refs))]
            worst = [min(row) for row in scores]
            if min(worst) < self.ncc_threshold:
                raise TeachError(f"Reference photos do not look alike (check book {int(np.argmin(worst)) + 1}). "
                                 "Re-teach with good books.")
            self.use_ncc, self.refs = True, refs
            return {"method": "whole-page", "keypoints": keypoints}

        # Check every reference against the others exactly as a production book will be checked.
        inliers, patches = [], []
        for i, ref in enumerate(refs):
            others = refs[:i] + refs[i + 1:]
            n, patch = self._score(ref, others)
            inliers.append(n)
            patches.append(patch)
        typical = float(np.median(inliers))
        if min(inliers) < max(self.cfg["min_inliers"], typical * float(self.cfg["inlier_ratio"])):
            raise TeachError(f"Reference photos do not match each other (check book {int(np.argmin(inliers)) + 1}). "
                             "Re-teach with good books, and check light and photo delay.")
        self.inlier_threshold = max(int(self.cfg["min_inliers"]), int(typical * float(self.cfg["inlier_ratio"])))
        self.baseline_patch = int(max(patches))
        self.set_diff_threshold(self.cfg)
        self.use_ncc, self.refs = False, refs
        return {
            "method": "print points",
            "keypoints": keypoints,
            "typical_inliers": int(typical),
            "inlier_threshold": self.inlier_threshold,
            "baseline_patch": int(max(patches)),
            "diff_threshold": self.diff_threshold,
        }

    def _score(self, page: _Page, refs: list[_Page]) -> tuple[int, int]:
        """(best inlier count, smallest changed patch against the two best-aligned refs)."""
        aligned = []
        for ref in refs:
            n, H = self._align(page, ref)
            if H is not None:
                aligned.append((n, H, ref))
            if len(aligned) >= 2 and min(a[0] for a in aligned[:2]) >= 2 * self.inlier_threshold:
                break  # two clearly good alignments are enough
            if len(aligned) >= 2 and max(a[0] for a in aligned) < self.inlier_threshold / 2:
                break  # clearly a different book; save the time
        if not aligned:
            return 0, 10 ** 6
        aligned.sort(key=lambda a: a[0], reverse=True)
        best_n = aligned[0][0]
        patch = min(self._diff_patch(page, ref, H) for _, H, ref in aligned[:2])
        return best_n, patch

    # Used by the packing station, which asks several titles' matchers about one photo.
    def prepare(self, gray: np.ndarray) -> _Page:
        return self._prepare(gray)

    def best_inliers(self, page: _Page) -> int:
        best = 0
        for i, ref in enumerate(self.refs):
            best = max(best, self._align(page, ref)[0])
            if best >= 2 * self.inlier_threshold or (i >= 1 and best < self.inlier_threshold / 2):
                break
        return best

    def score_page(self, page: _Page) -> tuple[int, int]:
        """(print points that line up, largest changed patch) for an already prepared photo."""
        return self._score(page, self.refs)

    def check(self, gray: np.ndarray) -> Result:
        t0 = time.perf_counter()
        page = self._prepare(gray)

        if self.use_ncc:
            best = max(self._ncc(page, r) for r in self.refs)
            same = best >= self.ncc_threshold
            return Result(same, "ok" if same else "Page looks different", ncc=round(best, 3),
                          ms=round((time.perf_counter() - t0) * 1000, 1))

        n, patch = self._score(page, self.refs)
        result = self.decide(n, patch)
        result.ms = round((time.perf_counter() - t0) * 1000, 1)
        return result

    def decide(self, n: int, patch: int) -> Result:
        """Same title or not, from the two scores of _score()."""
        result = Result(False, "", inliers=n, inlier_threshold=self.inlier_threshold,
                        diff_patch=min(patch, 10 ** 6), diff_threshold=self.diff_threshold)
        if n < self.inlier_threshold:
            result.reason = "Different book: print does not match"
            result.diff_patch = 0
        elif patch > self.diff_threshold:
            result.reason = "Different book: part of the page differs"
        else:
            result.same, result.reason = True, "ok"
        return result
