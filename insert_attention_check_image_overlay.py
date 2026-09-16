from __future__ import annotations

import argparse
import csv
import json
import random
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


@dataclass
class ImageOverlayConfig:
    image_path: Path
    start_sec: float
    duration_sec: float
    position: str
    overlay_width_ratio: float
    overlay_alpha: float
    box_alpha: float
    box_padding: int
    margin: int
    seed: Optional[int]


def get_video_info(video_path: Path) -> dict:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = float(cap.get(cv2.CAP_PROP_FPS))
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    if fps <= 0:
        raise RuntimeError("Could not read FPS from video.")

    duration_sec = frame_count / fps if frame_count > 0 else 0.0
    return {
        "fps": fps,
        "frame_count": frame_count,
        "width": width,
        "height": height,
        "duration_sec": duration_sec,
    }


def choose_start_sec(
    duration_sec: float,
    start_sec: Optional[float],
    overlay_duration_sec: float,
    min_start_sec: float,
    max_start_ratio: float,
    seed: Optional[int],
) -> float:
    if start_sec is not None:
        if start_sec < 0:
            raise ValueError("--start-sec must be >= 0")
        if start_sec + overlay_duration_sec > duration_sec:
            raise ValueError(
                f"Overlay exceeds video duration: start={start_sec}, "
                f"duration={overlay_duration_sec}, video={duration_sec:.2f}"
            )
        return start_sec

    rng = random.Random(seed)

    # 最後だけ見れば突破できる状態を避けるため，終盤は避ける。
    max_start_sec = min(duration_sec - overlay_duration_sec - 1.0, duration_sec * max_start_ratio)

    if max_start_sec <= min_start_sec:
        return max(0.0, min(duration_sec - overlay_duration_sec, duration_sec * 0.5))

    return rng.uniform(min_start_sec, max_start_sec)


def compute_box_position(
    frame_w: int,
    frame_h: int,
    box_w: int,
    box_h: int,
    position: str,
    margin: int,
) -> tuple[int, int]:
    if position == "top-right":
        return frame_w - box_w - margin, margin
    if position == "top-left":
        return margin, margin
    if position == "bottom-right":
        return frame_w - box_w - margin, frame_h - box_h - margin
    if position == "bottom-left":
        return margin, frame_h - box_h - margin
    if position == "center":
        return (frame_w - box_w) // 2, (frame_h - box_h) // 2

    raise ValueError(f"Unsupported position: {position}")


def load_overlay_image(image_path: Path) -> np.ndarray:
    image = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise RuntimeError(f"Cannot open overlay image: {image_path}")

    return image


def resize_overlay_image(
    overlay: np.ndarray,
    frame_w: int,
    overlay_width_ratio: float,
) -> np.ndarray:
    if overlay_width_ratio <= 0 or overlay_width_ratio > 1:
        raise ValueError("--overlay-width-ratio must be in (0, 1].")

    target_w = max(1, int(frame_w * overlay_width_ratio))
    h, w = overlay.shape[:2]
    scale = target_w / w
    target_h = max(1, int(h * scale))

    return cv2.resize(overlay, (target_w, target_h), interpolation=cv2.INTER_AREA)


def alpha_blend_roi(
    roi: np.ndarray,
    overlay: np.ndarray,
    overlay_alpha: float,
) -> np.ndarray:
    """ROI上にoverlayを合成する。overlayがRGBAならalphaを利用する。"""
    if overlay.shape[2] == 4:
        overlay_rgb = overlay[:, :, :3]
        overlay_a = overlay[:, :, 3:4].astype(np.float32) / 255.0
        alpha = overlay_a * overlay_alpha
    else:
        overlay_rgb = overlay[:, :, :3]
        alpha = overlay_alpha

    roi_f = roi.astype(np.float32)
    overlay_f = overlay_rgb.astype(np.float32)

    blended = overlay_f * alpha + roi_f * (1.0 - alpha)
    return np.clip(blended, 0, 255).astype(np.uint8)


def draw_image_overlay(
    frame: np.ndarray,
    overlay_image: np.ndarray,
    position: str,
    overlay_width_ratio: float,
    overlay_alpha: float,
    box_alpha: float,
    box_padding: int,
    margin: int,
) -> np.ndarray:
    output = frame.copy()
    frame_h, frame_w = frame.shape[:2]

    resized = resize_overlay_image(
        overlay=overlay_image,
        frame_w=frame_w,
        overlay_width_ratio=overlay_width_ratio,
    )

    image_h, image_w = resized.shape[:2]

    box_w = image_w + box_padding * 2
    box_h = image_h + box_padding * 2

    x, y = compute_box_position(
        frame_w=frame_w,
        frame_h=frame_h,
        box_w=box_w,
        box_h=box_h,
        position=position,
        margin=margin,
    )

    x = max(0, min(x, frame_w - box_w))
    y = max(0, min(y, frame_h - box_h))

    # 半透明の黒背景カード
    if box_alpha > 0:
        bg = output.copy()
        cv2.rectangle(
            bg,
            (x, y),
            (x + box_w, y + box_h),
            (0, 0, 0),
            thickness=-1,
        )
        output = cv2.addWeighted(bg, box_alpha, output, 1.0 - box_alpha, 0)

    img_x1 = x + box_padding
    img_y1 = y + box_padding
    img_x2 = img_x1 + image_w
    img_y2 = img_y1 + image_h

    roi = output[img_y1:img_y2, img_x1:img_x2]
    output[img_y1:img_y2, img_x1:img_x2] = alpha_blend_roi(
        roi=roi,
        overlay=resized,
        overlay_alpha=overlay_alpha,
    )

    return output


def has_audio_stream(video_path: Path) -> bool:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return False

    return bool(result.stdout.strip())


def mux_audio_with_ffmpeg(
    original_video_path: Path,
    silent_video_path: Path,
    output_path: Path,
) -> bool:
    if not has_audio_stream(original_video_path):
        return False

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(silent_video_path),
        "-i",
        str(original_video_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-shortest",
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return False

    if result.returncode != 0:
        print("[warning] ffmpeg audio mux failed.")
        print(result.stderr)
        return False

    return True


def insert_image_overlay(
    input_video_path: Path,
    output_video_path: Path,
    config: ImageOverlayConfig,
    keep_audio: bool,
) -> dict:
    info = get_video_info(input_video_path)
    overlay_image = load_overlay_image(config.image_path)

    cap = cv2.VideoCapture(str(input_video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {input_video_path}")

    fps = info["fps"]
    width = info["width"]
    height = info["height"]
    frame_count = info["frame_count"]

    start_frame = int(round(config.start_sec * fps))
    end_frame = int(round((config.start_sec + config.duration_sec) * fps))

    output_video_path.parent.mkdir(parents=True, exist_ok=True)

    temp_output_path = output_video_path
    if keep_audio:
        tmp_dir = Path(tempfile.mkdtemp())
        temp_output_path = tmp_dir / f"{output_video_path.stem}_silent.mp4"

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(temp_output_path), fourcc, fps, (width, height))

    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"Cannot write output video: {temp_output_path}")

    frame_idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        if start_frame <= frame_idx < end_frame:
            frame = draw_image_overlay(
                frame=frame,
                overlay_image=overlay_image,
                position=config.position,
                overlay_width_ratio=config.overlay_width_ratio,
                overlay_alpha=config.overlay_alpha,
                box_alpha=config.box_alpha,
                box_padding=config.box_padding,
                margin=config.margin,
            )

        writer.write(frame)
        frame_idx += 1

    cap.release()
    writer.release()

    muxed = False
    if keep_audio:
        muxed = mux_audio_with_ffmpeg(
            original_video_path=input_video_path,
            silent_video_path=temp_output_path,
            output_path=output_video_path,
        )
        if not muxed:
            temp_output_path.replace(output_video_path)

    metadata = {
        "input_video": str(input_video_path),
        "output_video": str(output_video_path),
        "overlay_image": str(config.image_path),
        "fps": fps,
        "width": width,
        "height": height,
        "frame_count": frame_count,
        "video_duration_sec": info["duration_sec"],
        "overlay_start_sec": config.start_sec,
        "overlay_duration_sec": config.duration_sec,
        "overlay_end_sec": config.start_sec + config.duration_sec,
        "overlay_start_frame": start_frame,
        "overlay_end_frame": end_frame,
        "position": config.position,
        "overlay_width_ratio": config.overlay_width_ratio,
        "overlay_alpha": config.overlay_alpha,
        "box_alpha": config.box_alpha,
        "keep_audio_requested": keep_audio,
        "audio_muxed": muxed,
    }

    return metadata


def write_metadata(metadata: dict, json_path: Path, csv_path: Path) -> None:
    json_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(metadata.keys()))
        writer.writeheader()
        writer.writerow(metadata)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Insert an attention-check image overlay into a video.",
    )

    parser.add_argument("--input", type=str, required=True, help="Path to input video.")
    parser.add_argument(
        "--image",
        type=str,
        required=True,
        help="Path to attention-check image. PNG/JPG supported. PNG alpha is supported.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path to output video. Default: <input>_with_check_image.mp4",
    )
    parser.add_argument(
        "--start-sec",
        type=float,
        default=None,
        help="Overlay start time in seconds. If omitted, randomly selected.",
    )
    parser.add_argument(
        "--duration-sec",
        type=float,
        default=2.0,
        help="Overlay duration in seconds.",
    )
    parser.add_argument(
        "--min-start-sec",
        type=float,
        default=8.0,
        help="Minimum random start time in seconds.",
    )
    parser.add_argument(
        "--max-start-ratio",
        type=float,
        default=0.75,
        help="Maximum random start time as ratio of video duration.",
    )
    parser.add_argument(
        "--position",
        type=str,
        choices=["top-right", "top-left", "bottom-right", "bottom-left", "center"],
        default="top-right",
        help="Overlay position.",
    )
    parser.add_argument(
        "--overlay-width-ratio",
        type=float,
        default=0.18,
        help="Overlay image width relative to video width. Example: 0.18 means 18%%.",
    )
    parser.add_argument(
        "--overlay-alpha",
        type=float,
        default=1.0,
        help="Overlay image opacity. 0.0 transparent, 1.0 opaque.",
    )
    parser.add_argument(
        "--box-alpha",
        type=float,
        default=0.35,
        help="Background box opacity. 0.0 disables box.",
    )
    parser.add_argument("--box-padding", type=int, default=12)
    parser.add_argument("--margin", type=int, default=28)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="Try to keep original audio using ffmpeg.",
    )
    parser.add_argument("--metadata-json", type=str, default=None)
    parser.add_argument("--metadata-csv", type=str, default=None)

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    input_video_path = Path(args.input)
    if not input_video_path.exists():
        raise FileNotFoundError(f"Input video not found: {input_video_path}")

    image_path = Path(args.image)
    if not image_path.exists():
        raise FileNotFoundError(f"Overlay image not found: {image_path}")

    output_video_path = (
        Path(args.output)
        if args.output is not None
        else input_video_path.with_name(f"{input_video_path.stem}_with_check_image.mp4")
    )

    info = get_video_info(input_video_path)

    start_sec = choose_start_sec(
        duration_sec=info["duration_sec"],
        start_sec=args.start_sec,
        overlay_duration_sec=args.duration_sec,
        min_start_sec=args.min_start_sec,
        max_start_ratio=args.max_start_ratio,
        seed=args.seed,
    )

    config = ImageOverlayConfig(
        image_path=image_path,
        start_sec=start_sec,
        duration_sec=args.duration_sec,
        position=args.position,
        overlay_width_ratio=args.overlay_width_ratio,
        overlay_alpha=args.overlay_alpha,
        box_alpha=args.box_alpha,
        box_padding=args.box_padding,
        margin=args.margin,
        seed=args.seed,
    )

    metadata = insert_image_overlay(
        input_video_path=input_video_path,
        output_video_path=output_video_path,
        config=config,
        keep_audio=args.keep_audio,
    )

    metadata_json_path = (
        Path(args.metadata_json)
        if args.metadata_json
        else output_video_path.with_suffix(".json")
    )
    metadata_csv_path = (
        Path(args.metadata_csv)
        if args.metadata_csv
        else output_video_path.with_suffix(".csv")
    )

    write_metadata(metadata=metadata, json_path=metadata_json_path, csv_path=metadata_csv_path)

    print("=" * 80)
    print("Attention-check image overlay inserted.")
    print(f"input_video: {input_video_path}")
    print(f"overlay_image: {image_path}")
    print(f"output_video: {output_video_path}")
    print(f"metadata_json: {metadata_json_path}")
    print(f"metadata_csv: {metadata_csv_path}")
    print("-" * 80)
    print(f"overlay_start_sec: {metadata['overlay_start_sec']:.3f}")
    print(f"overlay_end_sec: {metadata['overlay_end_sec']:.3f}")
    print(f"position: {metadata['position']}")
    print(f"overlay_width_ratio: {metadata['overlay_width_ratio']}")
    print(f"audio_muxed: {metadata['audio_muxed']}")
    print("=" * 80)


if __name__ == "__main__":
    main()
