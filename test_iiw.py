import json
import os
import numpy as np
import tensorflow as tf
import importlib
import cv2
from skimage import io
import argparse
from model import SfMNet, lambSH_layer, pred_illuDecomp_layer
from glob import glob
from utils.whdr import compute_whdr

tf.compat.v1.disable_eager_execution()

parser = argparse.ArgumentParser(description='InverseRenderNet')
parser.add_argument('--iiw', help='Root directory for iiw-dataset')
parser.add_argument('--model', help='Path to trained model')


args = parser.parse_args()

iiw = args.iiw
test_ids = np.load('iiw_test_ids.npy')



input_height = 200
input_width = 200



# compute pseudo inverse for input matrix
def pinv(A, reltol=1e-6):
	# compute SVD of input A
	s, u, v = tf.linalg.svd(A)

	# invert s and clear entries lower than reltol*s_max
	atol = tf.reduce_max(input_tensor=s) * reltol
	s = tf.boolean_mask(tensor=s, mask=s>atol)
	s_inv = tf.linalg.tensor_diag(1./s)

	# compute v * s_inv * u_t as psuedo inverse
	return tf.matmul(v, tf.matmul(s_inv, tf.transpose(a=u)))



inputs_var = tf.compat.v1.placeholder(tf.float32, (None, input_height, input_width, 3))
masks_var = tf.compat.v1.placeholder(tf.float32, (None, input_height, input_width, 1))
train_flag = tf.compat.v1.placeholder(tf.bool, ())
am_deconvOut, _ = SfMNet.SfMNet(inputs=inputs_var,is_training=train_flag, height=input_height, width=input_width, n_layers=30, n_pools=4, depth_base=32)


# separate albedo, error mask and shadow mask from deconvolutional output
albedos = am_deconvOut

# post-process on raw albedo and nm_pred
albedos = tf.nn.sigmoid(albedos) * masks_var + tf.constant(1e-4)

irn_vars = tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.GLOBAL_VARIABLES, scope='conv') + tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.GLOBAL_VARIABLES, scope='am') + tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.GLOBAL_VARIABLES, scope='nm')
model_path = tf.train.get_checkpoint_state(args.model).model_checkpoint_path

total_loss = 0 
sess = tf.compat.v1.InteractiveSession()
saver = tf.compat.v1.train.Saver(irn_vars)
saver.restore(sess, model_path)


for counter, test_id in enumerate(test_ids):
    img_file = str(test_id)+'.png'
    judgement_file = str(test_id)+'.json'

    img_path = os.path.join(iiw, 'data', img_file)
    judgement_path = os.path.join(iiw, 'data', judgement_file)

    img = io.imread(img_path)
    judgement = json.load(open(judgement_path))

    ori_width, ori_height = img.shape[:2]

    img = cv2.resize(img, (input_width, input_height))
    img = np.float32(img)/255.
    img = img[None, :, :, :]
    mask = np.ones((1, input_height, input_width, 1), np.bool)


    [albedos_val] = sess.run([albedos], feed_dict={train_flag:False, inputs_var:img, masks_var:mask})

    albedos_val = cv2.resize(albedos_val[0], (ori_width, ori_height))

    albedos_val = (albedos_val-albedos_val.min()) / (albedos_val.max()-albedos_val.min())
    albedos_val = albedos_val/2+.5


    loss = compute_whdr(albedos_val, judgement)
    total_loss += loss
    print('whdr:{:f}\twhdr_avg:{:f}'.format(loss, total_loss/(counter+1)))


print("IIW TEST WHDR %f"%(total_loss/len(test_ids)))


