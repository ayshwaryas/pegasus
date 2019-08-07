import time
import numpy as np
import pandas as pd

from scipy.sparse import issparse
from scipy.stats import entropy, chi2
from sklearn.neighbors import NearestNeighbors
from joblib import effective_n_jobs
from typing import List, Tuple


def calculate_nearest_neighbors(
    X: np.array,
    num_threads: int,
    method: str = 'hnsw',
    K: int = 100,
    M: int = 20,
    efC: int = 200,
    efS: int = 200,
    random_state: int = 0,
    full_speed: int = False,
):
    """Calculate nearest neighbors
    X is the sample by feature matrix
    Return K -1 neighbors, the first one is the point itself and thus omitted.
    TODO: Documentation
    """

    nsample = X.shape[0]

    if nsample <= 1000:
        method = 'sklearn'

    if nsample < K:
        print(
            "Warning: in calculate_nearest_neighbors, number of samples = {} < K = {}!\n Set K to {}.".format(
                nsample, K, nsample
            )
        )
        K = nsample

    knn_index = None
    if method == "hnsw":
        import hnswlib

        assert not issparse(X)
        # Build hnsw index
        knn_index = hnswlib.Index(space="l2", dim=X.shape[1])
        knn_index.init_index(
            max_elements=nsample, ef_construction=efC, M=M, random_seed=random_state
        )
        knn_index.set_num_threads(num_threads if full_speed else 1)
        knn_index.add_items(X)

        # KNN query
        knn_index.set_ef(efS)
        knn_index.set_num_threads(num_threads)
        indices, distances = knn_index.knn_query(X, k=K)
        # eliminate the first neighbor, which is the node itself
        for i in range(nsample):
            if indices[i, 0] != i:
                indices[i, 1:] = indices[i, 0:-1]
                distances[i, 1:] = distances[i, 0:-1]
        indices = indices[:, 1:].astype(int)
        distances = np.sqrt(distances[:, 1:])
    else:
        assert method == "sklearn"
        knn = NearestNeighbors(
            n_neighbors=K - 1, n_jobs=num_threads
        )  # eliminate the first neighbor, which is the node itself
        knn.fit(X)
        distances, indices = knn.kneighbors()

    return indices, distances


def neighbors(data: 'AnnData', K: int, rep: 'str' = 'pca', n_jobs: int = -1, random_state: int = 0, full_speed: bool = False) -> Tuple[List[int], List[float]]:
    """
    TODO: Documentation.
    :params. n_jobs, -1 represents to use all cpus
    :params. rep: representation, 'X_' + rep should be in data.obsm
    :params. full_speed: True will result in non-reproducible results
    Return: if cached, may return more neighbors
    """

    start = time.time()

    rep_key = 'X_' + rep
    indices_key = rep + '_knn_indices'
    distances_key = rep + 'knn_distances'

    indices = distances = None
    need_calc = True
    if indices_key in data.uns:
        exist_K = data.uns[indices_key].shape[1] + 1
        if K <= exist_K:
            indices = data.uns[indices_key]
            distances = data.uns[distances_key]
            need_calc = False

    if need_calc:
        indices, distances = calculate_nearest_neighbors(
            data.obsm[rep_key],
            effective_n_jobs(n_jobs),
            K=K,
            random_state=random_state,
            full_speed=full_speed,
        )
        data.uns[indices_key] = indices
        data.uns[distances_key] = distances
    else:
        print("Found cached kNN results, no calculation is required.")

    end = time.time()
    print(
        "Nearest neighbor search is finished in {:.2f}s.".format(end - start)
    )

    return indices, distances


def select_cells(distances, frac, K=25, alpha=1.0, random_state=0):
    """
    TODO: documentation (not user API)
    """

    start_time = time.time()

    nsample = distances.shape[0]

    if K > distances.shape[1]:
        print(
            "Warning: in select_cells, K = {} > the number of calculated nearest neighbors!\nSet K to {}".format(
                K, distances.shape[1]
            )
        )
        K = distances.shape[1]

    probs = np.zeros(nsample)
    if alpha == 0.0:
        probs[:] = 1.0  # uniform
    elif alpha == 1.0:
        probs[:] = distances[:, K - 1]
    else:
        probs[:] = distances[:, K - 1] ** alpha
    probs /= probs.sum()

    np.random.seed(random_state)
    selected = np.zeros(nsample, dtype=bool)
    selected[
        np.random.choice(nsample, size=int(nsample * frac), replace=False, p=probs)
    ] = True

    end_time = time.time()
    print("select_cells finished. Time spent = {:.2}s.".format(end_time - start_time))

    return selected


def calc_kBET_for_one_chunk(knn_indices, attr_values, ideal_dist, K):
    dof = ideal_dist.size - 1

    ns = knn_indices.shape[0]
    results = np.zeros((ns, 2))
    for i in range(ns):
        observed_counts = (
            pd.Series(attr_values[knn_indices[i, :]]).value_counts(sort=False).values
        )
        expected_counts = ideal_dist * K
        stat = np.sum(
            np.divide(
                np.square(np.subtract(observed_counts, expected_counts)),
                expected_counts,
            )
        )
        p_value = 1 - chi2.cdf(stat, dof)
        results[i, 0] = stat
        results[i, 1] = p_value

    return results


def calc_kBET(
    data: 'AnnData',
    attr: str,
    rep: str = 'pca',
    K: int = 25,
    alpha: float = 0.05,
    n_jobs: int = -1,
    random_state: int = 0,
    temp_folder: str = None,
) -> Tuple[float, float, float]:
    """
    TODO: Documentation
    This kBET metric is based on paper "A test metric for assessing single-cell RNA-seq batch correction" [M. Büttner, et al.] in Nature Methods, 2018.

    :return:
        stat_mean: average chi-square statistic over all the data points.
        pvalue_mean: average p-value over all the data points.
    """
    assert attr in data.obs
    if data.obs[attr].dtype.name != 'category':
        data.obs[attr] = pd.Categorical(data.obs[attr])

    from joblib import Parallel, delayed
    

    ideal_dist = (
        data.obs[attr].value_counts(normalize=True, sort=False).values
    )  # ideal no batch effect distribution
    nsample = data.shape[0]
    nbatch = ideal_dist.size

    attr_values = data.obs[attr].values.copy()
    attr_values.categories = range(nbatch)

    indices, distances = neighbors(
        data, K, rep = rep, n_jobs=n_jobs, random_state=random_state
    )
    knn_indices = np.concatenate(
        (np.arange(nsample).reshape(-1, 1), indices[:, 0 : K - 1]), axis=1
    )  # add query as 1-nn

    # partition into chunks
    n_jobs = min(effective_n_jobs(n_jobs), nsample)
    starts = np.zeros(n_jobs + 1, dtype=int)
    quotient = nsample // n_jobs
    remainder = nsample % n_jobs
    for i in range(n_jobs):
        starts[i + 1] = starts[i] + quotient + (1 if i < remainder else 0)

    kBET_arr = np.concatenate(
        Parallel(n_jobs=n_jobs, max_nbytes=1e7, temp_folder=temp_folder)(
            delayed(calc_kBET_for_one_chunk)(
                knn_indices[starts[i] : starts[i + 1], :], attr_values, ideal_dist, K
            )
            for i in range(n_jobs)
        )
    )

    res = kBET_arr.mean(axis=0)
    stat_mean = res[0]
    pvalue_mean = res[1]
    accept_rate = (kBET_arr[:, 1] >= alpha).sum() / nsample

    return (stat_mean, pvalue_mean, accept_rate)


def calc_kSIM(
    data: 'AnnData', attr: str, rep: str ='pca', K: int =25, min_rate: float = 0.9, n_jobs: int = -1, random_state: int = 0
) -> Tuple[float, float]:
    """
    TODO: Documentation.
    This kSIM metric measures if attr are not diffused too much
    """
    assert attr in data.obs
    nsample = data.shape[0]

    indices, distances = neighbors(
        data, K, rep, n_jobs=n_jobs, random_state=random_state
    )
    knn_indices = np.concatenate(
        (np.arange(nsample).reshape(-1, 1), indices[:, 0 : K - 1]), axis=1
    )  # add query as 1-nn

    labels = np.reshape(data.obs[attr].values[knn_indices.ravel()], (-1, K))
    same_labs = labels == labels[:, 0].reshape(-1, 1)
    correct_rates = same_labs.sum(axis=1) / K

    kSIM_mean = correct_rates.mean()
    kSIM_accept_rate = (correct_rates >= min_rate).sum() / nsample

    return (kSIM_mean, kSIM_accept_rate)


# def calc_JSD(P, Q):
#     M = (P + Q) / 2
#     return (entropy(P, M, base = 2) + entropy(Q, M, base = 2)) / 2.0


# def calc_kBJSD_for_one_datapoint(pos, attr_values, knn_indices, ideal_dist):
#     idx = np.append(knn_indices[pos], [pos])
#     empirical_dist = pd.Series(attr_values[idx]).value_counts(normalize = True, sort = False).values
#     return calc_JSD(ideal_dist, empirical_dist)


# def calc_kBJSD(data, attr, rep_key = 'X_pca', K = 25, n_jobs = 1, random_state = 0, temp_folder = None):
#     assert attr in data.obs
#     if data.obs[attr].dtype.name != 'category':
#         data.obs[attr] = pd.Categorical(data.obs[attr])

#     from joblib import Parallel, delayed

#     ideal_dist = data.obs[attr].value_counts(normalize = True, sort = False).values # ideal no batch effect distribution
#     nsample = data.shape[0]
#     nbatch = ideal_dist.size

#     attr_values = data.obs[attr].values.copy()
#     attr_values.categories = range(nbatch)

#     indices, distances = neighbors(data, K, rep_key, n_jobs = n_jobs, random_state = random_state)
#     knn_indices = indices[:, 0 : K - 1]
#     kBJSD_arr = np.array(Parallel(n_jobs = 1, max_nbytes = 1e7, temp_folder = temp_folder)(delayed(calc_kBJSD_for_one_datapoint)(i, attr_values, knn_indices, ideal_dist) for i in range(nsample)))

#     return kBJSD_arr.mean()
