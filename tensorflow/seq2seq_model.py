
#!/usr/bin/env python
# -*- coding: utf-8 -*-
import math

import numpy as np
import tensorflow as tf
import tensorflow.contrib.seq2seq as seq2seq

from tensorflow.python.ops.rnn_cell import GRUCell
from tensorflow.python.ops.rnn_cell import LSTMCell
from tensorflow.python.ops.rnn_cell import MultiRNNCell
from tensorflow.python.ops.rnn_cell import DropoutWrapper, ResidualWrapper
# for bidictional rnn
from tensorflow.contrib import rnn

from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.layers.core import Dense
from tensorflow.python.util import nest

from tensorflow.contrib.seq2seq.python.ops import attention_wrapper
from tensorflow.contrib.seq2seq.python.ops import beam_search_decoder

import data_utils as data_utils

class Seq2SeqModel(object):

    def __init__(self, config, mode):

        assert mode.lower() in ['train', 'decode']

        self.config = config
        self.mode = mode.lower()

        self.cell_type = config['cell_type']
        self.hidden_units = config['hidden_units']
        self.depth = config['depth']
        self.attention_type = config['attention_type']
        self.embedding_size = config['embedding_size']
        self.bidirectional = config['bidirectional']

        # hidden_units for encoder and decoder
        self.encoder_hidden_units = self.hidden_units
        self.decoder_hidden_units = 2 * self.hidden_units if self.bidirectional else self.hidden_units
       
        self.num_encoder_symbols = config['num_encoder_symbols']
        self.num_decoder_symbols = config['num_decoder_symbols']

        self.use_residual = config['use_residual']
        self.attn_input_feeding = config['attn_input_feeding']
        self.use_dropout = config['use_dropout']
        self.keep_prob = 1.0 - config['dropout_rate']

        self.optimizer = config['optimizer']
        self.learning_rate = config['learning_rate']
        self.max_gradient_norm = config['max_gradient_norm']
        self.global_step = tf.Variable(0, trainable=False, name='global_step')
        self.global_epoch_step = tf.Variable(0, trainable=False, name='global_epoch_step')
        self.global_epoch_step_op = \
	    tf.assign(self.global_epoch_step, self.global_epoch_step+1)

        self.dtype = tf.float16 if config['use_fp16'] else tf.float32
        self.keep_prob_placeholder = tf.placeholder(self.dtype, shape=[], name='keep_prob')

        self.use_beamsearch_decode=False 
        if self.mode == 'decode':
            self.beam_width = config['beam_width']
            self.use_beamsearch_decode = True if self.beam_width > 1 else False
            self.max_decode_step = config['max_decode_step']
 
        self.build_model()

       
    def build_model(self):
        print("building model..")

        self.init_placeholders()
        self.build_encoder()
        self.build_decoder()

        self.summary_op = tf.summary.merge_all()


    def init_placeholders(self):       
        # encoder_inputs: [batch_size, max_time_steps]
        self.encoder_inputs = tf.placeholder(dtype=tf.int32,
            shape=(None, None), name='encoder_inputs')

        # encoder_inputs_length: [batch_size]
        self.encoder_inputs_length = tf.placeholder(
            dtype=tf.int32, shape=(None,), name='encoder_inputs_length')

        # get dynamic batch_size
        self.batch_size = tf.shape(self.encoder_inputs)[0]
        if self.mode == 'train':

            # decoder_inputs: [batch_size, max_time_steps]
            self.decoder_inputs = tf.placeholder(
                dtype=tf.int32, shape=(None, None), name='decoder_inputs')
            # decoder_inputs_length: [batch_size]
            self.decoder_inputs_length = tf.placeholder(
                dtype=tf.int32, shape=(None,), name='decoder_inputs_length')

            decoder_start_token = tf.ones(
                shape=[self.batch_size, 1], dtype=tf.int32) * data_utils.START_TOKEN
            decoder_end_token = tf.ones(
                shape=[self.batch_size, 1], dtype=tf.int32) * data_utils.END_TOKEN            

            # decoder_inputs_train: [batch_size , max_time_steps + 1]
            self.decoder_inputs_train = tf.concat([decoder_start_token,
                                                  self.decoder_inputs], axis=1)

            # decoder_inputs_length_train: [batch_size]
            self.decoder_inputs_length_train = self.decoder_inputs_length + 1

            self.decoder_targets_train = tf.concat([self.decoder_inputs,
                                                   decoder_end_token], axis=1)


    def build_encoder(self):
        print("building encoder..")
        with tf.variable_scope('encoder'):
            # Building encoder_cell
            self.encoder_cell = self.build_encoder_cell()

            sqrt3 = math.sqrt(3)
            initializer = tf.random_uniform_initializer(-sqrt3, sqrt3, dtype=self.dtype)
             
            self.encoder_embeddings = tf.get_variable(name='embedding',
                shape=[self.num_encoder_symbols, self.embedding_size],
                initializer=initializer, dtype=self.dtype)
            
            # Embedded_inputs: [batch_size, time_step, embedding_size]
            self.encoder_inputs_embedded = tf.nn.embedding_lookup(
                params=self.encoder_embeddings, ids=self.encoder_inputs)

            input_layer = Dense(self.encoder_hidden_units, dtype=self.dtype, name='input_projection')

            # Embedded inputs having gone through input projection layer
            self.encoder_inputs_embedded = input_layer(self.encoder_inputs_embedded)


            if self.bidirectional:
                with tf.variable_scope("BidirectionalEncoder"):
                    ((encoder_fw_outputs,
                      encoder_bw_outputs),
                     (encoder_fw_state,
                      encoder_bw_state)) = (
                        tf.nn.bidirectional_dynamic_rnn(
                            # cell_fw=self.encoder_cell[0] if isinstance(self.encoder_cell, tuple) else self.encoder_cell,
                            # cell_bw=self.encoder_cell[1] if isinstance(self.encoder_cell, tuple) else self.encoder_cell,
                            cell_fw=self.encoder_cell,
                            cell_bw=self.encoder_cell,
                            inputs=self.encoder_inputs_embedded,
                            sequence_length=self.encoder_inputs_length,
                            time_major=False,
                            dtype=self.dtype))

                    self.outputs = tf.concat(
                        (encoder_fw_outputs, encoder_bw_outputs), 2,
                        name="bidirectional_output_concat")
                    # output = tf.concat([output_fw, output_bw], axis=-1)

                    if isinstance(encoder_fw_state, rnn.LSTMStateTuple):  # LstmCell
                        state_c = tf.concat(
                            (encoder_fw_state.c, encoder_bw_state.c), 1, name="bidirectional_concat_c")
                        state_h = tf.concat(
                            (encoder_fw_state.h, encoder_bw_state.h), 1, name="bidirectional_concat_h")
                        self.state = rnn.LSTMStateTuple(c=state_c, h=state_h)
                    elif isinstance(encoder_fw_state, tuple) \
                            and isinstance(encoder_fw_state[0], rnn.LSTMStateTuple):  # MultiLstmCell
                        self.state = tuple(map(
                            lambda fw_state, bw_state: rnn.LSTMStateTuple(
                                c=tf.concat((fw_state.c, bw_state.c), 1,
                                            name="bidirectional_concat_c"),
                                h=tf.concat((fw_state.h, bw_state.h), 1,
                                            name="bidirectional_concat_h")),
                            encoder_fw_state, encoder_bw_state))
                    else:
                        self.state = tf.concat(
                            (encoder_fw_state, encoder_bw_state), 1,
                            name="bidirectional_state_concat")
                self.encoder_outputs, self.encoder_last_state = self.outputs, self.state

            else:
                self.encoder_outputs, self.encoder_last_state = tf.nn.dynamic_rnn(
                    cell=self.encoder_cell, inputs=self.encoder_inputs_embedded,
                    sequence_length=self.encoder_inputs_length, dtype=self.dtype,
                    time_major=False)


    def build_decoder(self):
        print("building decoder and attention..")
        with tf.variable_scope('decoder'):
            # Building decoder_cell and decoder_initial_state
            self.decoder_cell, self.decoder_initial_state = self.build_decoder_cell()

            sqrt3 = math.sqrt(3)
            initializer = tf.random_uniform_initializer(-sqrt3, sqrt3, dtype=self.dtype)
             
            self.decoder_embeddings = tf.get_variable(name='embedding',
                shape=[self.num_decoder_symbols, self.embedding_size],
                initializer=initializer, dtype=self.dtype)

            input_layer = Dense(self.decoder_hidden_units, dtype=self.dtype, name='input_projection')

            output_layer = Dense(self.num_decoder_symbols, name='output_projection')

            if self.mode == 'train':
                # decoder_inputs_embedded: [batch_size, max_time_step + 1, embedding_size]
                self.decoder_inputs_embedded = tf.nn.embedding_lookup(
                    params=self.decoder_embeddings, ids=self.decoder_inputs_train)
               
                # Embedded inputs having gone through input projection layer
                self.decoder_inputs_embedded = input_layer(self.decoder_inputs_embedded)

                training_helper = seq2seq.TrainingHelper(inputs=self.decoder_inputs_embedded,
                                                   sequence_length=self.decoder_inputs_length_train,
                                                   time_major=False,
                                                   name='training_helper')

                training_decoder = seq2seq.BasicDecoder(cell=self.decoder_cell,
                                                   helper=training_helper,
                                                   initial_state=self.decoder_initial_state,
                                                   output_layer=output_layer)
                                                   #output_layer=None)

                max_decoder_length = tf.reduce_max(self.decoder_inputs_length_train)

                (self.decoder_outputs_train, self.decoder_last_state_train, 
                 self.decoder_outputs_length_train) = (seq2seq.dynamic_decode(
                    decoder=training_decoder,
                    output_time_major=False,
                    impute_finished=True,
                    maximum_iterations=max_decoder_length))

                self.decoder_logits_train = tf.identity(self.decoder_outputs_train.rnn_output) 
                # Use argmax to extract decoder symbols to emit
                self.decoder_pred_train = tf.argmax(self.decoder_logits_train, axis=-1,
                                                    name='decoder_pred_train')

                masks = tf.sequence_mask(lengths=self.decoder_inputs_length_train, 
                                         maxlen=max_decoder_length, dtype=self.dtype, name='masks')

                self.loss = seq2seq.sequence_loss(logits=self.decoder_logits_train, 
                                                  targets=self.decoder_targets_train,
                                                  weights=masks,
                                                  average_across_timesteps=True,
                                                  average_across_batch=True,)

                tf.summary.scalar('loss', self.loss)

                # Contruct graphs for minimizing loss
                self.init_optimizer()

            elif self.mode == 'decode':

                start_tokens = tf.ones([self.batch_size,], tf.int32) * data_utils.START_TOKEN
                end_token = data_utils.END_TOKEN

                def embed_and_input_proj(inputs):
                    return input_layer(tf.nn.embedding_lookup(self.decoder_embeddings, inputs))
                    
                if not self.use_beamsearch_decode:
                    decoding_helper = seq2seq.GreedyEmbeddingHelper(start_tokens=start_tokens,
                                                                    end_token=end_token,
                                                                    embedding=embed_and_input_proj)

                    print("building greedy decoder..")
                    inference_decoder = seq2seq.BasicDecoder(cell=self.decoder_cell,
                                                             helper=decoding_helper,
                                                             initial_state=self.decoder_initial_state,
                                                             output_layer=output_layer)
                else:
                    print("building beamsearch decoder..")
                    inference_decoder = beam_search_decoder.BeamSearchDecoder(cell=self.decoder_cell,
                                                               embedding=embed_and_input_proj,
                                                               start_tokens=start_tokens,
                                                               end_token=end_token,
                                                               initial_state=self.decoder_initial_state,
                                                               beam_width=self.beam_width,
                                                               output_layer=output_layer,)

                (self.decoder_outputs_decode, self.decoder_last_state_decode,
                 self.decoder_outputs_length_decode) = (seq2seq.dynamic_decode(
                    decoder=inference_decoder,
                    output_time_major=False,
                    #impute_finished=True,	# error occurs
                    maximum_iterations=self.max_decode_step))

                if not self.use_beamsearch_decode:
                    self.decoder_pred_decode = tf.expand_dims(self.decoder_outputs_decode.sample_id, -1)

                else:
                    self.decoder_pred_decode = self.decoder_outputs_decode.predicted_ids


    def build_single_cell(self, hidden_units):
        cell_type = LSTMCell
        if (self.cell_type.lower() == 'gru'):
            cell_type = GRUCell
        cell = cell_type(hidden_units)

        if self.use_dropout:
            cell = DropoutWrapper(cell, dtype=self.dtype,
                                  output_keep_prob=self.keep_prob_placeholder,)
        if self.use_residual:
            cell = ResidualWrapper(cell)
            
        return cell


    # Building encoder cell
    def build_encoder_cell (self):

        return MultiRNNCell([self.build_single_cell(self.encoder_hidden_units) for i in range(self.depth)])



    def build_decoder_cell(self):

        encoder_outputs = self.encoder_outputs
        encoder_last_state = self.encoder_last_state
        encoder_inputs_length = self.encoder_inputs_length

        if self.use_beamsearch_decode:
            print ("use beamsearch decoding..")
            encoder_outputs = seq2seq.tile_batch(
                self.encoder_outputs, multiplier=self.beam_width)
            encoder_last_state = nest.map_structure(
                lambda s: seq2seq.tile_batch(s, self.beam_width), self.encoder_last_state)
            encoder_inputs_length = seq2seq.tile_batch(
                self.encoder_inputs_length, multiplier=self.beam_width)

        self.attention_mechanism = attention_wrapper.BahdanauAttention(
            num_units=self.decoder_hidden_units, memory=encoder_outputs,
            memory_sequence_length=encoder_inputs_length,) 

        if self.attention_type.lower() == 'luong':
            self.attention_mechanism = attention_wrapper.LuongAttention(
                num_units=self.decoder_hidden_units, memory=encoder_outputs,
                memory_sequence_length=encoder_inputs_length,)
 
        # Building decoder_cell
        self.decoder_cell_list = [
            self.build_single_cell(self.decoder_hidden_units) for i in range(self.depth)]
        decoder_initial_state = encoder_last_state

        def attn_decoder_input_fn(inputs, attention):
            if not self.attn_input_feeding:
                return inputs

            _input_layer = Dense(self.decoder_hidden_units, dtype=self.dtype,
                                 name='attn_input_feeding')
            return _input_layer(array_ops.concat([inputs, attention], -1))

        self.decoder_cell_list[-1] = attention_wrapper.AttentionWrapper(
            cell=self.decoder_cell_list[-1],
            attention_mechanism=self.attention_mechanism,
            attention_layer_size=self.decoder_hidden_units,
            cell_input_fn=attn_decoder_input_fn,
            initial_cell_state=encoder_last_state[-1],
            alignment_history=False,
            name='Attention_Wrapper')

        batch_size = self.batch_size if not self.use_beamsearch_decode \
                     else self.batch_size * self.beam_width
        initial_state = [state for state in encoder_last_state]

        initial_state[-1] = self.decoder_cell_list[-1].zero_state(
          batch_size=batch_size, dtype=self.dtype)
        decoder_initial_state = tuple(initial_state)

        return MultiRNNCell(self.decoder_cell_list), decoder_initial_state


    def init_optimizer(self):
        print("setting optimizer..")
        trainable_params = tf.trainable_variables()
        if self.optimizer.lower() == 'adadelta':
            self.opt = tf.train.AdadeltaOptimizer(learning_rate=self.learning_rate)
        elif self.optimizer.lower() == 'adam':
            self.opt = tf.train.AdamOptimizer(learning_rate=self.learning_rate)
        elif self.optimizer.lower() == 'rmsprop':
            self.opt = tf.train.RMSPropOptimizer(learning_rate=self.learning_rate)
        else:
            self.opt = tf.train.GradientDescentOptimizer(learning_rate=self.learning_rate)

        # Compute gradients of loss w.r.t. all trainable variables
        gradients = tf.gradients(self.loss, trainable_params)

        # Clip gradients by a given maximum_gradient_norm
        clip_gradients, _ = tf.clip_by_global_norm(gradients, self.max_gradient_norm)

        self.updates = self.opt.apply_gradients(
            zip(clip_gradients, trainable_params), global_step=self.global_step)

    def save(self, sess, path, var_list=None, global_step=None):
        # var_list = None returns the list of all saveable variables
        saver = tf.train.Saver(var_list)

        save_path = saver.save(sess, save_path=path, global_step=global_step)
        print('model saved at %s' % save_path)
        

    def restore(self, sess, path, var_list=None):
        # var_list = None returns the list of all saveable variables
        saver = tf.train.Saver(var_list)
        saver.restore(sess, save_path=path)
        print('model restored from %s' % path)


    def train(self, sess, encoder_inputs, encoder_inputs_length, 
              decoder_inputs, decoder_inputs_length):
        # Check if the model is 'training' mode
        if self.mode.lower() != 'train':
            raise ValueError("train step can only be operated in train mode")

        input_feed = self.check_feeds(encoder_inputs, encoder_inputs_length,
                                      decoder_inputs, decoder_inputs_length, False)
        # Input feeds for dropout
        input_feed[self.keep_prob_placeholder.name] = self.keep_prob
 
        output_feed = [self.updates,
                       self.loss,
                       self.summary_op]
        
        outputs = sess.run(output_feed, input_feed)
        return outputs[1], outputs[2]


    def eval(self, sess, encoder_inputs, encoder_inputs_length,
             decoder_inputs, decoder_inputs_length):
        
        input_feed = self.check_feeds(encoder_inputs, encoder_inputs_length,
                                      decoder_inputs, decoder_inputs_length, False)
        # Input feeds for dropout
        input_feed[self.keep_prob_placeholder.name] = 1.0

        output_feed = [self.loss,
                       self.summary_op]
        outputs = sess.run(output_feed, input_feed)
        return outputs[0], outputs[1]


    def predict(self, sess, encoder_inputs, encoder_inputs_length):
        
        input_feed = self.check_feeds(encoder_inputs, encoder_inputs_length, 
                                      decoder_inputs=None, decoder_inputs_length=None, 
                                      decode=True)

        # Input feeds for dropout
        input_feed[self.keep_prob_placeholder.name] = 1.0
 
        output_feed = [self.decoder_pred_decode]
        outputs = sess.run(output_feed, input_feed)

		# GreedyDecoder: [batch_size, max_time_step]
        return outputs[0]	# BeamSearchDecoder: [batch_size, max_time_step, beam_width]


    def check_feeds(self, encoder_inputs, encoder_inputs_length, 
                    decoder_inputs, decoder_inputs_length, decode):

        input_batch_size = encoder_inputs.shape[0]
        if input_batch_size != encoder_inputs_length.shape[0]:
            raise ValueError("Encoder inputs and their lengths must be equal in their "
                "batch_size, %d != %d" % (input_batch_size, encoder_inputs_length.shape[0]))

        if not decode:
            target_batch_size = decoder_inputs.shape[0]
            if target_batch_size != input_batch_size:
                raise ValueError("Encoder inputs and Decoder inputs must be equal in their "
                    "batch_size, %d != %d" % (input_batch_size, target_batch_size))
            if target_batch_size != decoder_inputs_length.shape[0]:
                raise ValueError("Decoder targets and their lengths must be equal in their "
                    "batch_size, %d != %d" % (target_batch_size, decoder_inputs_length.shape[0]))

        input_feed = {}
    
        input_feed[self.encoder_inputs.name] = encoder_inputs
        input_feed[self.encoder_inputs_length.name] = encoder_inputs_length

        if not decode:
            input_feed[self.decoder_inputs.name] = decoder_inputs
            input_feed[self.decoder_inputs_length.name] = decoder_inputs_length

        return input_feed 

