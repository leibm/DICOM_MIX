# -*- coding: utf-8 -*-
"""
归一化诊断脚本：验证输出文件是否能被 pydicom 正确读取。

用法:
    python test_normalize.py <输入.dcm 或 输入目录>

示例:
    python test_normalize.py ./raw_multiframe.dcm
    python test_normalize.py ./raw_dicom_folder/
"""

import sys
import os
import pydicom
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian


def diagnose_dataset(ds: Dataset, label: str = ""):
    """诊断单个 Dataset 的关键属性。"""
    print(f"\n{'='*60}")
    print(f"  诊断: {label}")
    print(f"{'='*60}")

    # 基本属性
    print(f"  is_little_endian : {getattr(ds, 'is_little_endian', 'MISSING')}")
    print(f"  is_implicit_VR   : {getattr(ds, 'is_implicit_VR', 'MISSING')}")

    # file_meta
    if hasattr(ds, "file_meta") and ds.file_meta is not None:
        fm = ds.file_meta
        print(f"  file_meta:")
        print(f"    MediaStorageSOPClassUID : {getattr(fm, 'MediaStorageSOPClassUID', 'MISSING')}")
        print(f"    TransferSyntaxUID       : {getattr(fm, 'TransferSyntaxUID', 'MISSING')}")
    else:
        print(f"  file_meta: MISSING")

    # 像素相关标签
    pixel_tags = [
        "Rows", "Columns", "BitsAllocated", "BitsStored", "HighBit",
        "SamplesPerPixel", "PhotometricInterpretation", "PixelRepresentation",
        "NumberOfFrames", "PlanarConfiguration",
    ]
    print(f"  像素标签:")
    for tag in pixel_tags:
        val = getattr(ds, tag, "MISSING")
        print(f"    {tag:30s} = {val}")

    # PixelData
    if "PixelData" in ds:
        pd_len = len(ds.PixelData)
        rows = int(getattr(ds, "Rows", 0))
        cols = int(getattr(ds, "Columns", 0))
        bits = int(getattr(ds, "BitsAllocated", 16))
        samples = int(getattr(ds, "SamplesPerPixel", 1))
        frames = int(getattr(ds, "NumberOfFrames", 1))
        expected = rows * cols * samples * (bits // 8) * frames
        # 考虑行填充
        row_bytes = cols * samples * (bits // 8)
        if row_bytes % 2 == 1:
            row_bytes += 1
        expected_padded = row_bytes * rows * frames

        print(f"  PixelData:")
        print(f"    实际长度   : {pd_len}")
        print(f"    期望长度   : {expected} (无填充) / {expected_padded} (有行填充)")
        if pd_len not in (expected, expected_padded):
            print(f"    *** 长度不匹配! ***")
    else:
        print(f"  PixelData: MISSING")

    # Modality / ImageType
    print(f"  Modality    : {getattr(ds, 'Modality', 'MISSING')}")
    print(f"  ImageType   : {getattr(ds, 'ImageType', 'MISSING')}")
    print(f"  SOPClassUID : {getattr(ds, 'SOPClassUID', 'MISSING')}")


def test_read_back(filepath: str):
    """尝试读取文件并诊断。"""
    print(f"\n测试读取: {filepath}")
    try:
        ds = pydicom.dcmread(filepath, force=True)
        print(f"  pydicom 读取成功")
        diagnose_dataset(ds, os.path.basename(filepath))

        # 尝试访问 pixel_array
        try:
            arr = ds.pixel_array
            print(f"  pixel_array 成功: shape={arr.shape}, dtype={arr.dtype}")
        except Exception as e:
            print(f"  pixel_array 失败: {e}")

    except Exception as e:
        print(f"  pydicom 读取失败: {e}")


def main():
    if len(sys.argv) < 2:
        print("用法: python test_normalize.py <输入.dcm 或 目录>")
        sys.exit(1)

    path = sys.argv[1]
    if os.path.isfile(path):
        # 诊断原始文件
        ds = pydicom.dcmread(path, force=True)
        diagnose_dataset(ds, f"原始文件: {os.path.basename(path)}")

        # 模拟单帧展开并保存测试
        n_frames = int(getattr(ds, "NumberOfFrames", 1))
        if n_frames > 1:
            print(f"\n  检测到多帧文件 ({n_frames} 帧)，测试展开第 0 帧...")

            # 模拟展开（简化版）
            frame_ds = ds.copy()
            frame_ds.is_little_endian = getattr(ds, "is_little_endian", True)
            frame_ds.is_implicit_VR = getattr(ds, "is_implicit_VR", False)
            if hasattr(ds, "file_meta") and ds.file_meta is not None:
                frame_ds.file_meta = ds.file_meta.copy()

            frame_ds.NumberOfFrames = 1
            if "PerFrameFunctionalGroupsSequence" in frame_ds:
                delattr(frame_ds, "PerFrameFunctionalGroupsSequence")
            if "SharedFunctionalGroupsSequence" in frame_ds:
                delattr(frame_ds, "SharedFunctionalGroupsSequence")

            # 提取像素数据
            orig_pixel_data = ds.PixelData
            bits = int(getattr(ds, "BitsAllocated", 16))
            samples = int(getattr(ds, "SamplesPerPixel", 1))
            rows = int(getattr(ds, "Rows", 0))
            cols = int(getattr(ds, "Columns", 0))
            bytes_per_sample = bits // 8
            row_bytes = cols * samples * bytes_per_sample
            if row_bytes % 2 == 1:
                row_bytes += 1
            bytes_per_frame = row_bytes * rows

            start = 0
            end = bytes_per_frame
            frame_ds.PixelData = orig_pixel_data[start:end]

            # 设置 CT 标签
            frame_ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
            frame_ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            frame_ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
            frame_ds.SOPInstanceUID = pydicom.uid.generate_uid()
            frame_ds.Modality = "CT"
            frame_ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]

            # 保存测试
            test_path = "test_output_frame0.dcm"
            frame_ds.save_as(test_path, write_like_original=False)
            print(f"  测试文件已保存: {test_path}")

            # 读取回测试文件
            test_read_back(test_path)
        else:
            print(f"\n  单帧文件，无需展开测试")

    elif os.path.isdir(path):
        dcm_files = [f for f in os.listdir(path) if f.lower().endswith(".dcm")]
        if not dcm_files:
            print(f"目录中未找到 .dcm 文件: {path}")
            sys.exit(1)
        for f in sorted(dcm_files):
            test_read_back(os.path.join(path, f))
    else:
        print(f"路径不存在: {path}")
        sys.exit(1)


if __name__ == "__main__":
    main()
