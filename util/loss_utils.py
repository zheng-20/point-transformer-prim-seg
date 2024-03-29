from collections import Counter
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
# from utils.main_utils import npy, v
# from scipy.optimize import linear_sum_assignment
from sklearn.cluster import KMeans
from functools import wraps
import time
from lapsolver import solve_dense
from torch.autograd import Variable

from lib.pointops.functions import pointops
DIVISION_EPS = 1e-10

def npy(var):
    return var.data.cpu().numpy()

def v(var, cuda=True, volatile=False):
    if type(var) == torch.Tensor or type(var) == torch.DoubleTensor:
        res = Variable(var.float(), volatile=volatile)
    elif type(var) == np.ndarray:
        res = Variable(torch.from_numpy(var), volatile=volatile)
    if cuda:
        res = res.cuda()
    return res

def timing(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        r = func(*args, **kwargs)
        end = time.perf_counter()
        print('{}.{} : {}'.format(func.__module__, func.__name__, end - start))
        return r

    return wrapper


def get_one_hot(targets, nb_classes):
    #res = np.eye(nb_classes)[np.array(targets, dtype=np.int8).reshape(-1)]
    # check none-type
    #if targets.min() == -1:
    #   idx = np.argwhere(targets == -1)
    #   res[idx] = 0
    #return res.reshape(list(targets.shape)+[nb_classes])

    one_hot = nn.functional.one_hot(targets, nb_classes)

    return one_hot


def hungarian_matching(W_pred, W_gt):
    # This non-tf function does not backprob gradient, only output matching indices
    # W_pred - NxK
    # W_gt - NxK'
    # Output: matching_indices
    # The matching does not include gt background instance
    # calculate RIoU
    n_points = W_pred.shape[0]
    #n_max_labels = min(W_gt.shape[1], W_pred.shape[1])
    #matching_indices = np.zeros([n_max_labels], dtype=np.int32)

    dot = np.sum(np.expand_dims(W_pred, axis=2) * np.expand_dims(W_gt, axis=1),
                 axis=0)  # K'xK
    denominator = np.expand_dims(np.sum(W_pred, axis=0),
                                 axis=1) + np.expand_dims(np.sum(W_gt, axis=0),
                                                          axis=0) - dot
    cost = dot / np.maximum(denominator, DIVISION_EPS)  # K'xK
    row_ind, col_ind = solve_dense(-cost)  # want max solution
    #matching_indices[b, :n_gt_labels] = col_ind

    return row_ind, col_ind

def compute_riou(W_pred, W_gt, pred_ind, gt_ind):
    # W_pred - NxK
    # W_gt - NxK'

    N, _ = W_pred.shape

    pred_ind = torch.LongTensor(pred_ind).unsqueeze(0).repeat(N, 1).to(
        W_pred.device)
    gt_ind = torch.LongTensor(gt_ind).unsqueeze(0).repeat(N, 1).to(W_gt.device)

    W_pred_reordered = torch.gather(W_pred, -1, pred_ind)
    W_gt_reordered = torch.gather(W_gt, -1, gt_ind)

    dot = torch.sum(W_gt_reordered * W_pred_reordered, dim=0)  # K
    denominator = torch.sum(W_gt_reordered, dim=0) + torch.sum(
        W_pred_reordered, dim=0) - dot
    mIoU = dot / (denominator + DIVISION_EPS)  # K
    return mIoU

def compute_miou(cluster_pred, I_gt):
    '''
    compute per-primitive riou loss
    cluster_pred: (1, N)
    I_gt: (1, N), must contains -1
    '''
    assert (cluster_pred.shape[0] == 1)

    one_hot_pred = get_one_hot(cluster_pred,
                               cluster_pred.max() + 1)[0]  # (N, K)

    if I_gt.min() == -1:
        one_hot_gt = get_one_hot(I_gt + 1,
                                 I_gt.max() +
                                 2)[0][:, 1:]  # (N, K'), remove background
    else:
        one_hot_gt = get_one_hot(I_gt, I_gt.max() + 1)[0]

    pred_ind, gt_ind = hungarian_matching(npy(one_hot_pred), npy(one_hot_gt))

    riou = compute_riou(one_hot_pred, one_hot_gt, pred_ind, gt_ind)
    k = riou.shape[0]
    mean_riou = riou.sum() / k
    return mean_riou


def compute_type_miou(type_per_point, T_gt, cluster_pred, I_gt):
    '''
    compute per-primitive-instance type iou
    type_per_point: (1, N, K), K = 4
    T_gt: (1, N)
    '''
    assert (type_per_point.shape[0] == 1)
    
    # get T_pred: (1, N)
    if len(type_per_point.shape) == 3:
        B, N, _ = type_per_point.shape
        T_pred = torch.argmax(type_per_point, dim=-1) # (B, N)
    else:
        T_pred = type_per_point

   
    one_hot_pred = get_one_hot(cluster_pred,
                               cluster_pred.max() + 1)[0]  # (N, K)

    if I_gt.min() == -1:
        # (N, K'), remove background
        one_hot_gt = get_one_hot(I_gt + 1,
                                 I_gt.max() + 2)[0][:, 1:]  
    else:
        one_hot_gt = get_one_hot(I_gt, I_gt.max() + 1)[0]

    pred_ind, gt_ind = hungarian_matching(npy(one_hot_pred), npy(one_hot_gt))
    type_iou = torch.Tensor([0.0]).to(T_gt.device)
    cnt = 0
    
    for p_ind, g_ind in zip(pred_ind, gt_ind):
        gt_type_label = T_gt[I_gt == g_ind].mode()[0]
        pred_type_label = T_pred[cluster_pred == p_ind].mode()[0]
        if gt_type_label == pred_type_label:
            type_iou += 1
        cnt += 1
    
    type_iou /= cnt
    return type_iou

def compute_type_miou_abc(type_per_point, T_gt, cluster_pred, I_gt):
    '''
    compute per-primitive-instance type iou
    type_per_point: (1, N, K), K = 6/10
    T_gt: (1, N)
    '''
    assert (type_per_point.shape[0] == 1)
    
    # get T_pred: (1, N)
    if len(type_per_point.shape) == 3:
        B, N, _ = type_per_point.shape
        T_pred = torch.argmax(type_per_point, dim=-1) # (B, N)
    else:
        T_pred = type_per_point
     
    T_pred[T_pred == 6] = 0
    T_pred[T_pred == 7] = 0
    T_pred[T_pred == 9] = 0
    T_pred[T_pred == 8] = 2
    
    T_gt[T_gt == 6] = 0
    T_gt[T_gt == 7] = 0
    T_gt[T_gt == 9] = 0
    T_gt[T_gt == 8] = 2
   
    one_hot_pred = get_one_hot(cluster_pred,
                               cluster_pred.max() + 1)[0]  # (N, K)

    if I_gt.min() == -1:
        # (N, K'), remove background
        one_hot_gt = get_one_hot(I_gt + 1,
                                 I_gt.max() + 2)[0][:, 1:]  
    else:
        one_hot_gt = get_one_hot(I_gt, I_gt.max() + 1)[0]

    pred_ind, gt_ind = hungarian_matching(npy(one_hot_pred), npy(one_hot_gt))
    type_iou = torch.Tensor([0.0]).to(T_gt.device)
    cnt = 0
    riou = compute_riou(one_hot_pred, one_hot_gt, pred_ind, gt_ind)
    for p_ind, g_ind in zip(pred_ind, gt_ind):
        gt_type_label = T_gt[I_gt == g_ind].mode()[0]
        try:
            pred_type_label = T_pred[cluster_pred == p_ind].mode()[0]
        except:
            continue
        if gt_type_label == pred_type_label:
            type_iou += 1
           
        cnt += 1
    
    type_iou /= cnt
    return type_iou

def compute_embedding_loss(pred_feat, gt_label, t_pull=0.5, t_push=1.5):
    '''
    pred_feat: (B, N, K)
    gt_label: (B, N)
    '''
    batch_size, num_pts, feat_dim = pred_feat.shape
    device = pred_feat.device
    pull_loss = torch.Tensor([0.0]).to(device)
    push_loss = torch.Tensor([0.0]).to(device)
    for i in range(batch_size):
        num_class = gt_label[i].max() + 2

        embeddings = []

        for j in range(num_class):
            mask = (gt_label[i] == (j - 1))
            feature = pred_feat[i][mask]
            if len(feature) == 0:
                continue
            embeddings.append(feature)  # (M, K)

        centers = []

        for feature in embeddings:
            center = torch.mean(feature, dim=0).view(1, -1)
            centers.append(center)

        # intra-embedding loss
        pull_loss_tp = torch.Tensor([0.0]).to(device)
        for feature, center in zip(embeddings, centers):
            dis = torch.norm(feature - center, 2, dim=1) - t_pull
            dis = F.relu(dis)
            pull_loss_tp += torch.mean(dis)

        pull_loss = pull_loss + pull_loss_tp / len(embeddings)

        # inter-embedding loss
        centers = torch.cat(centers, dim=0)  # (num_class, K)

        if centers.shape[0] == 1:
            continue

        dst = torch.norm(centers[:, None, :] - centers[None, :, :], 2, dim=2)

        eye = torch.eye(centers.shape[0]).to(device)
        pair_distance = torch.masked_select(dst, eye == 0)

        pair_distance = t_push - pair_distance
        pair_distance = F.relu(pair_distance)
        push_loss += torch.mean(pair_distance)

    pull_loss = pull_loss / batch_size
    push_loss = push_loss / batch_size
    loss = pull_loss + push_loss
    return loss, pull_loss, push_loss

def k_means(p, num_class=2):
    b, N, c = p.shape

    IDX = torch.zeros(b, N).to(p.device).long()

    for i in range(b):
        cur_feat = npy(p[i])
        kmeans = KMeans(n_clusters=num_class, random_state=0).fit(cur_feat)
        IDX[i] = v(kmeans.labels_)

    return IDX

def compute_normal_loss(pred, gt):

    b, N, _ = pred.shape
    normal_loss = torch.acos((pred * gt).sum(-1).clamp(-0.99, 0.99))

    normal_loss = normal_loss.sum() / (b * N)

    return normal_loss

def compute_type_loss(pred, gt):
    '''
    pred: (B, N, K)
    gt: (B, N)
    '''

    type_loss = nn.CrossEntropyLoss()
    valid_class = (gt != -1)  # remove background
    gt = gt[valid_class]

    pred = pred[valid_class]

    loss = type_loss(pred, gt)
    
    return loss

def compute_nnl_loss(pred, gt):
    '''
    pred: (B, N, K)
    gt: (B, N)
    '''

    type_loss = nn.NLLLoss()
    valid_class = (gt != -1)  # remove background
    gt = gt[valid_class]

    pred = pred[valid_class]

    loss = type_loss(pred, gt)
    
    return loss


def compute_instance_loss(pred, gt):
    '''
    pred: (B, N, K)
    gt: (B, N)
    '''
    type_loss = nn.CrossEntropyLoss()
    valid_class = (gt != -1)  # remove background

    gt = gt[valid_class]

    pred = pred[valid_class]

    loss = type_loss(pred, gt)
    
    return loss

def compute_param_loss(pred, T_gt, T_param_gt):
    '''
    only add loss to corresponding type
    pred: (B, N, 22)
    T_gt: (B, N)
    T_param_gt: (B, N, 22)
    '''
    param_list = {5:[0,4], 1:[4,8], 4:[8,15], 3:[15,22]}

    #[0, 4, 8, 15, 22]

    b, N, _ = pred.shape
    
    #l2_loss = nn.MSELoss(reduction='sum')
    l2_loss = nn.MSELoss()

    total_loss = 0
    length = 0
    cnt = 0
    for b in range(pred.shape[0]):
        for i in [1, 4, 5, 3]:
            index = T_gt[b] == i
            tmp_pred = pred[b][index]
            tmp_gt = T_param_gt[b][index]

            if tmp_pred.shape[0] == 0:
                continue
            if tmp_gt.sum() == 0: # no parameters to process
                continue

            tmp_pred = tmp_pred[:, param_list[i][0]:param_list[i][1]]
            tmp_gt = tmp_gt[:, param_list[i][0]:param_list[i][1]].float()
            
            valid_mask = tmp_gt.sum(1) != 0

            tmp_pred = tmp_pred[valid_mask]
            tmp_gt = tmp_gt[valid_mask]
            
            if tmp_gt.shape[0] == 0:
                continue

            tmp_loss = l2_loss(tmp_pred, tmp_gt)

            # ignore wrong type label 
            if tmp_gt.max() > 10 or tmp_loss > 50: 
                continue

            total_loss += tmp_loss
            
            length += tmp_pred.shape[0]
            cnt += 1

    #TODO: only happened in test phase
    if cnt == 0:
        if torch.isnan(l2_loss(tmp_pred, tmp_gt.float())).sum() > 0:
            return torch.Tensor([0.0]).to(T_gt.device)
        return l2_loss(tmp_pred, tmp_gt.float())

    total_loss = total_loss / cnt

    return total_loss

def compute_boundary_loss_v2(p, features, target):
    total_loss = torch.Tensor([0.0]).to(features.device)
    cnt = 0
    batch_size, _, _ = features.shape
    # nsample = 8
    # neighbor_idx = pointops.knnquery(nsample, p, p)
    # nsample -= 1
    for i in range(batch_size):
    #     if target[i].min() == -1:
    #         labels = get_one_hot(target[i] + 1,
    #                                     target[i].max() + 2)
    #     else:
    #         labels = get_one_hot(target[i], target[i].max() + 1)

    #     neighbor_idx_i = neighbor_idx[i][..., 1:].contiguous()  # [m, nsample-1]
    #     m = neighbor_idx_i.shape[0]

    #     neighbor_label = labels[neighbor_idx_i.view(-1).long(), :].view(m, nsample, labels.shape[1]) # (m, nsample, ncls)
    #     neighbor_feature = features[i][neighbor_idx_i.view(-1).long(), :].view(m, nsample, features.shape[2])

    #     labels = torch.argmax(torch.unsqueeze(labels, -2), -1)  # [m, 1]
    #     neighbor_label = torch.argmax(neighbor_label, -1)  # [m, nsample]
    #     posmask = labels == neighbor_label  # [m, nsample]

    #     point_mask = torch.sum(posmask.int(), -1)  # (m)
    #     point_mask = torch.logical_and(0 < point_mask, point_mask < nsample)    # 边界点mask

    #     if not torch.any(point_mask):
    #         loss = .0
    #         total_loss += loss
    #         cnt += 1
    #         continue


        num_class = target[i].max() + 2

        boundary_embeddings = []
        embeddings = []

        for j in range(num_class):
            mask = (target[i] == (j - 1))
            feature = features[i][mask]
            if len(feature) == 0:
                continue
            search_p = p[i][mask] #搜索当前patch的边界点
            search_p = torch.unsqueeze(search_p, dim=0)
            p_i = torch.unsqueeze(p[i], dim=0)
            nsample = 4
            neighbor_idx = pointops.knnquery(nsample, p_i, search_p)
            # neighbor_idx_i = neighbor_idx[..., 1:].contiguous()
            neighbor_label = target[i][neighbor_idx.view(-1).long()].view(-1, nsample)
            posmask = neighbor_label == j-1
            point_mask = torch.sum(posmask.int(), -1)
            point_mask = point_mask < nsample

            # if not torch.any(point_mask):
            #     loss = .0
            #     total_loss += loss
            #     cnt += 1
            #     continue

            boundary_feature = feature[point_mask]

            boundary_embeddings.append(boundary_feature)  # (M, K)
            embeddings.append(feature)  # (M, K)

        centers = []

        for feature in embeddings:
            center = torch.mean(feature, dim=0).view(1, -1)
            centers.append(center)
        
        pull_loss_tp = torch.Tensor([0.0]).to(features.device)
        for feature, center in zip(boundary_embeddings, centers):
            if len(feature) == 0:
                pull_loss_tp += .0
                continue
            dis = torch.norm(feature - center, 2, dim=1) - 0.5
            dis = F.relu(dis)
            pull_loss_tp += torch.mean(dis)

        total_loss = total_loss + pull_loss_tp / len(boundary_embeddings)

    total_loss = total_loss / batch_size
    return total_loss


def compute_boundary_loss(p, features, target):
    
    total_loss = torch.Tensor([0.0]).to(features.device)
    cnt = 0
    batch_size, _, _ = features.shape
    nsample = 8
    neighbor_idx = pointops.knnquery(nsample, p, p)
    nsample -= 1
    for i in range(batch_size):
        # labels = F.one_hot(target, target.max()+1).float()
        if target[i].min() == -1:
            labels = get_one_hot(target[i] + 1,
                                        target[i].max() + 2)
        else:
            labels = get_one_hot(target[i], target[i].max() + 1)

        neighbor_idx_i = neighbor_idx[i][..., 1:].contiguous()  # [m, nsample-1]
        m = neighbor_idx_i.shape[0]

        neighbor_label = labels[neighbor_idx_i.view(-1).long(), :].view(m, nsample, labels.shape[1]) # (m, nsample, ncls)
        neighbor_feature = features[i][neighbor_idx_i.view(-1).long(), :].view(m, nsample, features.shape[2])

        labels = torch.argmax(torch.unsqueeze(labels, -2), -1)  # [m, 1]
        neighbor_label = torch.argmax(neighbor_label, -1)  # [m, nsample]
        posmask = labels == neighbor_label  # [m, nsample]

        point_mask = torch.sum(posmask.int(), -1)  # (m)
        point_mask = torch.logical_and(0 < point_mask, point_mask < nsample)    # 边界点mask

        if not torch.any(point_mask):
            loss = .0
            total_loss += loss
            cnt += 1
            continue

        posmask = posmask[point_mask]
        features_i = features[i][point_mask]    # 边界点的特征
        neighbor_feature = neighbor_feature[point_mask] # 边界点的邻域点的特征

        # dist_l2
        dist = torch.unsqueeze(features_i, -2) - neighbor_feature
        dist = torch.sqrt(torch.sum(dist ** 2, axis=-1) + 1e-12) # [m, nsample]

        # # dist_kl
        # features_i = F.log_softmax(features_i, dim=-1)
        # features_i = features_i.unsqueeze(-2)
        # features_i = features_i.expand([neighbor_feature.shape[0], nsample, 128])
        # neighbor_feature = F.log_softmax(neighbor_feature, dim=-1)
        # dist = F.kl_div(neighbor_feature, features_i, reduction='none', log_target=True)
        # dist = F.softmax(neighbor_feature, dim=-1) * (F.log_softmax(neighbor_feature, dim=-1) - F.log_softmax(features_i, dim=-1))  
        # dist = dist.sum(-1) # 边界点与邻域点的kl散度度量

        # compute loss
        dist = -dist
        dist = dist - torch.max(dist, -1, keepdim=True)[0]  # NOTE: max return both (max value, index)

        dist = dist / 1
        exp = torch.exp(dist)

        # if invalid_mask is not None:
        #     valid_mask = 1 - invalid_mask
        #     exp = exp * valid_mask

        # softnn
        pos = torch.sum(exp * posmask, axis=-1)  # (m)
        neg = torch.sum(exp, axis=-1)  # (m)
        loss = -torch.log(pos / neg + 1e-12)

        # # nce
        # neg = torch.sum(exp * (~posmask), axis=-1)  # (m)
        # pos = torch.sum(exp * posmask, axis=-1)  # (m)
        # exp = torch.sum(exp, axis=-1)  # (m)
        # loss = (pos / (exp + neg) + 1e-12)
        # loss = -torch.log(loss)

        loss = torch.mean(loss)

        total_loss += loss
        cnt += 1
    
    total_loss = total_loss / cnt

    return total_loss

def compute_boundary_detect_loss(pred, gt):
    '''
    pred: (B, N, K)
    gt: (B, N)
    '''

    gt_np = gt.cpu().numpy()
    N_sum = 0
    P_sum = 0
    for i in range(gt_np.shape[0]):
        count = Counter(gt_np[i])
        for k, v in count.items():
            if k == 0:
                N_sum += v
            if k == 1:
                P_sum += v
    
    gt = gt.to(torch.int64)
    gt = F.one_hot(gt).float()
    pos_weight = torch.full_like(gt[0], N_sum/P_sum)

    bound_loss = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    loss = bound_loss(pred, gt)

    return loss