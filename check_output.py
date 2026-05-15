# -*- coding: utf-8 -*-
"""
检查归一化输出文件的关键 DICOM 标签
用法: python check_output.py <目录路径>
"""
import sys, os, pydicom

def check(dcm_dir):
    files = sorted([f for f in os.listdir(dcm_dir) if f.lower().endswith('.dcm')])
    print(f"目录: {dcm_dir}")
    print(f"文件数: {len(files)}")

    prev_ipp = None
    problems = []

    for i, fname in enumerate(files):
        fpath = os.path.join(dcm_dir, fname)
        ds = pydicom.dcmread(fpath, force=True)

        if i == 0:
            print(f"\n{'='*70}")
            print(f"[首帧 {fname}] 关键标签:")
            print(f"  SOPClassUID:              {getattr(ds, 'SOPClassUID', 'MISSING')}")
            print(f"  Modality:                 {getattr(ds, 'Modality', 'MISSING')}")
            print(f"  SeriesInstanceUID:        {getattr(ds, 'SeriesInstanceUID', 'MISSING')[-16:]}")
            print(f"  StudyInstanceUID:         {getattr(ds, 'StudyInstanceUID', 'MISSING')[-16:]}")
            print(f"  NumberOfFrames:           {getattr(ds, 'NumberOfFrames', 'MISSING')}")
            print(f"  Rows:                     {getattr(ds, 'Rows', 'MISSING')}")
            print(f"  Columns:                  {getattr(ds, 'Columns', 'MISSING')}")
            print(f"  BitsAllocated:            {getattr(ds, 'BitsAllocated', 'MISSING')}")
            print(f"  BitsStored:               {getattr(ds, 'BitsStored', 'MISSING')}")
            print(f"  HighBit:                  {getattr(ds, 'HighBit', 'MISSING')}")
            print(f"  SamplesPerPixel:          {getattr(ds, 'SamplesPerPixel', 'MISSING')}")
            print(f"  PixelRepresentation:      {getattr(ds, 'PixelRepresentation', 'MISSING')}")
            print(f"  PhotometricInterpretation:{getattr(ds, 'PhotometricInterpretation', 'MISSING')}")
            print(f"  ImageOrientationPatient:  {getattr(ds, 'ImageOrientationPatient', 'MISSING')}")
            print(f"  ImagePositionPatient:     {getattr(ds, 'ImagePositionPatient', 'MISSING')}")
            print(f"  PixelSpacing:             {getattr(ds, 'PixelSpacing', 'MISSING')}")
            print(f"  SliceThickness:           {getattr(ds, 'SliceThickness', 'MISSING')}")
            print(f"  SpacingBetweenSlices:     {getattr(ds, 'SpacingBetweenSlices', 'MISSING')}")
            print(f"  PixelData len:            {len(ds.PixelData) if 'PixelData' in ds else 'MISSING'}")
            ts = getattr(getattr(ds, 'file_meta', None), 'TransferSyntaxUID', 'MISSING')
            print(f"  TransferSyntaxUID:        {ts}")
            msc = getattr(getattr(ds, 'file_meta', None), 'MediaStorageSOPClassUID', 'MISSING')
            print(f"  MediaStorageSOPClassUID:  {msc}")
            print(f"{'='*70}")

        ipp = list(getattr(ds, 'ImagePositionPatient', [None, None, None]))
        series_uid = getattr(ds, 'SeriesInstanceUID', 'MISSING')
        modality = getattr(ds, 'Modality', 'MISSING')
        sop_class = getattr(ds, 'SOPClassUID', 'MISSING')

        # 检查 SeriesUID 一致性
        if i > 0 and series_uid != getattr(pydicom.dcmread(os.path.join(dcm_dir, files[0]), force=True), 'SeriesInstanceUID', None):
            if i == 1:
                problems.append(f"  !! SeriesInstanceUID 不一致")

        # 检查 Modality
        if modality != 'CT' and i == 0:
            problems.append(f"  !! Modality={modality}，应为 CT")

        # 检查 IPP Z 变化
        if prev_ipp is not None and ipp[2] is not None:
            dz = abs(ipp[2] - prev_ipp[2])
            if dz < 0.001:
                problems.append(f"  !! 帧 {i}: Z 坐标未变化 ({ipp[2]:.4f} == {prev_ipp[2]:.4f})")
        prev_ipp = ipp

    # 打印 IPP 序列
    print(f"\n[ImagePositionPatient Z 序列 (前10帧 + 后5帧)]")
    for i, fname in enumerate(files[:10]):
        ds = pydicom.dcmread(os.path.join(dcm_dir, fname), force=True)
        ipp = getattr(ds, 'ImagePositionPatient', ['?','?','?'])
        print(f"  帧 {i:3d}: X={ipp[0]:>12} Y={ipp[1]:>12} Z={ipp[2]:>12}  ({fname})")
    if len(files) > 15:
        print(f"  ...")
    for i in range(max(10, len(files)-5), len(files)):
        ds = pydicom.dcmread(os.path.join(dcm_dir, files[i]), force=True)
        ipp = getattr(ds, 'ImagePositionPatient', ['?','?','?'])
        print(f"  帧 {i:3d}: X={ipp[0]:>12} Y={ipp[1]:>12} Z={ipp[2]:>12}  ({files[i]})")

    # Z 排序检查
    z_values = []
    for fname in files:
        ds = pydicom.dcmread(os.path.join(dcm_dir, fname), force=True)
        ipp = getattr(ds, 'ImagePositionPatient', [0,0,0])
        z_values.append(float(ipp[2]))

    is_sorted = all(z_values[i] <= z_values[i+1] for i in range(len(z_values)-1))
    is_reverse = all(z_values[i] >= z_values[i+1] for i in range(len(z_values)-1))
    has_dupes = len(z_values) != len(set(z_values))

    print(f"\n[排序检查]")
    print(f"  Z 升序: {is_sorted}")
    print(f"  Z 降序: {is_reverse}")
    print(f"  Z 重复: {has_dupes} (唯一值 {len(set(z_values))}/{len(z_values)})")
    print(f"  Z 范围: {min(z_values):.4f} ~ {max(z_values):.4f}")

    if problems:
        print(f"\n[问题]")
        for p in problems:
            print(p)
    else:
        print(f"\n[未发现明显问题]")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python check_output.py <归一化输出目录>")
        sys.exit(1)
    check(sys.argv[1])
