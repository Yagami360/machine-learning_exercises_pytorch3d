import os
import numpy as np
import json

import torch
import torch.nn as nn
import torch.nn.functional as F

from data.tailornet_dataset import TailornetDataset

from utils.utils import load_checkpoint

#--------------------------------
# utils
#--------------------------------
# Lists the indices of joints which affect the deformations of particular garment
VALID_THETA = {
    't-shirt': [0, 1, 2, 3, 6, 9, 12, 13, 14, 16, 17, 18, 19],
    'old-t-shirt': [0, 1, 2, 3, 6, 9, 12, 13, 14, 16, 17, 18, 19],
    'shirt': [0, 1, 2, 3, 6, 9, 12, 13, 14, 16, 17, 18, 19, 20, 21],
    'pant': [0, 1, 2, 4, 5, 7, 8],
    'skirt' : [0, 1, 2, ],
}

def mask_thetas(thetas, cloth_type):
    """
    thetas: shape [N, 72]
    cloth_type: e.g. t-shirt
    """
    valid_theta = VALID_THETA[cloth_type]
    mask = torch.zeros_like(thetas).view(-1, 24, 3)
    mask[:, valid_theta, :] = 1.
    mask = mask.view(-1, 72)
    return thetas * mask

def mask_betas(betas, cloth_type):
    """
    betas: shape [N, 10]
    cloth_type: e.g. t-shirt
    """
    valid_beta = [0, 1]
    mask = torch.zeros_like(betas)
    mask[:, valid_beta] = 1.
    return betas * mask

def mask_gammas(gammas, cloth_type):
    """
    gammas: shape [N, 4]
    cloth_type: e.g. t-shirt
    """
    valid_gamma = [0, 1]
    mask = torch.zeros_like(gammas)
    mask[:, valid_gamma] = 1.
    gammas = gammas * mask
    if cloth_type == 'old-t-shirt':
        gammas = gammas + torch.tensor(
            [[0., 0., 1.5, 0.]], dtype=torch.float32, device=gammas.device)
    return gammas

def mask_inputs(thetas, betas, gammas, cloth_type):
    if thetas is not None:
        thetas = mask_thetas(thetas, cloth_type)
    if betas is not None:
        betas = mask_betas(betas, cloth_type)
    if gammas is not None:
        gammas = mask_gammas(gammas, cloth_type)
    return thetas, betas, gammas


#--------------------------------
# MLP 層
#--------------------------------
class FullyConnected(nn.Module):
    def __init__(self, input_size, output_size, hidden_size=1024, num_layers=None):
        super(FullyConnected, self).__init__()
        net = [
            nn.Linear(input_size, hidden_size),
            nn.ReLU(inplace=True),
            nn.Dropout(p=0.2),
        ]
        for i in range(num_layers - 2):
            net.extend([
                nn.Linear(hidden_size, hidden_size),
                nn.ReLU(inplace=True),
            ])
        net.extend([
            nn.Linear(hidden_size, output_size),
        ])
        self.net = nn.Sequential(*net)

    def forward(self, x):
        return self.net(x)

#--------------------------------
# TailorNet のサブネットワーク
#--------------------------------
class TailorNetLF(nn.Module):
    """
    LF（低周波）メッシュ生成器
    """
    def __init__(self, params, n_verts ):
        super(TailorNetLF, self).__init__()
        self.params = params
        self.cloth_type = params['garment_class']

        self.mlp = FullyConnected(
            input_size = 72+10+4, output_size = n_verts, 
            hidden_size = params['hidden_size'] if 'hidden_size' in params else 1024, 
            num_layers = params['num_layers'] if 'num_layers' in params else 3
        )
        return

    def forward(self, thetas, betas, gammas):
        thetas, betas, gammas = mask_inputs(thetas, betas, gammas, cloth_type=self.cloth_type)
        pred_verts = self.mlp(torch.cat((thetas, betas, gammas), dim=1))
        return pred_verts


class TailorNetHF(nn.Module):
    """
    HF（高周波）メッシュ生成器
    """
    def __init__(self, params, n_verts ):
        super(TailorNetHF, self).__init__()
        self.params = params
        self.cloth_type = params['garment_class']

        self.mlp = FullyConnected(
            input_size = 72, output_size = n_verts, 
            hidden_size = params['hidden_size'] if 'hidden_size' in params else 1024, 
            num_layers = params['num_layers'] if 'num_layers' in params else 3
        )
        return

    def forward(self, thetas, betas=None, gammas=None):
        #print( "[{}] thetas.shape={}".format(self.__class__.__name__, thetas.shape) )
        thetas = mask_thetas(thetas=thetas, cloth_type=self.cloth_type)
        #print( "[thetas] sum={}".format(torch.sum(thetas)) )    # sum= -0.34928515553474426
        pred_verts = self.mlp(thetas)
        #print( "[pred_verts] sum={}".format(torch.sum(pred_verts)) )    # sum=0.7465648651123047
        return pred_verts

class TailorNetSS2G(nn.Module):
    """
    ϕ=(γ,β) から頂点変位 D への写像を行う MLP / MLP(beta, gammas) / ss2g
    """
    def __init__(self, params, n_verts ):
        super(TailorNetSS2G, self).__init__()
        self.params = params
        self.cloth_type = params['garment_class']

        self.mlp = FullyConnected(
            input_size = 10+4, output_size = n_verts, 
            hidden_size = params['hidden_size'] if 'hidden_size' in params else 1024, 
            num_layers = params['num_layers'] if 'num_layers' in params else 3
        )
        return

    def forward(self, thetas=None, betas=None, gammas=None):
        _, betas, gammas = mask_inputs(None, betas, gammas, self.cloth_type)
        pred_verts = self.mlp(torch.cat((betas, gammas), dim=1))
        return pred_verts


#--------------------------------
# TailorNet
#--------------------------------
class TailorNet(nn.Module):
    def __init__(self, tailornet_dataset_dir, load_checkpoints_dir, cloth_type = "old-t-shirt", gender = "female", kernel_sigma = 0.01, device = torch.device("cpu"), debug = False ):
        super(TailorNet, self).__init__()
        self.tailornet_dataset_dir = tailornet_dataset_dir
        self.load_checkpoints_dir = load_checkpoints_dir
        self.cloth_type = cloth_type
        self.gender = gender
        self.kernel_sigma = kernel_sigma
        self.device = device
        self.debug = debug

        # 
        dataset = TailornetDataset( dataset_dir = tailornet_dataset_dir, cloth_type = cloth_type, gender = gender, shape_style_pair_list = "pivots.txt", debug = debug )
        self.basis = dataset.unpose_v.to(self.device)

        #------------------
        # lf layers
        #------------------
        # load parames
        file_path = os.path.join(load_checkpoints_dir, "{}_{}_weights/tn_orig_lf/{}_{}".format(cloth_type, gender, cloth_type, gender), 'params.json')
        if( os.path.exists(file_path) ):
            with open(file_path) as f:
                params = json.load(f)
        else:
            with open(os.path.join(load_checkpoints_dir, "tn_orig_baseline/t-shirt_female", 'params.json')) as f:
                params = json.load(f)

        self.tailornet_lf = TailorNetLF(params=params, n_verts=dataset.unpose_v.shape[1] * 3).to(device)

        # load checkpoints
        file_path = os.path.join(load_checkpoints_dir, "{}_{}_weights/tn_orig_lf/{}_{}".format(cloth_type, gender, cloth_type, gender), 'lin.pth.tar')
        if( os.path.exists(file_path) ):
            load_checkpoint(self.tailornet_lf.mlp, device, file_path )

        #------------------
        # hf layers
        #------------------
        self.tailornet_hfs = []
        for shape_idx, style_idx in dataset.shape_style_pairs:
            # load parames
            file_path = os.path.join(load_checkpoints_dir, "{}_{}_weights/tn_orig_hf/{}_{}".format(cloth_type, gender, cloth_type, gender), "{}_{}".format(shape_idx,style_idx), 'params.json')
            if( os.path.exists(file_path) ):
                with open(file_path) as f:
                    params = json.load(f)
            else:
                with open(os.path.join(load_checkpoints_dir, "tn_orig_baseline/t-shirt_female", 'params.json')) as f:
                    params = json.load(f)

            # define HF Layer
            tailornet_hf = TailorNetHF(params=params, n_verts=dataset.unpose_v.shape[1] * 3).to(device)

            # load checkpoints
            file_path = os.path.join(load_checkpoints_dir, "{}_{}_weights/tn_orig_hf/{}_{}".format(cloth_type, gender, cloth_type, gender), "{}_{}".format(shape_idx,style_idx), 'lin.pth.tar')
            if( os.path.exists(file_path) ):
                load_checkpoint(tailornet_hf.mlp, device, file_path)

            self.tailornet_hfs.append(tailornet_hf)

        #------------------
        # ϕ=(γ,β) から頂点変位 D への写像を行う MLP
        #------------------
        # load parames
        file_path = os.path.join(load_checkpoints_dir, "{}_{}_weights/tn_orig_ss2g/{}_{}".format(cloth_type, gender, cloth_type, gender), 'params.json')
        if( os.path.exists(file_path) ):
            with open(file_path) as f:
                params = json.load(f)
        else:
            with open(os.path.join(load_checkpoints_dir, "tn_orig_baseline/t-shirt_female", 'params.json')) as f:
                params = json.load(f)

        self.tailornet_ss2g = TailorNetSS2G(params=params, n_verts=dataset.unpose_v.shape[1] * 3).to(device)

        # load checkpoints
        file_path = os.path.join(load_checkpoints_dir, "{}_{}_weights/tn_orig_ss2g/{}_{}".format(cloth_type, gender, cloth_type, gender), 'lin.pth.tar')
        if( os.path.exists(file_path) ):
            load_checkpoint(self.tailornet_ss2g.mlp, device, file_path)

        if( debug ):
            print( "len(self.tailornet_hfs)", len(self.tailornet_hfs) )

        return

    def __repr__(self):
        hf_str = ""
        for tailornet_hf in self.tailornet_hfs:
             hf_str += str(tailornet_hf) + "\n"

        return self.__class__.__name__ + '\n' + str(self.tailornet_lf) + '\n' + hf_str + "\n" + str(self.tailornet_ss2g)

    def eval(self):
        self.tailornet_lf.eval()
        _ = [ tailornet_hf.eval() for tailornet_hf in self.tailornet_hfs ]
        self.tailornet_ss2g.eval()
        return

    def forward(self, betas, thetas, gammas):
        batch_size = thetas.shape[0]

        # 高周波形状を計算
        pred_disp_hf_pivot = torch.stack([
            tailornet_hf.forward(thetas, betas, gammas).view(batch_size, -1, 3) for tailornet_hf in self.tailornet_hfs
        ]).transpose(0, 1)
        #print( "pred_disp_hf_pivot : ", pred_disp_hf_pivot )
        #print( "pred_disp_hf_pivot.shape : ", pred_disp_hf_pivot.shape)     # torch.Size([1, 20, V, 3])
        #print( "[pred_disp_hf_pivot] sum = {}".format(torch.sum(pred_disp_hf_pivot)) )    # sum = 18.1131591796875

        # 高周波形状をカーネル関数で重み付け和
        pred_disp_hf = self.interp4(thetas, betas, gammas, pred_disp_hf_pivot, sigma = self.kernel_sigma )
        #print( "pred_disp_hf.shape : ", pred_disp_hf.shape)                 # torch.Size([1, V, 3])
        #print( "pred_disp_hf : ", pred_disp_hf )
        #print( "[pred_disp_hf] sum = {}".format(torch.sum(pred_disp_hf)) )    # sum = 0.03325019031763077 -> nan

        # 低周波形状を計算
        pred_disp_lf = self.tailornet_lf.forward(thetas, betas, gammas).view(batch_size, -1, 3)
        #print( "pred_disp_lf : ", pred_disp_lf )

        # 低周波成分 + 高周波成分
        pred_disp = pred_disp_lf + pred_disp_hf
        return pred_disp

    def interp4(self, thetas, betas, gammas, pred_disp_pivot, sigma=0.5):
        """
        RBF カーネル関数で重み付けして混合した高周波形状を計算
        """
        # disp for given shape-style in canon pose
        bs = pred_disp_pivot.shape[0]
        rest_verts = self.tailornet_ss2g.forward(betas=betas, gammas=gammas).view(bs, -1, 3)
        #print( "rest_verts : ", rest_verts )        
        #print( "self.basis : ", self.basis )
        #print( "rest_verts.shape : ", rest_verts.shape )
        #print( "self.basis.shape : ", self.basis.shape )
        #print( "sum(rest_verts) : ", torch.sum(rest_verts) )    # tensor(-46.7538)
        #print( "sum(self.basis) : ", torch.sum(self.basis) )    # tensor(-769.6298)

        # distance of given shape-style from pivots in terms of displacement
        # difference in canon pose
        dist = rest_verts.unsqueeze(1) - self.basis.unsqueeze(0)
        dist = (dist ** 2).sum(-1).mean(-1) * 1000.
        #print( "dist : ", dist )

        # compute normalized RBF distance
        weight = torch.exp(-dist/sigma)
        #print( "weight : ", weight )
        weight = weight / weight.sum(1, keepdim=True)

        # interpolate using weights
        pred_disp = (pred_disp_pivot * weight.unsqueeze(-1).unsqueeze(-1)).sum(1)
        return pred_disp
