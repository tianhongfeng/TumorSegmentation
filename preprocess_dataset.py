# encoding: utf-8
"""
 @project:TumorSegmenation
 @author: Jiang Hui
 @language:Python 3.7.2 [GCC 7.3.0] :: Anaconda, Inc. on linux
 @time: 2020/2/19 00:04
 @desc: 生成肝脏肿瘤级联网络的训练集
"""

import os
import shutil
import numpy as np
import SimpleITK as sitk
import scipy.ndimage as ndimage
from config.configuration import DefaultConfig
from tqdm import tqdm
import pickle
import warnings

warnings.filterwarnings("ignore")
opt = DefaultConfig()

# 预处理之前，清空之前处理的数据
if os.path.exists(opt.train_data_root):
    shutil.rmtree(opt.train_data_root)

os.mkdir(opt.train_data_root)
os.mkdir(opt.train_data_root + '/ct')
os.mkdir(opt.train_data_root + '/seg')


def generate_clock(image, array, start, end):
    """
    生成厚度为48的切片块
    :param image: 原始的volume.nii或segmentation.nii所对应的image
    :param array: 原始的volume.nii或segmentation.nii所对应的array
    :param start: 切片块的起始下标
    :param end:   切片块的终止下标
    :return: 返回切片块对应的image文件
    """
    array_block = array[start:end + 1, :, :] if end else array[start:, :, :]
    image_block = sitk.GetImageFromArray(array_block)
    image_block.SetDirection(image.GetDirection())
    image_block.SetOrigin(image.GetOrigin())
    image_block.SetSpacing(
        (opt.slice_thickness,
         opt.slice_thickness,
         opt.slice_thickness)
    )
    return image_block


def split_train_val():
    """
    将原始数据集按照3:1随机划分为训练集和验证集
    :return: 返回的是训练集的文件名列表、验证集的文件名列表
    """
    origin_volumes = [volume for volume in os.listdir(opt.origin_train_root + '/ct')]

    # shuffle数据，每次随机的结果不同
    np.random.seed(2020)
    origin_volumes = np.random.permutation(origin_volumes)

    train_volumes = origin_volumes[:int(0.75 * len(origin_volumes))]
    val_volumes = origin_volumes[int(0.75 * len(origin_volumes)):]
    return train_volumes, val_volumes


def normalize_spacing(sitk_image, new_spacing=[1.0, 1.0, 1.0], is_label=False):
    '''
    sitk_image:
    new_spacing: x,y,z
    is_label: if True, using Interpolator `sitk.sitkNearestNeighbor`
    '''
    size = np.array(sitk_image.GetSize())
    spacing = np.array(sitk_image.GetSpacing())
    new_spacing = np.array(new_spacing)
    new_size = size * spacing / new_spacing
    new_spacing_refine = size * spacing / new_size
    new_spacing_refine = [float(s) for s in new_spacing_refine]
    new_size = [int(s) for s in new_size]

    resample = sitk.ResampleImageFilter()
    resample.SetOutputDirection(sitk_image.GetDirection())
    resample.SetOutputOrigin(sitk_image.GetOrigin())
    resample.SetSize(new_size)
    resample.SetOutputSpacing(new_spacing_refine)

    if is_label:
        resample.SetInterpolator(sitk.sitkNearestNeighbor)
    else:
        # resample.SetInterpolator(sitk.sitkBSpline)
        resample.SetInterpolator(sitk.sitkLinear)

    newimage = resample.Execute(sitk_image)
    return newimage


def preprocess(volumes):
    """
    只对训练集中的volumes文件进行预处理，验证集文件不作处理
    :param volumes:随机划分的训练集的文件名列表，如 'volume-1.nii', 'volume-10.nii' 等
    :return:
    """
    idx = 0  # 切片块文件编号

    for ii, volume in tqdm(enumerate(volumes), total=len(volumes)):

        # 读取volume.nii文件
        ct = sitk.ReadImage(os.path.join(opt.origin_train_root + '/ct', volume), sitk.sitkInt16)
        ct_array = sitk.GetArrayFromImage(ct)  # ndarray类型，shape为(切片数, 512, 512)

        # 读取segmentation.nii文件
        seg = sitk.ReadImage(
            os.path.join(opt.origin_train_root + '/seg', volume.replace('volume', 'segmentation')))
        seg_array = sitk.GetArrayFromImage(seg)
        # seg_array[seg_array > 0] = 1  # 合并肝脏标签和肿瘤标签

        # 将灰度值在阈值之外的截断掉
        ct_array[ct_array > opt.gray_upper] = opt.gray_upper
        ct_array[ct_array < opt.gray_lower] = opt.gray_lower

        # 对切片块中的每一个切片，进行归一化操作
        ct_array = ct_array.astype(np.float32)
        ct_array = ct_array / 200

        # 采样原始array
        ct_array = ndimage.zoom(ct_array, opt.zoom_scale, order=1)  # shape变为(切片数//2,256,256)，采用三次插值
        seg_array = ndimage.zoom(seg_array, opt.zoom_scale, order=0)  # shape变为(切片数//2,256,256)，采用最近邻插值

        # 搜索切片分块区间
        z = np.any(seg_array, axis=(1, 2))  # 判断每一张切片中是否包含1（1表示肝脏），返回一个长度等于切片数的布尔数组
        start_slice, end_slice = np.where(z)[0][[0, -1]]  # np.where(z)返回数组中不为0的下标list
        start_slice = max(0, start_slice - opt.expand_slice)
        end_slice = min(seg_array.shape[0], end_slice + opt.expand_slice)
        if end_slice - start_slice < opt.block_size - 1:  # 过滤掉不足以生成一个切片块的原始样本
            continue
        ct_array = ct_array[start_slice:end_slice + 1, :, :]  # 截取原始CT影像中包含肝脏区间及拓张的所有切片
        seg_array = seg_array[start_slice:end_slice + 1, :, :]

        # 开始生成厚度为48的切片块，并写入文件中，保存为nii格式
        l, r = 0, opt.block_size - 1
        while r < ct_array.shape[0]:
            # volume切片块和segmentation切片块生成
            ct_block = generate_clock(ct, ct_array, l, r)
            seg_block = generate_clock(seg, seg_array, l, r)

            ct_block_name = 'volume-' + str(idx) + '.nii'
            seg_block_name = 'segmentation-' + str(idx) + '.nii'
            sitk.WriteImage(ct_block, os.path.join(opt.train_data_root + '/ct', ct_block_name))
            sitk.WriteImage(seg_block, os.path.join(opt.train_data_root + '/seg', seg_block_name))

            idx += 1
            l += opt.stride
            r = l + opt.block_size - 1

        # 如果每隔opt.stride不能完整的将所有切片分块时，从后往前取到最后一个block
        if r != ct_array.shape[0] + opt.stride:
            # volume切片块生成
            ct_block = generate_clock(ct, ct_array, -opt.block_size, None)
            seg_block = generate_clock(seg, seg_array, -opt.block_size, None)

            ct_block_name = 'volume-' + str(idx) + '.nii'
            seg_block_name = 'segmentation-' + str(idx) + '.nii'
            sitk.WriteImage(ct_block, os.path.join(opt.train_data_root + '/ct', ct_block_name))
            sitk.WriteImage(seg_block, os.path.join(opt.train_data_root + '/seg', seg_block_name))

            idx += 1


if __name__ == '__main__':
    # 随机划分训练集和验证集
    volumes_train, volumes_val = split_train_val()

    # 持久化验证集的文件名列表，供后面测试调用，这里是覆盖填写模式
    with open('data/val_volumes_list.txt', 'wb') as f:
        pickle.dump(volumes_val, f)

    # 对训练集的文件进行预处理（灰度值截断、下采样、分块并写入到新文件中）
    preprocess(volumes_train)
