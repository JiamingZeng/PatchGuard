import cv2
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
from  torchvision import datasets, transforms, utils

import nets.bagnet
import nets.resnet
from utils.defense_utils import *

import os 
import joblib
import argparse
from tqdm import tqdm
import numpy as np 
from scipy.special import softmax
from math import ceil
import PIL

parser = argparse.ArgumentParser()

parser.add_argument("--model_dir",default='checkpoints',type=str,help="path to checkpoints")
parser.add_argument('--data_dir', default='data', type=str,help="path to data")
parser.add_argument('--dataset', default='imagenette', choices=('imagenette','imagenet','cifar'),type=str,help="dataset")
parser.add_argument("--model",default='bagnet17',type=str,help="model name")
parser.add_argument("--clip",default=-1,type=int,help="clipping value; do clipping when this argument is set to positive")
parser.add_argument("--aggr",default='none',type=str,help="aggregation methods. set to none for local feature")
parser.add_argument("--skip",default=1,type=int,help="number of example to skip")
parser.add_argument("--thres",default=0.0,type=float,help="detection threshold for robust masking")
parser.add_argument("--patch_size",default=-1,type=int,help="size of the adversarial patch")
parser.add_argument("--m",action='store_true',help="use robust masking")
parser.add_argument("--cbn",action='store_true',help="use cbn")

args = parser.parse_args()

MODEL_DIR=os.path.join('.',args.model_dir)
DATA_DIR=os.path.join(args.data_dir,args.dataset)
DATASET = args.dataset
def get_dataset(ds,data_dir):
    if ds in ['imagenette','imagenet']:
        ds_dir=os.path.join(data_dir,'val')
        ds_transforms = transforms.Compose([
                transforms.Resize(256),
                transforms.CenterCrop(224),
                transforms.ToTensor(),
                transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
            ])
        dataset_ = datasets.ImageFolder(ds_dir,ds_transforms)
        class_names = dataset_.classes
    elif ds == 'cifar':
        ds_transforms = transforms.Compose([
            transforms.Resize(192, interpolation=PIL.Image.BICUBIC),
            transforms.ToTensor(),
            transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
        ])
        dataset_ = datasets.CIFAR10(root=data_dir, train=False, download=True, transform=ds_transforms)
        class_names = dataset_.classes
    return dataset_,class_names

val_dataset_,class_names = get_dataset(DATASET,DATA_DIR)
skips = list(range(0, len(val_dataset_), args.skip))
val_dataset = torch.utils.data.Subset(val_dataset_, skips)
val_loader = torch.utils.data.DataLoader(val_dataset, batch_size=8,shuffle=False)

#build and initialize model
device = 'cuda' #if torch.cuda.is_available() else 'cpu'

if args.clip > 0:
    clip_range = [0,args.clip]
else:
    clip_range = None

if 'bagnet17' in args.model:
    model = nets.bagnet.bagnet17(pretrained=True,clip_range=clip_range,aggregation=args.aggr)
    rf_size=17
elif 'bagnet33' in args.model:
    model = nets.bagnet.bagnet33(pretrained=True,clip_range=clip_range,aggregation=args.aggr)
    rf_size=33
elif 'bagnet9' in args.model:
    model = nets.bagnet.bagnet9(pretrained=True,clip_range=clip_range,aggregation=args.aggr)
    rf_size=9


if DATASET == 'imagenette':
    num_ftrs = model.fc.in_features
    model.fc = nn.Linear(num_ftrs, len(class_names))
    model = torch.nn.DataParallel(model)
    checkpoint = torch.load(os.path.join(MODEL_DIR,args.model+'_nette.pth'))
    model.load_state_dict(checkpoint['model_state_dict']) 
    args.patch_size = args.patch_size if args.patch_size>0 else 32     
elif  DATASET == 'imagenet':
    model = torch.nn.DataParallel(model)
    checkpoint = torch.load(os.path.join(MODEL_DIR,args.model+'_net.pth'))
    model.load_state_dict(checkpoint['state_dict'])
    args.patch_size = args.patch_size if args.patch_size>0 else 32 
elif  DATASET == 'cifar':
    num_ftrs = model.fc.in_features
    model.fc = nn.Linear(num_ftrs, len(class_names))
    model = torch.nn.DataParallel(model)
    checkpoint = torch.load(os.path.join(MODEL_DIR,args.model+'_192_cifar.pth'))
    model.load_state_dict(checkpoint['net'])
    args.patch_size = args.patch_size if args.patch_size>0 else 30


rf_stride=8
window_size = ceil((args.patch_size + rf_size -1) / rf_stride)
print("window_size",window_size)

    
model = model.to(device)
model.eval()
cudnn.benchmark = True

accuracy_list=[]
result_list=[]
clean_corr=0

upper_lower_record = np.zeros((2, 10, 2))
flattened = np.zeros((3, 10, 10))
flattened_softmax = np.zeros((3, 10, 10))
count_total = np.zeros((2, 10))
count1 = 0
count0 = 0
count2 = 0
percentage_chart = np.zeros(3)
wrong_images, correct_masks, window_y_ss = [], [], []

invTrans = transforms.Compose([ transforms.Normalize(mean = [ 0., 0., 0. ],
                                                     std = [ 1/0.2023, 1/0.1994, 1/0.2010 ]),
                                transforms.Normalize(mean = [ -0.4914, -0.4822, -0.4465 ],
                                                     std = [ 1., 1., 1. ]),
                               ])

for data,labels in tqdm(val_loader):
    
    data=data.to(device)
    labels = labels.numpy()
    output_clean = model(data).detach().cpu().numpy() # logits
    # output_clean = softmax(output_clean,axis=-1) # confidence
    #output_clean = (output_clean > 0.2).astype(float) # predictions with confidence threshold

    #note: the provable analysis of robust masking is cpu-intensive and can take some time to finish
    #you can dump the local feature and do the provable analysis with another script so that GPU mempry is not always occupied  
    for i in range(len(labels)):
        if args.m:#robust masking
            local_feature = output_clean[i]
            result, lower, upper, wrong_image, correct_mask, window_y_s = provable_masking(local_feature,labels[i],thres=args.thres,window_shape=[window_size,window_size])
            if result == 1:
                upper_lower_record[0][labels[i]][0] += lower 
                upper_lower_record[0][labels[i]][1] += upper 
                count_total[0][labels[i]] += 1
                # flattened[1][labels[i]] += np.mean(output_clean[i],axis=(0,1))
                # flattened_softmax[1][labels[i]] += softmax(np.mean(output_clean[i],axis=(0,1))) 
                # percentage = np.max(output_clean[i], 2)
                # print(percentage)
                count1 += 1
                inv_tensor = invTrans(data[i])
                utils.save_image(inv_tensor, "images/vunlerable_{}.png".format(count1))
            elif result == 2:
                upper_lower_record[1][labels[i]][0] += lower 
                upper_lower_record[1][labels[i]][1] += upper 
                count_total[1][labels[i]] += 1
                # flattened[2][labels[i]] += np.mean(output_clean[i],axis=(0,1)) 
                # flattened_softmax[2][labels[i]] += softmax(np.mean(output_clean[i],axis=(0,1))) 
                # percentage = np.max(output_clean[i], 2)
                # print(data[i].shape)
                inv_tensor = invTrans(data[i])
                utils.save_image(inv_tensor, "images/certified_{}.png".format(count2))
                count2 += 1
            else:
                # flattened[0][labels[i]] += np.mean(output_clean[i],axis=(0,1)) 
                # flattened_softmax[0][labels[i]] += softmax(np.mean(output_clean[i],axis=(0,1))) 
                count0 += 1
            wrong_images.append(wrong_image)
            correct_masks.append(correct_mask)
            window_y_ss.append(window_y_s)
            result_list.append(result)
            clean_pred = masking_defense(local_feature,thres=args.thres,window_shape=[window_size,window_size])
            clean_corr += clean_pred == labels[i]
            
        elif args.cbn:#cbn 
            # note that cbn results reported in the paper is obtained with vanilla BagNet (without provable adversrial training), since
            # the provable adversarial training is proposed in our paper. We will find that our training technique also benifits CBN
            result = provable_clipping(output_clean[i],labels[i],window_shape=[window_size,window_size])
            result_list.append(result)
            clean_pred = clipping_defense(output_clean[i])
            clean_corr += clean_pred == labels[i]   
    acc_clean = np.sum(np.argmax(np.mean(output_clean,axis=(1,2)),axis=1) == labels)
    accuracy_list.append(acc_clean)
    if count1 + count2 > 30:
        break

cases,cnt=np.unique(result_list,return_counts=True)
for i in range(10):
    upper_lower_record[0][i] /= count_total[0][i]
    upper_lower_record[1][i] /= count_total[1][i]

print("upper_lower_record", upper_lower_record)
print("Provable robust accuracy:",cnt[-1]/len(result_list) if len(cnt)==3 else 0)
print("Clean accuracy with defense:",clean_corr/len(result_list))
print("Clean accuracy without defense:",np.sum(accuracy_list)/len(val_dataset))
print("------------------------------")
print("Provable analysis cases (0: incorrect prediction; 1: vulnerable; 2: provably robust):",cases)
print("Provable analysis breakdown",cnt/len(result_list))
print(count_total)
from matplotlib import pyplot as plt
# Creating histogram
fig, ax = plt.subplots(3, 1)
ax[0].hist(wrong_images, bins = [0, 50, 100, 150, 200])
ax[0].set_title("wrong image")
 
# Show plot
ax[1].hist(correct_masks, bins = [800, 1000, 1200, 1400, 1600])
ax[1].set_title("correct image")

ax[2].hist(window_y_ss, bins = [800, 1000, 1200, 1400, 1600])
ax[2].set_title("window_y_star image")

plt.savefig("all.png")

import pandas as pd
df = pd.DataFrame(upper_lower_record[0])
df.to_csv("upper_lower_record_attack_possible.csv")
df = pd.DataFrame(upper_lower_record[1])
df.to_csv("upper_lower_record_certified.csv")
# np.set_printoptions(precision=3)
# print("flattened", flattened[1] / count1)
# df = pd.DataFrame(flattened[1] / count1)
# df.to_csv("flattened.csv")
# print("softmax flattened", flattened_softmax[1] / count1)
# df = pd.DataFrame(flattened_softmax[1] / count1)
# df.to_csv("flattened_softmax.csv")