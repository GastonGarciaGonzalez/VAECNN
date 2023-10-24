# -*- coding: utf-8 -*-
"""
Created on Fri Apr 29 07:03:05 2022

@author: gastong@fing.edu.uy
"""



import tensorflow as tf
from tensorflow.keras.layers import Layer, Input, Conv1D, Lambda, Reshape, RepeatVector, Concatenate, Cropping1D
from tensorflow.keras.models import Model
from tensorflow.keras import backend as K
from tensorflow.keras import optimizers
from tensorflow import keras
import pandas as pd
import numpy as np
from sklearn.metrics import f1_score, recall_score, precision_score
from prts import ts_precision, ts_recall
import pickle
from utils import set_index, preprocessing, MTS2UTS_cond, UTS2MTS

@keras.utils.register_keras_serializable()
class Sampling(Layer):
    """Uses (z_mean, z_log_var) to sample z, the vector encoding a digit."""
    def __init__(self, name=None, k=1, **kwargs):
        super(Sampling, self).__init__(name=name)
        self.k = k
        super(Sampling, self).__init__(**kwargs)
        
    def get_config(self):
        config = super(Sampling, self).get_config()
        config['k'] = self.k
        return config #dict(list(config.items()))
    
    def call(self, inputs):
        z_mean, z_log_var = inputs
        batch = K.shape(z_mean)[0]
        seq = K.shape(z_mean)[1]
        dim = K.shape(z_mean)[2]
        epsilon = K.random_normal(shape=(batch, seq, dim))
        return z_mean + tf.exp(0.5 * z_log_var) * epsilon


class DCVAE:
    
    def __init__(self,
                 T=32,
                 M=12,
                 cnn_units = [16,16,16,16,16],
                 dil_rate = [1, 2, 4, 8,16],
                 kernel=2,
                 strs=1,
                 batch_size=32,
                 J=1,
                 epochs=100,
                 learning_rate=1e-3,
                 lr_decay=True,
                 decay_rate=0.96,
                 decay_step=1000,
                 name = '',
                 epsilon = 1e-12,  
                 summary=False,
                 ):
        
        
        # network parameters
        input_shape = (T, 1)
        self.cnn_units = cnn_units
        self.dil_rate = dil_rate
        self.M = M
        self.T = T
        self.J = J
        self.batch_size = batch_size
        self.epochs = epochs  
        self.name = name

        
        # model = encoder + decoder
        
        # Build encoder model
        # =============================================================================
        # model = encoder + decoder
        
        # Build encoder model
        # =============================================================================
        # Input
        in_sam = Input(shape=input_shape, name='input_samples')
        in_time_class_info = Input(shape=(T, 9), name='input_time_class_info')

        # Hidden layers (1D Dilated Convolution)
        h_enc_cnn = Concatenate(axis=-1)([in_sam, in_time_class_info])
        for i in range(len(cnn_units)):
            h_enc_cnn = Conv1D(cnn_units[i], kernel, activation='selu', use_bias=False,
                           strides=strs, padding="causal",
                           dilation_rate=dil_rate[i], name='dcnn_enc_%d'%(i))(h_enc_cnn)
            
        # Lastest    
        z_mean = Conv1D(J, 1, activation=None,
                           strides=strs, padding="causal", name='z_mean', use_bias=False)(h_enc_cnn)
        z_log_var = Conv1D(J, 1, activation=None,
                           strides=strs, padding="causal", name='z_log_var', use_bias=False)(h_enc_cnn)
                    
        z_mean = Cropping1D(cropping=(self.T-1, 0))(z_mean)
        z_log_var = Cropping1D(cropping=(self.T-1, 0))(z_log_var)
            
        # Reparameterization trick 
        # Output
        z = Sampling(name='z')((z_mean, z_log_var))
        # Instantiate encoder model
        self.encoder = Model([in_sam, in_time_class_info], [z_mean, z_log_var, z], name='encoder')

        if summary:
            self.encoder.summary() 
        # =============================================================================

        # Build decoder model
        # =============================================================================
        # Input
        latent = Input(shape=(1, J), name='z_sampling_z')

        # Hidden layers (1D Dilated Convolution)
        latent_reshape = Reshape((J, ), input_shape=(1, J))(latent)
        repeat_z = RepeatVector(T)(latent_reshape)
        h_dec_cnn = Concatenate(axis=-1)([repeat_z, in_time_class_info])

        for i in range(len(cnn_units)):
            h_dec_cnn = Conv1D(cnn_units[i], kernel, activation='selu', use_bias=False,
                           strides=strs, padding="causal",
                           dilation_rate=dil_rate[i], name='dcnn_dec_%d'%i)(h_dec_cnn)
            
        # Lastest/Output
        x__mean = Conv1D(1, 1, activation=None, padding="causal",
                                  name='x__mean_output', use_bias=False)(h_dec_cnn)
        x_log_var = Conv1D(1, 1, activation=None, padding="causal",
                                  name='x_log_var_output', use_bias=False)(h_dec_cnn)

        # Instantiate decoder model
        self.decoder = Model([latent, in_time_class_info], [x__mean, x_log_var], name='decoder')
        if summary:
            self.decoder.summary()
        # =============================================================================

        # Instantiate DC-VAE model
        # =============================================================================
        [x__mean, x_log_var] = self.decoder([
            self.encoder([in_sam, in_time_class_info])[2], in_time_class_info])
        self.vae = Model([in_sam, in_time_class_info], [x__mean, x_log_var], name='vae')
        
        # Loss
        # Reconstruction term
        MSE = -0.5*K.mean(K.square((in_sam - x__mean)/K.exp(x_log_var)),axis=-1) #Mean in M
        sigma_trace = -K.mean(x_log_var, axis=(-1)) #Mean in M
        log_likelihood = MSE+sigma_trace
        reconstruction_loss = K.mean(-log_likelihood) #Mean in the batch and T  
       
        # Priori hypothesis term
        kl_loss = 1 + z_log_var - K.square(z_mean) - K.exp(z_log_var)
        kl_loss = K.mean(kl_loss, axis=-1) #Mean in J
        kl_loss *= -0.5
        kl_loss = tf.reduce_mean(kl_loss) #Mean in the batch and T
        
        # Total
        vae_loss = K.mean(reconstruction_loss + kl_loss)
        self.vae.add_loss(vae_loss)
        
        # Learning rate
        if lr_decay: 
            lr = optimizers.schedules.ExponentialDecay(learning_rate,
                                                    decay_steps=decay_step,
                                                    decay_rate=decay_rate,
                                                    staircase=True,
                                                    )
        else:
            lr = learning_rate

        # Optimaizer
        opt = optimizers.Adam(learning_rate=lr)

        # Metrics
        self.vae.add_metric(reconstruction_loss, name='reconst')
        self.vae.add_metric(kl_loss, name='kl')

        self.vae.compile(optimizer=opt)


    def fit(self, df_X=None, val_percent=0.1, seed=42):
    
        # Data preprocess
        X, cond_info = MTS2UTS_cond(df_X, T=self.T)
        ix_rand = np.random.permutation(X.shape[0])
        X = np.array(X)[ix_rand]
        cond_info = np.array(cond_info)[ix_rand]  

        # Callbacks
        early_stopping_cb = keras.callbacks.EarlyStopping(min_delta=1e-2,
                                                      patience=5,                                            
                                                      verbose=1,
                                                      mode='min')
        model_checkpoint_cb= keras.callbacks.ModelCheckpoint(
            filepath=self.name+'_best_model.h5',
            verbose=1,
            mode='min',
            save_best_only=True)
        
          
        # Model train
        self.history_ = self.vae.fit((X, cond_info),
                     batch_size=self.batch_size,
                     epochs=self.epochs,
                     validation_split = val_percent,
                     callbacks=[early_stopping_cb,
                                model_checkpoint_cb]
                     ) 
        
        # Save models
        self.encoder.save(self.name+'_encoder.h5')
        self.decoder.save(self.name+'_decoder.h5')
        self.vae.save(self.name+'_complete.h5')

        return self




    def alpha_selection(self, load_model=False, df_X=None, df_y=None,
                           custom_metrics=False, al=0, cardinality='reciprocal',
                           bias='front'):
        
                   
        # Model
        if load_model:
            self.vae = keras.models.load_model(self.name+'_complete.h5',
                                                  custom_objects={'sampling': Sampling},
                                                  compile = False)

        # Inference model. Auxiliary model so that in the inference 
        # the prediction is only the last value of the sequence
        inp = Input(shape=(self.T, self.M))
        x = self.vae(inp) # apply trained model on the input
        out = Lambda(lambda y: [y[0][:,-1,:], y[1][:,-1,:]])(x)
        inference_model = Model(inp, out)

        # Data
        X = df_X.values
        y = df_y.values
        dataset_val_th = timeseries_dataset_from_array(
            X, None, self.T, sequence_stride=1, sampling_rate=1,
            batch_size=self.batch_size)  
            
        # Predict
        prediction = inference_model.predict(dataset_val_th)
        # The first T-1 data of each sequence are discarded
        reconstruct = prediction[0]
        log_var = prediction[1]
        sig = np.sqrt(np.exp(log_var))
        
        # Data evaluate (The first T-1 data are discarded)
        X_evaluate = X[self.T-1:]
        y_evaluate = y[self.T-1:]
        
        print('Alpha selection...')
        best_f1 = np.zeros(self.M)
        max_alpha = 7
        best_alpha_up = max_alpha*np.ones(self.M)
        best_alpha_down = max_alpha*np.ones(self.M)
        for alpha_up in np.arange(max_alpha, 1, -1):
            for alpha_down in np.arange(max_alpha, 1, -1):
                
                thdown = reconstruct - alpha_down*sig
                
                thup = reconstruct + alpha_up*sig
                
                pre_predict = (X_evaluate < thdown) | (X_evaluate > thup)
                pre_predict = pre_predict.astype(int)
                
                for c in range(self.M):
                    if custom_metrics:
                        if np.allclose(np.unique(pre_predict[:,c]), np.array([0, 1])) or np.allclose(np.unique(pre_predict[:,c]), np.array([1])):
                            pre_value = ts_precision(y_evaluate[:,c], pre_predict[:,c], 
                                          al, cardinality, bias)
                            rec_value = ts_recall(y_evaluate[:,c], pre_predict[:,c], 
                                          al, cardinality, bias)
                            f1_value = 2*(pre_value*rec_value)/(pre_value+rec_value+1e-6)
                        else:
                            pre_value = 0
                            rec_value = 0
                            f1_value = 0
                    else:
                        f1_value = f1_score(y_evaluate[:,c], pre_predict[:,c], pos_label=1)
                        pre_value = precision_score(y_evaluate[:,c], pre_predict[:,c], pos_label=1)
                        rec_value = recall_score(y_evaluate[:,c], pre_predict[:,c], pos_label=1)
                    
                    if f1_value >= best_f1[c]:
                        best_f1[c] = f1_value
                        best_alpha_up[c] = alpha_up
                        best_alpha_down[c] = alpha_down

        self.alpha_up = best_alpha_up
        self.alpha_down = best_alpha_down
        self.f1_val = best_f1
        
        with open(self.name + '_alpha_up.pkl', 'wb') as f:
            pickle.dump(best_alpha_up, f)
            f.close()
        with open(self.name + '_alpha_down.pkl', 'wb') as f:
            pickle.dump(best_alpha_down, f)
            f.close()
        
        return self

         
    def predict(self,
                df_X=None, 
                load_model=False,
                model='best_model',
                only_predict=True,
                load_alpha=True,
                alpha_set=[]):
        
        # Trained model
        if load_model:
            self.vae = keras.models.load_model(self.name+'_'+model+'.h5',
                                                    custom_objects={'sampling': Sampling},
                                                    compile = True)
            self.encoder = keras.models.load_model(self.name+'_encoder.h5',
                                                    custom_objects={'sampling': Sampling},
                                                    compile = False)

        # Inference model. Auxiliary model so that in the inference 
        # the prediction is only the last value of the sequence
        inp = Input(shape=(self.T, self.M))
        x = self.vae(inp) # apply trained model on the input
        out = Lambda(lambda y: [y[0][:,-1,:], y[1][:,-1,:]])(x)
        inference_model = Model(inp, out)
        
        # Data preprocess
        sam_val, sam_ix, sam_class = MTS2UTS_cond(df_X, T=self.T)
        
        # Predictions
        prediction = self.vae.predict(np.stack(sam_val))
        # The first T-1 data of each sequence are discarded
        reconstruct = prediction[0]
        sig = np.sqrt(np.exp(prediction[1]))
        
        # Data evaluate (The first T-1 data are discarded)
        df_evaluate = UTS2MTS(sam_val, sam_ix, sam_class)
        df_reconstruct = UTS2MTS(reconstruct, sam_ix, sam_class)
        df_sig = UTS2MTS(sig, sam_ix, sam_class)
        
        # Thresholds
        if len(alpha_set) == self.M:
            alpha = np.array(alpha_set)
        elif load_alpha:
            with open(self.name + '_alpha.pkl', 'rb') as f:
                alpha = pickle.load(f)
                f.close()
        else:
            alpha = self.alpha
            
        thdown = df_reconstruct.values - alpha*df_sig.values
        thup = df_reconstruct.values + alpha*df_sig.values
        
        # Evaluation
        pred = (df_evaluate.values < thdown) | (df_evaluate.values > thup)
        df_predict = pd.DataFrame(pred, columns=df_X.columns, index=df_X.iloc[self.T-1:].index)
        
        if only_predict:
            return df_predict
        else:
            latent_space = self.encoder.predict(np.stack(sam_val))[2]
            return df_predict, df_reconstruct, df_sig, latent_space, sam_ix, sam_class


    def evaluate(self, df_X=None, load_model=False, model='best_model'):
        # Data preprocess
        sam_val, sam_ix, sam_class = MTS2UTS(df_X, T=self.T)

        # Trained model
        if load_model:
            print('=====================================')
            self.vae = keras.models.load_model(self.name+'_'+model+'.h5',
                                                    custom_objects={'sampling': Sampling},
                                                    compile = True)

        # Model evaluate
        value_elbo, reconstruction, kl = self.vae.evaluate(np.stack(sam_val), np.stack(sam_val),
                     batch_size=self.batch_size,
                     )  
        
        return value_elbo, reconstruction, kl
        