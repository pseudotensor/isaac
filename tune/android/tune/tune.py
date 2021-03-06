# Copyright 2015-2017 Philippe Tillet
# 
# Permission is hereby granted, free of charge, to any person obtaining 
# a copy of this software and associated documentation files 
# (the "Software"), to deal in the Software without restriction, 
# including without limitation the rights to use, copy, modify, merge, 
# publish, distribute, sublicense, and/or sell copies of the Software, 
# and to permit persons to whom the Software is furnished to do so, 
# subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be 
# included in all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, 
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF 
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, 
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE 
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.


import random, argparse, json, os
from math import log, isinf
from itertools import chain, product
from numpy import argsort, argmax, where, delete, bincount
from operator import mul
import isaac as sc
from tools import profile_execution_failure
from isaac.external.sklearn.forest import RandomForestRegressor
import optimize, tools, model
from json import encoder
import json, csv
import numpy as np


encoder.FLOAT_REPR = lambda o: format(o, '.2f')
encoder.separators = (',',':')

def unique(L):
    seen = set()
    seen_add = seen.add
    return [ x for x in L if not (x in seen or seen_add(x))]

def pow2range(a, b):
    return [2**x for x in range(a, b)]


class Tuner:

    def __init__(self, logger, device, operation, dtype, json_path, progress_bar):
        self.logger = logger
        self.device = device
        self.operation = operation
        self.dtype = dtype
        self.json_path = json_path
        self.progress_bar = progress_bar
        
  
    def run(self, level = 'intermediate'): 
        
        assert level in ['simple', 'intermediate', 'full']
        tools.dtype = self.dtype
        device = self.device
        operation = self.operation
        context = sc.driver.context(device)
        
        if self.logger:
            self.logger.info("----------------")
            self.logger.info(operation.__name__.replace('_','-').upper())
            self.logger.info(tools.dtype.__name__.upper())
            self.logger.info("----------------")

        #BLAS1 training sizes
        if operation in [sc.templates.elementwise_1d, sc.templates.reduce_1d]:
            sizes = [(10**x,) for x in range(3,8)]
        
        #BLAS2 training sizes
        if operation in [sc.templates.elementwise_2d, sc.templates.reduce_2d_rows, sc.templates.reduce_2d_cols]:
            sizes = []
            #Square
            for N in [896, 1280, 1760, 2560]:
                sizes += [(N, N)]
            #Short/Fat
            for M in [16, 32, 64, 128, 512, 1024]:
                for N in [1024, 4096, 16384, 65536]:
                    sizes += [(M, N)]
            #Tall/Skinny
            for N in [16, 32, 64, 128, 512, 1024]:
                for M in [1024, 4096, 16384, 65536]:
                    sizes += [(M, N)]
        
        #BLAS3 training sizes
        if operation in [sc.templates.gemm_nn, sc.templates.gemm_nt, sc.templates.gemm_tn, sc.templates.gemm_tt]:
            sizes = []
            #Square
            for N in [896, 1760, 2048, 2560]:
                sizes += [(N, N, N)]
            #LaPack
            for N in [896, 1760, 2048, 2560]:
			   for K in [16, 32, 64, 128]:
				   sizes += [(N, N, K)]
            #Covariance
            for N in [16, 32, 64, 128, 256]:
			   for K in [16000,32000,64000,128000]:
				   sizes += [(N, N, K)]
            #DeepSpeech
            for M in [1760, 2048, 2560, 4096]:
                for N in [16, 32, 64, 128, 7000]:
                    sizes += [(M, N, M)]
            for K in [1760, 2048, 2560, 4096]:
				for M, N in [(5124,9124),(35,8457)]:
					sizes += [(M, N, K)]
            for M, K in [(7680,2560),(3072,1024)]:
                for N in [16, 32, 64, 128]:
					sizes += [(M, N, K)]

        #Training data
        performance = tools.metric_of(operation)
        profiles, X, Y = [], [], []
        
        #Restore progress
        savepath = os.path.join('save', tools.dtype.__name__, operation.__name__)
        if not os.path.exists(savepath):
            os.makedirs(savepath)
        try:
            with open(os.path.join(savepath, 'X.csv')) as f:
                X = [tuple(map(int, row)) for row in csv.reader(f, delimiter=',')]
            with open(os.path.join(savepath, 'profiles.csv')) as f:
                profiles = [map(int,row) for v in row for row in csv.reader(f, delimiter=',')]
            with open(os.path.join(savepath, 'Y.csv')) as f:
                Y = [map(float, row) for row in csv.reader(f, delimiter=',')]
            #Recompute Y
            #Y = []
            #for x in X:
            #    tree, _ = tools.tree_of(operation, x, context)
            #    Y.append([performance(x, tools.benchmark(operation(*best), tree)) for best in profiles])
        except:
            pass
        
      	#Save data
        def save():
            for (fname, data) in zip(['X.csv',  'Y.csv', 'profiles.csv'], [X, Y, profiles]):
                with open(os.path.join(savepath, fname), 'wb') as f:
                    csv.writer(f).writerows(data)
        #Tuning
        for idx, x in enumerate(sizes):
            #Create new line on log
            if idx>0:
             self.progress_bar.set_finished()
            self.progress_bar.set_prefix(', '.join(map(str, x)))
            #Skip if already saved
            if x in X:
                row = Y[X.index(x)]
                self.progress_bar.update(1, 1, profiles[argmax(row)], max(row))
                continue
            #Best existing profile for x
            tree, operands = tools.tree_of(operation, x, context)
            y = [performance(x, tools.benchmark(operation(*p), tree)) for p in profiles]
            best = profiles[np.argmax(y)] if y else None            
            #Retune if necessary
            tune =  not (best and optimize.is_local_optimum(best, operation, x, context))
            if tune:
                optimizer = optimize.GeneticOptimizer(self.logger, naccept=1000, niter=1000, cxpb=.4, mutpb=.4, popsize=20, progress_bar = self.progress_bar)
                best = optimizer.run(operation, x, context, prior=best)[0]
                if best not in profiles:
                    profiles.append(best)
                    for xx,yy in zip(X, Y):
                        tree, _ = tools.tree_of(operation, xx, context)
                        time = tools.benchmark(operation(*best), tree)
                        yy.append(performance(xx, time))
            #Update dataset
            X.append(x)
            tree, operands = tools.tree_of(operation, x, context)
            y = [performance(x,tools.benchmark(operation(*prf), tree)) for prf in profiles]
            Y.append(y)
            #Save data
            save()
            #print performance info in case no tuning was done
            if not tune:
                row = Y[X.index(x)]
                self.progress_bar.update(1, 1, profiles[argmax(row)], max(row))
        self.progress_bar.set_finished()
        save()     
        #Adding external profiles
        for prof in tools.external_profiles(operation):
			profiles.append(prof.__class__.__name__)
			for x, y in zip(X, Y):
				tree, operands = tools.tree_of(operation, x, context)
				perf = performance(x,tools.benchmark(prof, tree, operation))
				y.append(perf)
        #Pruning of useless profiles
        X = np.array(X)
        Y = np.array(Y)
        if len(Y[0]) > 1:
            idx = np.where(np.bincount(np.argmax(Y, 1), minlength=len(profiles))==0)[0]
            profiles = [p for ip,p in enumerate(profiles) if ip not in idx]
            Y = np.delete(Y, idx, axis=1) 
        #Exporting to JSON
        json_path = tools.sanitize(device.name) + '.json' if not self.json_path else self.json_path
        if os.path.isfile(json_path):
            json_data = json.load(open(json_path, 'r'))
        else:
            json_data = {}
            json_data["version"] = "1.0"
        operation_name = operation.__name__
        if operation_name not in json_data:
            json_data[operation_name] = {}
        json_data[operation_name][tools.dtype.__name__] = {}
        D = json_data[operation_name][tools.dtype.__name__]
        if len(profiles) > 1:
            clf, nrmse = model.train(X, Y, profiles)
            D['predictor'] = [{'children_left': e.tree_.children_left.tolist(),
                                'children_right': e.tree_.children_right.tolist(),
                                'threshold': e.tree_.threshold.astype('float64').tolist(),
                                'feature': e.tree_.feature.astype('float64').tolist(),
                                'value': e.tree_.value[:,:,0].astype('float64').tolist()} for e in clf.estimators_]
        D['profiles'] = [tools.convert(x) for x in profiles]
        json.dump(json_data, open(json_path,'w'))
