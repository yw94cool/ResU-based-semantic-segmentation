from __future__ import print_function
import tensorflow as tf
import numpy as np

import TensorflowUtils as utils
import read_MITSceneParsingData as scene_parsing
import datetime
import BatchDatsetReader as dataset
from six.moves import xrange

import scipy.misc as misc
import imageio
from matplotlib import pyplot as plt
from matplotlib import cm
import time
import pandas as pd
import glob
import pydensecrf.densecrf as dcrf
from pydensecrf.utils import compute_unary, create_pairwise_bilateral, create_pairwise_gaussian, softmax_to_unary

# os.environ["CUDA_VISIBLE_DEVICES"] = ""

FLAGS = tf.flags.FLAGS
tf.flags.DEFINE_integer("batch_size", "16", "batch size for training")
tf.flags.DEFINE_string("logs_dir", "logs/", "path to logs directory")
tf.flags.DEFINE_string("data_dir", "Data_zoo/MIT_SceneParsing/", "path to dataset")
tf.flags.DEFINE_float("learning_rate", "1e-4", "Learning rate for Adam Optimizer")
tf.flags.DEFINE_string("model_dir", "Model_zoo/", "Path to vgg model mat")
tf.flags.DEFINE_bool('debug', "true", "Debug mode: True/ False")
tf.flags.DEFINE_string('mode', "test", "Mode train/ test/ visualize")

MODEL_URL = 'http://www.vlfeat.org/matconvnet/models/beta16/imagenet-vgg-verydeep-19.mat'

MAX_ITERATION = int(1e5 + 1)
NUM_OF_CLASSESS = 6
IMAGE_SIZE = 400


def vgg_net(weights, image):
    layers = (
        'conv1_1', 'relu1_1', 'conv1_2', 'relu1_2', 'pool1',

        'conv2_1', 'relu2_1', 'conv2_2', 'relu2_2', 'pool2',

        'conv3_1', 'relu3_1', 'conv3_2', 'relu3_2', 'conv3_3',
        'relu3_3', 'conv3_4', 'relu3_4', 'pool3',

        'conv4_1', 'relu4_1', 'conv4_2', 'relu4_2', 'conv4_3',
        'relu4_3', 'conv4_4', 'relu4_4', 'pool4',

        'conv5_1', 'relu5_1', 'conv5_2', 'relu5_2', 'conv5_3',
        'relu5_3', 'conv5_4', 'relu5_4'
    )

    net = {}
    current = image
    for i, name in enumerate(layers):
        kind = name[:4]
        if kind == 'conv':
            kernels, bias = weights[i][0][0][0][0]

            if i == 0:
                kernel_add = kernels[:, :, 2, :]
                kernel_add = kernel_add[:, :, np.newaxis, :]
                kernels = np.concatenate((kernels, kernel_add), axis=2)
            kernels = utils.get_variable(np.transpose(kernels, (1, 0, 2, 3)), name=name + "_w")
            bias = utils.get_variable(bias.reshape(-1), name=name + "_b")
            current = utils.conv2d_basic(current, kernels, bias)
        elif kind == 'relu':
            current = tf.nn.relu(current, name=name)
            if FLAGS.debug:
                utils.add_activation_summary(current)
        elif kind == 'pool':
            current = utils.avg_pool_2x2(current)
        net[name] = current

    return net


def inference(image, keep_prob):
    """
    Semantic segmentation network definition
    :param image: input image. Should have values in range 0-255
    :param keep_prob:
    :return:
    """
    print("setting up vgg initialized conv layers ...")
    model_data = utils.get_model_data(FLAGS.model_dir, MODEL_URL)
    mean_pixel = [86.59, 92.48, 86.04, 90.07]

    weights = np.squeeze(model_data['layers'])

    processed_image = utils.process_image(image, mean_pixel)
    with tf.variable_scope("inference"):
        image_net = vgg_net(weights, processed_image)
        conv_final_layer = image_net["conv5_3"]

        pool5 = utils.max_pool_2x2(conv_final_layer)

        W6 = utils.weight_variable([7, 7, 512, 4096], name="W6")
        b6 = utils.bias_variable([4096], name="b6")
        conv6 = utils.conv2d_basic(pool5, W6, b6)
        relu6 = tf.nn.relu(conv6, name="relu6")
        if FLAGS.debug:
            utils.add_activation_summary(relu6)
        relu_dropout6 = tf.nn.dropout(relu6, keep_prob=keep_prob)

        W7 = utils.weight_variable([1, 1, 4096, 4096], name="W7")
        b7 = utils.bias_variable([4096], name="b7")
        conv7 = utils.conv2d_basic(relu_dropout6, W7, b7)
        relu7 = tf.nn.relu(conv7, name="relu7")

        if FLAGS.debug:
            utils.add_activation_summary(relu7)
        relu_dropout7 = tf.nn.dropout(relu7, keep_prob=keep_prob)

        W8 = utils.weight_variable([1, 1, 4096, NUM_OF_CLASSESS], name="W8")
        b8 = utils.bias_variable([NUM_OF_CLASSESS], name="b8")
        conv8 = utils.conv2d_basic(relu_dropout7, W8, b8)
        # annotation_pred1 = tf.argmax(conv8, dimension=3, name="prediction1")

        # now to upscale to actual image size
        deconv_shape1 = image_net["pool4"].get_shape()
        W_t1 = utils.weight_variable([4, 4, deconv_shape1[3].value, NUM_OF_CLASSESS], name="W_t1")
        b_t1 = utils.bias_variable([deconv_shape1[3].value], name="b_t1")
        conv_t1 = utils.conv2d_transpose_strided(conv8, W_t1, b_t1, output_shape=tf.shape(image_net["pool4"]))
        fuse_1 = tf.add(conv_t1, image_net["pool4"], name="fuse_1")

        deconv_shape2 = image_net["pool3"].get_shape()
        W_t2 = utils.weight_variable([4, 4, deconv_shape2[3].value, deconv_shape1[3].value], name="W_t2")
        b_t2 = utils.bias_variable([deconv_shape2[3].value], name="b_t2")
        conv_t2 = utils.conv2d_transpose_strided(fuse_1, W_t2, b_t2, output_shape=tf.shape(image_net["pool3"]))
        fuse_2 = tf.add(conv_t2, image_net["pool3"], name="fuse_2")

        shape = tf.shape(image)
        deconv_shape3 = tf.stack([shape[0], shape[1], shape[2], NUM_OF_CLASSESS])
        W_t3 = utils.weight_variable([16, 16, NUM_OF_CLASSESS, deconv_shape2[3].value], name="W_t3")
        b_t3 = utils.bias_variable([NUM_OF_CLASSESS], name="b_t3")
        conv_t3 = utils.conv2d_transpose_strided(fuse_2, W_t3, b_t3, output_shape=deconv_shape3, stride=8)

        annotation_pred = tf.argmax(conv_t3, dimension=3, name="prediction")
        probabilities = tf.nn.softmax(conv_t3)

    return tf.expand_dims(annotation_pred, dim=3), conv_t3, probabilities

def train(loss_val, var_list):
    optimizer = tf.train.AdamOptimizer(FLAGS.learning_rate)
    grads = optimizer.compute_gradients(loss_val, var_list=var_list)
    if FLAGS.debug:
        # print(len(var_list))
        for grad, var in grads:
            utils.add_gradient_summary(grad, var)
    return optimizer.apply_gradients(grads)


def main(argv=None):
    keep_probability = tf.placeholder(tf.float32, name="keep_probabilty")
    if FLAGS.mode == "test":
        image = tf.placeholder(tf.float32, shape=[None, 2000, 2000, 4], name="input_image")
    else:
        image = tf.placeholder(tf.float32, shape=[None, IMAGE_SIZE, IMAGE_SIZE, 4], name="input_image")
    annotation = tf.placeholder(tf.int32, shape=[None, IMAGE_SIZE, IMAGE_SIZE, 1], name="annotation")

    pred_annotation, logits, final_probabilities = inference(image, keep_probability)
    tf.summary.image("input_image", image, max_outputs=2)
    tf.summary.image("ground_truth", tf.cast(annotation, tf.uint8), max_outputs=2)
    tf.summary.image("pred_annotation", tf.cast(pred_annotation, tf.uint8), max_outputs=2)
    loss = tf.reduce_mean((tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits,
                                                                          labels=tf.squeeze(annotation, squeeze_dims=[3]),
                                                                          name="entropy")))
    correct_pred = tf.equal(tf.cast(pred_annotation, tf.uint8), tf.cast(annotation, tf.uint8))
    accuracy = tf.reduce_mean(tf.cast(correct_pred, tf.float32))

    tf.summary.scalar("entropy", loss)
    tf.summary.scalar("accuracy", accuracy)

    trainable_var = tf.trainable_variables()
    if FLAGS.debug:
        for var in trainable_var:
            utils.add_to_regularization_and_summary(var)
    train_op = train(loss, trainable_var)

    print("Setting up summary op...")
    summary_op = tf.summary.merge_all()

    print("Setting up image reader...")
    train_records, valid_records = scene_parsing.read_dataset(FLAGS.data_dir)
    print(len(train_records))
    print(len(valid_records))

    print("Setting up dataset reader")
    image_options = {'resize': False, 'resize_size': IMAGE_SIZE}

    if FLAGS.mode == 'train':
        train_dataset_reader = dataset.BatchDatset(train_records, image_options)
        validation_dataset_reader = dataset.BatchDatset(valid_records, image_options)

    # gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.7)
    sess = tf.Session(config=tf.ConfigProto(log_device_placement=True, device_count = {'GPU': 1}))

    print("Setting up Saver...")
    saver = tf.train.Saver()
    summary_writer = tf.summary.FileWriter(FLAGS.logs_dir, sess.graph)

    sess.run(tf.global_variables_initializer())
    ckpt = tf.train.get_checkpoint_state(FLAGS.logs_dir)
    if ckpt and ckpt.model_checkpoint_path:
        saver.restore(sess, ckpt.model_checkpoint_path)
        print("Model restored...")

    if FLAGS.mode == "train":

        train_row = 0
        valid_row = 0
        train_record = pd.DataFrame(columns=['itr', 'train_loss', 'train_acc'])
        valid_record = pd.DataFrame(columns=['itr', 'valid_loss', 'valid_acc'])

        for itr in xrange(MAX_ITERATION):
            train_images, train_annotations = train_dataset_reader.next_batch(FLAGS.batch_size)
            feed_dict = {image: train_images, annotation: train_annotations, keep_probability: 0.85}

            sess.run(train_op, feed_dict=feed_dict)

            #test = sess.run(pred_annotation, feed_dict=feed_dict)

            if itr % 10 == 0:
                train_acc = sess.run(accuracy, feed_dict=feed_dict)
                train_loss, summary_str = sess.run([loss, summary_op], feed_dict=feed_dict)
                print("Step: %-6d, Train_loss: %-10g, Train_Acc: %-10g" % (itr, train_loss, train_acc))
                summary_writer.add_summary(summary_str, itr)

                train_record.loc[train_row] = [itr, train_loss, train_acc]
                train_row += 1
            if itr % 10 == 0:
                valid_images, valid_annotations = validation_dataset_reader.next_batch(FLAGS.batch_size)
                valid_loss = sess.run(loss, feed_dict={image: valid_images, annotation: valid_annotations,
                                                       keep_probability: 1.0})
                valid_acc = sess.run(accuracy, feed_dict={image: valid_images, annotation: valid_annotations,
                                                          keep_probability: 1.0})
                print("Step: %-6d, Valid_loss: %-10g, Valid_acc: %-10g <--- %s" % (itr, valid_loss, valid_acc, datetime.datetime.now()))

                if valid_acc > 0.90 or itr % 500 == 0:
                    saver.save(sess, "D:/DeepSEG/model.ckpt", itr)

                valid_record.loc[valid_row] = [itr, valid_loss, valid_acc]
                valid_row += 1

            train_record.to_csv("D:/DeepSEG/train_record.csv", index=False, sep=',', mode='w')
            valid_record.to_csv("D:/DeepSEG/valid_record.csv", index=False, sep=',', mode='w')

    elif FLAGS.mode == "visualize":
        valid_images, valid_annotations = validation_dataset_reader.get_random_batch(FLAGS.batch_size)
        pred = sess.run(pred_annotation, feed_dict={image: valid_images, annotation: valid_annotations,
                                                    keep_probability: 1.0})
        valid_annotations = np.squeeze(valid_annotations, axis=3)
        pred = np.squeeze(pred, axis=3)

        for itr in range(FLAGS.batch_size):
            utils.save_image(valid_images[itr].astype(np.uint8), FLAGS.logs_dir, name="inp_" + str(5+itr))
            utils.save_image(valid_annotations[itr].astype(np.uint8), FLAGS.logs_dir, name="gt_" + str(5+itr))
            utils.save_image(pred[itr].astype(np.uint8), FLAGS.logs_dir, name="pred_" + str(5+itr))
            print("Saved image: %d" % itr)

    # 更新：测试
    elif FLAGS.mode == "test":
        #test_dir = '..\\test_image\\test0\\*11_crop05.npy'
        test_dir = 'D:/DeepSEG/test/*11_crop05.npy'
        test_file_list = glob.glob(test_dir)
        for f in test_file_list:
            test_path = f
            image_test = np.load (test_path)
            test_image = image_test.reshape((1, image_test.shape[0], image_test.shape[1], image_test.shape[2]))


            start_time = time.time()

            last_probabilities = sess.run(final_probabilities, feed_dict={image: test_image, keep_probability: 1.0})
            fig = plt.figure()
            ax = fig.add_subplot(111)
            ax.imshow(last_probabilities.squeeze()[:, :, 2], cmap=cm.jet)
            plt.show()
            processed_probabilities = last_probabilities.squeeze()
            processed_probabilities = processed_probabilities.transpose((2, 0, 1))
            unary = softmax_to_unary(processed_probabilities)
            unary = np.ascontiguousarray(unary)
            d = dcrf.DenseCRF(image_test.shape[0] * image_test.shape[1], 6)
            d.setUnaryEnergy(unary)

            feats = create_pairwise_gaussian(sdims=(5, 5), shape=image_test.shape[:2]) # 10
            d.addPairwiseEnergy(feats, compat=10, kernel=dcrf.DIAG_KERNEL, normalization=dcrf.NORMALIZE_SYMMETRIC)# 3

            feats = create_pairwise_bilateral(sdims=(25, 25), schan=(10, 10, 10, 50), img=image_test, chdim=2)# 50 20
            d.addPairwiseEnergy(feats, compat=5, kernel=dcrf.DIAG_KERNEL, normalization=dcrf.NORMALIZE_SYMMETRIC)# 10

            Q = d.inference(10)
            pred_image = np.argmax(Q, axis=0)
            end_time = time.time()
            test_time = end_time - start_time

            image_pred = pred_image.reshape((image_test.shape[0], image_test.shape[1]))

            rgb_img = np.zeros([image_pred.shape[0], image_pred.shape[1], 3], dtype=np.uint8)

            for i in np.arange(0, image_pred.shape[0]):
                for j in np.arange(0, image_pred.shape[1]):
                    if image_pred[i][j] == 0:
                        rgb_img[i][j][0] = 255
                        rgb_img[i][j][1] = 255
                        rgb_img[i][j][2] = 255
                    elif image_pred[i][j] == 1:
                        rgb_img[i][j][0] = 255
                        rgb_img[i][j][1] = 0
                        rgb_img[i][j][2] = 0
                    elif image_pred[i][j] == 2:
                        rgb_img[i][j][0] = 0
                        rgb_img[i][j][1] = 0
                        rgb_img[i][j][2] = 255
                    elif image_pred[i][j] == 3:
                        rgb_img[i][j][0] = 0
                        rgb_img[i][j][1] = 255
                        rgb_img[i][j][2] = 0
                    elif image_pred[i][j] == 4:
                        rgb_img[i][j][0] = 255
                        rgb_img[i][j][1] = 255
                        rgb_img[i][j][2] = 0
                    elif image_pred[i][j] == 5:
                        rgb_img[i][j][0] = 0
                        rgb_img[i][j][1] = 255
                        rgb_img[i][j][2] = 255
            save_path = f[:-4] + '_test.tif'
            imageio.imwrite(save_path, rgb_img)
            print ('Test image '+ f[-27:-4] +' saved...')
            print ('Time: %f' % test_time)


if __name__ == "__main__":
    tf.app.run()
