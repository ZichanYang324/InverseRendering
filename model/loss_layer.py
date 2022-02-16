# formulate loss function based on supplied ground truth and outputs from network

import importlib
import tensorflow as tf
import numpy as np
import os
from model import SfMNet, lambSH_layer, pred_illuDecomp_layer, sup_illuDecomp_layer, reproj_layer

def loss_formulate(albedos, nm_pred, am_sup, nm_gt, inputs, dms, cams, scale_xs, scale_ys, masks, pair_label, preTrain_flag, am_smt_w_var, reproj_w_var, reg_loss_flag=True):

	# define gamma nonlinear mapping factor
	gamma = tf.constant(2.2)

	albedos = tf.nn.sigmoid(albedos) * masks + tf.constant(1e-4)

	### pre-process nm_pred such that in range (-1,1)
	nm_pred_norm = tf.sqrt(tf.reduce_sum(input_tensor=nm_pred**2, axis=-1, keepdims=True)+tf.constant(1.))
	nm_pred_xy = nm_pred / nm_pred_norm
	nm_pred_z = tf.constant(1.) / nm_pred_norm
	nm_pred_xyz = tf.concat([nm_pred_xy, nm_pred_z], axis=-1) * masks

	# selete normal map used in rendering - gt or pred
	normals = nm_gt if preTrain_flag else nm_pred_xyz


	# reconstruct SH lightings from predicted statistical SH lighting model
	lighting_model = '../hdr_illu_pca'
	lighting_vectors = tf.constant(np.load(os.path.join(lighting_model,'pcaVector.npy')),dtype=tf.float32)
	lighting_means = tf.constant(np.load(os.path.join(lighting_model,'mean.npy')),dtype=tf.float32)
	lightings_var = tf.constant(np.load(os.path.join(lighting_model,'pcaVariance.npy')),dtype=tf.float32)
	
	if preTrain_flag:
		lightings = sup_illuDecomp_layer.illuDecomp(inputs,albedos,nm_gt,gamma)
	else:
		lightings =pred_illuDecomp_layer.illuDecomp(inputs,albedos,nm_pred_xyz,gamma,masks)

	lightings_pca = tf.matmul((lightings - lighting_means), pinv(lighting_vectors))

	# recompute lightings from lightins_pca which could add weak constraint on lighting reconstruction 
	lightings = tf.matmul(lightings_pca,lighting_vectors) + lighting_means 

	# reshape 27-D lightings to 9*3 lightings
	lightings = tf.reshape(lightings,[tf.shape(input=lightings)[0],9,3])


	### lighting prior loss
	var = tf.reduce_mean(input_tensor=lightings_pca**2,axis=0)

	illu_prior_loss = tf.compat.v1.losses.absolute_difference(var, lightings_var)

	illu_prior_loss = tf.math.log(illu_prior_loss + 1.)


	### stereo supervision based on albedos reprojection consistancy
	reproj_tb = tf.cast(tf.equal(pair_label,tf.transpose(a=pair_label)), dtype=tf.float32)
	reproj_tb = tf.cast(tf.linalg.set_diag(reproj_tb, tf.zeros([tf.shape(input=inputs)[0]])),tf.bool)
	reproj_list = tf.compat.v1.where(reproj_tb)
	img1_inds = tf.expand_dims(reproj_list[:,0],axis=-1)
	img2_inds = tf.expand_dims(reproj_list[:,1],axis=-1)
	albedo1 = tf.gather_nd(albedos,img1_inds)
	dms1 = tf.gather_nd(dms,img1_inds)
	cams1 = tf.gather_nd(cams,img1_inds)
	albedo2 = tf.gather_nd(albedos,img2_inds)
	cams2 = tf.gather_nd(cams,img2_inds)
	scale_xs1 = tf.gather_nd(scale_xs, img1_inds)
	scale_xs2 = tf.gather_nd(scale_xs, img2_inds)
	scale_ys1 = tf.gather_nd(scale_ys, img1_inds)
	scale_ys2 = tf.gather_nd(scale_ys, img2_inds)

	input1 = tf.gather_nd(inputs, img1_inds)

	# mask_indices contains indices for image index inside batch and spatial locations, and ignores the rgb channel index
	reproj_albedo1, reproj_mask = reproj_layer.map_reproj(dms1,albedo2,cams1,cams2,scale_xs1,scale_xs2,scale_ys1,scale_ys2)

	reproj_albedo1 = reproj_albedo1+tf.constant(1e-4) # numerical stable constant



	### scale intensities for each image
	num_imgs = tf.shape(input=reproj_mask)[0]
	im_ = tf.constant(0)
	output = tf.TensorArray(dtype=tf.float32,size=num_imgs)	

	def body(im_, output):
		reproj_mask_ = reproj_mask[im_]
		albedo1_ = tf.boolean_mask(tensor=albedo1[im_],mask=reproj_mask_)
		reproj_albedo1_ = tf.boolean_mask(tensor=reproj_albedo1[im_],mask=reproj_mask_)


		k = tf.reduce_sum(input_tensor=albedo1_*reproj_albedo1_,keepdims=True)/(tf.reduce_sum(input_tensor=reproj_albedo1_**2,keepdims=True)+tf.constant(1e-4))

		output = output.write(im_,k)
		im_ += tf.constant(1)

		return im_, output

	def condition(im_, output):
		return tf.less(im_,num_imgs)

	_,output = tf.while_loop(cond=condition, body=body, loop_vars=[im_, output])


	ks = tf.expand_dims(output.stack(), axis=-1)



	albedo1_pixels = tf.boolean_mask(tensor=albedo1, mask=reproj_mask)
	reproj_albedo1_pixels = tf.boolean_mask(tensor=reproj_albedo1*ks, mask=reproj_mask)
	reproj_err = tf.compat.v1.losses.mean_squared_error(cvtLab(albedo1_pixels), cvtLab(reproj_albedo1_pixels))


	### formulate loss based on paired batches ###
	# self-supervision based on intensity reconstruction
	shadings, renderings_mask = lambSH_layer.lambSH_layer(tf.ones_like(albedos), normals, lightings, 1.)

	# compare rendering intensity by Lab
	inputs_pixels = cvtLab(tf.boolean_mask(tensor=inputs,mask=renderings_mask))
	renderings = cvtLab(tf.boolean_mask(tensor=tf.pow(albedos*shadings,1./gamma),mask=renderings_mask))
	render_err = tf.compat.v1.losses.mean_squared_error(inputs_pixels,renderings)


	### compute rendering loss from cross-projected alebdo map
	cross_shadings = tf.gather_nd(shadings, img1_inds)
	inputs_pixels = cvtLab(tf.boolean_mask(tensor=input1,mask=reproj_mask))
	cross_renderings = cvtLab(tf.boolean_mask(tensor=tf.pow(tf.nn.relu(cross_shadings*reproj_albedo1*ks), 1./gamma),mask=reproj_mask))
	cross_render_err = tf.compat.v1.losses.mean_squared_error(inputs_pixels,cross_renderings)


	### measure smoothness of albedo map
	Gx = tf.constant(1/2)*tf.expand_dims(tf.expand_dims(tf.constant([[-1,1]], dtype=tf.float32), axis=-1), axis=-1)
	Gy = tf.constant(1/2)*tf.expand_dims(tf.expand_dims(tf.constant([[-1],[1]], dtype=tf.float32), axis=-1), axis=-1)
	Gx_3 = tf.tile(Gx, multiples=(1,1,3,1))	
	Gy_3 = tf.tile(Gy, multiples=(1,1,3,1))	
	albedo_lab = tf.reshape(cvtLab(tf.reshape(albedos,[-1,3])),[-1,200,200,3])

	aGx = tf.nn.conv2d(input=albedos, filters=Gx_3, padding='SAME', strides=(1,1,1,1))
	aGy = tf.nn.conv2d(input=albedos, filters=Gy_3, padding='SAME', strides=(1,1,1,1))
	aGxy = tf.concat([aGx,aGy], axis=-1)


	# compute pixel-wise smoothness weights by angle distance between neighbour pixels' chromaticities
	inputs_pad = tf.pad(tensor=inputs, paddings=tf.constant([[0,0], [0,1], [0,1], [0,0]]))
	chroma_pad = tf.nn.l2_normalize(inputs_pad, axis=-1)

	chroma = chroma_pad[:,:-1,:-1,:]
	chroma_X = chroma_pad[:,:-1,1:,:]
	chroma_Y = chroma_pad[:,1:,:-1,:]
	chroma_Gx = tf.reduce_sum(input_tensor=chroma*chroma_X, axis=-1, keepdims=True)**tf.constant(2.) - tf.constant(1.)
	chroma_Gy = tf.reduce_sum(input_tensor=chroma*chroma_Y, axis=-1, keepdims=True)**tf.constant(2.) - tf.constant(1.)
	chroma_Gx = tf.exp(chroma_Gx / tf.constant(0.0001))
	chroma_Gy = tf.exp(chroma_Gy / tf.constant(0.0001))
	chroma_Gxy = tf.concat([chroma_Gx, chroma_Gy], axis=-1)

	int_pad = tf.reduce_sum(input_tensor=inputs_pad**tf.constant(2.), axis=-1, keepdims=True)
	int = int_pad[:,:-1,:-1,:]
	int_X = int_pad[:,:-1,1:,:]
	int_Y = int_pad[:,1:,:-1,:]

	int_Gx = tf.compat.v1.where(condition=int < int_X, x=int, y=int_X)
	int_Gy = tf.compat.v1.where(condition=int < int_Y, x=int, y=int_Y)
	int_Gx = tf.constant(1.) + tf.exp(- int_Gx / tf.constant(.8))
	int_Gy = tf.constant(1.) + tf.exp(- int_Gy / tf.constant(.8))
	int_Gxy = tf.concat([int_Gx, int_Gy], axis=-1)

	Gxy_weights = int_Gxy * chroma_Gxy
	albedo_smt_error = tf.reduce_mean(input_tensor=tf.abs(aGxy)*Gxy_weights)


	### albedo map pseudo-supervision loss
	if preTrain_flag:
		am_loss = tf.constant(0.)
	else:
		amSup_mask = tf.not_equal(tf.reduce_sum(input_tensor=nm_gt,axis=-1),0)
		am_sup_pixel = cvtLab(tf.boolean_mask(tensor=am_sup, mask=amSup_mask))
		albedos_pixel = cvtLab(tf.boolean_mask(tensor=albedos, mask=amSup_mask))
		am_loss = tf.compat.v1.losses.mean_squared_error(am_sup_pixel, albedos_pixel)



	### regualarisation loss
	reg_loss = sum(tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.REGULARIZATION_LOSSES))


	### compute nm_pred error
	nmSup_mask = tf.not_equal(tf.reduce_sum(input_tensor=nm_gt,axis=-1),0)
	nm_gt_pixel = tf.boolean_mask(tensor=nm_gt, mask=nmSup_mask)
	nm_pred_pixel = tf.boolean_mask(tensor=nm_pred_xyz, mask=nmSup_mask)
	nm_prod = tf.reduce_sum(input_tensor=nm_pred_pixel * nm_gt_pixel, axis=-1, keepdims=True)	
	nm_cosValue = tf.constant(0.9999)
	nm_prod = tf.clip_by_value(nm_prod, -nm_cosValue, nm_cosValue)
	nm_angle = tf.acos(nm_prod) + tf.constant(1e-4)
	nm_loss = tf.reduce_mean(input_tensor=nm_angle**2)



	### compute gradient loss
	nm_pred_Gx = conv2d_nosum(nm_pred_xyz, Gx)
	nm_pred_Gy = conv2d_nosum(nm_pred_xyz, Gy)
	nm_pred_Gxy = tf.concat([nm_pred_Gx, nm_pred_Gy], axis=-1)
	normals_Gx = conv2d_nosum(nm_gt, Gx)
	normals_Gy = conv2d_nosum(nm_gt, Gy)
	normals_Gxy = tf.concat([normals_Gx, normals_Gy], axis=-1)

	nm_pred_smt_error = tf.compat.v1.losses.mean_squared_error(nm_pred_Gxy, normals_Gxy)


	### total loss
	render_err *= tf.constant(.1)
	reproj_err *= tf.constant(.05) * reproj_w_var
	cross_render_err *= tf.constant(.1)
	am_loss *= tf.constant(.1)
	illu_prior_loss *= tf.constant(.01)
	albedo_smt_error *= tf.constant(50.) * am_smt_w_var
	nm_pred_smt_error *= tf.constant(1.)
	nm_loss *= tf.constant(1.)



	if reg_loss_flag == True:
		loss = render_err + reproj_err + cross_render_err + reg_loss + illu_prior_loss + albedo_smt_error + nm_pred_smt_error + nm_loss + am_loss
	else:
		loss = render_err + reproj_err + cross_render_err + illu_prior_loss + albedo_smt_error + nm_pred_smt_error + nm_loss + am_loss

	return lightings, albedos, nm_pred_xyz, loss, render_err, reproj_err, cross_render_err, reg_loss, illu_prior_loss, albedo_smt_error, nm_pred_smt_error, nm_loss, am_loss



# input RGB is 2d tensor with shape (n_pix, 3)
def cvtLab(RGB):

	# threshold definition
	T = tf.constant(0.008856)

	# matrix for converting RGB to LUV color space
	cvt_XYZ = tf.constant([[0.412453,0.35758,0.180423],[0.212671,0.71516,0.072169],[0.019334,0.119193,0.950227]])

	# convert RGB to XYZ
	XYZ = tf.matmul(RGB,tf.transpose(a=cvt_XYZ))

	# normalise for D65 white point
	XYZ /= tf.constant([[0.950456, 1., 1.088754]])*100

	mask = tf.cast(tf.greater(XYZ,T), dtype=tf.float32)

	fXYZ = XYZ**(1/3)*mask + (1.-mask)*(tf.constant(7.787)*XYZ + tf.constant(0.137931))

	M_cvtLab = tf.constant([[0., 116., 0.], [500., -500., 0.], [0., 200., -200.]])

	Lab = tf.matmul(fXYZ, tf.transpose(a=M_cvtLab)) + tf.constant([[-16., 0., 0.]])
	mask = tf.cast(tf.equal(Lab, tf.constant(0.)), dtype=tf.float32)

	Lab += mask * tf.constant(1e-4)

	return Lab





# compute pseudo inverse for input matrix
def pinv(A, reltol=1e-6):
	# compute SVD of input A
	s, u, v = tf.linalg.svd(A)

	# invert s and clear entries lower than reltol*s_max
	atol = tf.reduce_max(input_tensor=s) * reltol
	s = tf.compat.v1.where(s>atol, s, atol*tf.ones_like(s))
	s_inv = tf.linalg.tensor_diag(1./s)

	# compute v * s_inv * u_t as psuedo inverse
	return tf.matmul(v, tf.matmul(s_inv, tf.transpose(a=u)))



# compute regular 2d convolution on 3d data
def conv2d_nosum(input, kernel):
	input_x = input[:,:,:,0:1]
	input_y = input[:,:,:,1:2]
	input_z = input[:,:,:,2:3]

	output_x = tf.nn.conv2d(input=input_x, filters=kernel, strides=(1,1,1,1), padding='SAME')
	output_y = tf.nn.conv2d(input=input_y, filters=kernel, strides=(1,1,1,1), padding='SAME')
	output_z = tf.nn.conv2d(input=input_z, filters=kernel, strides=(1,1,1,1), padding='SAME')

	return tf.concat([output_x,output_y,output_z], axis=-1)



# compute regular 2d convolution on 3d data
def conv2d_nosum_2ch(input, kernel):
	input_x = input[:,:,:,0:1]
	input_y = input[:,:,:,1:2]

	output_x = tf.nn.conv2d(input=input_x, filters=kernel, strides=(1,1,1,1), padding='SAME')
	output_y = tf.nn.conv2d(input=input_y, filters=kernel, strides=(1,1,1,1), padding='SAME')

	return tf.concat([output_x,output_y], axis=-1)








