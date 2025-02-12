import numpy as np
import os, sys
import random
import tensorflow as tf
import tqdm
import pickle

def get_images(paths, labels, nb_samples=None, shuffle=True):
	if nb_samples is not None:
		sampler = lambda x: random.sample(x, nb_samples)
	else:
		sampler = lambda x: x
	images = [(i, os.path.join(path, image)) \
	          for i, path in zip(labels, paths) \
	          for image in sampler(os.listdir(path))]
	if shuffle:
		random.shuffle(images)
	return images

class DataGenerator:
	"""
	Data Generator capable of generating batches of sinusoid or Omniglot data.
	A "class" is considered a class of omniglot digits or a particular sinusoid function.
	"""

	def __init__(self, nway, kshot, kquery, meta_batchsz, total_batch_num = 200000):
		"""

		:param nway:
		:param kshot:
		:param kquery:
		:param meta_batchsz:
		"""
		self.meta_batchsz = meta_batchsz
		# number of images to sample per class
		self.nimg = kshot + kquery
		self.nway = nway
		self.imgsz = (84, 84)
		self.total_batch_num = total_batch_num
		self.dim_input = np.prod(self.imgsz) * 3 # 21168
		self.dim_output = nway

		metatrain_folder = '/hdd1/liangqu/datasets/miniimagenet/train'
		metaval_folder = '/hdd1/liangqu/datasets/miniimagenet/test'

		self.metatrain_folders = [os.path.join(metatrain_folder, label) \
		                     for label in os.listdir(metatrain_folder) \
		                     if os.path.isdir(os.path.join(metatrain_folder, label)) \
		                     ]
		self.metaval_folders = [os.path.join(metaval_folder, label) \
		                   for label in os.listdir(metaval_folder) \
		                   if os.path.isdir(os.path.join(metaval_folder, label)) \
		                   ]
		self.rotations = [0]


		print('metatrain_folder:', self.metatrain_folders[:2])
		print('metaval_folders:', self.metaval_folders[:2])


	def make_data_tensor(self, training=True):
		"""

		:param training:
		:return:
		"""
		if training:
			folders = self.metatrain_folders
			num_total_batches = self.total_batch_num
		else:
			folders = self.metaval_folders
			num_total_batches = 600


		if training and os.path.exists('filelist.pkl'):

			labels = np.arange(self.nway).repeat(self.nimg).tolist()
			with open('filelist.pkl', 'rb') as f:
				all_filenames = pickle.load(f)
				print('load episodes from file, len:', len(all_filenames))

		else: # test or not existed.

			# 16 in one class, 16*5 in one task
			# [task1_0_img0, task1_0_img15, task1_1_img0,]
			all_filenames = []
			for _ in tqdm.tqdm(range(num_total_batches), 'generating episodes'): # 200000
				# from image folder sample 5 class randomly
				sampled_folders = random.sample(folders, self.nway)
				random.shuffle(sampled_folders)  # YQ: 重复了
				# sample 16 images from selected folders, and each with label 0-4, (0/1..., path), orderly, no shuffle!
				# YQ: 对于当前5-way 1shot 15query而言，5way的label重命名为0~4
				# len: 5 * 16
				labels_and_images = get_images(sampled_folders, range(self.nway), nb_samples=self.nimg, shuffle=False)

				# make sure the above isn't randomized order
				labels = [li[0] for li in labels_and_images]
				filenames = [li[1] for li in labels_and_images]
				all_filenames.extend(filenames)

			if training: # only save for training.
				with open('filelist.pkl', 'wb') as f:
					pickle.dump(all_filenames,f)
					print('save all file list to filelist.pkl')

		# make queue for tensorflow to read from
		print('creating pipeline ops')
		filename_queue = tf.train.string_input_producer(tf.convert_to_tensor(all_filenames), shuffle=False)
		image_reader = tf.WholeFileReader()
		_, image_file = image_reader.read(filename_queue)

		image = tf.image.decode_jpeg(image_file, channels=3)
		# tensorflow format: N*H*W*C
		image.set_shape((self.imgsz[0], self.imgsz[1], 3))
		# reshape(image, [84*84*3])
		image = tf.reshape(image, [self.dim_input])
		# convert to range(0,1)
		image = tf.cast(image, tf.float32) / 255.0

		examples_per_batch = self.nway * self.nimg   # 5*16
		# batch here means batch of meta-learning, including 4 tasks = 4*80
		batch_image_size = self.meta_batchsz * examples_per_batch # 4* 80

		print('batching images')
		images = tf.train.batch(
			[image],
			batch_size=batch_image_size, # 4*80
			num_threads= self.meta_batchsz,
			capacity=   256 + 3 * batch_image_size, # 256 + 3* 4*80
		)

		all_image_batches, all_label_batches = [], []
		print('manipulating images to be right order')
		# images contains current batch, namely 4 task, 4* 80
		for i in range(self.meta_batchsz): # 4
			# current task, 80 images
			image_batch = images[i * examples_per_batch:(i + 1) * examples_per_batch]

			# as all labels of all task are the same, which is 0,0,..1,1,..2,2,..3,3,..4,4...
			label_batch = tf.convert_to_tensor(labels)
			new_list, new_label_list = [], []
			# for each image from 0 to 15 in all 5 class
			for k in range(self.nimg): # 16
				class_idxs = tf.range(0, self.nway) # 0-4
				class_idxs = tf.random_shuffle(class_idxs)
				# it will cope with 5 images parallelly
				#    [0, 16, 32, 48, 64] or [1, 17, 33, 49, 65]
				true_idxs = class_idxs * self.nimg + k
				new_list.append(tf.gather(image_batch, true_idxs))

				new_label_list.append(tf.gather(label_batch, true_idxs))

			# [80, 84*84*3]
			# YQ: 这80张图片，每5张每5张一组，共16组，第1组可为support set，后15组可为query set；
			# 在每组内部，5张图片label为0~4；
			new_list = tf.concat(new_list, 0)  # has shape [self.num_classes*self.num_samples_per_class, self.dim_input]
			# [80]
			new_label_list = tf.concat(new_label_list, 0)
			all_image_batches.append(new_list)
			all_label_batches.append(new_label_list)

		# [4, 80, 84*84*3]
		# YQ: 一个batch内共有4个task，每个task的5-way都是不一样的；
		all_image_batches = tf.stack(all_image_batches)
		# [4, 80]
		all_label_batches = tf.stack(all_label_batches)
		# [4, 80, 5]
		all_label_batches = tf.one_hot(all_label_batches, self.nway)

		print('image_b:', all_image_batches)
		print('label_onehot_b:', all_label_batches)

		return all_image_batches, all_label_batches

