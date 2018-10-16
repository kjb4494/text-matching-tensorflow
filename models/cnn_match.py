import os
import numpy as np
import tensorflow as tf

from models.base import Model
from models.model_helper import get_embeddings, make_negative_mask


class CNNMatch(Model):
    def __init__(self, dataset, config, mode=tf.contrib.learn.ModeKeys.TRAIN):
        super(CNNMatch, self).__init__(dataset, config)
        if mode == "train":
            self.mode = tf.contrib.learn.ModeKeys.TRAIN
        elif (mode == "val") | (mode == tf.contrib.learn.ModeKeys.EVAL):
            self.mode = tf.contrib.learn.ModeKeys.EVAL
        else:
            raise NotImplementedError()
        self.filter_sizes = [int(filter_size) for filter_size in self.config.filter_sizes.split(",")]
        self.build_model()
        self.init_saver()

    def build_model(self):
        # build index table
        index_table = tf.contrib.lookup.index_table_from_file(
            vocabulary_file=self.config.vocab_list,
            num_oov_buckets=0,
            default_value=0)

        # get data iterator
        self.data_iterator = self.data.get_data_iterator(index_table, mode=self.mode)
        self.negatives_iterator = self.data.get_negatives_iterator(index_table, mode=self.mode)

        # get inputs
        with tf.variable_scope("inputs"):
            # get next batch if there is no feeded data
            next_batch = self.data_iterator.get_next()
            next_negatives = self.negatives_iterator.get_next()
            
            self.input_queries = tf.placeholder_with_default(next_batch["input_queries"],
                                                             [None, self.config.max_length],
                                                             name="input_queries")
            self.input_replies = tf.placeholder_with_default(next_batch["input_replies"],
                                                             [None, self.config.max_length],
                                                             name="input_replies")
            self.input_negatives = tf.placeholder_with_default(next_negatives["input_negatives"], 
                                                               [None, self.config.max_length], 
                                                               name="input_negatives")
            self.query_lengths = tf.placeholder_with_default(tf.squeeze(next_batch["query_lengths"]),
                                                             [None],
                                                             name="query_lengths")
            self.reply_lengths = tf.placeholder_with_default(tf.squeeze(next_batch["reply_lengths"]),
                                                             [None],
                                                             name="reply_lengths")
            self.negative_lengths = tf.placeholder_with_default(tf.squeeze(next_negatives["negative_lengths"]),
                                                             [None],
                                                             name="negative_lengths")

            # get hyperparams
            self.embed_dropout_keep_prob = tf.placeholder(tf.float32, name="embed_dropout_keep_prob")
            self.lstm_dropout_keep_prob = tf.placeholder(tf.float32, name="lstm_dropout_keep_prob")
            self.num_negative_samples = tf.placeholder(tf.int32, name="num_negative_samples")
            self.hidden_dropout_keep_prob = tf.placeholder(tf.float32, name="hidden_dropout_keep_prob")
            self.add_echo = tf.placeholder(tf.bool, name="add_echo")
            
        with tf.variable_scope("properties"):
            # length properties
            cur_batch_length = tf.shape(self.input_queries)[0]
            
            # learning rate and optimizer
            learning_rate =  tf.train.exponential_decay(self.config.learning_rate,
                                                        self.global_step_tensor,
                                                        decay_steps=100000, decay_rate=0.9)
            self.optimizer = tf.train.AdamOptimizer(learning_rate)

        # embedding layer
        with tf.variable_scope("embedding"):
            embeddings = tf.Variable(get_embeddings(self.config.vocab_list,
                                                    self.config.pretrained_embed_dir,
                                                    self.config.vocab_size,
                                                    self.config.embed_dim),
                                     trainable=True,
                                     name="embeddings")
            queries_embedded = tf.expand_dims(tf.to_float(tf.nn.embedding_lookup(embeddings, self.input_queries, name="queries_embedded")), -1)
            replies_embedded = tf.expand_dims(tf.to_float(tf.nn.embedding_lookup(embeddings, self.input_replies, name="replies_embedded")), -1)
            negatives_embedded = tf.expand_dims(tf.to_float(tf.nn.embedding_lookup(embeddings, self.input_negatives, name="negatives_embedded")), -1)

        # build CNN layer
        with tf.variable_scope("convolution_layer"):
            queries_pooled_outputs = list()
            replies_pooled_outputs = list()
            negatives_pooled_outputs = list()
            
            for i, filter_size in enumerate(self.filter_sizes):
                filter_shape = [filter_size, self.config.embed_dim, 1, self.config.num_filters]
                with tf.variable_scope("conv-maxpool-query-{}".format(filter_size)):
                    W = tf.get_variable("W", shape=filter_shape, initializer=tf.truncated_normal_initializer(stddev=0.1))
                    b = tf.get_variable("b", shape=[self.config.num_filters], initializer=tf.constant_initializer(0.1))
                    conv = tf.nn.conv2d(queries_embedded,
                                        W,
                                        strides=[1, 1, 1, 1],
                                        padding="VALID",
                                        name="conv")
                    h = tf.nn.relu(tf.nn.bias_add(conv, b), name="relu")
                    pooled = tf.nn.max_pool(h,
                                            ksize=[1, self.config.max_length - filter_size + 1, 1, 1], 
                                            strides=[1, 1, 1, 1], 
                                            padding="VALID", 
                                            name="pool")
                    queries_pooled_outputs.append(pooled)
                    
                with tf.variable_scope("conv-maxpool-reply-{}".format(filter_size)):
                    W = tf.get_variable("W", shape=filter_shape, initializer=tf.truncated_normal_initializer(stddev=0.1))
                    b = tf.get_variable("b", shape=[self.config.num_filters], initializer=tf.constant_initializer(0.1))
                    conv_1 = tf.nn.conv2d(replies_embedded, 
                                        W, 
                                        strides=[1, 1, 1, 1], 
                                        padding="VALID", 
                                        name="conv")
                    h_1 = tf.nn.relu(tf.nn.bias_add(conv_1, b), name="relu")
                    pooled_1 = tf.nn.max_pool(h_1, 
                                            ksize=[1, self.config.max_length - filter_size + 1, 1, 1], 
                                            strides=[1, 1, 1, 1], 
                                            padding="VALID", 
                                            name="pool")
                    replies_pooled_outputs.append(pooled_1)
                
                with tf.variable_scope("conv-maxpool-reply-{}".format(filter_size), reuse=True):
                    W = tf.get_variable("W", shape=filter_shape)
                    b = tf.get_variable("b", shape=[self.config.num_filters])
                    conv_2 = tf.nn.conv2d(negatives_embedded, 
                                          W, 
                                          strides=[1, 1, 1, 1], 
                                          padding="VALID", 
                                          name="conv")
                    h_2 = tf.nn.relu(tf.nn.bias_add(conv_2, b), name="relu")
                    pooled_2 = tf.nn.max_pool(h_2, 
                                            ksize=[1, self.config.max_length - filter_size + 1, 1, 1], 
                                            strides=[1, 1, 1, 1], 
                                            padding="VALID", 
                                            name="pool")
                    negatives_pooled_outputs.append(pooled_2)

        # combine all pooled outputs
        num_filters_total = self.config.num_filters * len(self.filter_sizes)
        self.queries_encoded = tf.reshape(tf.concat(queries_pooled_outputs, 3), 
                                          [-1, num_filters_total], name="queries_encoded")
        self.replies_encoded = tf.reshape(tf.concat(replies_pooled_outputs, 3), 
                                          [-1, num_filters_total], name="replies_encoded")
        self.negatives_encoded = tf.reshape(tf.concat(negatives_pooled_outputs, 3), 
                                            [-1, num_filters_total], name="negatives_encoded")
        
        with tf.variable_scope("dense_layer"):
            M = tf.get_variable("M", 
                                shape=[num_filters_total, num_filters_total], 
                                initializer=tf.contrib.layers.xavier_initializer())
            self.queries_transformed = tf.matmul(self.queries_encoded, M)

        with tf.name_scope("negative_sampling"):
            l2_loss = tf.constant(0.0)
            self.queries_transformed_negatives = tf.tile(self.queries_transformed, [self.num_negative_samples, 1])
            
        with tf.variable_scope("similarities"):
            self.positive_similarities = tf.reduce_sum(tf.multiply(self.queries_transformed, 
                                                                   self.replies_encoded), 
                                                       axis=1, 
                                                       keepdims=True)
            self.negative_similarities = tf.reduce_sum(tf.multiply(self.queries_transformed_negatives,
                                                                   self.negatives_encoded),
                                                       axis=1, 
                                                       keepdims=True)
            self.positive_input = tf.concat([self.queries_transformed,
                                             self.positive_similarities, 
                                             self.replies_encoded], 1, name="positive_input")
            self.negative_input = tf.concat([self.queries_transformed_negatives, 
                                             self.negative_similarities, 
                                             self.negatives_encoded], 1, name="negative_input")
        
        with tf.variable_scope("hidden_layer"):
            W = tf.get_variable("W_hidden", 
                                shape=[2*num_filters_total+1, self.config.num_hidden], 
                                initializer=tf.contrib.layers.xavier_initializer())
            b = tf.get_variable("b", 
                                shape=[self.config.num_hidden], 
                                initializer=tf.constant_initializer(0.1))
            l2_loss += tf.nn.l2_loss(W)
            l2_loss += tf.nn.l2_loss(b)
            self.positive_hidden_output = tf.nn.relu(tf.nn.xw_plus_b(self.positive_input, W, b, name="positive_hidden_output"))
            self.negative_hidden_output = tf.nn.relu(tf.nn.xw_plus_b(self.negative_input, W, b, name="negative_hidden_output"))
        
        with tf.variable_scope("dropout"):
            self.positive_output_drop = tf.nn.dropout(self.positive_hidden_output, 
                                                      self.hidden_dropout_keep_prob,
                                                      name="positive_output_drop")
            self.negative_output_drop = tf.nn.dropout(self.negative_hidden_output,
                                                      self.hidden_dropout_keep_prob,
                                                      name="negative_output_drop")
        
        with tf.variable_scope("output_layer"):
            W = tf.get_variable("W_output", 
                                shape=[self.config.num_hidden, 1], 
                                initializer=tf.contrib.layers.xavier_initializer())
            b = tf.get_variable("b", 
                                shape=[1], 
                                initializer=tf.constant_initializer(0.1))
            self.positive_scores = tf.nn.xw_plus_b(self.positive_output_drop, W, b, name="positive_outputs")
            self.negative_scores = tf.nn.xw_plus_b(self.negative_output_drop, W, b, name="negative_outputs")
            
        with tf.variable_scope("prediction"):
            self.scores = tf.concat([self.positive_scores, self.negative_scores], 0)
            self.probs = tf.sigmoid(self.scores, name="probs")
            self.predictions = tf.cast(self.probs > 0.5, dtype=tf.int32)
            positive_labels = tf.ones_like(self.positive_scores)
            negative_labels = tf.zeros_like(self.negative_scores)
            self.labels = tf.concat([positive_labels, negative_labels], axis=0)
            
        with tf.variable_scope("loss"):
            losses = tf.nn.sigmoid_cross_entropy_with_logits(labels=self.labels, logits=self.scores)
            self.loss = tf.reduce_mean(losses)
            self.train_step = self.optimizer.minimize(self.loss)
            
        with tf.variable_scope("score"):
            correct_predictions = tf.equal(self.predictions, tf.to_int32(self.labels))
            self.accuracy = tf.reduce_mean(tf.cast(correct_predictions, "float"), name="accuracy")
