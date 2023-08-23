import os
import glob
import json
import torch
import math
import numpy as np
import enum
from tqdm import tqdm
import sklearn.metrics as mets
from types import SimpleNamespace
import matplotlib.pyplot as plt
from tqdm import trange
from tensorboard.backend.event_processing import event_accumulator

## ---------- MAGIC NUMBERS ---------- ##

# TODO: move all magic numbers to this
IMG_SIZE = 256



## ---------- Paths to important directories ---------- ##


paths = SimpleNamespace(
    OCCS = '/your/path/here/',
    SHPFILES = '/your/path/here/',
    MODELS = '/your/path/here/',
    IMAGES = '/your/path/here/',
    RASTERS = '/your/path/here/',
    BASELINES = '/your/path/here/',
    RESULTS = '/your/path/here/',
    MISC = '/your/path/here/',
    DOCS = '/your/path/here/',
    SCRATCH = '/your/path/here/',
    RUNS = '/your/path/here/',
    BLOB_ROOT = '/your/path/here/')


# have to usethe slow european image, us image got removed finally 'https://naipblobs.blob.core.windows.net/', 

## ---------- Base class for function type checking enum ---------- ##

# hacky enum class to enable 
# enums to call functions.
# only dot operator and bracket
# operator work. Parens operator will fail
class FuncEnum(enum.Enum):
    def __call__(self, *args):
        return self.value(*args)
    
# overrides uniterpretable error 
# messages for missing dot or 
# bracket operators
class MetaEnum(enum.EnumMeta):
    def __getitem__(cls, name):
        
        if name in cls._member_map_.values():
            return name
        elif name in cls._member_map_.keys(): 
            return cls._member_map_[name] 
        else:
            raise ValueError("%r is not a valid %s" % (name, cls.__qualname__))
    def __getattr__(cls, name):
        if enum._is_dunder(name):
            raise AttributeError(name)
        if name in cls._member_map_.keys():
            return cls._member_map_[name]
        else:
            raise ValueError("%r is not a valid %s" % (name, cls.__qualname__))
    def valid(self):
        return self._member_names_
 
    
## ---------- File loading ---------- ##

def setup_pretrained_dirs():
    if not os.path.exists(f"{paths.MODELS}/pretrained/"):
        os.makedirs(f"{paths.MODELS}/pretrained/")
    return f"{paths.MODELS}/pretrained/"

def get_tfevent(cfg):
    # get all the possible tfEvent files
    files = glob.glob(f'{paths.RUNS}/*{cfg.exp_id}*')
    # figure out which tfEvent corresponds to the model
    # grab a random checkpoint for model
    model = f"{paths.MODELS}{cfg.model}_{cfg.loss}/{cfg.exp_id}_lr{str(cfg.lr).split('.')[-1]}_e10.tar"
    model_time = os.path.getmtime(model)
    # get tfEvent file with closest timestamp to model checkpoint
    filetimes = {file : abs(os.path.getmtime(file)-model_time) for file in files}
    filetimes =sorted(filetimes.items(), key=lambda x: x[1])
    return filetimes[0]

## ---------- Tensorboard helper methods ---------- ##

def extract_test_accs(cfg, n_test_obs):
    # get all the possible tfEvent files
    file, filetime = get_tfevent(cfg)
    # open up the tfEvent file
    ea = event_accumulator.EventAccumulator(file, size_guidance={ 
        event_accumulator.SCALARS: 0,})
    ea.Reload()
    tags = ea.Tags()['scalars']
    # extract accuracies (easy)
    tags = {met.split('test/')[-1] : [k.value for k in ea.Scalars(met)] for met in tags if 'loss' not in met}
    
    # extract losses (less easy)
    losses = [t for t in ea.Tags()['scalars'] if ('loss' in t) and ('test/' in t)]
    for lname in losses:
        loss = ea.Scalars(lname)
        # some models don't use all the losses
        # so throw out the ones that don't
        # only really relevant for the inception baseline
        if loss[0] == 0.0:
            continue
        batchsize = loss[1].step
        assert batchsize == cfg.batchsize, 'config and tfEvent dont match up!'
        nbatches = math.ceil(n_test_obs/batchsize)
        nepochs = len(loss)// nbatches
        suloss = [] 
        # collate the loss (summed per-epoch)
        for i in range(0, len(loss), nbatches):
            cur_loss = [loss[j].value for j in range(i, i+nbatches)]
            suloss.append(sum(cur_loss))
        tags[lname.split('test/')[-1]] = suloss
    return tags
    
def extract_train_time(cfg, n_obs, epoch):
    # get all the possible tfEvent files
    file, filetime = get_tfevent(cfg)
    # open up the tfEvent file
    ea = event_accumulator.EventAccumulator(file, size_guidance={ 
        event_accumulator.SCALARS: 0,})
    ea.Reload()
    # get the overall loss scalar (guaranteed all models have)
    loss = ea.Scalars('train/tot_loss')
    batchsize = loss[1].step
    nbatches = math.ceil(n_obs/batchsize)
    nepochs = len(loss)// nbatches
    # get the difference in seconds of last batch of training
    # for current epoch from start of training
    return loss[(nbatches*(epoch+1))-1].wall_time - loss[0].wall_time
    
def get_mean_epoch(tags):
    epochs = []
    # set up dict for each possible epoch to store what metrics are maxed when
    mets = tags.keys()
    # remove micro + weighted binary acc entries (uncessary)
    # also remove mAP (same as PRC_AUC!) 
    # and extra losses (only keeping total loss)
    mets = [met for met in mets if  ('weighted' not in met) and ('micro' not in met) and ('loss' not in met) and ('mAP' != met) and ('top' not in met)]
    # add back just in total loss and one topK accuracy
    mets.append('tot_loss')
    mets.append('top30_accuracy')
    print(f" using these metrics: {mets}")
    for met in mets:
        curr = tags[met]
        # pair epochs and accuracy
        paired = zip(range(len(curr)), curr)
        # sort pairs by max accuracy
        spaired = sorted(paired, key=lambda x: x[1])
        # if it's a loss, want the minimizer
        # else want the maximizer for accuracies        
        epoch, val = spaired[0] if 'loss' in met else spaired[-1]
        epochs.append(epoch)
    # return average best epoch
    return int(np.mean(epochs))


## ---------- Data manipulation ---------- ##

# https://stackoverflow.com/questions/2659900/slicing-a-list-into-n-nearly-equal-length-partitions
def partition(lst, n):
    division = len(lst) / n
    return [lst[round(division * i):round(division * (i + 1))] for i in range(n)]

# https://stackoverflow.com/questions/312443/how-do-you-split-a-list-into-evenly-sized-chunks
def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def dict_key_2_index(df, key):
    return {
        k:v for k, v in
        zip(df[key].unique(), np.arange(len(df[key].unique())))
    }

# more clear version of uniform scaling
# https://stackoverflow.com/questions/5294955/how-to-scale-down-a-range-of-numbers-with-a-known-min-and-max-value
def scale(x, min_=None, max_=None, out_range=(-1,1)):

    if min_ == None and max_ == None:
        min_, max_ = np.min(x), np.max(x)
    return ((out_range[1]-out_range[0])*(x-min_))/(max_-min_)+out_range[0]

## ---------- plotting functions ---------- ##

# Based on: https://github.com/dominikjaeckle/Color2D
# https://stackoverflow.com/questions/41966600/matplotlib-colormap-with-two-parameter/41967868#41967868
class ColorMap2D:
    def __init__(self, filename=None, xmin=None, xmax=None, ymin=None, ymax=None, transformation=None):
        self._colormap_file = filename or "./bremm.png"
        
        self._img = plt.imread(self._colormap_file)
        if transformation == 'flip':
            self._img = np.flip(self._img, axis=0)
        elif transformation == 'flip and transpose':
            self._img = np.transpose(np.flip(np.rollaxis(self._img, 0, 1), axis=1), axes=[1,0,2])
        
        self._width = self._img.shape[1]
        self._height = self._img.shape[0]

        self.xmin, self.xmax = xmin, xmax
        self.ymin, self.ymax = ymin, ymax

    def _scale(self, u: float, u_min: float, u_max: float) -> float:
        return ((u + 1) - (u_min + 1)) / ((u_max + 1) - (u_min + 1))

    def _scale_x(self, x: float) -> int:
        val = self._scale(x, self._range_x[0], self._range_x[1])
        # clip extreme values to range
        val  = max((val, 0))
        val  = min((val, 1))
        return int(val * (self._width - 1))

    def _scale_y(self, y: float) -> int:
        val = self._scale(y, self._range_y[0], self._range_y[1])
        val  = max((val, 0))
        val  = min((val, 1))
        return int(val * (self._height - 1))

    def __call__(self, X):
        assert len(X.shape) == 2
        # allow for self-defined range
        if self.xmin is None:
            self.xmin = X[:, 0].min()
        if self.xmax is None:
            self.xmax = X[:, 0].max()
        if self.ymin is None:
            self.ymin = X[:, 1].min()
        if self.ymax is None:
            self.ymax = X[:, 1].max()
        self._range_x = (self.xmin, self.xmax)
        self._range_y = (self.ymin, self.ymax)

        output = np.zeros((X.shape[0], 3))
        for i in trange(X.shape[0]):
            x, y = X[i, :]
            xp = self._scale_x(x)
            yp = self._scale_y(y)
            output[i, :] = self._img[xp, yp]
        return output


## ---------- Accuracy metrics ---------- ##

# calculates intersection of two tensors
# assumed y_t is already in pres/abs form
def pres_intersect(ob_t, y_t):
    # only where both have the same species keep
    sum_ = ob_t + y_t
    int_ = sum_ > 1
    return torch.sum(int_, dim=1)

# calculates union of two tensors
def pres_union(ob_t, y_t):
    sum_ = ob_t + y_t
    sum_ = sum_ > 0
    return torch.sum(sum_, dim=1)

def precision_per_obs(ob_t: torch.tensor, y_t: torch.tensor, threshold=.5):
    ob_t = torch.as_tensor(ob_t)
    y_t = torch.as_tensor(y_t)
    # threshold
    ob_t = (ob_t >= threshold).float()
    # get intersection
    top = pres_intersect(ob_t, y_t).float()
    # get # predicted species
    bottom = torch.sum(ob_t, dim=1)
    # divide!
    ans = torch.div(top, bottom)
    # if nan (divide by 0) just set to 0.0
    ans[ans != ans] = 0
    return ans

def recall_per_obs(ob_t, y_t, threshold=.5):
    ob_t = torch.as_tensor(ob_t)
    y_t = torch.as_tensor(y_t)
    # threshold
    ob_t = (ob_t >= threshold).float()
    # get intersection
    top = pres_intersect(ob_t, y_t).float()
    # get # observed species
    bottom = torch.sum(y_t, dim=1)
    ans = torch.div(top, bottom)
    # if nan (divide by 0) just set to 0.0
    # this relies on the assumption that all nans are 0-division
    ans[ans != ans] = 0
    return ans

def accuracy_per_obs(ob_t, y_t, threshold=.5):
    ob_t = torch.as_tensor(ob_t)
    y_t = torch.as_tensor(y_t)
    # threshold
    ob_t = (ob_t >= threshold).float()
    # intersection
    top = pres_intersect(ob_t, y_t).float()
    # union
    bottom = pres_union(ob_t, y_t)
    ans = torch.div(top, bottom)
    # if nan (divide by 0) just set to 0.0
    # this relies on the assumption that all nans are 0-division
    ans[ans != ans] = 0
    return ans

def f1_per_obs(ob_t, y_t, threshold=.5):
    pre = precision_per_obs(ob_t, y_t, threshold)
    rec = recall_per_obs(ob_t, y_t, threshold)
    ans =  2*(pre*rec)/(pre+rec)
    # if denom=0, F1 is 0
    ans[ans != ans] = 0.0
    return ans
    
def zero_one_accuracy(y_true, y_preds, threshold=0.5):
    assert y_preds.min() >= 0.0 and(y_preds.max() <= 1.0), 'predictions must be converted to probabilities!'
    y_obs = y_preds >= threshold
    n_correct = sum([y_obs[i,label] for (i,label) in enumerate(y_true)])
    return n_correct / len(y_true)

    
def obs_topK(ytrue, yobs, K):
    # ytrue should be spec_id, not all_specs_id
    yobs = torch.as_tensor(yobs)
    ytrue = torch.as_tensor(ytrue)
    # convert to probabilities if not done already
    assert yobs.min() >= 0.0 and(yobs.max() <= 1.0), 'predictions must be converted to probabilities!'
    # convert
    tk = torch.topk(yobs, K)
    # compare indices and will be 1 for every row where
    # yob is in topK, 0 else. Summing across all dimensions
    # gives you final sum (since only 1 obs per row)
    perob = (tk[1]== ytrue.unsqueeze(1).repeat(1,K)).sum().item()
    # don't forget to average
    return (perob / len(ytrue)), (tk[1]== ytrue.unsqueeze(1).repeat(1,K))

def species_topK(ytrue, yobs, K):
    assert yobs.shape[1] > 1, "predictions are not multilabel!"
    nspecs = yobs.shape[1]
    yobs = torch.as_tensor(yobs)
    ytrue = torch.as_tensor(ytrue)
    # convert to probabilities if not done already
    if (yobs.min() <= 0.0) or (yobs.max() >= 1.0):
        yobs = torch.sigmoid(yobs)
    # convert
    tk = torch.topk(yobs, K)
    # get all unique species label and their indices
    unq = torch.unique(ytrue, sorted=False, return_inverse=True)
    # make a dict to store the results for each species
    specs = {v.item():[] for v in unq[0]}
    # go through each row and assign it to the corresponding
    # species using the reverse_index item from torch.unique
    for val, row in zip(unq[1], tk[1]):
        specs[unq[0][val.item()].item()].append(row)
    sas = []
    # add every species so csv writing works
    for i in range(nspecs):
        # ignore not present species
        spec = specs.get(i)
        if spec is None:
            sas.append(np.nan)
            continue
        nspecs += 1
        # spoof ytrue for this species
        yt = torch.full((len(spec),K), i)
        # and calculate 'per-obs' accuracy
        sas.append((torch.stack(spec)== yt).sum().item()/len(spec))
    sas = np.array(sas)
    gsas = sas[~np.isnan(sas)]
    sas = np.array(sas)
    gsas = sas[~np.isnan(sas)]
    return (sum(gsas) / len(gsas)), sas


def mean_calibrated_roc_auc_prc_auc(y_true, y_obs, npoints=50):
    assert y_true.shape == y_obs.shape
    assert y_true.shape[1] > 1
    assert y_obs.shape[1] > 1
    roc_auc, prc_auc = [],[]
    for i in range(y_obs.shape[1]):
        ra,pa = calibrated_roc_auc_prc_auc(y_true[:,i], y_obs[:,i])
        roc_auc.append(ra)
        prc_auc.append(pa)
    return roc_auc, prc_auc

# precision: tp / (tp + fp)
# recall, TPR: tp / (tp + fn)
# FPR = FP/(FP+TN)
def calibrated_roc_auc_prc_auc(y_true, y_obs, npoints=50):
    cutoffs = np.linspace(0.0, 1.0, npoints)
    tpr, fpr, pre = [],[],[]
    # ignore when there is no actual present case of this species
    if y_true.sum() == 0:
        return (np.nan, np.nan)
    for i in cutoffs:
        pred = (y_obs >= i).astype(np.short)
        tn, fp, fn, tp = mets.confusion_matrix(y_true, pred).ravel()
        if (tp + fn) > 0:
            tpr.append(tp / (tp + fn))
        else:
            tpr.append(0)
        if (fp+tn) > 0:
            fpr.append(fp/(fp+tn))
        else:
            fpr.append(0)
        if (tp + fp) > 0:
            pre.append(tp / (tp + fp))
        else:
            pre.append(0)
    # precision-recall x=recall, y=precision
    # roc: x= fpr, y= tpr
    return (mets.auc(tpr, pre),  mets.auc(fpr, tpr))    

