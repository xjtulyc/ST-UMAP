import os
import pickle
from itertools import product

import numba
import numpy as np
from scipy import sparse
from scipy.ndimage import gaussian_filter
from scipy.sparse import hstack
from skimage.filters import threshold_otsu
from skimage.morphology import square, erosion, reconstruction
from sklearn.neighbors import NearestNeighbors, LocalOutlierFactor
from tqdm import tqdm
from tqdm import trange

from data_preprocess.preprocess import dataloader_STARmap_MousePlacenta, dataloader_STARmap_human_cardiac_organoid


def binarize_dapi(dapi, fast_preprocess, gauss_blur, sigma):
    """
    Binarize raw dapi image

    params : - dapi (ndarray) = raw DAPI image

    returns : - dapi_binary (ndarray) = binarization of Dapi image
              - dapi_stacked (ndarray) =  2D stacked binarized image
    """
    print('Start binarize dapi')
    degree = len(dapi.shape)
    if gauss_blur:
        dapi = gaussian_filter(dapi, sigma=sigma)
    if fast_preprocess:
        if degree == 2:
            # binarize dapi
            thresh = threshold_otsu(dapi)
            binary = dapi >= thresh
            dapi_binary = np.array(binary).astype(float)
            dapi_stacked = dapi_binary
        else:
            dapi_binary = []
            for t in tqdm(np.arange(dapi.shape[2])):
                dapi_one_page = dapi[:, :, t]
                thresh = threshold_otsu(dapi_one_page)
                binary = dapi_one_page >= thresh
                dapi_binary.append(binary)  # z,y,x
                ### erosion on dapi binary
            dapi_binary = np.array(dapi_binary).transpose((1, 2, 0))  # y,x,z
            dapi_stacked = np.amax(dapi_binary, axis=2)

    else:
        if degree == 2:
            # binarize dapi
            dapi_marker = erosion(dapi, square(5))
            dapi_recon = reconstruction(dapi_marker, dapi)
            thresh = threshold_otsu(dapi_recon)
            binary = dapi_recon >= thresh
            dapi_binary = np.array(binary).astype(float)
            dapi_binary[dapi == 0] = False
            dapi_stacked = dapi_binary
        else:
            dapi_binary = []
            for t in tqdm(np.arange(dapi.shape[2])):
                dapi_one_page = dapi[:, :, t]
                dapi_marker = erosion(dapi_one_page, square(5))
                dapi_recon = reconstruction(dapi_marker, dapi_one_page)
                if len(np.unique(dapi_recon)) < 2:
                    thresh = 0
                    binary = dapi_recon >= thresh
                else:
                    thresh = threshold_otsu(dapi_recon)
                    binary = dapi_recon >= thresh
                dapi_binary.append(binary)  # z,y,x
                ### erosion on dapi binary
            dapi_binary = np.array(dapi_binary).transpose((1, 2, 0))  # y,x,z
            dapi_binary[dapi == 0] = False
            dapi_stacked = np.amax(dapi_binary, axis=2)

    return (dapi_binary, dapi_stacked)


def preprocessing_data(spots, dapi_grid_interval, dapi_binary, LOF, contamination, xy_radius, pct_filter):
    '''
    Apply preprocessing on spots, thanks to dapi.
    We remove the 10% spots with lowest density

    params :    - spots (dataframe) = spatial locations and gene identity
                - dapi_binary (ndarray) = binarized dapi image

    returns :   - spots (dataframe)
    '''
    print('Start preprocessing data')
    sampling_mat = np.zeros(dapi_binary.shape)
    if len(dapi_binary.shape) == 3:
        for ii, jj, kk in product(range(sampling_mat.shape[0]), range(sampling_mat.shape[1]),
                                  range(sampling_mat.shape[2])):
            if ii % dapi_grid_interval == 1 and jj % dapi_grid_interval == 1 and kk % dapi_grid_interval == 1:
                sampling_mat[ii, jj, kk] = 1
        dapi_sampled = dapi_binary * sampling_mat
        dapi_coord = np.argwhere(dapi_sampled > 0)

        all_points = np.concatenate(
            (np.array(spots.loc[:, ['spot_location_2', 'spot_location_1', 'spot_location_3']]), dapi_coord), axis=0)

        # compute neighbors within radius for local density
        knn = NearestNeighbors(radius=xy_radius * 2)
        knn.fit(all_points)
        spots_array = np.array(spots.loc[:, ['spot_location_2', 'spot_location_1', 'spot_location_3']])
        neigh_dist, neigh_array = knn.radius_neighbors(spots_array)

        # global low-density removal
        dis_neighbors = [(ii * ii).sum(0) for ii in neigh_dist]
        thresh = np.percentile(dis_neighbors, pct_filter * 100)
        noisy_points = np.argwhere(dis_neighbors < thresh)[:, 0]
        spots['is_noise'] = 0
        spots.loc[noisy_points, 'is_noise'] = -1

        # LOF
        if LOF:
            res_num_neighbors = [i.shape[0] for i in neigh_array]
            thresh = np.percentile(res_num_neighbors, 10)
            clf = LocalOutlierFactor(n_neighbors=int(thresh), contamination=contamination)
            spots_array = np.array(spots.loc[:, ['spot_location_2', 'spot_location_1', 'spot_location_3']])
            y_pred = clf.fit_predict(spots_array)
            spots.loc[y_pred == -1, 'is_noise'] = -1

        # spots in DAPI as inliers
        inDAPI_points = [i[0] and i[1] and i[2] for i in zip(spots_array[:, 0] - 1 < dapi_binary.shape[0],
                                                             spots_array[:, 1] - 1 < dapi_binary.shape[1],
                                                             spots_array[:, 2] - 1 < dapi_binary.shape[2])]
        test = dapi_binary[
            (spots_array[:, 0] - 1)[inDAPI_points], (spots_array[:, 1] - 1)[inDAPI_points], (spots_array[:, 2] - 1)[
                inDAPI_points]]
        inx = 0
        for indi, i in enumerate(inDAPI_points):
            if i == True:
                inDAPI_points[indi] = test[inx]
                inx = inx + 1
        spots.loc[inDAPI_points, 'is_noise'] = 0
    else:
        for ii, jj in product(range(sampling_mat.shape[0]), range(sampling_mat.shape[1])):
            if ii % dapi_grid_interval == 1 and jj % dapi_grid_interval == 1:
                sampling_mat[ii, jj] = 1

        dapi_sampled = dapi_binary * sampling_mat
        dapi_coord = np.argwhere(dapi_sampled > 0)

        all_points = np.concatenate((np.array(spots.loc[:, ['spot_location_2', 'spot_location_1']]), dapi_coord),
                                    axis=0)

        # compute neighbors within radius for local density
        knn = NearestNeighbors(radius=xy_radius)
        knn.fit(all_points)
        spots_array = np.array(spots.loc[:, ['spot_location_2', 'spot_location_1']])
        neigh_dist, neigh_array = knn.radius_neighbors(spots_array)

        # global low-density removal
        dis_neighbors = [ii.sum(0) for ii in neigh_dist]
        res_num_neighbors = [ii.shape[0] for ii in neigh_array]

        thresh = np.percentile(dis_neighbors, pct_filter * 100)
        noisy_points = np.argwhere(dis_neighbors < thresh)[:, 0]
        spots['is_noise'] = 0
        spots.loc[noisy_points, 'is_noise'] = -1

        # LOF
        if LOF:
            thresh = np.percentile(res_num_neighbors, 10)
            clf = LocalOutlierFactor(n_neighbors=int(thresh), contamination=contamination)
            spots_array = np.array(spots.loc[:, ['spot_location_2', 'spot_location_1']])
            y_pred = clf.fit_predict(spots_array)
            spots.loc[y_pred == -1, 'is_noise'] = -1

        # spots in DAPI as inliers
        test = dapi_binary[list(spots_array[:, 0] - 1), list(spots_array[:, 1] - 1)]
        spots.loc[test == True, 'is_noise'] = 0

        inDAPI_points = [i[0] and i[1] for i in zip(spots_array[:, 0] - 1 < dapi_binary.shape[0],
                                                    spots_array[:, 1] - 1 < dapi_binary.shape[1])]
        test = dapi_binary[(spots_array[:, 0] - 1)[inDAPI_points], (spots_array[:, 1] - 1)[inDAPI_points]]
        inx = 0
        for indi, i in enumerate(inDAPI_points):
            if i == True:
                inDAPI_points[indi] = test[inx]
                inx = inx + 1
        spots.loc[inDAPI_points, 'is_noise'] = 0

    return (spots)


def preprocess(spots, dapi_binary, xy_radius, dapi_grid_interval=5, LOF=False, contamination=0.1, pct_filter=0.1):
    preprocessing_data(spots, dapi_grid_interval, dapi_binary, LOF, contamination, xy_radius,
                       pct_filter)
    pass


def get_distance_matrix(points):
    points_num = points.shape[0]
    print(points_num)
    distance_matrix = np.zeros((points_num, points_num), dtype=np.float16)
    for i in trange(points_num):
        for j in range(points_num):
            point_i = points[i]
            point_j = points[j]
            distance_matrix[i, j] = (np.sum((point_i - point_j) ** 2))
            pass
    return np.sqrt(distance_matrix)


def NGC(spots, xy_radius, z_radius):
    '''
    Compute the NGC coordinates

    params :    - radius float) = radius for neighbors search
                - num_dim (int) = 2 or 3, number of dimensions used for cell segmentation
                - gene_list (1Darray) = list of genes used in the dataset

    returns :   NGC matrix. Each row is a NGC vector
    '''
    print('NGC')
    if num_dims == 3:
        radius = max(xy_radius, z_radius)
        X_data = np.array(spots[['spot_location_1', 'spot_location_2', 'spot_location_3']])
    else:
        radius = xy_radius
        X_data = np.array(spots[['spot_location_1', 'spot_location_2']])
    knn = NearestNeighbors(radius=radius)
    knn.fit(X_data)
    spot_number = spots.shape[0]
    res_dis, res_neighbors = knn.radius_neighbors(X_data, return_distance=True)
    if num_dims == 3:
        ### remove nearest spots outside z_radius
        if radius == xy_radius:
            smaller_radius = z_radius
        else:
            smaller_radius = xy_radius
        for indi, i in tqdm(enumerate(res_neighbors)):
            res_neighbors[indi] = i[X_data[i, 2] - X_data[indi, 2] <= smaller_radius]
            res_dis[indi] = res_dis[indi][X_data[i, 2] - X_data[indi, 2] <= smaller_radius]

    res_ngc = sparse.lil_matrix((spot_number, len(gene_list)), dtype=np.int8)
    for i in trange(spot_number):
        neighbors_i = res_neighbors[i]
        genes_neighbors_i = spots.loc[neighbors_i, :].groupby('gene').size()
        res_ngc[i, genes_neighbors_i.index.to_numpy() - np.min(gene_list)] = np.array(genes_neighbors_i)
        # res_ngc[i] /= len(neighbors_i)
    return res_ngc


def distance2(x, y):
    x_y = np.array(x - y, dtype=np.float32)
    return np.sqrt(np.sum(np.dot(x_y, x_y)))


def spearman_corr(x, y):
    norm_x = x - np.mean(x)
    norm_y = y - np.mean(y)
    corr = np.sum(np.array(np.dot(norm_x, norm_y), dtype=np.float32))
    s_x = np.sum(np.array(np.dot(norm_x, norm_x), dtype=np.float32))
    s_y = np.sum(np.array(np.dot(norm_y, norm_y), dtype=np.float32))
    corr = corr / np.sqrt(s_x * s_y)
    return corr


# @numba.njit(parallel=True)
def readST(dataset_file=r'stumap/dataset/STARmap_MousePlacenta'):
    with open(os.path.join(dataset_file, 'h_ngc_R.pkl'), 'rb') as f:
        ngc_R = pickle.loads(f.read())
        pass
    with open(os.path.join(dataset_file, 'h_ngc_3R.pkl'), 'rb') as f:
        ngc_3R = pickle.loads(f.read())
        pass
    with open(os.path.join(dataset_file, 'h_ngc_5R.pkl'), 'rb') as f:
        ngc_5R = pickle.loads(f.read())
        pass
    with open(os.path.join(dataset_file, 'h_p.pkl'), 'rb') as f:
        p = pickle.loads(f.read())
        pass
    st_data = []
    msngc = hstack((ngc_R, ngc_3R, ngc_5R), format='lil')
    for i in trange(p.shape[0]):
        st_data.append({'p': p[i], 'msngc': msngc[i, :].toarray()})
        pass
    with open(os.path.join(dataset_file, 'h_st.pkl'), 'wb') as f:
        pickle.dump(st_data, f)
        pass
    return st_data

def main():
    spots, dapi, gene = dataloader_STARmap_human_cardiac_organoid()
    # spots, dapi, gene, label = dataloader_STARmap_MousePlacenta()
    data = np.array(spots)
    Physical_coordinates = data[:, :2]
    Gene_list = data[:, 2]

    # distance_matrix = get_distance_matrix(Physical_coordinates)
    # set radius parameters
    # 设置超参数
    fast_preprocess = False
    gauss_blur = False
    sigma = 1
    dapi_binary, dapi_stacked = binarize_dapi(dapi, fast_preprocess, gauss_blur, sigma)
    num_gene = np.max(spots['gene'])
    gene_list = np.arange(1, num_gene + 1)
    num_dims = len(dapi.shape)
    # pixel
    xy_radius = 10
    z_radius = 7

    # find the noise points
    pct_filter = 0.1
    print('start preprocess')
    preprocess(spots, dapi_binary, xy_radius, pct_filter=pct_filter)
    print('end preprocess')
    spots['is_noise'] = spots['is_noise'] + 1
    spots['is_noise'] = spots['is_noise'] - np.min(spots['is_noise']) - 1
    min_spot_per_cell = 5
    cell_num_threshold = 0.001
    dapi_grid_interval = 4
    add_dapi = True
    use_genedis = True
    spots_denoised = spots.loc[spots['is_noise'] == 0, :].copy()
    if 'level_0' in spots.columns:
        spots_denoised = spots_denoised.drop('level_0', axis=1)
    spots_denoised.reset_index(inplace=True)
    print(f'After denoising, mRNA spots: {spots_denoised.shape[0]}')
    ngc_R = NGC(spots_denoised, xy_radius, z_radius)
    ngc_3R = NGC(spots_denoised, xy_radius * 3, z_radius * 3)
    ngc_5R = NGC(spots_denoised, xy_radius * 5, z_radius * 5)
    print(f'NGC shape is ' + str(ngc_R.shape))
    with open('h_ngc_R.pkl', 'wb') as f:
        pickle.dump(ngc_R, f)
        pass
    with open('h_ngc_3R.pkl', 'wb') as f:
        pickle.dump(ngc_3R, f)
        pass
    with open('h_ngc_5R.pkl', 'wb') as f:
        pickle.dump(ngc_5R, f)
        pass
    with open('h_p.pkl', 'wb') as f:
        pickle.dump(spots_denoised[['spot_location_3', 'spot_location_2', 'spot_location_1']].values, f)
        pass

if __name__ == '__main__':
    # main()
    pass
    # with open('ngc_R.pkl', 'rb') as f:
    #     ngc_R = pickle.loads(f.read())
