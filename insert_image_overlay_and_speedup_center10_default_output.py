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
    """確認画像の挿入開始時刻を決める。

    start_secが指定されている場合はその値を使う。
    指定されていない場合は，動画の中央付近からランダムに選ぶ。
    デフォルトでは，動画全体の40%〜60%付近に表示する。
    """
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

    if duration_sec <= overlay_duration_sec:
        return 0.0

    # できるだけ中央付近にする。
    # 例: 60秒動画なら 24〜36秒付近から開始。
    center_min = duration_sec * 0.40
    center_max = duration_sec * 0.60

    # 動画の範囲外に出ないよう調整。
    min_allowed = max(0.0, min_start_sec)
    max_allowed = max(0.0, duration_sec - overlay_duration_sec)

    start_min = max(center_min, min_allowed)
    start_max = min(center_max, max_allowed)

    # 短い動画などで40〜60%範囲が使えない場合は，中央に最も近い範囲から選ぶ。
    if start_max <= start_min:
        center_start = (duration_sec - overlay_duration_sec) / 2.0
        jitter = min(duration_sec * 0.05, max_allowed)
        start_min = max(0.0, center_start - jitter)
        start_max = min(max_allowed, center_start + jitter)

    if start_max <= start_min:
        return max(0.0, min(max_allowed, (duration_sec - overlay_duration_sec) / 2.0))

    return rng.uniform(start_min, start_max)


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


def make_atempo_filter(speed: float) -> str:
    """ffmpeg atempoは0.5〜100程度だが，環境互換のため2倍以下を連結する。"""
    if speed <= 0:
        raise ValueError("--speed must be > 0")

    filters: list[str] = []
    remaining = speed

    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0

    filters.append(f"atempo={remaining:.6g}")
    return ",".join(filters)


def mux_audio_and_speed_with_ffmpeg(
    original_video_path: Path,
    silent_video_path: Path,
    output_path: Path,
    speed: float,
) -> bool:
    """ffmpegで映像・音声ともにspeed倍速化して書き出す。"""
    if not has_audio_stream(original_video_path):
        return False

    setpts_value = 1.0 / speed
    atempo_filter = make_atempo_filter(speed)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(silent_video_path),
        "-i",
        str(original_video_path),
        "-filter_complex",
        f"[0:v]setpts={setpts_value:.10f}*PTS[v];[1:a]{atempo_filter}[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
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
        print("[warning] ffmpeg speed/audio processing failed.")
        print(result.stderr)
        return False

    return True


def speed_up_video_with_ffmpeg_no_audio(
    silent_video_path: Path,
    output_path: Path,
    speed: float,
) -> bool:
    """ffmpegで音声なし動画をspeed倍速化する。"""
    setpts_value = 1.0 / speed

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(silent_video_path),
        "-filter:v",
        f"setpts={setpts_value:.10f}*PTS",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return False

    if result.returncode != 0:
        print("[warning] ffmpeg video speed processing failed.")
        print(result.stderr)
        return False

    return True


def insert_image_overlay_normal_speed(
    input_video_path: Path,
    temp_video_path: Path,
    config: ImageOverlayConfig,
) -> dict:
    """まず通常速度で画像挿入済みの音声なし動画を作る。"""
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

    temp_video_path.parent.mkdir(parents=True, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(temp_video_path), fourcc, fps, (width, height))

    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"Cannot write temp video: {temp_video_path}")

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

    return {
        **info,
        "overlay_start_frame": start_frame,
        "overlay_end_frame": end_frame,
        "temp_video": str(temp_video_path),
    }


def insert_image_overlay_and_speed_up(
    input_video_path: Path,
    output_video_path: Path,
    config: ImageOverlayConfig,
    speed: float,
    keep_audio: bool,
) -> dict:
    """画像挿入後，speed倍速の動画として保存する。"""
    if speed <= 0:
        raise ValueError("--speed must be > 0")

    output_video_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        temp_normal_path = tmp_dir / "overlay_normal_speed.mp4"

        info = insert_image_overlay_normal_speed(
            input_video_path=input_video_path,
            temp_video_path=temp_normal_path,
            config=config,
        )

        audio_muxed = False
        speed_done = False

        if keep_audio:
            audio_muxed = mux_audio_and_speed_with_ffmpeg(
                original_video_path=input_video_path,
                silent_video_path=temp_normal_path,
                output_path=output_video_path,
                speed=speed,
            )
            speed_done = audio_muxed

        if not speed_done:
            speed_done = speed_up_video_with_ffmpeg_no_audio(
                silent_video_path=temp_normal_path,
                output_path=output_video_path,
                speed=speed,
            )

        if not speed_done:
            # ffmpegがない場合のフォールバック:
            # OpenCVでフレームを間引いて高速化する。音声はなし。
            print("[warning] ffmpeg is unavailable or failed. Falling back to OpenCV frame skipping without audio.")
            fallback_speed_up_by_frame_skipping(
                input_video_path=temp_normal_path,
                output_video_path=output_video_path,
                speed=speed,
            )

    original_duration_sec = info["duration_sec"]
    output_duration_sec = original_duration_sec / speed

    return {
        "input_video": str(input_video_path),
        "output_video": str(output_video_path),
        "overlay_image": str(config.image_path),
        "fps": info["fps"],
        "width": info["width"],
        "height": info["height"],
        "frame_count": info["frame_count"],
        "original_duration_sec": original_duration_sec,
        "speed": speed,
        "output_duration_sec_estimated": output_duration_sec,
        "overlay_start_sec_original": config.start_sec,
        "overlay_duration_sec_original": config.duration_sec,
        "overlay_end_sec_original": config.start_sec + config.duration_sec,
        "overlay_start_sec_output_estimated": config.start_sec / speed,
        "overlay_duration_sec_output_estimated": config.duration_sec / speed,
        "overlay_end_sec_output_estimated": (config.start_sec + config.duration_sec) / speed,
        "overlay_start_frame": info["overlay_start_frame"],
        "overlay_end_frame": info["overlay_end_frame"],
        "position": config.position,
        "overlay_width_ratio": config.overlay_width_ratio,
        "overlay_alpha": config.overlay_alpha,
        "box_alpha": config.box_alpha,
        "keep_audio_requested": keep_audio,
        "audio_muxed": audio_muxed,
    }


def fallback_speed_up_by_frame_skipping(
    input_video_path: Path,
    output_video_path: Path,
    speed: float,
) -> None:
    """ffmpegなしの簡易倍速。フレーム間引きでspeed倍速相当にする。音声なし。"""
    info = get_video_info(input_video_path)
    cap = cv2.VideoCapture(str(input_video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open temp video: {input_video_path}")

    fps = info["fps"]
    width = info["width"]
    height = info["height"]

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_video_path), fourcc, fps, (width, height))

    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"Cannot write output video: {output_video_path}")

    # 出力フレームnに対応する入力フレーム floor(n * speed) を使う
    input_frame_idx = 0
    output_frame_idx = 0
    next_keep_idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        if input_frame_idx >= next_keep_idx:
            writer.write(frame)
            output_frame_idx += 1
            next_keep_idx = int(round(output_frame_idx * speed))

        input_frame_idx += 1

    cap.release()
    writer.release()


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
        description="Insert an attention-check image overlay for 10 seconds near the center of a video and save a 5x sped-up video.",
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
        help="Path to output video. Default: insert_<image_stem>/<video_stem>_5x.mp4",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=5.0,
        help="Playback speed multiplier. Default: 5.0.",
    )
    parser.add_argument(
        "--start-sec",
        type=float,
        default=None,
        help="Overlay start time in original video seconds. If omitted, randomly selected.",
    )
    parser.add_argument(
        "--duration-sec",
        type=float,
        default=10.0,
        help="Overlay duration in original video seconds. Default is fixed to 10 sec for 5x output.",
    )
    parser.add_argument(
        "--min-start-sec",
        type=float,
        default=0.0,
        help="Minimum random start time in original video seconds. Usually not needed because insertion is center-random.",
    )
    parser.add_argument(
        "--max-start-ratio",
        type=float,
        default=0.60,
        help="Kept for compatibility. Random insertion is centered around 40%-60% of the video.",
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
    parser.add_argument("--overlay-alpha", type=float, default=1.0)
    parser.add_argument("--box-alpha", type=float, default=0.35)
    parser.add_argument("--box-padding", type=int, default=12)
    parser.add_argument("--margin", type=int, default=28)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="Try to keep original audio and speed it up using ffmpeg.",
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

    if args.output is not None:
        output_video_path = Path(args.output)
    else:
        image_name = image_path.stem
        output_dir = Path(f"insert_{image_name}")
        output_video_path = output_dir / f"{input_video_path.stem}_{args.speed:g}x.mp4"

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

    metadata = insert_image_overlay_and_speed_up(
        input_video_path=input_video_path,
        output_video_path=output_video_path,
        config=config,
        speed=args.speed,
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
    print("Attention-check image overlay inserted and video sped up.")
    print(f"input_video: {input_video_path}")
    print(f"overlay_image: {image_path}")
    print(f"output_video: {output_video_path}")
    print(f"metadata_json: {metadata_json_path}")
    print(f"metadata_csv: {metadata_csv_path}")
    print("-" * 80)
    print(f"speed: {metadata['speed']}x")
    print(f"original_duration_sec: {metadata['original_duration_sec']:.3f}")
    print(f"output_duration_sec_estimated: {metadata['output_duration_sec_estimated']:.3f}")
    print(f"overlay_start_sec_original: {metadata['overlay_start_sec_original']:.3f}")
    print(f"overlay_end_sec_original: {metadata['overlay_end_sec_original']:.3f}")
    print(f"overlay_start_sec_output_estimated: {metadata['overlay_start_sec_output_estimated']:.3f}")
    print(f"overlay_end_sec_output_estimated: {metadata['overlay_end_sec_output_estimated']:.3f}")
    print(f"position: {metadata['position']}")
    print(f"audio_muxed: {metadata['audio_muxed']}")
    print("=" * 80)


if __name__ == "__main__":
    main()
