"""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
Created by Jongmin Sung (jongmin.sung@gmail.com)

Single molecule binding and unbinding analysis for anaphase promoting complex (apc) 

class Data() 
- path, name, load(), list[], n_movie, movies = [Movie()], plot(), analysis(), spot_size, frame_rate, 
- path, name, n_row, n_col, n_frame, pixel_size, 
- background, spots = [], molecules = [Molecule()]

class Molecule()
- position (row, col), intensity 

"""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""

from __future__ import division, print_function, absolute_import
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import cm
from pathlib import Path  
import os
import shutil
from timeit import default_timer as timer
from scipy.stats import norm
from scipy.ndimage import gaussian_filter
from scipy.optimize import curve_fit
from scipy.optimize import minimize
from tifffile import TiffFile
from imreg_dft.imreg import translation
from skimage.feature import peak_local_max
from skimage.filters import threshold_local
from skimage.filters import rank
from skimage.morphology import disk
from sklearn.mixture import GaussianMixture 
from hmmlearn import hmm
from inspect import currentframe, getframeinfo
fname = getframeinfo(currentframe()).filename # current file name
current_dir = Path(fname).resolve().parent

# User input ----------------------------------------------------------------

# Jongmin PC
data_dir = current_dir.parent/'data' 
directory = data_dir/'Short_binding'#/'antiSTREP_NHY13+Cdh1akapNH72_NHP5_1nM_1000frames_30.28ms_R1_1 WShowJongmin'
#directory = data_dir/'19-05-29 Movies 300pix300pi'

# Trap PC
#data_dir = current_dir.parent
#directory = data_dir/'new analysis'

pass_with_result = True 

# ---------------------------------------------------------------------------

def str2bool(v):
  return v.lower() in ("yes", "true", "t", "1")

def is_inlier(I, m=4):
    dev = np.abs(I - np.median(I))
    mdev = np.median(dev)
    s = dev/mdev if mdev else 0.
    return s < m

def running_avg(x, n):
    m = int((n-1)/2)
    y =  np.convolve(x, np.ones((n,))/n, mode='valid') 
    z = [np.round(i) for i in y]
    k = np.asarray(z[:1]*m + z + z[-1:]*m, dtype=int)
    return k

def gaussian(x, m, s, n):
    return n/(2*np.pi*s**2)**0.5*np.exp(-(x-m)**2/(2*s**2))

def sum_two_gaussian(x, m1, s1, n1, m2, s2, n2):
#    x = running_avg(x,3)
    if m1 < m2:
        m_1, m_2 = m1, m2
        s_1, s_2 = s1, s2
        n_1, n_2 = n1, n2
    else:
        m_1, m_2 = m2, m1
        s_1, s_2 = s2, s1
        n_1, n_2 = n2, n1

    return gaussian(x, m_1, s_1, n_1) + gaussian(x, m_2, s_2, n_2)

def get_icdf(t, dt):
    if len(t) == 0:
        return [], []

    t = t/dt # per frame
    icdf = []
    x = np.arange(int(max(t)+1))
    for i in x:
        icdf.append(sum(i<=t)/len(t)) 
    return x*dt, icdf


def icdf(k, T, t, cl):
    if cl == 2:
        A = k*T-1
        return 1 - (np.exp(-k*t)*(k*t-A)+A)/(np.exp(-k*T)+A)        
    else:
        return (np.exp(-k*t)-np.exp(-k*T))/(1-np.exp(-k*T))

def pdf(k, T, t, cl):
    if cl == 2:
        return (k*T-k*t)/(k*T-1+np.exp(-k*T))*k*np.exp(-k*t)  
    else:        
        return k*np.exp(-k*t)/(1-np.exp(-k*T))

def LL(k, T, t, cl):     
    k = abs(k)
    if cl == 2:
        return np.sum(np.log(k*T-k*t)-np.log(k*T-1+np.exp(-k*T))+np.log(k)-k*t) 
    else:        
        return np.sum(np.log(k)-k*t-np.log(1-np.exp(-k*T)))

def MLE(T, t, cl):
    fun = lambda *args: -LL(*args)
    p0 = [1/np.mean(t)]
    result = minimize(fun, p0, method='SLSQP', args=(T, t, cl)) 
    return abs(1/result["x"][0])

def Info(k, T, t, cl):
    dk = k/100
    return abs(LL(k+dk,T,t,cl)+LL(k-dk,T,t,cl)-2*LL(k,T,t,cl))/dk**2

def get_weighted_mean(m1, s1, m2, s2, m3, s3):
    m = np.array([m1, m2, m3])
    s = np.array([s1, s2, s3])
    w = np.array([1/i**2 for i in s])

    # Exclude nan
    m = m[w > 0]
    s = s[w > 0]
    w = w[w > 0]        

    weighted_mean = sum([m[i]*w[i] for i in range(len(w))])/sum(w)
    weighted_error = 1/sum(w)**0.5

#    weighted_mean = (w1*m1+w2*m2+w3*m3)/(w1+w2+w3) 
#    weighted_error = (w1+w2+w3)**-0.5
    return weighted_mean, weighted_error

class Movie:
    def __init__(self, path):
        self.path = path
        self.dir = path.parent
        self.name = path.name

    # Read info.txt and movie.tif   
    def read_movie(self):  
        # Read info.txt
        self.info = {}
        with open(Path(self.dir/'info.txt')) as f:
            for line in f:
                line = line.replace(" ", "") # remove white space
                if line == '\n': # skip empty line
                    continue
                (key, value) = line.rstrip().split("=")
                self.info[key] = value

        # Parameters for analysis 
        self.time_interval = float(self.info['time_interval'])
#        self.time_exposure = float(self.info['time_exposure'])
        self.spot_size = int(self.info['spot_size'])     
        self.drift_correct = str2bool(self.info['drift_correct'])
        self.flatfield_correct = str2bool(self.info['flatfield_correct'])   
        self.frame_offset = int(self.info['frame_offset'])      
        self.intensity_min_cutoff = float(self.info['intensity_min_cutoff']) 
        self.intensity_max_cutoff = float(self.info['intensity_max_cutoff']) 
        self.HMM_RMSD_cutoff = float(self.info['HMM_RMSD_cutoff']) 
        self.HMM_unbound_cutoff = float(self.info['HMM_unbound_cutoff']) 
        self.HMM_bound_cutoff = float(self.info['HMM_bound_cutoff'])             
        self.save_trace = int(self.info['save_trace'])

        # Read movie.tif
        with TiffFile(self.path) as tif:
            imagej_hyperstack = tif.asarray()
            imagej_metadata = str(tif.imagej_metadata)
            self.metadata = imagej_metadata.split(',')

#        # write meta_data    
#        with open(self.path.parent/'meta_data.txt', 'w') as f:
#            for item in imagej_metadata:
#                f.write(item+'\n')

        # Number of frame and window size 
        self.n_frame = np.size(imagej_hyperstack, 0)
        self.window = self.n_frame*self.time_interval

        # Crop the image to make the size integer multiple of 10
        self.bin_size = 20
        n_row = np.size(imagej_hyperstack, 1)
        self.n_row = int(int(n_row/self.bin_size)*self.bin_size)        
        n_col = np.size(imagej_hyperstack, 2) 
        self.n_col = int(int(n_col/self.bin_size)*self.bin_size)
        self.I_original = imagej_hyperstack[:,:self.n_row,:self.n_col]

        # Crop movie at the center if the size is larger than 300x300
        if self.n_row > 300:
            print('[frame, row, col] = [%d, %d, %d]' %(self.n_frame, self.n_row, self.n_col))  
            print("Crop for row=300, col=300 \n")
            self.n_row = 300
            self.n_col = 300
            c = int(self.n_row/2)
            self.I_original = self.I_original[:,c-50:c+250,c-50:c+250]

        print('[frame, row, col] = [%d, %d, %d]' %(self.n_frame, self.n_row, self.n_col))  


    def correct_offset(self):
        self.I_offset = self.I_original.copy()
#        self.I_original_min = np.min(self.I_original, axis=0)
#        for i in range(self.n_frame):
#            self.I_offset[i] = self.I_original[i] - self.I_original_min


    def correct_flatfield(self):
        self.I_offset_max = np.max(self.I_offset, axis=0)
        self.I_flatfield = self.I_offset.copy()

        # Flatfield correct
        if self.flatfield_correct:
            print('flatfield_correct = True')

            # Masking from local threshold        
            self.mask = self.I_offset_max > threshold_local(self.I_offset_max, block_size=51, offset=-31) 
            self.I_mask = self.I_offset_max*self.mask 
            self.I_mask_out = self.I_offset_max*(1-self.mask) 

            # Local averaging signals
            self.I_bin = np.zeros((self.n_row, self.n_col))
            m = self.bin_size
            for i in range(int(self.n_row/m)):
                for j in range(int(self.n_col/m)):
                    window = self.I_mask[i*m:(i+1)*m, j*m:(j+1)*m].flatten()          
                    signals = [signal for signal in window if signal > 0]
                    if signals:
                        self.I_bin[i*m:(i+1)*m,j*m:(j+1)*m] = np.mean(signals)

            # Fill empty signal with the local mean. 
            for i in range(int(self.n_row/m)):
                for j in range(int(self.n_col/m)):
                    if self.I_bin[i*m,j*m] == 0:
                        window = self.I_bin[max(0,(i-1)*m):min((i+2)*m,self.n_row), max(0,(j-1)*m):min((j+2)*m,self.n_col)].flatten() 
                        signals = [signal for signal in window if signal > 0] # Take only positive signal
                        if signals:
                            self.I_bin[i*m:(i+1)*m,j*m:(j+1)*m] = np.mean(signals)   

            # Remaining empty signal will be filled with the global mean. 
            self.I_bin[self.I_bin==0] = np.mean(self.I_bin[self.I_bin>0])

            # Smoothening the sharp bolder.
            self.I_bin_filter = gaussian_filter(self.I_bin, sigma=10)

            # Flatfield correct by normalization
            self.I_flatfield = np.array(self.I_offset)
            for i in range(self.n_frame):
                self.I_flatfield[i,] = self.I_offset[i,] / self.I_bin_filter * np.max(self.I_bin_filter)    

            # Local averaging signals after flatfield correction
            self.I_flatfield_max = np.max(self.I_flatfield, axis=0)
            self.I_flatfield_mask = self.I_flatfield_max*self.mask 
            self.I_flatfield_bin = np.zeros((self.n_row, self.n_col))
            for i in range(int(self.n_row/m)):
                for j in range(int(self.n_col/m)):
                    window = self.I_flatfield_mask[i*m:(i+1)*m, j*m:(j+1)*m].flatten()          
                    signals = [signal for signal in window if signal > 0]
                    if signals:
                        self.I_flatfield_bin[i*m:(i+1)*m,j*m:(j+1)*m] = np.mean(signals)
        else:
            print('flatfield_correct = False')


    def correct_drift(self):
        self.I_drift = self.I_flatfield.copy()
        self.drift_row = np.zeros(len(self.I_drift), dtype='int')
        self.drift_col = np.zeros(len(self.I_drift), dtype='int')

        # Drift correct
        if self.drift_correct:
            print('drift_correct = True')

            I = self.I_flatfield.copy()
            I_ref = I[int(len(I)/2),] # Mid frame as a reference frame

            # Translation as compared with I_ref
            d_row = np.zeros(len(I), dtype='int')
            d_col = np.zeros(len(I), dtype='int')
            for i, I_frame in enumerate(I):
                result = translation(I_ref, I_frame)
                d_row[i] = round(result['tvec'][0])
                d_col[i] = round(result['tvec'][1])      

            # Changes of translation between the consecutive frames
            dd_row = d_row[1:] - d_row[:-1]
            dd_col = d_col[1:] - d_col[:-1]

            # Sudden jump in translation set to zero
            step_limit = 2
            dd_row[abs(dd_row)>step_limit] = 0
            dd_col[abs(dd_col)>step_limit] = 0

            # Adjusted translation
            d_row[0] = 0
            d_col[0] = 0
            d_row[1:] = np.cumsum(dd_row)
            d_col[1:] = np.cumsum(dd_col)

            # Offset mid to zero
            self.drift_row = d_row 
            self.drift_col = d_col      

            # Running avg
            self.drift_row = running_avg(self.drift_row, 5)
            self.drift_col = running_avg(self.drift_col, 5)      

            # Offset to zero
            self.drift_row = self.drift_row - self.drift_row[0]  
            self.drift_col = self.drift_col - self.drift_col[0]  

            # Translate images
            for i in range(len(I)):
                self.I_drift[i,] = np.roll(self.I_drift[i,], self.drift_row[i], axis=0)
                self.I_drift[i,] = np.roll(self.I_drift[i,], self.drift_col[i], axis=1)        
        else:
            print('drift_correct = False')
      
        # Simple name after the corrections
        self.I = self.I_drift.copy()
        self.I_max = np.max(self.I, axis=0)


    # Find spots where molecules bind
    def find_peak(self):
        # Find local maxima from I_max
#        self.I_max_smooth = self.I_max.copy()        
#        self.I_max_smooth = rank.mean(self.I_max, selem=disk(1))
        self.I_max_smooth = gaussian_filter(self.I_max, sigma=1)

        # Find local maxima from I_max
        self.peak = peak_local_max(self.I_max_smooth, min_distance=int(self.spot_size*1.0))        
        self.n_peak = len(self.peak[:, 1])
        self.peak_row = self.peak[::-1,0]
        self.peak_col = self.peak[::-1,1]

        # Get the time trace of each spots
        self.peak_trace = np.zeros((self.n_peak, self.n_frame))
        for i in range(self.n_peak):
            # Get the trace from each spot
            r = self.peak_row[i]
            c = self.peak_col[i]
            s = int((self.spot_size-1)/2)
            self.peak_trace[i] = np.sum(np.sum(self.I[:,r-s:r+s+1,c-s:c+s+1], axis=2), axis=1)/self.spot_size**2


    # Find true spots from the peaks 
    def find_spot(self):
        # Find inliers with I_min
        self.peak_min = np.min(self.peak_trace, axis=1)
        self.is_peak_min_inlier = is_inlier(self.peak_min, float(self.info['intensity_min_cutoff'])) 

        # Find inliers with I_max
        self.peak_max = np.max(self.peak_trace, axis=1)

        # Train and predict data with GaussianMixture model 
        X = self.peak_max.reshape(-1,1)
        gmm = GaussianMixture(n_components=2).fit(X)
        labels = gmm.predict(X)
        
        # Compare two groups
        g0_n = len(self.peak_max[labels==0])
        g1_n = len(self.peak_max[labels==1])
        g0_m = np.median(self.peak_max[labels==0])
        g1_m = np.median(self.peak_max[labels==1])        
        g0_s = np.std(self.peak_max[labels==0])
        g1_s = np.std(self.peak_max[labels==1])

        # Conditions for two groups
        cond1 = max(g0_n, g1_n)/min(g0_n, g1_n) < 10
        cond2 = abs(g0_m-g1_m)/min(g0_s, g1_s) > 1 

        # If there are two groups
        if cond1 & cond2:
            print('\nFound two groups ')
            # Group in higher intensity is inliers.
            if self.peak_max[labels==0].mean() > self.peak_max[labels==1].mean():
                self.is_peak_max_inlier = labels==0
            else:               
                self.is_peak_max_inlier = labels==1

            # Exclude outliers
            inliers_std = np.std(self.peak_max[self.is_peak_max_inlier])
            inliers_mean = np.mean(self.peak_max[self.is_peak_max_inlier])
            for i, I_max in enumerate(self.peak_max):
                if abs(I_max - inliers_mean)/inliers_std > 2:
                    self.is_peak_max_inlier[i] = False
        else:
            print('\nFound one group ')
            self.is_peak_max_inlier = is_inlier(self.peak_max, float(self.info['intensity_max_cutoff'])) 

        # Find lnliers from both I_min and I_max
        self.is_peak_inlier = self.is_peak_min_inlier & self.is_peak_max_inlier

        # Find spots from the peak lnliers
        self.n_spot = sum(self.is_peak_inlier)
        self.trace = self.peak_trace[self.is_peak_inlier]
        self.spot_row = self.peak_row[self.is_peak_inlier]        
        self.spot_col = self.peak_col[self.is_peak_inlier]   

        # Find two group from the entire intensity
        X = self.trace.reshape(-1,1)
        gmm = GaussianMixture(n_components=2).fit(X)
        labels = gmm.predict(X)        

        # Compare two groups
        g0_n = len(X[labels==0])
        g1_n = len(X[labels==1])
        g0_m = np.median(X[labels==0])
        g1_m = np.median(X[labels==1])        
        g0_s = np.std(X[labels==0])
        g1_s = np.std(X[labels==1])
        self.I_param = [g0_m, g0_s, g0_n, g1_m, g1_s, g1_n]


    # Fit traces
    def fit_spot(self):
        self.trace_fit = np.zeros((self.n_spot, self.n_frame))
        self.state = np.zeros((self.n_spot, self.n_frame))        
        self.rmsd = np.zeros(self.n_spot)
        self.I_u = np.zeros(self.n_spot)
        self.I_b = np.zeros(self.n_spot)

        # Fit the time trace using HMM    
        for i, trace in enumerate(self.trace):
            X = trace.reshape(len(trace), 1) 
          
            # Set a new model for traidning
            param=set(X.ravel())
            remodel = hmm.GaussianHMM(n_components=2, covariance_type="full", n_iter=100)        
        
            # Set initial parameters for training
            remodel.startprob_ = np.array([self.I_param[2]/(self.I_param[2]+self.I_param[5]), 
                                           self.I_param[5]/(self.I_param[2]+self.I_param[5])])
            remodel.transmat_ = np.array([[0.98, 0.02], 
                                          [0.20, 0.80]])
            remodel.means_ = np.array([self.I_param[0], self.I_param[3]])  
            remodel.covars_ = np.array([[[self.I_param[1]]],
                                        [[self.I_param[4]]]])
           
            # Estimate model parameters (training)
            remodel.fit(X)

            # Find most likely state sequence corresponding to X
            Z = remodel.predict(X)

            # Reorder state number such that X[Z=0] < X[Z=1] 
            if remodel.means_[0] > remodel.means_[1]:
                Z = 1 - Z
                remodel.means_ = remodel.means_[::-1]

            # Intensity trace fit     
            self.state[i] = np.array(Z) 
            self.trace_fit[i] = (1-Z)*remodel.means_[0] + Z*remodel.means_[1]     
            self.rmsd[i] = (np.mean((self.trace_fit[i] - trace)**2))**0.5           

            # Mean intensity of the two states
            self.I_u[i] = remodel.means_[0]
            self.I_b[i] = remodel.means_[1]

        # Find inliners and exclude outliers
        self.is_rmsd_inlier = is_inlier(self.rmsd, float(self.info['HMM_RMSD_cutoff']))
        self.is_I_u_inlier = is_inlier(self.I_u, float(self.info['HMM_unbound_cutoff']))
        self.is_I_b_inlier = is_inlier(self.I_b, float(self.info['HMM_bound_cutoff']))
        self.is_trace_inlier = self.is_rmsd_inlier & self.is_I_u_inlier & self.is_I_b_inlier

        # Save inlier traces
        self.state_inlier = self.state[self.is_trace_inlier]
        self.trace_inlier = self.trace[self.is_trace_inlier]
        self.I_u_inlier = self.I_u[self.is_trace_inlier]        
        self.I_b_inlier = self.I_b[self.is_trace_inlier]
        self.rmsd_inlier = self.rmsd[self.is_trace_inlier]

        print('Found', self.n_peak, 'peaks. ')     
        print('Rejected', self.n_peak - len(self.rmsd_inlier), 'outliers.')   


    def find_event(self):
        self.dwell_1 = [] # Bound, class 1 (pre-existing)
        self.dwell_2 = [] # Bound, class 2 (complete)
        self.dwell_3 = [] # Bound, class 3 (incomplete)
        self.wait_1 = [] # Unbound, class 1 (pre-existing)
        self.wait_2 = [] # Unbound, class 2 (complete)
        self.wait_3 = [] # Unbound, class 3 (incomplete)

        for _, state in enumerate(self.state_inlier):
            tb = [] # Frame at binding
            tu = [] # Frame at unbinding
    
            # Find binding and unbinding moment
            for i in range(self.n_frame-1):
                if state[i] == 0 and state[i+1] == 1: # binding
                    tb.append(i) 
                elif state[i] == 1 and state[i+1] == 0: # unbinding
                    tu.append(i) 
                else:
                    pass

            # Cases 
            if len(tb) + len(tu) == 0: # n_event = 0
                continue
            elif len(tb) + len(tu) == 1: # n_event = 1
                if len(tb) == 1: # One binding event
                    self.wait_1.append(tb[0]+1)
                    self.dwell_3.append(self.n_frame-tb[-1]-1)
                else: # One unbinding event 
                    self.dwell_1.append(tu[0]+1)
                    self.wait_3.append(self.n_frame-tu[-1]-1)
            else: # n_event > 1 
                # First event is w1 or d1
                if state[0] == 0: # Unbound state at the beginning
                    self.wait_1.append(tb[0]+1)
                else: # Bound state at the beginning
                    self.dwell_1.append(tu[0]+1)

                # Last event is w3 or d3
                if state[-1] == 0: # Unbound state at the end
                    self.wait_3.append(self.n_frame-tu[-1]-1)
                else: # Bound state at the end
                    self.dwell_3.append(self.n_frame-tb[-1]-1)

                # All the rests are w2 or d2
                t = tb + tu # Concatenate and sort in order 
                t.sort()
                dt = [t[i+1]-t[i] for i in range(len(t)-1)]
                dt_odd = dt[0::2]
                dt_even = dt[1::2]

                if state[0] == 0: # Odd events are d2, event events are w2
                    self.dwell_2.extend(dt_odd)
                    self.wait_2.extend(dt_even)
                else: # Odd events are w2, event events are d2
                    self.wait_2.extend(dt_odd)
                    self.dwell_2.extend(dt_even)                


    def  exclude_short(self):

        # Offset to get rid of short events 
        self.offset = self.frame_offset + 0.5

        self.dwell_1 = np.array(self.dwell_1)-self.offset
        self.dwell_2 = np.array(self.dwell_2)-self.offset
        self.dwell_3 = np.array(self.dwell_3)-self.offset

        self.wait_1 = np.array(self.wait_1)-self.offset
        self.wait_2 = np.array(self.wait_2)-self.offset
        self.wait_3 = np.array(self.wait_3)-self.offset

        # Exclude short frames and convert unit in sec 
        self.dwell_1 = self.dwell_1[self.dwell_1>0]*self.time_interval
        self.dwell_2 = self.dwell_2[self.dwell_2>0]*self.time_interval
        self.dwell_3 = self.dwell_3[self.dwell_3>0]*self.time_interval

        self.wait_1 = self.wait_1[self.wait_1>0]*self.time_interval
        self.wait_2 = self.wait_2[self.wait_2>0]*self.time_interval
        self.wait_3 = self.wait_3[self.wait_3>0]*self.time_interval

    def exclude_long(self):
        cutoff = 10
        self.dwell_1 = self.dwell_1[self.dwell_1 < np.median(self.dwell_1)*cutoff]
        self.dwell_2 = self.dwell_2[self.dwell_2 < np.median(self.dwell_2)*cutoff]
        self.dwell_3 = self.dwell_3[self.dwell_3 < np.median(self.dwell_3)*cutoff]

        self.wait_1 = self.wait_1[self.wait_1 < np.median(self.wait_1)*cutoff]
        self.wait_2 = self.wait_2[self.wait_2 < np.median(self.wait_2)*cutoff]
        self.wait_3 = self.wait_3[self.wait_3 < np.median(self.wait_3)*cutoff]      

   
    def estimate_time(self):

        # MLE estimation 
        self.MLE_dwell_1 = MLE(self.window, self.dwell_1, 1) 
        self.MLE_dwell_2 = MLE(self.window, self.dwell_2, 2) 
        self.MLE_dwell_3 = MLE(self.window, self.dwell_3, 3) 

        self.MLE_wait_1 = MLE(self.window, self.wait_1, 1) 
        self.MLE_wait_2 = MLE(self.window, self.wait_2, 2) 
        self.MLE_wait_3 = MLE(self.window, self.wait_3, 3) 
                    
        # MLE error from Fisher Information
        self.Error_dwell_1 = Info(self.MLE_dwell_1, self.window, self.dwell_1, 1)**-0.5 
        self.Error_dwell_2 = Info(self.MLE_dwell_2, self.window, self.dwell_2, 2)**-0.5         
        self.Error_dwell_3 = Info(self.MLE_dwell_3, self.window, self.dwell_3, 3)**-0.5 

        self.Error_wait_1 = Info(self.MLE_wait_1, self.window, self.wait_1, 1)**-0.5 
        self.Error_wait_2 = Info(self.MLE_wait_2, self.window, self.wait_2, 2)**-0.5 
        self.Error_wait_3 = Info(self.MLE_wait_3, self.window, self.wait_3, 3)**-0.5 

        # Weighted MLE
        self.MLE_dwell, self.Error_dwell = get_weighted_mean(self.MLE_dwell_1, self.Error_dwell_1, 
                                                             self.MLE_dwell_2, self.Error_dwell_2, 
                                                             self.MLE_dwell_3, self.Error_dwell_3)

        self.MLE_wait, self.Error_wait = get_weighted_mean(self.MLE_wait_1, self.Error_wait_1, 
                                                            self.MLE_wait_2, self.Error_wait_2, 
                                                            self.MLE_wait_3, self.Error_wait_3)

    def save_result(self):
        # Write the result to an output file
        with open(Path(self.dir/'result.txt'), "w") as f:
            f.write('directory = %s \n' %(self.dir))
            f.write('name = %s \n' %(self.name))
            f.write('time interval = %.2f [s] \n' %(self.time_interval))
            f.write('number of frame = %d \n' %(self.n_frame))
            f.write('number of spots = %d \n\n' %(len(self.rmsd_inlier)))
            f.write('dwell time (class 1) = %.2f +/- %.2f [s] (N = %d) \n' %(self.MLE_dwell_1, self.Error_dwell_1, len(self.dwell_1)))
            f.write('dwell time (class 2) = %.2f +/- %.2f [s] (N = %d) \n' %(self.MLE_dwell_2, self.Error_dwell_2, len(self.dwell_2)))
            f.write('dwell time (class 3) = %.2f +/- %.2f [s] (N = %d) \n' %(self.MLE_dwell_3, self.Error_dwell_3, len(self.dwell_3)))
            f.write('dwell time (combined) = %.2f +/- %.2f [s] (N = %d) \n\n' %(self.MLE_dwell, self.Error_dwell, len(self.dwell_1)+len(self.dwell_2)+len(self.dwell_3)))
            f.write('wait time (class 1) = %.2f +/- %.2f [s] (N = %d) \n' %(self.MLE_wait_1, self.Error_wait_1, len(self.wait_1)))
            f.write('wait time (class 2) = %.2f +/- %.2f [s] (N = %d) \n' %(self.MLE_wait_2, self.Error_wait_2, len(self.wait_2)))
            f.write('wait time (class 3) = %.2f +/- %.2f [s] (N = %d) \n' %(self.MLE_wait_3, self.Error_wait_3, len(self.wait_3)))
            f.write('wait time (combined) = %.2f +/- %.2f [s] (N = %d) \n\n' %(self.MLE_wait, self.Error_wait, len(self.wait_1)+len(self.wait_2)+len(self.wait_3)))
   
      
    def plot0_clean(self):
        # clean all existing png files in the folder
        files = os.listdir(self.dir)    
        for file in files:
            if file.endswith('png'):
                os.remove(self.dir/file)    


    def plot1_original_min_max(self):
        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(figsize=(20, 10), ncols=2, nrows=2, dpi=300)

        I_min = np.min(self.I_original, axis=0)
        I_max = np.max(self.I_original, axis=0)

        sp = ax1.imshow(I_min, cmap='gray')
        fig.colorbar(sp, ax=ax1) 

        ax2.hist(I_min.ravel(), 20, histtype='step', lw=2, color='k')    
        ax2.set_yscale('log')
        ax2.set_xlim(0, np.max(I_max)) 
        ax2.set_xlabel('Intensity')
        ax2.set_ylabel('Counts')
        ax2.set_title('Min projection - original')

        sp = ax3.imshow(I_max, cmap='gray')
        fig.colorbar(sp, ax=ax3) 

        ax4.hist(I_max.ravel(), 50, histtype='step', lw=2, color='k')                      
        ax4.set_yscale('log')
        ax4.set_xlim(0, np.max(I_max)) 
        ax4.set_xlabel('Intensity')
        ax4.set_ylabel('Counts')
        ax4.set_title('Max projection - original')

        fig.tight_layout()
        fig.savefig(self.dir/'plot1_original_min_max.png')   
        plt.close(fig)                                                                                                                                                                                                                                                                                                                                                                                                                                                            


    def plot2_flatfield(self):              
        if str2bool(self.info['flatfield_correct']) == False:
            return None

        fig, ((ax1, ax2, ax3), (ax4, ax5, ax6)) = plt.subplots(figsize=(20, 10), ncols=3, nrows=2, dpi=300)

        sp = ax1.imshow(self.I_offset_max, cmap=cm.gray)
        fig.colorbar(sp, ax=ax1) 
        ax1.set_title('Max intensity - original')      
  
        ax2.imshow(self.mask, cmap=cm.gray)
        ax2.set_title('Mask')           

        sp = ax3.imshow(self.I_bin, cmap=cm.gray)
        fig.colorbar(sp, ax=ax3) 
        ax3.set_title('Intensity - bin')

        sp = ax4.imshow(self.I_bin_filter, cmap=cm.gray)
        fig.colorbar(sp, ax=ax4) 
        ax4.set_title('Intensity - bin filter')        

        sp = ax5.imshow(self.I_max, cmap=cm.gray)
        fig.colorbar(sp, ax=ax5) 
        ax5.set_title('Max intensity - flatfield')

        sp = ax6.imshow(self.I_flatfield_bin, cmap=cm.gray)
        fig.colorbar(sp, ax=ax6) 
        ax6.set_title('Intensity flatfield - bin')

        fig.tight_layout()
        fig.savefig(self.dir/'plot2_flatfield.png')   
        plt.close(fig)


    def plot3_drift(self):                      
        I_row = np.squeeze(self.I[:,int(self.n_row/2),:])
        I_col = np.squeeze(self.I[:,:,int(self.n_col/2)])

        fig, ((ax1, ax2), (ax3, ax4), (ax5, ax6)) = plt.subplots(figsize=(20, 10), ncols=2, nrows=3, dpi=300)

        ax1.plot(self.drift_row, 'k')
        ax1.set_yticks(np.arange(min(self.drift_row), max(self.drift_row)+1, 1.0))
        ax1.set_xlabel('Frame')
        ax1.set_ylabel('Pixel')
        ax1.set_title('Drift in Y')

        ax2.plot(self.drift_col, 'k')
        ax2.set_yticks(np.arange(min(self.drift_col), max(self.drift_col)+1, 1.0))
        ax2.set_xlabel('Frame')
        ax2.set_ylabel('Pixel')
        ax2.set_title('Drift in X')

        ax3.imshow(I_col, cmap='gray')
        ax3.set_xlabel('Y')
        ax3.set_ylabel('Frame')

        ax4.imshow(I_row, cmap='gray')
        ax4.set_xlabel('X')
        ax4.set_ylabel('Frame')

        ax5.plot(np.mean(I_col, axis=0), 'ko-')
        ax5.set_xlim([0, self.n_col])
        ax5.set_xlabel('Y')

        ax6.plot(np.mean(I_row, axis=0), 'ko-')
        ax6.set_xlim([0, self.n_row])
        ax6.set_xlabel('X')

        fig.tight_layout()
        fig.savefig(self.dir/'plot3_drift.png')   
        plt.close(fig)

    def plot4_peak(self):
        fig = plt.figure(figsize=(20, 10), dpi=300)

        fig, (ax1, ax2, ax3) = plt.subplots(figsize=(20, 10), ncols=3, nrows=1, dpi=300)

        ax1.imshow(self.I_max, cmap=cm.gray)
        ax1.set_title('Max intensity')   

        ax2.imshow(self.I_max_smooth, cmap=cm.gray)
        ax2.set_title('Max intensity - smooth') 

        ax3.imshow(self.I_max_smooth, cmap=cm.gray)
        ax3.scatter(self.peak_col, self.peak_row, lw=0.8, s=50, facecolors='none', edgecolors='y')
        ax3.set_title('Peaks') 

        fig.tight_layout()
        fig.savefig(self.dir/'plot4_peak.png')   
        plt.close(fig)


    def plot5_spot(self):
        fig = plt.figure(figsize=(20, 10), dpi=300)
        gs = fig.add_gridspec(2, 2)

        ax1 = fig.add_subplot(gs[:, 0])
        ax1.imshow(self.I_max, cmap=cm.gray)
        color = [['b','r'][int(i)] for i in self.is_peak_inlier] 
        ax1.scatter(self.peak_col, self.peak_row, lw=0.8, s=50, facecolors='none', edgecolors=color)
        ax1.set_xlabel('X')
        ax1.set_ylabel('Y')        
        ax1.set_title('Spots: selected (R), rejected (B)')  

        bins = np.linspace(min(self.peak_min), max(self.peak_min), 50)     
        ax2 = fig.add_subplot(gs[0, 1])
        ax2.hist(self.peak_min, bins = bins, histtype='step', lw=2, color='b')
        ax2.hist(self.peak_min[self.is_peak_min_inlier], bins = bins, histtype='step', lw=2, color='r')   
        ax2.set_xlabel('Intensity')
        ax2.set_ylabel('Counts')     
        ax2.set_title('Intensity min')

        bins = np.linspace(min(self.peak_max), max(self.peak_max), 50)     
        ax3 = fig.add_subplot(gs[1, 1])
        ax3.hist(self.peak_max, bins = bins, histtype='step', lw=2, color='b')
        ax3.hist(self.peak_max[self.is_peak_max_inlier], bins = bins, histtype='step', lw=2, color='r')
#        ax3.plot(self.I_max_x, self.I_max_fit, 'k')
        ax3.set_xlabel('Intensity')
        ax3.set_ylabel('Counts')    
        ax3.set_title('Intensity max')

        fig.savefig(self.dir/'plot5_spot.png')   
        plt.close(fig)


    def plot6_spot_fit(self):
        spot_trace = self.trace.flatten()
        n, bins = np.histogram(self.trace.flatten(), bins=100, density=False)
        x = (bins[1:]+bins[:-1])/2
        x_fit = np.linspace(min(x), max(x), 1000)
        y_fit = sum_two_gaussian(x_fit, *self.I_param)*(bins[1]-bins[0])

        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(figsize=(20, 10), ncols=2, nrows=2, dpi=300)     
 
        ax1.step(x, n, where='mid', c='k', lw=2)
#        ax1.scatter(self.I_x[is_max], self.I_hist[is_max], lw=2, s=100, facecolors='none', edgecolors='r')
        ax1.plot(x_fit, y_fit, 'r')    
        ax1.set_yscale('log')      
        ax1.set_xlabel('Intensity')
        ax1.set_ylabel('Counts')
        ax1.set_title('Intensity of entire traces')   

        bins = np.linspace(min(self.rmsd), max(self.rmsd), 50)          
        ax2.hist(self.rmsd, bins = bins, histtype='step', lw=2, color='b')   
        ax2.hist(self.rmsd[self.is_rmsd_inlier], bins = bins, histtype='step', lw=2, color='r')   
        ax2.set_title('RMSD of fitting (HMM)')
        ax2.set_xlabel('RMSD')
        ax2.set_ylabel('Counts')

        bins = np.linspace(min(self.I_u), max(self.I_u), 50)  
        ax3.hist(self.I_u, bins = bins, histtype='step', lw=2, color='b')      
        ax3.hist(self.I_u[self.is_I_u_inlier], bins = bins, histtype='step', lw=2, color='r')    
        ax3.set_title('Intensity unbound (HMM)')
        ax3.set_xlabel('Intensity')
        ax3.set_ylabel('Counts')
 
        bins = np.linspace(min(self.I_b), max(self.I_b), 50)  
        ax4.hist(self.I_b, bins = bins, histtype='step', lw=2, color='b')      
        ax4.hist(self.I_b[self.is_I_b_inlier], bins = bins, histtype='step', lw=2, color='r')      
        ax4.set_title('Intensity bound (HMM)')
        ax4.set_xlabel('Intensity')
        ax4.set_ylabel('Counts')

        fig.savefig(self.dir/'plot6_spot_fit.png')   
        plt.close(fig)


    def plot7_dwell_pdf(self):

        t1 = np.array(self.dwell_1)
        t2 = np.array(self.dwell_2)
        t3 = np.array(self.dwell_3)
        t_max = max(t1.tolist()+t2.tolist()+t3.tolist())

        n_bin = 50

        if t_max > n_bin*self.time_interval: 
            interval = np.ceil(t_max/self.time_interval/n_bin)*self.time_interval
            bins = np.arange(0, interval*(n_bin+1), interval)
        else:
            interval = self.time_interval
            bins = np.arange(0, t_max+interval, interval) 

        x_fit = np.linspace(0, t_max+interval/2, 100)

        fig, ((ax1, ax2, ax3), (ax4, ax5, ax6)) = plt.subplots(figsize=(20, 10), ncols=3, nrows=2, dpi=300)
  
        ax1.hist(t1, bins=bins, histtype='step', lw=1, color='k', density=True)
        ax1.plot(x_fit, pdf(1/self.MLE_dwell_1, self.window, x_fit, 1), 'r', lw=1)  
        ax1.set_ylabel('Probability density')
        ax1.set_title('Class 1 (N = %d)' %(len(t1)))

        ax2.hist(t2, bins=bins, histtype='step', lw=1, color='k', density=True)
        ax2.plot(x_fit, pdf(1/self.MLE_dwell_2, self.window, x_fit, 2), 'r', lw=1)                   
        ax2.set_ylabel('Probability density')
        ax2.set_title('Class 2 (N = %d), Combined Exp_Finite = %.2f +/- %.2f [s]' %(len(t2), self.MLE_dwell, self.Error_dwell))
  
        ax3.hist(t3, bins=bins, histtype='step', lw=1, color='k', density=True)
        ax3.plot(x_fit, pdf(1/self.MLE_dwell_3, self.window, x_fit, 3), 'r', lw=1)        
        ax3.set_ylabel('Probability density')
        ax3.set_title('Class 3 (N = %d)' %(len(t3)))
  
        ax4.hist(t1, bins=bins, histtype='step', lw=1, color='k', density=True)
        ax4.plot(x_fit, pdf(1/self.MLE_dwell_1, self.window, x_fit, 1), 'r', lw=1)      
        ax4.set_yscale('log')
        ax4.set_xlabel('Dwell Time [s]')
        ax4.set_ylabel('Probability density')
        ax4.set_title('Exp_Finite = %.2f +/- %.2f [s]' %(self.MLE_dwell_1, self.Error_dwell_1))        
  
        ax5.hist(t2, bins=bins, histtype='step', lw=1, color='k', density=True)
        ax5.plot(x_fit, pdf(1/self.MLE_dwell_2, self.window, x_fit, 2), 'r', lw=1)          
        ax5.set_yscale('log')
        ax5.set_xlabel('Dwell Time [s]')
        ax5.set_ylabel('Probability density')
        ax5.set_title('Exp_Finite = %.2f +/- %.2f [s]' %(self.MLE_dwell_2, self.Error_dwell_2))  
   
        ax6.hist(t3, bins=bins, histtype='step', lw=1, color='k', density=True)
        ax6.plot(x_fit, pdf(1/self.MLE_dwell_3, self.window, x_fit, 3), 'r', lw=1)         
        ax6.set_yscale('log')
        ax6.set_xlabel('Dwell Time [s]')
        ax6.set_ylabel('Probability density')
        ax6.set_title('Exp_Finite = %.2f +/- %.2f [s]' %(self.MLE_dwell_3, self.Error_dwell_3))  

        fig.savefig(self.dir/'plot7_dwell_pdf.png')   
        plt.close(fig)


    def plot8_wait_pdf(self):

        t1 = np.array(self.wait_1)
        t2 = np.array(self.wait_2)
        t3 = np.array(self.wait_3)
        t_max = max(t1.tolist()+t2.tolist()+t3.tolist())

        n_bin = 50

        if t_max > n_bin*self.time_interval: 
            interval = np.ceil(t_max/self.time_interval/n_bin)*self.time_interval
            bins = np.arange(0, interval*(n_bin+1), interval)
        else:
            interval = self.time_interval
            bins = np.arange(0, t_max+interval, interval) 

        x_fit = np.linspace(0, t_max+interval/2, 100)

        fig, ((ax1, ax2, ax3), (ax4, ax5, ax6)) = plt.subplots(figsize=(20, 10), ncols=3, nrows=2, dpi=300)   
   
        ax1.hist(t1, bins=bins, histtype='step', lw=1, color='k', density=True)
        ax1.plot(x_fit, pdf(1/self.MLE_wait_1, self.window, x_fit, 1), 'r', lw=1)  
        ax1.set_ylabel('Probability density')
        ax1.set_title('Class 1 (N = %d)' %(len(t1)))

        ax2.hist(t2, bins=bins, histtype='step', lw=1, color='k', density=True)
        ax2.plot(x_fit, pdf(1/self.MLE_wait_2, self.window, x_fit, 2), 'r', lw=1)                   
        ax2.set_ylabel('Probability density')
        ax2.set_title('Class 2 (N = %d), Combined Exp_Finite = %.2f +/- %.2f [s]' %(len(t2), self.MLE_wait, self.Error_wait))
  
        ax3.hist(t3, bins=bins, histtype='step', lw=1, color='k', density=True)
        ax3.plot(x_fit, pdf(1/self.MLE_wait_3, self.window, x_fit, 3), 'r', lw=1)        
        ax3.set_ylabel('Probability density')
        ax3.set_title('Class 3 (N = %d)' %(len(t3)))

        ax4.hist(t1, bins=bins, histtype='step', lw=1, color='k', density=True)
        ax4.plot(x_fit, pdf(1/self.MLE_wait_1, self.window, x_fit, 1), 'r', lw=1)      
        ax4.set_yscale('log')
        ax4.set_xlabel('Wait Time [s]')
        ax4.set_ylabel('Probability density')
        ax4.set_title('Exp_Finite = %.2f +/- %.2f [s]' %(self.MLE_wait_1, self.Error_wait_1))        
  
        ax5.hist(t2, bins=bins, histtype='step', lw=1, color='k', density=True)
        ax5.plot(x_fit, pdf(1/self.MLE_wait_2, self.window, x_fit, 2), 'r', lw=1)          
        ax5.set_yscale('log')
        ax5.set_xlabel('Wait Time [s]')
        ax5.set_ylabel('Probability density')
        ax5.set_title('Exp_Finite = %.2f +/- %.2f [s]' %(self.MLE_wait_2, self.Error_wait_2))  
   
        ax6.hist(t3, bins=bins, histtype='step', lw=1, color='k', density=True)
        ax6.plot(x_fit, pdf(1/self.MLE_wait_3, self.window, x_fit, 3), 'r', lw=1)         
        ax6.set_yscale('log')
        ax6.set_xlabel('Wait Time [s]')
        ax6.set_ylabel('Probability density')
        ax6.set_title('Exp_Finite = %.2f +/- %.2f [s]' %(self.MLE_wait_3, self.Error_wait_3))  
        fig.savefig(self.dir/'plot8_wait_pdf.png')   
        plt.close(fig)


    def plot9_dwell_icdf(self):

        t1 = np.array(self.dwell_1)
        t2 = np.array(self.dwell_2)
        t3 = np.array(self.dwell_3)
        t_max = max(t1.tolist()+t2.tolist()+t3.tolist())

        bins = np.arange(0, t_max, 1)
        x_fit = np.linspace(0, t_max, 100)

        fig, ((ax1, ax2, ax3), (ax4, ax5, ax6)) = plt.subplots(figsize=(20, 10), ncols=3, nrows=2, dpi=300)

        x1, n1 = get_icdf(t1, self.time_interval)
        ax1.step(x1, n1, where='mid', c='k', lw=1)         
        ax1.plot(x_fit, icdf(1/self.MLE_dwell_1, self.window, x_fit, 1), 'r', lw=1)     
        ax1.set_xlabel('Time [s]')
        ax1.set_ylabel('Survival probability')
        ax1.set_title('Class 1 (N = %d)' %(len(t1)))

        x2, n2 = get_icdf(t2, self.time_interval)
        ax2.step(x2, n2, where='mid', c='k', lw=1)        
        ax2.plot(x_fit, icdf(1/self.MLE_dwell_2, self.window, x_fit, 2), 'r', lw=1)       
        ax2.set_xlabel('Time [s]')
        ax2.set_ylabel('Survival probability')
        ax2.set_title('Class 2 (N = %d), Combined Exp_Finite = %.2f +/- %.2f [s]' %(len(t2), self.MLE_dwell, self.Error_dwell))

        x3, n3 = get_icdf(t3, self.time_interval)
        ax3.step(x3, n3, where='mid', c='k', lw=1)          
        ax3.plot(x_fit, icdf(1/self.MLE_dwell_3, self.window, x_fit, 3), 'r', lw=1)      
        ax3.set_xlabel('Time [s]')
        ax3.set_ylabel('Survival probability')
        ax3.set_title('Class 3 (N = %d)' %(len(t3)))

        ax4.step(x1, n1, where='mid', c='k', lw=1)            
        ax4.plot(x_fit, icdf(1/self.MLE_dwell_1, self.window, x_fit, 1), 'r', lw=1)       
        ax4.set_yscale('log')
        ax4.set_xlabel('Time [s]')
        ax4.set_ylabel('Survival probability')
        ax4.set_title('Exp_Finite = %.2f +/- %.2f [s]' %(self.MLE_dwell_1, self.Error_dwell_1))   

        ax5.step(x2, n2, where='mid', c='k', lw=1)           
        ax5.plot(x_fit, icdf(1/self.MLE_dwell_2, self.window, x_fit, 2), 'r', lw=1)          
        ax5.set_yscale('log')
        ax5.set_xlabel('Time [s]')
        ax5.set_ylabel('Survival probability')
        ax5.set_title('Exp_Finite = %.2f +/- %.2f [s]' %(self.MLE_dwell_2, self.Error_dwell_2)) 

        ax6.step(x3, n3, where='mid', c='k', lw=1)            
        ax6.plot(x_fit, icdf(1/self.MLE_dwell_3, self.window, x_fit, 3), 'r', lw=1)      
        ax6.set_yscale('log')
        ax6.set_xlabel('Time [s]')
        ax6.set_ylabel('Survival probability')
        ax6.set_title('Exp_Finite = %.2f +/- %.2f [s]' %(self.MLE_dwell_3, self.Error_dwell_3))   

        fig.savefig(self.dir/'plot9_dwell_icdf.png')   
        plt.close(fig)


    def plot10_wait_icdf(self):

        t1 = np.array(self.wait_1)
        t2 = np.array(self.wait_2)
        t3 = np.array(self.wait_3)
        t_max = max(t1.tolist()+t2.tolist()+t3.tolist())

        bins = np.arange(0, t_max, 1)
        x_fit = np.linspace(0, t_max, 100)

        fig, ((ax1, ax2, ax3), (ax4, ax5, ax6)) = plt.subplots(figsize=(20, 10), ncols=3, nrows=2, dpi=300)   

        x1, n1 = get_icdf(t1, self.time_interval)
        ax1.step(x1, n1, where='mid', c='k', lw=1)                 
        ax1.plot(x_fit, icdf(1/self.MLE_wait_1, self.n_frame*self.time_interval, x_fit, 1), 'r', lw=1)       
        ax1.set_xlabel('Time [s]')
        ax1.set_ylabel('Survival probability')
        ax1.set_title('Class 1 (N = %d)' %(len(t1)))

        x2, n2 = get_icdf(t2, self.time_interval)
        ax2.step(x2, n2, where='mid', c='k', lw=1)      
        ax2.plot(x_fit, icdf(1/self.MLE_wait_2, self.n_frame*self.time_interval, x_fit, 2), 'r', lw=1)    
        ax2.set_xlabel('Time [s]')
        ax2.set_ylabel('Survival probability')
        ax2.set_title('Class 2 (N = %d), Combined Exp_Finite = %.2f +/- %.2f [s]' %(len(t2), self.MLE_wait, self.Error_wait))

        x3, n3 = get_icdf(t3, self.time_interval)
        ax3.step(x3, n3, where='mid', c='k', lw=1)        
        ax3.plot(x_fit, icdf(1/self.MLE_wait_3, self.n_frame*self.time_interval, x_fit, 3), 'r', lw=1)     
        ax3.set_xlabel('Time [s]')
        ax3.set_ylabel('Survival probability')
        ax3.set_title('Class 3 (N = %d)' %(len(t3)))

        ax4.step(x1, n1, where='mid', c='k', lw=1)          
        ax4.plot(x_fit, icdf(1/self.MLE_wait_1, self.n_frame*self.time_interval, x_fit, 1), 'r', lw=1)      
        ax4.set_yscale('log')
        ax4.set_xlabel('Time [s]')
        ax4.set_ylabel('Survival probability')
        ax4.set_title('Exp_Finite = %.2f +/- %.2f [s]' %(self.MLE_wait_1, self.Error_wait_1))  

        ax5.step(x2, n2, where='mid', c='k', lw=1)       
        ax5.plot(x_fit, icdf(1/self.MLE_wait_2, self.n_frame*self.time_interval, x_fit, 2), 'r', lw=1)     
        ax5.set_yscale('log')
        ax5.set_xlabel('Time [s]')
        ax5.set_ylabel('Survival probability')
        ax5.set_title('Exp_Finite = %.2f +/- %.2f [s]' %(self.MLE_wait_2, self.Error_wait_2)) 

        ax6.step(x3, n3, where='mid', c='k', lw=1)         
        ax6.plot(x_fit, icdf(1/self.MLE_wait_3, self.n_frame*self.time_interval, x_fit, 3), 'r', lw=1)      
        ax6.set_yscale('log')
        ax6.set_xlabel('Time [s]')
        ax6.set_ylabel('Survival probability')
        ax6.set_title('Exp_Finite = %.2f +/- %.2f [s]' %(self.MLE_wait_3, self.Error_wait_3))  

        fig.savefig(self.dir/'plot10_wait_icdf.png')   
        plt.close(fig)



    def plot_trace_fit(self):
        # Make a new Trace folder   
        print("Plotting traces...")                                                                                                                                                                                                                                                                                      
        trace_dir = self.dir/'Traces'
        if os.path.exists(trace_dir): # Delete if already existing 
            shutil.rmtree(trace_dir)
        os.makedirs(trace_dir)
                
        # Save each trace
        time = np.arange(self.n_frame)*self.time_interval
        n_fig = min(self.save_trace, len(self.trace))        
        for i in range(n_fig):    
            r = self.spot_row[i]
            c = self.spot_col[i]
            s = int((self.spot_size-1)/2)
            I_row = np.transpose(np.squeeze(self.I[:,r-s:r+s+1,c]))
            I_col = np.transpose(np.squeeze(self.I[:,r,c-s:c+s+1]))

            fig, (ax1, ax2, ax3, ax4) = plt.subplots(figsize=(20, 10), ncols=1, nrows=4, dpi=300)   

            ax1.plot(time, self.trace[i], 'k', lw=2)
            color = ['b', 'r']
            ax1.plot(time, self.trace_fit[i], color=color[int(self.is_trace_inlier[i])], lw=2)    
            ax1.axhline(y=self.I_u_inlier.mean(), c='k', ls='--', lw=1) 
            ax1.axhline(y=self.I_b_inlier.mean(), c='k', ls='--', lw=1)     
            ax1.set_ylim([0, 1.5*self.I_b_inlier.mean()])                        
            ax1.set_ylabel('Intensity')
            ax1.set_xlabel('Time [s]')
            if self.is_trace_inlier[i] == True:
                title_sp = 'Data (K), Fit: Inlier (R)' 
            else:
                title_sp = 'Data (K), Fit: Outlier (B)'
            ax1.set_title(title_sp)

            ax2.plot(time, self.trace[i]-self.trace_fit[i], 'k', lw=2)        
            ax2.axhline(y=0, c='k', ls='-', lw=1)      
            ax2.axhline(y=max(self.rmsd_inlier), c='k', ls='--', lw=1)                     
            ax2.axhline(y=-max(self.rmsd_inlier), c='k', ls='--', lw=1)    
            ax2.set_ylim([-3*max(self.rmsd_inlier), 3*max(self.rmsd_inlier)])            
            ax2.set_ylabel('Intensity')
            ax2.set_xlabel('Time [s]')
            ax2.set_title('Residual')

            ax3.imshow(I_row, cmap='gray')
            ax3.set_xlabel('Frame')            
            ax3.set_ylabel('Row')
            ax3.set_title('Y = %d' %(r))

            ax4.imshow(I_col, cmap='gray')
            ax4.set_xlabel('Frame')            
            ax4.set_ylabel('Col')
            ax4.set_title('X = %d' %(c))

            fig.subplots_adjust(wspace=0.3, hspace=0.5)
            print("Save Trace %d (%d %%)" % (i+1, ((i+1)/n_fig)*100))
            fig_name = 'Trace%d.png' %(i+1)
            fig.savefig(trace_dir/fig_name) 
            fig.clf()
            plt.close(fig)   


                    
def main():
    start = timer()

    # Find all the movies (*.tif) in the directory tree
    movie_paths = [fn for fn in directory.glob('**/*.tif')]
#                   if not fn.name == 'GFP.tif']

    print('%d movies are found' %(len(movie_paths)))

    # Run through each movie
    for i, movie_path in enumerate(movie_paths):
        print('='*100)
        print('Movie #%d/%d' %(i+1, len(movie_paths)))
        print('Path:', movie_path.parent)
        print('Name:', movie_path.name)

        # Check info.txt exists.
        info_file = Path(movie_path.parent/'info.txt')
        if not info_file.exists():
            print('\ninfo.txt does not exist.\n')
            continue

        # Pass if result.txt already exists. 
        result_file = Path(movie_path.parent/'result.txt')
        if result_file.exists() and pass_with_result:
            print('\nresult.txt already exist.\n')
            continue            

        # Make a movie instance
        movie = Movie(movie_path)

        # Read the movie
        movie.read_movie()

        # Corrections: offset, flatfield, drift
        movie.correct_offset()
        movie.correct_flatfield()        
        movie.correct_drift()         

        # Find peaks where molecules bind
        movie.find_peak()

        # Find spots with good signal
        movie.find_spot()

        # Fit spots
        movie.fit_spot()          

        # Find binding, unbinding events
        movie.find_event()

        # Exclude short events
        movie.exclude_short()

        # Exclude long events
        movie.exclude_long()

        # Estimate dwell time
        movie.estimate_time()

        # Save the result into result.txt
        movie.save_result()

        # Plot the result     
        print("\nPlotting figures...\n")  
        movie.plot0_clean()
        movie.plot1_original_min_max()     
        movie.plot2_flatfield()              
        movie.plot3_drift()             
        movie.plot4_peak()  
        movie.plot5_spot()  
        movie.plot6_spot_fit()
        movie.plot7_dwell_pdf()
        movie.plot8_wait_pdf()
        movie.plot9_dwell_icdf()
        movie.plot10_wait_icdf()
        movie.plot_trace_fit() 

        end = timer()
        print('\n%d seconds have passed.\n' %(end-start))
        start = end

if __name__ == "__main__":
    main()


