import fractions
import logging

import numpy as np

from mvs.codec import (CODEC_JPEG, CODEC_H264, CODEC_H265, CODEC_AV1,
                        _AV_OK, _av, _encode_jpeg)

log = logging.getLogger("macvnc")


# ---------------------------------------------------------------------------
# EncoderPipeline — per-client H.264/H.265 with JPEG fallback
# ---------------------------------------------------------------------------
class EncoderPipeline:
    def __init__(self, target_codec, width, height, bitrate):
        self.target_codec = target_codec
        self.actual_codec = CODEC_JPEG
        self._cc = None
        self._last_pts = -1
        self._setup(width, height, bitrate)

    def _setup(self, width, height, bitrate):
        if not _AV_OK or self.target_codec == CODEC_JPEG:
            return
        # ── Apple VideoToolbox rate control: design failure documented ───────
        # Every well-designed video encoder (libx264, libx265, libaom, NVENC,
        # AMD VCE, Intel QSV) treats a strict bitrate target as binding —
        # under VBV/HRD or CBR the encoder reduces quality (raises quantizer,
        # coarsens motion vectors) until output fits the budget. That's the
        # contract: "never overshoot the link, allow blurriness on motion."
        #
        # Apple's VideoToolbox VBR mode (constant_bit_rate=0, the default)
        # does NOT do this. It treats bit_rate as an average target and
        # overshoots 2-5× on complex content rather than reducing quality.
        # This is documented Apple behavior, but it's a clear architectural
        # gap in a real-time encoder: there is no built-in protection
        # against filling a constrained link.
        #
        # Apple's only mitigation:
        #   - constant_bit_rate=1 for h264_videotoolbox (macOS 13+ only).
        #     Enforces strict CBR via DataRateLimits; respects the budget.
        #   - For hevc_videotoolbox / av1_videotoolbox there is NO Apple-
        #     supplied strict-rate option as of macOS Sequoia. Output WILL
        #     overshoot on complex content.
        #
        # Software fallbacks (libx264/libx265) have proper VBV rate control
        # but cost CPU. We prefer them only when constrained-link behavior
        # matters more than encode latency (not auto-selected currently).
        #
        # Relevant Apple docs:
        #   kVTCompressionPropertyKey_DataRateLimits
        #   kVTCompressionPropertyKey_AverageBitRate
        # FFmpeg implementation: libavcodec/videotoolboxenc.c
        # ────────────────────────────────────────────────────────────────────
        _h264_vt_opts = {"realtime": "1", "allow_sw": "1",
                         # Apple-blessed strict CBR (h264 only, macOS 13+).
                         # Encoder respects bitrate by adjusting quantizer
                         # rather than overshooting.
                         "constant_bit_rate": "1"}
        _hevc_vt_opts = {"realtime": "1", "allow_sw": "1",
                         # No CBR option exists for hevc_videotoolbox.
                         # Output WILL overshoot on complex content; the
                         # controller's lag-feedback loop is the only
                         # backstop. Switch to libx265 if strict rate
                         # control matters more than encode latency.
                         "constant_bit_rate": "0"}
        candidates = {
            CODEC_H264: [
                ("h264_videotoolbox", _h264_vt_opts),
                ("libx264", {"preset": "fast", "tune": "zerolatency",
                             "x264-params": "bframes=0:rc-lookahead=0:aq-mode=1"}),
            ],
            CODEC_H265: [
                ("hevc_videotoolbox", _hevc_vt_opts),
                ("libx265", {"preset": "fast", "tune": "zerolatency",
                             "x265-params": "bframes=0:rc-lookahead=0:aq-mode=1"}),
            ],
            CODEC_AV1: [
                # av1_videotoolbox: same VBR overshoot issue as HEVC, no
                # Apple-supplied strict-rate option.
                ("av1_videotoolbox", {"realtime": "1", "allow_sw": "0"}),
                ("libsvtav1", {"preset": "10",
                               "svtav1-params": "film-grain=0:irefresh-type=2"}),
                ("libaom-av1", {"cpu-used": "10", "usage": "realtime"}),
            ],
        }
        for name, opts in candidates.get(self.target_codec, []):
            try:
                cc = _av.CodecContext.create(name, "w")
                cc.width = width & ~1
                cc.height = height & ~1
                cc.pix_fmt = "yuv420p"
                cc.bit_rate = bitrate
                cc.time_base = fractions.Fraction(1, 1000)
                # Large GOP: one I-frame per 5 seconds at max fps. Static screens
                # produce near-zero P-frames; a short GOP would flood with large I-frames.
                cc.gop_size = 99999
                cc.options = opts
                cc.open()
                # Warm up hardware encoder — first frame is buffered, discard it
                dummy = _av.VideoFrame(cc.width, cc.height, "yuv420p")
                dummy.pts = 0
                list(cc.encode(dummy))
                self._last_pts = 0
                self._cc = cc
                self.actual_codec = self.target_codec
                log.info("Encoder: %s %dx%d @%dkbps", name, cc.width, cc.height, bitrate//1000)
                return
            except Exception as e:
                log.debug("Codec %s failed: %s", name, e)
        log.warning("No video codec available — JPEG fallback")

    def set_bitrate(self, bitrate):
        if self._cc is not None:
            try: self._cc.bit_rate = bitrate
            except Exception: pass

    @staticmethod
    def _to_rgb(frame):
        """Convert BGRA or RGB frame to RGB (in-place if already RGB)."""
        if frame.ndim == 3 and frame.shape[2] == 4:
            return np.ascontiguousarray(frame[:, :, 2::-1])
        return frame

    def encode(self, rgb, capture_ms, jpeg_quality=65):
        """Returns (payload, is_keyframe, codec_byte) or (None, False, _) on skip."""
        if self._cc is None:
            return _encode_jpeg(self._to_rgb(rgb), jpeg_quality), True, CODEC_JPEG
        try:
            fmt = "bgra" if (rgb.ndim == 3 and rgb.shape[2] == 4) else "rgb24"
            frame = _av.VideoFrame.from_ndarray(rgb, format=fmt)
            # Downscale to encoder dimensions if source is larger (libswscale Lanczos).
            if frame.width != self._cc.width or frame.height != self._cc.height:
                frame = frame.reformat(width=self._cc.width, height=self._cc.height, format=fmt,
                                       interpolation="LANCZOS")
            frame = frame.reformat(format="yuv420p")
            pts = max(self._last_pts + 1, capture_ms)
            frame.pts = pts
            self._last_pts = pts
            pkts = list(self._cc.encode(frame))
            if not pkts:
                return None, False, self.actual_codec
            pkt = pkts[0]
            is_kf = bool(getattr(pkt, "is_keyframe", True))
            return bytes(pkt), is_kf, self.actual_codec
        except Exception as e:
            log.warning("Encode error: %s — JPEG fallback", e)
            self._cc = None
            self.actual_codec = CODEC_JPEG
            return _encode_jpeg(self._to_rgb(rgb), jpeg_quality), True, CODEC_JPEG

    def encode_keyframe(self, rgb, capture_ms, quality):
        """Force an I-frame refresh — called after extended static period to sharpen quality.
        Attempts gop_size=1 + pict_type=I; VideoToolbox may ignore both, in which case
        the frame is still sent at the current (high) bitrate ceiling."""
        if self._cc is None:
            return _encode_jpeg(self._to_rgb(rgb), quality), True, CODEC_JPEG
        try:
            self._cc.gop_size = 1
        except Exception:
            pass
        pkts = []
        try:
            fmt = "bgra" if (rgb.ndim == 3 and rgb.shape[2] == 4) else "rgb24"
            frame = _av.VideoFrame.from_ndarray(rgb, format=fmt)
            if frame.width != self._cc.width or frame.height != self._cc.height:
                frame = frame.reformat(width=self._cc.width, height=self._cc.height, format=fmt,
                                       interpolation="LANCZOS")
            frame = frame.reformat(format="yuv420p")
            pts = max(self._last_pts + 1, capture_ms)
            frame.pts = pts
            try:
                frame.pict_type = 1   # AV_PICTURE_TYPE_I
            except Exception:
                pass
            pkts = list(self._cc.encode(frame))
            self._last_pts = pts
        except Exception as e:
            log.debug("encode_keyframe err: %s", e)
        try:
            self._cc.gop_size = 99999
        except Exception:
            pass
        if not pkts:
            return None, False, self.actual_codec
        return bytes(pkts[0]), True, self.actual_codec

    def close(self):
        if self._cc:
            try: self._cc.close()
            except Exception: pass
            self._cc = None
