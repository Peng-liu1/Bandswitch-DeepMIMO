#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Jun  4 16:08:03 2019

@author: farismismar
"""

import random
import os
import numpy as np
import pandas as pd

import itertools
import xgboost as xgb
from sklearn.metrics import roc_auc_score, roc_curve, auc
from sklearn.model_selection import GridSearchCV
from sklearn.metrics import confusion_matrix

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.ticker as tick
from matplotlib.ticker import MultipleLocator, FuncFormatter

os.chdir('/Users/farismismar/Desktop/DeepMIMO')

# 0) Some parameters
seed = 0
K_fold = 3
learning_rate = 0.01
max_users = 54481
r_exploitation = 0.3

r_training = 0.8

rate_threshold_35 = 4.3
rate_threshold_28 = 2.9

PTX_35 = 1 # in Watts for 3.5 GHz
PTX_28 = 1 # in Watts for 28 GHz

p_blockage = 0.5
blockage_loss = 40 # in dB

delta_f_35 = 180e3 # Hz/subcarrier
delta_f_28 = 180e3 # Hz/subcarrier
N_SC_35 = 1
N_SC_28 = 1

B_35 = N_SC_35 * delta_f_35
B_28 = N_SC_28 * delta_f_28
Nf = 7 # dB noise fig.

k_B = 1.38e-23 # Boltzmann
T = 290 # Kelvins

# 1) Read the data
# Add a few lines to caputre the seed for reproducibility.
random.seed(seed)
np.random.seed(seed)

def create_dataset():
    # Takes the two .csv files and merges them in a way that is useful for the Deep Learning.
    df35 = pd.read_csv('dataset/dataset_3.5_GHz.csv')
    df28 = pd.read_csv('dataset/dataset_28_GHz.csv')
    
    # Truncate to the first max_users rows, for efficiency for now
    df35 = df35.iloc[:max_users,:]
    df28 = df28.iloc[:max_users,:]
    
    # Map: 0 is ID; 1-256 are H real; 257-512 are Himag; 513-515 are x,y,z 
    
    # 2) Perform data wrangling and construct the proper channel matrix H
    H35_real = df35.iloc[:,1:256+1]
    H35_imag = df35.iloc[:,257:512+1]
    H35_loc = df35.iloc[:,513:]
    
    H28_real = df28.iloc[:,1:256+1]
    H28_imag = df28.iloc[:,257:512+1]
    H28_loc = df28.iloc[:,513:]

    # 2.5) We decided to slash the 3.5 Matrix to a UPA of 8x8 in the y-z plane.
    # Following the procedure
    H35_real_8 = []
    H35_imag_8 = []
    
    for user_id in np.arange(max_users):
        H35_real_i = H35_real.iloc[user_id, 0:256]
        H35_real_i = np.array(H35_real_i).reshape(32,8).T
        H35_real_i = H35_real_i.flatten()
        H35_real_i = H35_real_i[0:64]
        H35_imag_i = H35_imag.iloc[user_id, 0:256]
        H35_imag_i = np.array(H35_imag_i).reshape(32,8).T
        H35_imag_i = H35_imag_i.flatten()
        H35_imag_i = H35_imag_i[0:64]
        
        H35_real_8.append(H35_real_i)
        H35_imag_8.append(H35_imag_i)
        
    # Convert to pandas df    
    H35_real_8 = pd.DataFrame(H35_real_8)
    H35_imag_8 = pd.DataFrame(H35_imag_8)
    
    # Before moving forward, check if the loc at time t is equal
    assert(np.all(df35.iloc[:,513:516] == df28.iloc[:,513:516]))
    
    # Reset the column names of the imaginary H
    H35_imag.columns = H35_real.columns
    H28_imag.columns = H28_real.columns
    
    H35 = H35_real + 1j * H35_imag
    H28 = H28_real + 1j * H28_imag
    
    del H35_real, H35_imag, H35_loc, H28_real, H28_imag, H28_loc
    
    channel_gain_35 = []
    channel_gain_28 = []
    
    # Compute the channel gain |h|^2
    for i in np.arange(max_users):
        H35_i = np.array(H35.iloc[i,:])
        H28_i = np.array(H28.iloc[i,:])
        channel_gain_35.append(PTX_35 * np.vdot(H35_i, H35_i))
        channel_gain_28.append(PTX_28 * np.vdot(H28_i, H28_i))
    
    # 3) Feature engineering: introduce RSRP mmWave and sub-6 and y
    channel_gain_28 = np.array(channel_gain_28).astype(float)
    channel_gain_35 = np.array(channel_gain_35).astype(float)
    
    # introduce blockage as Bernoulli rando variable
    p = np.randon.binomial(1, p=p_blockage)
    channel_gain_28 *= 10 ** (p * blockage_loss/10.)
        
    noise_floor_35 = k_B * T * delta_f_35
    noise_floor_28 = k_B * T * delta_f_28
    
    noise_power_35 = 10 ** (Nf/10.) * noise_floor_35
    noise_power_28 = 10 ** (Nf/10.) * noise_floor_28
    
    # Get rid of unwanted columns in 3.5
    df35 = df35[['0', '513', '514', '515']]
    df35.columns = ['t', 'lon', 'lat', 'height']
    df35 = pd.concat([df35, H35_real_8, H35_imag_8], axis=1)
    
    df35.loc[:,'Capacity_35'] = B_35 * np.log2(1 + PTX_35 * 4 * channel_gain_35 / noise_power_35) / 1e6
    df28.loc[:,'Capacity_28'] = B_35 * np.log2(1 + PTX_28 * channel_gain_28 / noise_power_28) / 1e6
    
    # These columns are redundant
    df28.drop(['0', '513', '514', '515'], axis=1, inplace=True)
    df = pd.concat([df35, df28], axis=1)
    
    df.to_csv('dataset.csv', index=False)

def plot_confusion_matrix(y_test, y_pred, y_score):
    # Compute confusion matrix
    classes = [0,1]
    class_names = ['Deny','Grant']
    normalize = False
    
    cm = confusion_matrix(y_test, y_pred)
    np.set_printoptions(precision=2)
    
    # Plot non-normalized confusion matrix
    plt.figure(figsize=(8,5))
    
    plt.rc('text', usetex=True)
    plt.rc('font', family='serif')
    matplotlib.rcParams['text.usetex'] = True
    matplotlib.rcParams['font.size'] = 16
    matplotlib.rcParams['text.latex.preamble'] = [
        r'\usepackage{amsmath}',
        r'\usepackage{amssymb}']
    
    plt.imshow(cm, interpolation='nearest', cmap=plt.cm.Blues)
    plt.colorbar()
    tick_marks = np.arange(len(classes))
    plt.xticks(tick_marks, class_names, rotation=0)
    plt.yticks(tick_marks, class_names)
    
    fmt = '.2f' if normalize else 'd'
    thresh = cm.max() / 2.
    for i, j in itertools.product(range(cm.shape[0]), range(cm.shape[1])):
        plt.text(j, i, format(cm[i, j], fmt),
                 horizontalalignment="center",
                 color="white" if cm[i, j] > thresh else "black")
    
    plt.ylabel('True label')
    plt.xlabel('Predicted label')
    
    plt.tight_layout()
    plt.savefig('figures/conf_matrix.pdf', format='pdf')
    
    # Compute area under ROC curve
    roc_auc = roc_auc_score(y_test, y_score[:,1])
    print('The ROC AUC for this UE is {0:.6f}'.format(roc_auc))

def plot_roc(y_test, y_score, i=0):
    # Compute ROC curve and ROC area
    fpr, tpr, _ = roc_curve(y_test, y_score)
    roc_auc = auc(fpr, tpr)
    
    plt.figure(figsize=(8,5))
    
    plt.rc('text', usetex=True)
    plt.rc('font', family='serif')
    matplotlib.rcParams['text.usetex'] = True
    matplotlib.rcParams['font.size'] = 16
    matplotlib.rcParams['text.latex.preamble'] = [
        r'\usepackage{amsmath}',
        r'\usepackage{amssymb}']   

    lw = 2
    
    plt.plot(fpr, tpr,
         lw=lw, label="ROC curve (AUC = {:.6f})".format(roc_auc))
    
    plt.rc('text', usetex=True)
    plt.rc('font', family='serif')
    plt.plot([0, 1], [0, 1], color='black', lw=lw, linestyle='--')
    plt.xlim([0.0, 1.0])
    plt.ylim([0.0, 1.05])
    plt.grid()
    plt.xlabel('False Positive Rate')
    plt.ylabel('True Positive Rate')
#    plt.title(r'\textbf{Receiver Operating Characteristic -- UE \#' + '{0}'.format(i) + '}')
    plt.legend(loc="lower right")
    plt.savefig('figures/roc_{0}.pdf'.format(i), format='pdf')

def train_classifier(df):
    dataset = df.copy()    
    N_training = int(r_training * dataset.shape[0])
        
    training = dataset.iloc[:N_training,:]
    test = dataset.iloc[N_training:,:]
    
    eps = 1e-9
    X_train = training.drop('y', axis=1)
    y_train = training['y']
    X_test = test.drop('y', axis=1)
    y_test = test['y']

    w = len(y_train[y_train == 0]) / (eps + len(y_train[y_train == 1]))
    
    print('Positive class weight: {}'.format(w))
    
    classifier = xgb.XGBClassifier(seed=seed, learning_rate=0.05, n_estimators=1000, max_depth=8, scale_pos_weight=w, silent=True)
    #classifier.get_params().keys()
    
    # Hyperparameters
    alphas = np.linspace(0,1,2)
    lambdas = np.linspace(0,1,2)
    sample_weights = [0.5, 0.7]
    child_weights = [0, 10]
    objectives = ['binary:logistic']
    gammas = [0, 0.02, 0.04]
    
    hyperparameters = {'reg_alpha': alphas, 'reg_lambda': lambdas, 'objective': objectives, 
                       'colsample_bytree': sample_weights, 'min_child_weight': child_weights, 'gamma': gammas}
  
    gs_xgb = GridSearchCV(classifier, hyperparameters, scoring='roc_auc', cv=K_fold) # k-fold crossvalidation
    gs_xgb.fit(X_train, y_train)
    clf = gs_xgb.best_estimator_
    
    y_pred = clf.predict(X_test)
    y_score = clf.predict_proba(X_test)

    try:
        roc_auc = roc_auc_score(y_test, y_score[:,1])    
        print('The Training ROC AUC for this UE is {:.6f}'.format(roc_auc))
    except:
        print('The Training ROC AUC for this UE is N/A')

    return [y_pred, y_score, clf]

def predict_handover(df, clf):
    y_test = df['y']
    X_test = df.drop(['y'], axis=1)
    
    y_pred = clf.predict(X_test)
    y_score = clf.predict_proba(X_test)
    
    try:
        # Compute area under ROC curve
        roc_auc = roc_auc_score(y_test, y_score[:,1])
        print('The ROC AUC for this UE in the exploitation period is {:.6f}'.format(roc_auc))
    
        # Save the value
        f = open("figures/output.txt", 'a')
        f.write('ROC exploitation: {0},{1:.3f}\n'.format(r_exploitation, roc_auc))
        f.close()

        y_pred=pd.DataFrame(y_pred)
      
    except:
       print('The ROC AUC for this UE in the exploitation period is N/A')
       y_pred = None
       
    return y_pred

#create_dataset() # only uncomment for the first run.
df_ = pd.read_csv('dataset.csv')

# Dataset column names
# 0 user ID
# 1-3  (x,y,z) of user ID
# 4-67 real H35
# 68-131 imag 35
# 132 capacity rate_35 in MHz
# 133-388 real H28
# 389-644 imag H28
# 645 capacity_rate_28 in MHz

df = df_.iloc[:max_users,:]
del df_

df = df[['Capacity_35', 'Capacity_28', 'lon', 'lat', 'height']]

# َQ: Is there a correlation between the channels?

# Problem I am in 3.5 GHz (src) and want to HO to 28 GHz (target)
# Assume the measurement gap is 1 second, therefore, try to predict the future 28[t + 1]

# Create a negative lag and difference signals
df.loc[:,'Capacity_28_t+1'] = df.loc[:,'Capacity_28'].shift(-1)
df.loc[:,'Capacity_28_dt+1'] = df.loc[:,'Capacity_28'].diff(-1)

# Drop the NA data (a single unwanted row)
df.dropna(inplace=True)

# Define the HO criterion here.
df['y'] = pd.DataFrame((df.loc[:,'Capacity_35'] <= rate_threshold_35) & (df.loc[:,'Capacity_28_t+1'] >= rate_threshold_28), dtype=int)

# Change the order of columns to put 
column_order = ['lon', 'lat', 'Capacity_35', 'Capacity_28', 'Capacity_28_t+1', 'Capacity_28_dt+1', 'y']
df = df[column_order]

# Use this for the exploitation
N_exploit = int(r_exploitation * max_users)
train = df.iloc[:N_exploit,:]

[y_pred, y_score, clf] = train_classifier(train)

benchmark_data = df.iloc[N_exploit:,:]
y_pred_proposed = predict_handover(benchmark_data, clf)
y_score_proposed = clf.predict_proba(benchmark_data.drop(['y'], axis=1))
y_test_proposed = benchmark_data['y']

plot_confusion_matrix(y_test_proposed, y_pred_proposed, y_score_proposed)