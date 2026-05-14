# -*- coding: utf-8 -*-
"""
DICOM 文件深度诊断脚本
用法: python diagnose_dicom.py <文件路径>
"""
import sys, os, pydicom
from pydicom.dataset import Dataset

def diagnose(filepath):
    print(f"\n{'='*70}")
    print(f"文件: {filepath}")
    print(f"文件大小: {os.path.getsize(filepath)} 字节")
    print(f"{'='*70}")

    ds = pydicom.dcmread(filepath, force=True)

    # 基本元数据
    print(f"\n[基本元数据]")
    print(f"  SOPClassUID:           {getattr(ds, 'SOPClassUID', 'MISSING')}")
    print(f"  Modality:              {getattr(ds, 'Modality', 'MISSING')}")
    print(f"  NumberOfFrames:        {getattr(ds, 'NumberOfFrames', 'MISSING')}")
    print(f"  Rows:                  {getattr(ds, 'Rows', 'MISSING')}")
    print(f"  Columns:               {getattr(ds, 'Columns', 'MISSING')}")
    print(f"  BitsAllocated:         {getattr(ds, 'BitsAllocated', 'MISSING')}")
    print(f"  BitsStored:            {getattr(ds, 'BitsStored', 'MISSING')}")
    print(f"  HighBit:               {getattr(ds, 'HighBit', 'MISSING')}")
    print(f"  SamplesPerPixel:       {getattr(ds, 'SamplesPerPixel', 'MISSING')}")
    print(f"  PixelRepresentation:   {getattr(ds, 'PixelRepresentation', 'MISSING')}")
    print(f"  PhotometricInterpretation: {getattr(ds, 'PhotometricInterpretation', 'MISSING')}")

    # Transfer Syntax
    ts = getattr(getattr(ds, 'file_meta', None), 'TransferSyntaxUID', 'MISSING')
    print(f"\n[传输语法]")
    print(f"  TransferSyntaxUID:     {ts}")

    # PixelData 分析
    print(f"\n[像素数据分析]")
    pixel_data_len = len(ds.PixelData) if 'PixelData' in ds else 0
    print(f"  PixelData 长度:        {pixel_data_len} 字节")

    # 检查是否存在 Float Pixel Data 或 Double Float Pixel Data
    for tag_name in ['FloatPixelData', 'DoubleFloatPixelData']:
        if tag_name in ds:
            print(f"  {tag_name}: 存在，长度 {len(getattr(ds, tag_name))} 字节")

    # 计算期望大小
    n_frames = int(getattr(ds, 'NumberOfFrames', 1))
    rows = int(getattr(ds, 'Rows', 0))
    cols = int(getattr(ds, 'Columns', 0))
    bits = int(getattr(ds, 'BitsAllocated', 16))
    samples = int(getattr(ds, 'SamplesPerPixel', 1))
    bytes_per_sample = bits // 8

    expected_per_frame = rows * cols * samples * bytes_per_sample
    # 行填充
    row_bytes = cols * samples * bytes_per_sample
    if row_bytes % 2 == 1:
        row_bytes += 1
    expected_per_frame_padded = row_bytes * rows
    expected_total = expected_per_frame_padded * n_frames

    print(f"\n[大小校验]")
    print(f"  单帧期望大小(无填充):  {expected_per_frame} 字节")
    print(f"  单帧期望大小(有填充):  {expected_per_frame_padded} 字节")
    print(f"  {n_frames} 帧期望总大小:    {expected_total} 字节")
    print(f"  实际 PixelData 大小:    {pixel_data_len} 字节")

    if pixel_data_len == expected_total:
        print(f"  结果: ✅ 大小匹配")
    elif pixel_data_len == expected_per_frame_padded:
        print(f"  结果: ⚠️  仅够 1 帧数据！NumberOfFrames 可能不正确")
    elif pixel_data_len == expected_per_frame:
        print(f"  结果: ⚠️  仅够 1 帧数据(无填充)！NumberOfFrames 可能不正确")
    else:
        ratio = pixel_data_len / expected_total if expected_total > 0 else 0
        print(f"  结果: ❌ 不匹配 (实际/期望 = {ratio:.2%})")
        actual_frames = pixel_data_len // expected_per_frame_padded if expected_per_frame_padded > 0 else 0
        print(f"        按实际数据量估算约 {actual_frames} 帧")

    # pixel_array 测试
    print(f"\n[pixel_array 测试]")
    try:
        arr = ds.pixel_array
        print(f"  形状: {arr.shape}")
        print(f"  维度: {arr.ndim}")
        print(f"  数据类型: {arr.dtype}")
        print(f"  最小值: {arr.min()}, 最大值: {arr.max()}")
        if arr.ndim == 2:
            print(f"  ⚠️  警告: pixel_array 仅返回 2D 数组，实际只有 1 帧")
        elif arr.ndim == 3:
            print(f"  帧数: {arr.shape[0]}")
        elif arr.ndim == 4:
            print(f"  帧数: {arr.shape[0]}")
    except Exception as e:
        print(f"  ❌ pixel_array 失败: {e}")

    # PerFrameFunctionalGroupsSequence 检查
    pffgs = getattr(ds, 'PerFrameFunctionalGroupsSequence', None)
    if pffgs:
        print(f"\n[PerFrameFunctionalGroupsSequence]")
        print(f"  项目数: {len(pffgs)}")
        if len(pffgs) > 0:
            first = pffgs[0]
            if 'PlanePositionSequence' in first:
                pps = first.PlanePositionSequence[0]
                print(f"  第1帧位置: {getattr(pps, 'ImagePositionPatient', 'MISSING')}")
    else:
        print(f"\n[PerFrameFunctionalGroupsSequence]: 不存在")

    # SharedFunctionalGroupsSequence 检查
    sfgs = getattr(ds, 'SharedFunctionalGroupsSequence', None)
    if sfgs:
        print(f"\n[SharedFunctionalGroupsSequence]: 存在，{len(sfgs)} 项")
    else:
        print(f"\n[SharedFunctionalGroupsSequence]: 不存在")

    print(f"\n{'='*70}")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python diagnose_dicom.py <dicom文件路径>")
        sys.exit(1)
    diagnose(sys.argv[1])
