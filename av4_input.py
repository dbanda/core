import tensorflow as tf
from glob import glob
import os,time
from try106 import random_transition_matrix,affine_transform


def index_the_database(database_path):
    """indexes av4 database and returns two tensors of filesystem path: ligand files, and protein files"""
    ligand_file_list = []
    receptor_file_list = []
    for ligand_file in glob(os.path.join(database_path, "*_ligand.av4")):
        receptor_file = "/".join(ligand_file.split("/")[:-1]) + "/" + ligand_file.split("/")[-1][:4] + '.av4'
        if os.path.exists(receptor_file):
            ligand_file_list.append(ligand_file)
            receptor_file_list.append(receptor_file)

    index_list = range(len(ligand_file_list))
    return index_list,ligand_file_list, receptor_file_list

def read_receptor_and_ligand(filename_queue):
    """Reads ligand and protein from the filename queue. Returns tensors with coordinates
    and atoms of ligand and protein for future processing.
    Important: by default it does oversampling of the positive examples based on training epoch."""

    # FIXME: epoch counter won't increment unless sess.run() is called on it explicitly

    def decode_av4(serialized_record):
        # decode everything into int32
        tmp_decoded_record = tf.decode_raw(serialized_record, tf.int32)
        # first four bytes determine the number of frames
        number_of_frames = tf.slice(tmp_decoded_record, [0], [1])
        # labels are saved as in32 * number of frames in the record
        labels = tf.slice(tmp_decoded_record, [1], number_of_frames)
        # elements are saved as int32 and their number is == to the number of atoms
        number_of_atoms = ((tf.shape(tmp_decoded_record) - number_of_frames - 1) / (3 * number_of_frames + 1))
        elements = tf.slice(tmp_decoded_record, number_of_frames + 1, number_of_atoms)

        # coordinates are saved as a stack of X,Y,Z where the first(vertical) dimension
        # corresponds to the number of atoms
        # second (horizontal dimension) is x,y,z coordinate of every atom and is always 3
        # third (depth) dimension corresponds to the number of frames

        coords_shape = tf.concat(0, [number_of_atoms, [3], number_of_frames])
        tmp_coords = tf.slice(tmp_decoded_record, number_of_frames + number_of_atoms + 1,
                              tf.shape(tmp_decoded_record) - number_of_frames - number_of_atoms - 1)
        multiframe_coords = tf.bitcast(tf.reshape(tmp_coords, coords_shape), type=tf.float32)

        return labels,elements,multiframe_coords

    # read raw bytes of the ligand and receptor
    idx = filename_queue[0]
    serialized_ligand = tf.read_file(filename_queue[1])
    serialized_receptor = tf.read_file(filename_queue[2])

    # create an epoch counter
    epoch_counter = tf.Variable(0,tf.int32)
    def incr_epoch(): return epoch_counter+1
    def keep_epoch(): return epoch_counter
    epoch_counter = epoch_counter.assign(tf.cond(tf.equal(idx,0),incr_epoch,keep_epoch))

    # decode bytes into meaningful tensors
    ligand_labels, ligand_elements, multiframe_ligand_coords = decode_av4(serialized_ligand)
    receptor_labels, receptor_elements, multiframe_receptor_coords = decode_av4(serialized_receptor)

    # if the index of the examle is even, positive label is taken every even epoch
    # if the index of the example is odd, positive label is taken every odd epoch
    # current negative example increments once every two epochs, and slides along all of the negative examples

    def select_pos_frame(): return tf.constant(0)
    def select_neg_frame(): return tf.mod(tf.div(1+epoch_counter,2), tf.shape(ligand_labels) - 1) +1
    current_frame = tf.cond(tf.equal(tf.mod(epoch_counter+idx+1,2),1),select_pos_frame,select_neg_frame)

    # FIXME: why would gather sometimes return 3d and sometimes 2d array (?)
    # TODO: break on certain epoch
    ligand_coords = tf.squeeze(tf.gather(tf.transpose(multiframe_ligand_coords, perm=[2, 0, 1]),current_frame))
    label = tf.gather(ligand_labels,current_frame)
    receptor_coords = tf.squeeze(multiframe_receptor_coords)

    return tf.squeeze(current_frame),tf.squeeze(label),ligand_elements, ligand_coords, receptor_elements, receptor_coords


def convert_protein_and_ligand_to_image(ligand_elements,ligand_coords,receptor_elements,receptor_coords,side_pixels,pixel_size):
    """Take coordinates and elements of protein and ligand and convert them into an image.
    Return image with one dimension so far."""

    # FIXME abandon ligand when it does not fit (it's kept now)
    # TODO check if indeed it breaks in the last iteration cycle when a good affine transform is found

    # transform center ligand around zero
    ligand_center_of_mass = tf.reduce_mean(ligand_coords, reduction_indices=0)
    centered_ligand_coords = ligand_coords - ligand_center_of_mass
    centered_receptor_coords = receptor_coords - ligand_center_of_mass

    def generate_transition_matrix(attempt, transition_matrix):
        """Takes initial coordinates of the ligand, generates a random affine transform matrix and transforms coordinates."""
        transition_matrix = random_transition_matrix()
        attempt += 1
        return attempt, transition_matrix

    def not_all_in_the_box(attempt, transition_matrix, ligand_coords=centered_ligand_coords,box_size=(tf.cast(side_pixels,tf.float32)*pixel_size)):
        """Takes affine transform matrix and box dimensions, performs the transformation, and checks if all atoms
        are in the box."""
        transformed_coords, transition_matrix = affine_transform(ligand_coords, transition_matrix)

        not_all = tf.cast(tf.reduce_max(tf.cast(tf.square(box_size*0.5) - tf.square(transformed_coords) < 0,tf.int32)),tf.bool)

        within_iteration_limit = tf.cast(tf.reduce_sum(tf.cast(attempt < 1000, tf.float32)), tf.bool)
        return tf.logical_and(within_iteration_limit, not_all)

    attempt = tf.Variable(tf.constant(0, shape=[1]))
    transition_matrix = random_transition_matrix()

    last_attempt,final_transition_matrix = tf.while_loop(not_all_in_the_box, generate_transition_matrix, [attempt, transition_matrix],
                           parallel_iterations=5)

    # rotate receptor and ligand using affine transform found
    rotatated_ligand_coords,_ = affine_transform(centered_ligand_coords,final_transition_matrix)
    rotated_receptor_coords,_ = affine_transform(centered_receptor_coords,final_transition_matrix)

    # move coordinates of a complex to an integer number so as to put every atom on a grid
    # ceiled coords is an integer number out of real coordinates that corresponds to the index on the cell
    ceiled_ligand_coords = tf.cast(tf.round(-0.5 + (tf.cast(side_pixels,tf.float32)*0.5) + rotatated_ligand_coords),tf.int64)
    ceiled_receptor_coords = tf.cast(tf.round(-0.5 + (tf.cast(side_pixels, tf.float32) * 0.5) + rotated_receptor_coords),tf.int64)

    # crop atoms of the protein that do not fit inside the box
    top_filter = tf.reduce_max(ceiled_receptor_coords,reduction_indices=1)<side_pixels
    bottom_filter = tf.reduce_min(ceiled_receptor_coords,reduction_indices=1)>0
    retain_atoms = tf.logical_and(top_filter,bottom_filter)
    cropped_receptor_coords = tf.boolean_mask(ceiled_receptor_coords,retain_atoms)
    cropped_receptor_elements = tf.boolean_mask(receptor_elements,retain_atoms)

    # merge protein and ligand together. In this case an arbitrary value of 10 is added to the ligand
    complex_coords = tf.concat(0,[ceiled_ligand_coords,cropped_receptor_coords])
    complex_elements = tf.concat(0,[ligand_elements+10,cropped_receptor_elements])

    sparse_complex = tf.SparseTensor(indices=complex_coords, values=complex_elements,shape=[side_pixels,side_pixels,side_pixels])
    dense_complex = tf.sparse_tensor_to_dense(sparse_complex, validate_indices=False)
    # FIXME: sparse_tensor_to_dense has not been properly tested.
    # FIXME: I may need to sort indices according to TF's manual on the function
    # FIXME: try to save an image and see how it looks like

    return dense_complex


def image_and_label_queue(sess,batch_size,pixel_size,side_pixels,num_threads,database_path):

    # TODO: add epoch counter
    # create a list of files in the database
    index_list,ligand_file_list,receptor_file_list = index_the_database(database_path)

    # create a filename queue (tensor) with the names of the ligand and receptors
    index_tensor = tf.convert_to_tensor(index_list,dtype=tf.int32)
    ligand_files = tf.convert_to_tensor(ligand_file_list,dtype=tf.string)
    receptor_files = tf.convert_to_tensor(receptor_file_list,dtype=tf.string)

    filename_queue = tf.train.slice_input_producer([index_tensor,ligand_files,receptor_files],num_epochs=None,shuffle=True)

    # read one receptor and stack of ligands; choose one of the ligands from the stack according to the epoch
    current_frame,label,ligand_elements,ligand_coords,receptor_elements,receptor_coords = read_receptor_and_ligand(filename_queue)

    # convert coordinates of ligand and protein into an image
    dense_image = convert_protein_and_ligand_to_image(ligand_elements,ligand_coords,receptor_elements,receptor_coords,side_pixels,pixel_size)

    # create a batch of proteins and ligands to read them together
    # FIXME: TF likely a bug in TF - I can't group both init ops
    # selectively initialize some of the variables
    uninitialized_vars = []
    for var in tf.all_variables():
        try:
            sess.run(var)
        except tf.errors.FailedPreconditionError:
            uninitialized_vars.append(var)

    init_new_vars_op = tf.initialize_variables(uninitialized_vars)
    sess.run(init_new_vars_op)

    multithread_batch = tf.train.batch([current_frame,label,dense_image],batch_size,num_threads=num_threads,capacity=batch_size*3,shapes=[[],[],[side_pixels,side_pixels,side_pixels]])


    return multithread_batch
