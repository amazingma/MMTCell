import pandas as pd
import numpy as np
import torch
import torch.nn.functional as F
import scanpy as sc
import anndata as ad
from model import scTrans_model as create_model


def todense(adata):
    import scipy
    if isinstance(adata.X, scipy.sparse.csr_matrix) or isinstance(adata.X, scipy.sparse.csc_matrix):
        return adata.X.todense()
    else:
        return adata.X


def get_weight(att_mat,pathway):
    att_mat = torch.stack(att_mat).squeeze(1)
    att_mat = torch.mean(att_mat, dim=1)
    residual_att = torch.eye(att_mat.size(1))
    aug_att_mat = att_mat + residual_att
    aug_att_mat = aug_att_mat / aug_att_mat.sum(dim=-1).unsqueeze(-1)
    joint_attentions = torch.zeros(aug_att_mat.size())
    joint_attentions[0] = aug_att_mat[0]

    for n in range(1, aug_att_mat.size(0)):
        joint_attentions[n] = torch.matmul(aug_att_mat[n], joint_attentions[n-1])

    v = joint_attentions[-1]
    v = pd.DataFrame(v[0, 1:].detach().numpy()).T
    v.columns = pathway
    return v


def prediect(adata, model_weight_path, project, mask_path='', laten=False, save_att='X_att', save_lantent='X_lat',
             n_step=10000, cutoff=0.1, n_unannotated=1, batch_size=50, embed_dim=60, depth=2, num_heads=4):  # # embed_dim
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    num_genes = adata.shape[1]
    mask_path_1 = '.' + project + '/mask1.npy'
    mask1 = np.load(mask_path_1)
    mask_path_2 = '.' + project + '/mask2.npy'
    mask2 = np.load(mask_path_2)
    mask_path_3 = '.' + project + '/mask3.npy'
    mask3 = np.load(mask_path_3)
    mask_path_4 = '.' + project + '/mask4.npy'
    mask4 = np.load(mask_path_4)
    project_path = '.' + project
    pathway = pd.read_csv(project_path+'/pathway1.csv', index_col=0)
    dictionary = pd.read_table(project_path+'/label_dictionary.csv', sep=',', header=0, index_col=0)
    n_c = len(dictionary)
    label_name = dictionary.columns[0]
    dictionary.loc[(dictionary.shape[0])] = 'Unknown'
    dic = {}
    for i in range(len(dictionary)):
        dic[i] = dictionary[label_name][i]
    model = create_model(num_classes=n_c, num_genes=num_genes, mask_1=mask1, mask_2=mask2, mask_3=mask3, mask_4=mask4,
                         has_logits=False, depth=depth, num_heads=num_heads).to(device)
    model.load_state_dict(torch.load(model_weight_path, map_location=device))
    model.eval()

    all_line = adata.shape[0]
    n_line = 0
    n_batch = 0
    adata_list = []
    while n_line <= all_line:
        if (all_line-n_line) % batch_size != 1:
            expdata = pd.DataFrame(todense(adata[n_line:n_line+min(n_step, (all_line-n_line))]),
                                   index=np.array(adata[n_line:n_line+min(n_step, (all_line-n_line))].obs_names).tolist(),
                                   columns=np.array(adata.var_names).tolist())
            n_line = n_line+n_step
        else:
            expdata = pd.DataFrame(todense(adata[n_line:n_line+min(n_step, (all_line-n_line-2))]),
                                   index=np.array(adata[n_line:n_line+min(n_step, (all_line-n_line-2))].obs_names).tolist(),
                                   columns=np.array(adata.var_names).tolist())
            n_line = (all_line-n_line-2)
        expdata = np.array(expdata)
        expdata = torch.from_numpy(expdata.astype(np.float32))
        data_loader = torch.utils.data.DataLoader(expdata, batch_size=batch_size, shuffle=False, pin_memory=True)
        with torch.no_grad():
            for step, data in enumerate(data_loader):
                exp = data
                lat, pre, weights, _ = model(exp.to(device))
                # np.save('./log/weights_' + str(n_batch) + '.npy', weights.detach().cpu().numpy())
                pre = torch.squeeze(pre).cpu()
                pre = F.softmax(pre, 1)
                predict_class = np.empty(shape=0)
                pre_class = np.empty(shape=0)
                for i in range(len(pre)):
                    if torch.max(pre, dim=1)[0][i] >= cutoff:
                        predict_class = np.r_[predict_class, torch.max(pre, dim=1)[1][i].numpy()]
                    else:
                        predict_class = np.r_[predict_class, n_c]
                    pre_class = np.r_[pre_class, torch.max(pre, dim=1)[0][i]]
                l_p = torch.squeeze(lat).cpu().numpy()
                att = torch.squeeze(weights).cpu().numpy()
                meta = np.c_[predict_class, pre_class]
                meta = pd.DataFrame(meta)
                meta.columns = ['Prediction', 'Probability']
                meta.index = meta.index.astype('str')
                if laten:
                    l_p = l_p.astype('float32')
                    new = sc.AnnData(l_p, obs=meta)
                else:
                    att = att[:, 0:(len(pathway)-n_unannotated)]
                    att = att.astype('float32')
                    varinfo = pd.DataFrame(pathway.iloc[0:len(pathway)-n_unannotated, 0].values,
                                           index=pathway.iloc[0:len(pathway)-n_unannotated, 0], columns=['pathway_index'])
                    new = sc.AnnData(att, obs=meta, var=varinfo)
                adata_list.append(new)
                n_batch += 1
    new = ad.concat(adata_list, index_unique='__')
    new.obs.index = adata.obs.index
    new.obs['Prediction'] = new.obs['Prediction'].map(dic)
    result = new.obs['Prediction']
    return result