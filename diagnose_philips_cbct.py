# -*- coding: utf-8 -*-
"""
飞利浦 DSA CBCT 专用诊断脚本
用法: python diagnose_philips_cbct.py <文件路径>
"""
import sys, os, pydicom
from pydicom.dataset import Dataset

def diagnose(filepath):
    print(f"\n{'='*70}")
    print(f"飞利浦 CBCT 深度诊断: {os.path.basename(filepath)}")
    print(f"{'='*70}")

    ds = pydicom.dcmread(filepath, force=True)

    # 1. 基础信息
    print(f"\n[1. 基础元数据]")
    print(f"  SOPClassUID:           {getattr(ds, 'SOPClassUID', 'MISSING')}")
    print(f"  SOPInstanceUID:        {getattr(ds, 'SOPInstanceUID', 'MISSING')}")
    print(f"  Modality:              {getattr(ds, 'Modality', 'MISSING')}")
    print(f"  Manufacturer:          {getattr(ds, 'Manufacturer', 'MISSING')}")
    print(f"  ManufacturerModelName: {getattr(ds, 'ManufacturerModelName', 'MISSING')}")
    print(f"  SoftwareVersions:      {getattr(ds, 'SoftwareVersions', 'MISSING')}")

    # 2. 像素相关
    print(f"\n[2. 像素参数]")
    print(f"  NumberOfFrames:        {getattr(ds, 'NumberOfFrames', 'MISSING')}")
    print(f"  Rows:                  {getattr(ds, 'Rows', 'MISSING')}")
    print(f"  Columns:               {getattr(ds, 'Columns', 'MISSING')}")
    print(f"  BitsAllocated:         {getattr(ds, 'BitsAllocated', 'MISSING')}")
    print(f"  BitsStored:            {getattr(ds, 'BitsStored', 'MISSING')}")
    print(f"  HighBit:               {getattr(ds, 'HighBit', 'MISSING')}")
    print(f"  SamplesPerPixel:       {getattr(ds, 'SamplesPerPixel', 'MISSING')}")
    print(f"  PixelRepresentation:   {getattr(ds, 'PixelRepresentation', 'MISSING')}")
    print(f"  PhotometricInterpretation: {getattr(ds, 'PhotometricInterpretation', 'MISSING')}")

    # 3. Transfer Syntax
    ts = getattr(getattr(ds, 'file_meta', None), 'TransferSyntaxUID', 'MISSING')
    print(f"\n[3. 传输语法]")
    print(f"  TransferSyntaxUID:     {ts}")
    print(f"  是否压缩:              {'是' if ts not in ('1.2.840.10008.1.2', '1.2.840.10008.1.2.1', '1.2.840.10008.1.2.2') else '否'}")

    # 4. PixelData 详细分析
    print(f"\n[4. PixelData 分析]")
    pixel_data_len = len(ds.PixelData) if 'PixelData' in ds else 0
    print(f"  PixelData 标签存在:    {'是' if 'PixelData' in ds else '否'}")
    print(f"  PixelData 长度:        {pixel_data_len} 字节")

    # 检查 Float Pixel Data
    for tag_name in ['FloatPixelData', 'DoubleFloatPixelData']:
        if tag_name in ds:
            print(f"  {tag_name}: 存在，长度 {len(getattr(ds, tag_name))} 字节")

    # 如果是封装格式，检查片段结构
    if pixel_data_len > 0 and ts not in ('1.2.840.10008.1.2', '1.2.840.10008.1.2.1', '1.2.840.10008.1.2.2'):
        try:
            # 尝试解析封装片段
            from pydicom.encaps import get_frame_offsets, generate_pixel_data_frame
            offsets = get_frame_offsets(ds)
            print(f"  封装片段数:            {len(offsets)}")
            for i, off in enumerate(offsets[:5]):
                print(f"    片段 {i}: 偏移 {off}")
            if len(offsets) > 5:
                print(f"    ... 还有 {len(offsets)-5} 个片段")
        except Exception as e:
            print(f"  解析封装片段失败:      {e}")

    # 计算期望 vs 实际
    n_frames = int(getattr(ds, 'NumberOfFrames', 1))
    rows = int(getattr(ds, 'Rows', 0))
    cols = int(getattr(ds, 'Columns', 0))
    bits = int(getattr(ds, 'BitsAllocated', 16))
    samples = int(getattr(ds, 'SamplesPerPixel', 1))
    bytes_per_sample = bits // 8
    row_bytes = cols * samples * bytes_per_sample
    if row_bytes % 2 == 1:
        row_bytes += 1
    bytes_per_frame = row_bytes * rows
    expected_total = bytes_per_frame * n_frames

    print(f"\n[5. 大小校验]")
    print(f"  单帧大小:              {bytes_per_frame} 字节")
    print(f"  {n_frames} 帧期望总大小:    {expected_total} 字节")
    print(f"  实际 PixelData 大小:    {pixel_data_len} 字节")
    if bytes_per_frame > 0:
        actual_frames = pixel_data_len // bytes_per_frame
        print(f"  按大小估算实际帧数:     {actual_frames}")
    print(f"  结论:                  {'数据量匹配' if pixel_data_len == expected_total else '数据量不足!'}")

    # 6. pixel_array 测试
    print(f"\n[6. pixel_array 测试]")
    try:
        arr = ds.pixel_array
        print(f"  成功解码")
        print(f"  形状: {arr.shape}")
        print(f"  维度: {arr.ndim}")
        print(f"  数据类型: {arr.dtype}")
        if arr.ndim >= 2:
            print(f"  最小值: {arr.min()}, 最大值: {arr.max()}")
    except Exception as e:
        print(f"  解码失败: {e}")

    # 7. PerFrameFunctionalGroupsSequence 检查
    print(f"\n[7. PerFrameFunctionalGroupsSequence]")
    pffgs = getattr(ds, 'PerFrameFunctionalGroupsSequence', None)
    if pffgs:
        print(f"  项目数: {len(pffgs)}")
        if len(pffgs) > 0:
            first = pffgs[0]
            print(f"  第1项标签: {list(first.keys())[:10]}")
            if 'PlanePositionSequence' in first:
                pps = first.PlanePositionSequence[0]
                print(f"  第1帧位置: {getattr(pps, 'ImagePositionPatient', 'MISSING')}")
    else:
        print(f"  不存在")

    # 8. SharedFunctionalGroupsSequence 检查
    print(f"\n[8. SharedFunctionalGroupsSequence]")
    sfgs = getattr(ds, 'SharedFunctionalGroupsSequence', None)
    if sfgs:
        print(f"  项目数: {len(sfgs)}")
        if len(sfgs) > 0:
            print(f"  第1项标签: {list(sfgs[0].keys())[:10]}")
    else:
        print(f"  不存在")

    # 9. 私有标签检查（飞利浦常用组号）
    print(f"\n[9. 飞利浦私有标签检查]")
    philips_groups = [0x0019, 0x0021, 0x0023, 0x0025, 0x0027, 0x0029, 0x2001, 0x2003, 0x2005]
    for group in philips_groups:
        tags = [tag for tag in ds.keys() if tag.group == group]
        if tags:
            print(f"  组 0x{group:04X}: {len(tags)} 个标签")
            for tag in tags[:5]:
                elem = ds[tag]
                print(f"    {tag}: {elem.keyword or 'Private'} = {str(elem.value)[:60]}")
            if len(tags) > 5:
                print(f"    ... 还有 {len(tags)-5} 个标签")

    # 10. 文件系统中的其他文件
    dir_path = os.path.dirname(filepath)
    print(f"\n[10. 同目录其他文件]")
    if dir_path:
        files = sorted([f for f in os.listdir(dir_path) if f.lower().endswith('.dcm')])
        print(f"  目录中 .dcm 文件数: {len(files)}")
        for f in files[:10]:
            fpath = os.path.join(dir_path, f)
            fsize = os.path.getsize(fpath)
            print(f"    {f} ({fsize:,} 字节)")
        if len(files) > 10:
            print(f"    ... 还有 {len(files)-10} 个文件")

    print(f"\n{'='*70}")
    print(f"诊断完成")
    print(f"{'='*70}")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python diagnose_philips_cbct.py <dicom文件路径>")
        sys.exit(1)
    diagnose(sys.argv[1])
