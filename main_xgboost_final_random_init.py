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
import math

import itertools
import xgboost as xgb
from sklearn.metrics import roc_auc_score, roc_curve
from sklearn.model_selection import train_test_split, GridSearchCV
from sklearn.metrics import confusion_matrix

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.ticker as tick
from matplotlib.ticker import MultipleLocator, FuncFormatter

os.chdir('/Users/farismismar/Desktop/DeepMIMO')

# 0) Some parameters
seed = 0
K_fold = 2
learning_rate = 0.01
max_users = 54481
r_exploitation = 0.4
p_blockage = 0.4

# in Mbps
# [3.8569 3.9503 4.0436 4.1370 4.2303 4.3237 4.4170 4.5104].
rate_threshold = 4.1

# in ms
gap_duration = 1

# in Watts
PTX_35 = 1 # in Watts for 3.5 GHz
PTX_28 = 1 # in Watts for 28 GHz

# speed:
v_s = 25 # km/h not pedestrian, but vehicular speeds.

delta_f_35 = 180e3 # Hz/subcarrier
delta_f_28 = 180e3 # Hz/subcarrier
N_SC_35 = 1
N_SC_28 = 1

mmWave_BW_multiplier = 1.39 # should be 10, but not currently.
B_35 = N_SC_35 * delta_f_35
B_28 = N_SC_28 * delta_f_28 * mmWave_BW_multiplier
Nf = 7 # dB noise fig.

k_B = 1.38e-23 # Boltzmann
T = 290 # Kelvins

N_exploit = int(r_exploitation * max_users)

# 1) Read the data
# Add a few lines to caputre the seed for reproducibility.
random.seed(seed)
np.random.seed(seed)

def create_dataset():
    # Takes the three.csv files and merges them in a way that is useful for the Deep Learning.
    # regenerate the dataset for 3.5 (y,z = 8x4) and 28 (y, z = 64x4)
    df35 = pd.read_csv('dataset/dataset_3.5_GHz.csv')
    df28_b = pd.read_csv('dataset/dataset_28_GHz_blockage.csv')
    df28_nb = pd.read_csv('dataset/dataset_28_GHz.csv')
    
    # Truncate to the first max_users rows, for efficiency for now
    df35 = df35.iloc[:max_users,:]
    df28_b = df28_b.iloc[:max_users,:]
    df28_nb = df28_nb.iloc[:max_users,:]
    
    sub6_Y, sub6_Z = 8, 4
    mmWave_Y, mmWave_Z = 64, 4
    
    # Check that distances are similar
    assert(np.all(df28_b.iloc[:,-3:] == df28_nb.iloc[:,-3:]))
    
    # Based on blocking probability, create df28.
    p_b = np.random.binomial(1, p=p_blockage, size=max_users)
    df28 = df28_b.copy()
    df28.loc[(p_b==1),:] = df28_b.loc[(p_b == 1),:]
    df28.loc[(p_b==0),:] = df28_nb.loc[(p_b == 0),:]
    
    # Map: 0 is ID; 1-YZ+1 are H real; YZ+1-2YZ+1 are Himag; last three are x,y,z 
    # 2) Perform data wrangling and construct the proper channel matrix H
    H35_real = df35.iloc[:,1:(sub6_Y*sub6_Z+1)]
    H35_imag = df35.iloc[:,(sub6_Y*sub6_Z+1):(2*sub6_Y*sub6_Z+1)]
    H35_loc = df35.iloc[:,-3:]
        
    H28_real = df28.iloc[:,1:(mmWave_Y*mmWave_Z+1)]
    H28_imag = df28.iloc[:,(mmWave_Y*sub6_Z+1):(2*mmWave_Y*mmWave_Z+1)]
    H28_loc = df28.iloc[:,-3:]    
       
    # Before moving forward, check if the loc at time t is equal
    df35 = df35.rename(columns={df35.columns[-3]:  'lon', 
                         df35.columns[-2]:  'lat', 
                         df35.columns[-1]:  'height'})

    df28 = df28.rename(columns={df28.columns[-3]:  'lon', 
                         df28.columns[-2]:  'lat', 
                         df28.columns[-1]:  'height'})
    
    assert(np.all(df35.iloc[:,-3:] == df28.iloc[:,-3:]))
    
    # Reset the column names of the imaginary H
    H35_imag.columns = H35_real.columns
    H28_imag.columns = H28_real.columns
    
    H35 = H35_real + 1j * H35_imag
    H28 = H28_real + 1j * H28_imag
    
    del H35_loc, H28_real, H28_imag, H28_loc
    
    F_35 = compute_bf_codebook(M=sub6_Y*sub6_Z, f_c=3.5e9)
    F_28 = compute_bf_codebook(M=mmWave_Y*mmWave_Z, f_c=28e9)
    
    channel_gain_35 = []
    channel_gain_28 = []
    
    # TODO, from here onwards is still not yet complete.
    # Compute the channel gain |h*f|
    # Beamforming is now both vertical and horizontal
    for i in np.arange(max_users):
        h35_i = np.array(H35.iloc[i,:])
        h28_i = np.array(H28.iloc[i,:])
#        H35_i = h35_i.reshape(sub6_Y, sub6_Z)
#        H28_i = h28_i.reshape(mmWave_Y, mmWave_Z)
        
        channel_gain_35.append(compute_optimal_gain_bf_vector(h35_i, F_35))
        channel_gain_28.append(compute_optimal_gain_bf_vector(h28_i, F_28))
    
    # 3) Feature engineering: introduce RSRP mmWave and sub-6 and y
    channel_gain_28 = np.array(channel_gain_28).astype(float)
    channel_gain_35 = np.array(channel_gain_35).astype(float)
    
    # Get rid of unwanted columns in 3.5
    df35 = df35[['0', 'lon', 'lat', 'height']]
    df35.columns = ['user_id', 'lon', 'lat', 'height']
#    df35 = pd.concat([df35, H35_real, H35_imag], axis=1)

    df = df35.copy()    
    df.loc[:,'P_RX_35'] = 10*np.log10(PTX_35 * 1e3 * channel_gain_35)
    df.loc[:,'P_RX_28'] = 10*np.log10(PTX_28 * 1e3 * channel_gain_28)
    
    df = df.iloc[:max_users,:]
    df = df[['user_id', 'lon', 'lat', 'height', 'P_RX_35', 'P_RX_28']]
    df.to_csv('dataset.csv', index=False)
    
    return df

def compute_optimal_gain_bf_vector(h, F):
    M, MK = F.shape

    max_gain = 0

    for code_index in np.arange(MK):
        f_i = F[:,code_index]
        channel_gain = abs(np.vdot(h, f_i)) ** 2
        if (channel_gain > max_gain):
            max_gain = channel_gain
            
    return channel_gain
    
def compute_bf_codebook(M, f_c, k_oversampling=1):
    F = np.zeros([M, M*k_oversampling], dtype=complex) # F is M rows by Mk columns, where M corresponds to the antennas in the horizontal direction

    theta_n = math.pi * np.arange(start=0., stop=1., step=1./(k_oversampling*M))

    for n in np.arange(M*k_oversampling):
        f_n = _compute_bf_vector(f_c, theta_n[n], M)
        F[:,n] = f_n
            
    return F

def _compute_bf_vector(f_c, theta, M_ULA):
    # Create DFT beamforming codebook
    c = 3e8 # speed of light
    wavelength = c / f_c
    
    d = wavelength / 2. # antenna spacing 
    k = 2. * math.pi / wavelength

    exponent = 1j * k * d * math.cos(theta) * np.arange(M_ULA)
    
    f = 1. / math.sqrt(M_ULA) * np.exp(exponent)
    
    return f

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

def generate_roc(y_test, y_score):
    fpr, tpr, _ = roc_curve(y_test, y_score)

    roc_auc_score_value = roc_auc_score(y_test, y_score)
#    print('The ROC AUC for this UE is {0:.6f}'.format(roc_auc_score_value))

    return fpr, tpr, roc_auc_score_value 

def plot_roc(fpr, tpr, roc_auc, i=0):
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
    
def plot_throughput_cdf(T):
    fig = plt.figure(figsize=(10.24, 7.68))
    plt.rc('text', usetex=True)
    plt.rc('font', family='serif')
    matplotlib.rcParams['text.usetex'] = True
    matplotlib.rcParams['font.size'] = 20
    matplotlib.rcParams['text.latex.preamble'] = [
        r'\usepackage{amsmath}',
        r'\usepackage{amssymb}']   
    
    labels = T.columns

    num_bins = 50
    i = 0
    for data in T:
        data_ = T[data]

        counts, bin_edges = np.histogram(data_, bins=num_bins, density=True)
        cdf = np.cumsum(counts) / counts.sum()
        lw = 1 + 0.2*i
        i += 1
        ax = fig.gca()
        if data == 'Optimal':
            style = '--'
        elif data == 'Proposed':
#            lw = 3.5
            style = '+-'
        else:
            style = '-'
        ax.plot(bin_edges[1:], cdf, style, linewidth=lw)
    
    plt.legend(labels, loc="best")    
    plt.grid()
    plt.xlabel('Throughput (Mbps)')
    plt.ylabel('Throughput CDF')
    plt.savefig('figures/throughputs.pdf', format='pdf')
    
def plot_primary(X,Y, title, xlabel, ylabel, filename='plot.pdf'):
    fig = plt.figure(figsize=(10.24,7.68))

    plt.rc('text', usetex=True)
    plt.rc('font', family='serif')
    matplotlib.rcParams['text.usetex'] = True
    matplotlib.rcParams['font.size'] = 20
    matplotlib.rcParams['text.latex.preamble'] = [
        r'\usepackage{amsmath}',
        r'\usepackage{amssymb}']
    
    #plt.title(title)
    plt.xlabel(xlabel)
    
    ax = fig.gca()
    ax.set_autoscaley_on(True)
    
    plot_, = ax.plot(X, Y, 'k^-') #, label='ROC')

#    ax.set_xlim(xmin=0.15, xmax=0.55)
    
    ax.set_ylabel(ylabel)
#    ax.set_ylim(0.99, 1.01)
    
    plt.grid(True)
    fig.tight_layout()
    plt.savefig('figures/{}'.format(filename), format='pdf')
    plt.show()

def train_classifier(df, r_training=0.8):
    dataset = df.copy()
    
#    N_training = int(r_training * dataset.shape[0])
#    training = dataset.iloc[:N_training,:]
#    test = dataset.iloc[N_training:,:]
    
    training, test = train_test_split(dataset, train_size=r_training, random_state=seed)
    
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
        print('The Training ROC AUC for this classifier is {:.6f}'.format(roc_auc))
    except:
        print('The Training ROC AUC for this classifier is N/A')

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

def get_beam_training_time(df, freq=28e9, horiz_beams=32, vertical_beams=8):
    return 10e-3 * horiz_beams * vertical_beams # 10 us in ms per beam.

def get_coherence_time(df, freq):
    c = 299792458 # m/s
    BS_x, BS_y, BS_z = [235.504198, 489.503816, 6]
    np.random.seed(seed)

    n = df.shape[0]    
    
    # Check if freq is mmWave 
    # then the beam coherence time
    # else 
    # OFDM coherence
    # Constant for all users
    
    # Obtain D
    # alpha AoA equivalent random(0, pi) or 30 to 150 degrees
    if (freq > 20e9): # mm-Wave
        D = ((df['lon'] - BS_x) ** 2 + (df['lat'] - BS_y) ** 2 + (df['height'] - BS_z) ** 2) ** 0.5
        Theta_n = 102 / 64. * math.pi/180 # beamwidth approximation for ULA ### 64 antennas in the aziumuth direction # 3 dB BW of antenna
        alpha = np.random.uniform(0, math.pi, size=n)
        T_B = D / (v_s * 1000/3600 * np.sin(alpha)) * Theta_n / 2.
        T = np.array(T_B).mean() * 1e3 # in ms
        print('INFO: Average coherence time for mmWave is {} ms'.format(T))
        return T
    
    if (freq < 20e9): # sub-6
        T = c / (freq * v_s * 1000/3600) * 1e3 
        print('INFO: Coherence time for sub-6 is {} ms'.format(T))
        return T #* np.ones(n) # in ms

#df_ = create_dataset() # only uncomment for the first run, when the channel consideration changed.
df_ = pd.read_csv('dataset.csv')

df = df_.iloc[:max_users,:]
del df_

# Feature engineering: add SNR to the computation:
noise_floor_35 = k_B * T * delta_f_35 * 1e3
noise_floor_28 = k_B * T * delta_f_28 * mmWave_BW_multiplier * 1e3 # in mW

noise_power_35 = 10 ** (Nf/10.) * noise_floor_35
noise_power_28 = 10 ** (Nf/10.) * noise_floor_28 

df['Capacity_35'] = B_35*np.log2(1 + 10**(df['P_RX_35']/10.) / noise_power_35) / 1e6
df['Capacity_28'] = B_28*np.log2(1 + 10**(df['P_RX_28']/10.) / noise_power_28) / 1e6

df = df[['lon', 'lat', 'height', 'Capacity_35', 'Capacity_28']]

# Compute the Effective Achievable Rates
coherence_time_sub6 = get_coherence_time(df, freq=3.5e9)
coherence_time_mmWave = get_coherence_time(df, freq=28e9) 
beam_training_penalty_mmWave = get_beam_training_time(df, freq=28e9, horiz_beams=8, vertical_beams=32)
beam_training_penalty_sub6 = get_beam_training_time(df, freq=2.1e9, horiz_beams=8, vertical_beams=8)

# Write the formulas in Paper
coeff_sub6_no_ho = (coherence_time_sub6 - beam_training_penalty_sub6) / coherence_time_sub6
coeff_mmWave_no_ho = (coherence_time_mmWave - beam_training_penalty_mmWave) / coherence_time_mmWave
coeff_sub6_ho = (coherence_time_sub6 - beam_training_penalty_sub6 - gap_duration) / coherence_time_sub6
coeff_mmWave_ho = (coherence_time_mmWave - beam_training_penalty_mmWave - gap_duration) / coherence_time_mmWave

# ----------------------------------------------------------------------------
# TODO: Problem, initialize UEs randomly between 3.5 and 28 GHz (target)
# ----------------------------------------------------------------------------
df['Source'] = df['Capacity_35'].copy()
df['Target'] = df['Capacity_28'].copy()

##############################################################################
df['Source_is_3.5'] = (df['Source'] == df['Capacity_35']) + 0
df['Source_is_28'] = (df['Source'] == df['Capacity_28']) + 0

df['y'] = pd.DataFrame((df.loc[:,'Source'] < rate_threshold) & (df.loc[:,'Target'] >= df.loc[:,'Source']), dtype=int)

# Change the order of columns to put 
column_order = ['lon', 'lat', 'height', 'Source', 'Target', 'Source_is_3.5', 'Source_is_28', 'y']
df = df[column_order]

##############################################################################
# 1) Optimal algorithm
##############################################################################
df_optimal = df.copy()
df_optimal_ = df.copy()

df_optimal['y'] = pd.DataFrame((df_optimal.loc[:,'Source'] < rate_threshold) & (df_optimal.loc[:,'Target'] >= df_optimal.loc[:,'Source']), dtype=int)

# Now, apply the handover algorithm
# and compute the Effective Achievable Rate
df_optimal.loc[(df_optimal['y'] == 0) & (df_optimal['Source_is_3.5'] == 1), 'Capacity_Optimal'] = df_optimal.loc[(df_optimal['y'] == 0) & (df_optimal['Source_is_3.5'] == 1), 'Source'] * coeff_sub6_no_ho # no handover, the throughput is the source.
df_optimal.loc[(df_optimal['y'] == 0) & (df_optimal['Source_is_28'] == 1), 'Capacity_Optimal'] = df_optimal.loc[(df_optimal['y'] == 0) & (df_optimal['Source_is_28'] == 1), 'Source'] * coeff_mmWave_no_ho # no handover, the throughput is the source.

# create an EAR using _df_optimal
a = df_optimal_.loc[(df_optimal_['y'] == 1) & (df_optimal_['Source_is_3.5'] == 1), 'Target'] * coeff_mmWave_ho
b = df_optimal_.loc[(df_optimal_['y'] == 1) & (df_optimal_['Source_is_28'] == 1), 'Target'] * coeff_sub6_ho
d = pd.DataFrame([a, b]).T
d.fillna(0, axis=1, inplace=True)
df_optimal.loc[df_optimal['y'] == 1, 'Capacity_Optimal'] = d.apply(np.max, axis=1) # Handover takes place at the beginning of the frame and is NOT penalized for the gap.  It choose the max rate.
      
# Sample r_exploit data randomly from df_optimal
benchmark_data_optimal = df_optimal.iloc[np.random.randint(low=0, high=df_optimal.shape[0], size=N_exploit), :]

del df_optimal, a, b, d, df_optimal_

##############################################################################
# 2) Legacy algorithm
##############################################################################
df_legacy = df.copy()

# Now, apply the handover algorithm
# and compute the Effective Achievable Rate
df_legacy.loc[(df_legacy['y'] == 0) & (df_legacy['Source_is_3.5'] == 1), 'Capacity_Legacy'] = df_legacy.loc[(df_legacy['y'] == 0)  & (df_legacy['Source_is_3.5'] == 1), 'Source'] * coeff_sub6_no_ho # no handover, the throughput is the source.
df_legacy.loc[(df_legacy['y'] == 0) & (df_legacy['Source_is_28'] == 1), 'Capacity_Legacy'] = df_legacy.loc[(df_legacy['y'] == 0)  & (df_legacy['Source_is_28'] == 1), 'Source'] * coeff_mmWave_no_ho # no handover, the throughput is the source.

df_legacy.loc[(df_legacy['y'] == 1) & (df_legacy['Source_is_3.5'] == 1), 'Capacity_Legacy'] = df_legacy.loc[(df_legacy['y'] == 1)  & (df_legacy['Source_is_3.5'] == 1), 'Target'] * coeff_sub6_ho # Handover takes place at the beginning of the frame and is penalized for the gap.
df_legacy.loc[(df_legacy['y'] == 1) & (df_legacy['Source_is_28'] == 1), 'Capacity_Legacy'] = df_legacy.loc[(df_legacy['y'] == 1)  & (df_legacy['Source_is_28'] == 1), 'Target'] * coeff_mmWave_ho # Handover takes place at the beginning of the frame and is penalized for the gap.
##

# Sample r_exploit data randomly from df_legacy
benchmark_data_legacy = df_legacy.iloc[np.random.randint(low=0, high=df_legacy.shape[0], size=N_exploit), :]

del df_legacy

##############################################################################
# 3) Blind handover algorithm
##############################################################################
df_blind = df.copy()

df_blind['y'] = pd.DataFrame((df_blind.loc[:,'Source'] <= rate_threshold), dtype=int)

# Now, apply the handover algorithm
# and compute the Effective Achievable Rate
df_blind.loc[(df_blind['y'] == 0) & (df_blind['Source_is_3.5'] == 1), 'Capacity_Blind'] = df_blind.loc[(df_blind['y'] == 0)  & (df_blind['Source_is_3.5'] == 1), 'Source'] * coeff_sub6_no_ho # no handover, the throughput is the source.
df_blind.loc[(df_blind['y'] == 0) & (df_blind['Source_is_28'] == 1), 'Capacity_Blind'] = df_blind.loc[(df_blind['y'] == 0)  & (df_blind['Source_is_28'] == 1), 'Source'] * coeff_mmWave_no_ho # no handover, the throughput is the source.

df_blind.loc[(df_blind['y'] == 1) & (df_blind['Source_is_3.5'] == 1), 'Capacity_Blind'] = df_blind.loc[(df_blind['y'] == 1) & (df_blind['Source_is_3.5'] == 1), 'Target'] * coeff_sub6_no_ho # handover, the throughput is the target but no gap.
df_blind.loc[(df_blind['y'] == 1) & (df_blind['Source_is_28'] == 1), 'Capacity_Blind'] = df_blind.loc[(df_blind['y'] == 1) & (df_blind['Source_is_28'] == 1), 'Target'] * coeff_mmWave_no_ho # handover, the throughput is the target but no gap.
##

# Sample r_exploit data randomly from df_blind
benchmark_data_blind = df_blind.iloc[np.random.randint(low=0, high=df_blind.shape[0], size=N_exploit), :]

del df_blind

##############################################################################
# 4) Proposed algorithm
##############################################################################

# The height column must be deleted here before prediction is made
height = df['height']
df_proposed = df.drop(['height', 'Source_is_28'], axis=1) # delete the 28 column since it is equal to not 3.5.

# TODO, if the source is only 3.5, also delete that column
df_proposed = df_proposed.drop(['Source_is_3.5'], axis=1)

# Use this for the exploitation
train_valid, benchmark_data_proposed = train_test_split(df_proposed, test_size=r_exploitation, random_state=seed)
    
roc_graphs = pd.DataFrame()
roc_auc_values = []

# Change r_training and save roc1 then repeat
max_r_training = 0
max_score = 0
best_clf = None
X = np.arange(1,10,1)/10.
for r_t in X:
    try:
        [y_pred, y_score, clf] = train_classifier(train_valid, r_t)
        y_pred_proposed = predict_handover(benchmark_data_proposed, clf)
        y_score_proposed = clf.predict_proba(benchmark_data_proposed.drop(['y'], axis=1))
        y_test_proposed = benchmark_data_proposed['y']

        fpr, tpr, score = generate_roc(y_test_proposed, y_score_proposed[:,1])
        if (score > max_score):
            max_score = score
            max_r_training = r_t
            best_clf = clf
            
        roc_auc_values.append(score)
        
        roc_graphs = pd.concat([roc_graphs, pd.DataFrame(fpr), pd.DataFrame(tpr)], axis=1)
    except:
        roc_auc_values.append(np.nan)
        pass

# Replace all NaNs with 1.00000 since they are coming at the end
roc_graphs = roc_graphs.fillna(1)
roc_graphs.to_csv('roc_output.csv', index=False)
plot_primary(X, roc_auc_values, 'ROC vs Training', r'$r_\text{training}$', 'ROC AUC', filename='roc_vs_training.pdf')

# Now generate data with the best classifier.
y_pred_proposed = predict_handover(benchmark_data_proposed, best_clf)
y_score_proposed = best_clf.predict_proba(benchmark_data_proposed.drop(['y'], axis=1))
y_test_proposed = benchmark_data_proposed['y']

plot_confusion_matrix(y_test_proposed, y_pred_proposed, y_score_proposed)

# Put back the height column
benchmark_data_proposed['height'] = height

# Penalize the throughput rates aka Effective Achievable Rate
# Use the same formula as the blind formula
benchmark_data_proposed.loc[(benchmark_data_proposed['y'] == 0) & (benchmark_data_proposed['Source_is_3.5'] == 1), 'Capacity_Proposed'] = benchmark_data_proposed.loc[(benchmark_data_proposed['y'] == 0)  & (benchmark_data_proposed['Source_is_3.5'] == 1), 'Source'] * coeff_sub6_no_ho # no handover, the throughput is the source.
benchmark_data_proposed.loc[(benchmark_data_proposed['y'] == 0) & (benchmark_data_proposed['Source_is_28'] == 1), 'Capacity_Proposed'] = benchmark_data_proposed.loc[(benchmark_data_proposed['y'] == 0)  & (benchmark_data_proposed['Source_is_28'] == 1), 'Source'] * coeff_mmWave_no_ho # no handover, the throughput is the source.
benchmark_data_proposed.loc[(benchmark_data_proposed['y'] == 1) & (benchmark_data_proposed['Source_is_3.5'] == 1), 'Capacity_Proposed'] = benchmark_data_proposed.loc[(benchmark_data_proposed['y'] == 1) & (benchmark_data_proposed['Source_is_3.5'] == 1), 'Target'] * coeff_sub6_no_ho # handover, the throughput is the target but no gap.
benchmark_data_proposed.loc[(benchmark_data_proposed['y'] == 1) & (benchmark_data_proposed['Source_is_28'] == 1), 'Capacity_Proposed'] = benchmark_data_proposed.loc[(benchmark_data_proposed['y'] == 1) & (benchmark_data_proposed['Source_is_28'] == 1), 'Target'] * coeff_mmWave_no_ho # handover, the throughput is the target but no gap.
##

##############################################################################
# Plotting
##############################################################################
benchmark_data_optimal = benchmark_data_optimal.reset_index().drop(['index'], axis=1)
benchmark_data_proposed = benchmark_data_proposed.reset_index().drop(['index'], axis=1)
benchmark_data_legacy  = benchmark_data_legacy.reset_index().drop(['index'], axis=1)
benchmark_data_blind = benchmark_data_blind.reset_index().drop(['index'], axis=1)
benchmark_data_proposed = benchmark_data_proposed.reset_index().drop(['index'], axis=1)

# Temporary
benchmark_data_proposed['Capacity_35'] = benchmark_data_proposed['Source']
benchmark_data_proposed['Capacity_28'] = benchmark_data_proposed['Target']

data = pd.concat([benchmark_data_optimal['Capacity_Optimal'], benchmark_data_proposed['Capacity_Proposed'], benchmark_data_legacy['Capacity_Legacy'], benchmark_data_blind['Capacity_Blind'], benchmark_data_proposed['Capacity_35'], benchmark_data_proposed['Capacity_28']], axis=1, ignore_index=True)
data.columns = ['Optimal', 'Proposed', 'Legacy', 'Blind', 'Sub-6 only', 'mmWave only']
data.dropna(inplace=True)

data.to_csv('dataset_post.csv', index=False)
plot_throughput_cdf(data)