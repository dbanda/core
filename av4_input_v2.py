import tensorflow as tf
from glob import glob
import os,time
from av4_utils import generate_deep_affine_transform,affine_transform
multiframe_num = 100


def index_the_database_into_queue(database_path, shuffle):
    """Indexes av4 database and returns two lists of filesystem path: ligand files, and protein files.
    Ligands are assumed to end with _ligand.av4, proteins should be in the same folders with ligands.
    Each protein should have its own folder named similarly to the protein name (in the PDB)."""
    # TODO controls epochs here
    ligand_file_list = []
    receptor_file_list = []
    # for the ligand it's necessary and sufficient to have an underscore in it's name
    print "number of ligands:", len(glob(os.path.join(database_path + '/**/**/', "*[_]*.av4")))

    for ligand_file in glob(os.path.join(database_path + '/**/**/', "*[_]*.av4")):
        receptor_file = "/".join(ligand_file.split("/")[:-1]) + "/" + ligand_file.split("/")[-1][:4] + '.av4'
        if os.path.exists(receptor_file):
            ligand_file_list.append(ligand_file)
            receptor_file_list.append(receptor_file)
        else:

            # TODO: remove another naming system from Xiao's scripts                #
            receptor_file = os.path.join(os.path.dirname(ligand_file),
                                         os.path.basename(ligand_file).split("_")[0] + '.av4')
            if os.path.exists(receptor_file):  # remove later
                ligand_file_list.append(ligand_file)  #
                receptor_file_list.append(receptor_file)  #

    index_list = range(len(ligand_file_list))
    examples_in_database = len(index_list)

    if examples_in_database == 0:
        raise Exception('av4_input: No files found in the database path:', database_path)
    print "Indexed ligand-protein pairs in the database:", examples_in_database

    # create a filename queue (tensor) with the names of the ligand and receptors
    index_tensor = tf.convert_to_tensor(index_list, dtype=tf.int32)
    ligand_files = tf.convert_to_tensor(ligand_file_list, dtype=tf.string)
    receptor_files = tf.convert_to_tensor(receptor_file_list, dtype=tf.string)

    filename_queue = tf.train.slice_input_producer([index_tensor, ligand_files, receptor_files], num_epochs=None,
                                                   shuffle=shuffle)
    return filename_queue, examples_in_database


def read_receptor_and_multiframe_ligand(filename_queue, epoch_counter):
    def decode_av4(serialized_record):
        # decode everything into int32
        tmp_decoded_record = tf.decode_raw(serialized_record, tf.int32)
        # first four bytes describe the number of frames in a record
        number_of_frames = tf.slice(tmp_decoded_record, [0], [1])
        # labels are saved as int32 * number of frames in the record
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

        return labels, elements, multiframe_coords

    idx = filename_queue[0]
    ligand_file = filename_queue[1]
    serialized_ligand = tf.read_file(ligand_file)
    serialized_receptor = tf.read_file(filename_queue[2])

    ligand_labels, ligand_elements, multiframe_ligand_coords = decode_av4(serialized_ligand)
    receptor_labels, receptor_elements, multiframe_receptor_coords = decode_av4(serialized_receptor)

    
    
    # select some frame of ligands, if don't have enough frame, repeat it
    select_range = tf.range(0, multiframe_num)
    select_frame = tf.mod(select_range,tf.shape(ligand_elements)[0])
    multiple_ligand_coords = tf.gather(tf.transpose(multiframe_ligand_coords,perm=[2,0,1]),select_frame)
    labels = tf.gather(ligand_labels,select_frame)

    return ligand_file, tf.squeeze(epoch_counter), tf.squeeze(labels), ligand_elements, tf.squeeze(
        multiple_ligand_coords), receptor_elements, tf.squeeze(multiframe_receptor_coords)


def convert_protein_and_ligand_to_image(ligand_elements, multiple_ligand_coords, receptor_elements, receptor_coords, side_pixels,
                                        pixel_size,index):
    """Take coordinates and elements of protein and ligand and convert them into an image.
    Return image with one dimension so far."""

    # FIXME abandon ligand when it does not fit into the box (it's kept now)

    # max_num_attempts - maximum number of affine transforms for the ligand to be tried
    max_num_attemts = 1000
    # affine_transform_pool_size is the first(batch) dimension of tensor of transition matrices to be returned
    # affine tranform pool is only generated once in the beginning of training and randomly sampled afterwards
    affine_transform_pool_size = 10000
    ligand_coords = tf.gather(multiple_ligand_coords,index)
    # transform center ligand around zero
    ligand_center_of_mass = tf.reduce_mean(ligand_coords, reduction_indices=0)
    centered_ligand_coords = ligand_coords - ligand_center_of_mass
    centered_receptor_coords = receptor_coords - ligand_center_of_mass

    # use TF while loop to find such an affine transform matrix that can fit the ligand so that no atoms are outside
    box_size = (tf.cast(side_pixels, tf.float32) * pixel_size)

    def generate_transition_matrix(attempt, transition_matrix, batch_of_transition_matrices):
        """Takes initial coordinates of the ligand, generates a random affine transform matrix and transforms coordinates."""
        transition_matrix = tf.gather(batch_of_transition_matrices,
                                      tf.random_uniform([], minval=0, maxval=affine_transform_pool_size,
                                                        dtype=tf.int32))
        attempt += 1
        return attempt, transition_matrix, batch_of_transition_matrices

    def not_all_in_the_box(attempt, transition_matrix, batch_of_transition_matrices,
                           ligand_coords=centered_ligand_coords, box_size=box_size, max_num_attempts=max_num_attemts):
        """Takes affine transform matrix and box dimensions, performs the transformation, and checks if all atoms
        are in the box."""
        transformed_coords, transition_matrix = affine_transform(ligand_coords, transition_matrix)
        not_all = tf.cast(
            tf.reduce_max(tf.cast(tf.square(box_size * 0.5) - tf.square(transformed_coords) < 0, tf.int32)), tf.bool)
        within_iteration_limit = tf.cast(tf.reduce_sum(tf.cast(attempt < max_num_attemts, tf.float32)), tf.bool)
        return tf.logical_and(within_iteration_limit, not_all)

    attempt = tf.Variable(tf.constant(0, shape=[1]))
    batch_of_transition_matrices = tf.Variable(generate_deep_affine_transform(affine_transform_pool_size))

    transition_matrix = tf.gather(batch_of_transition_matrices,
                                  tf.random_uniform([], minval=0, maxval=affine_transform_pool_size, dtype=tf.int64))

    last_attempt, final_transition_matrix, _ = tf.while_loop(not_all_in_the_box, generate_transition_matrix,
                                                             [attempt, transition_matrix, batch_of_transition_matrices],
                                                             parallel_iterations=1)

    # rotate receptor and ligand using an affine transform matrix found
    rotatated_ligand_coords, _ = affine_transform(centered_ligand_coords, final_transition_matrix)
    rotated_receptor_coords, _ = affine_transform(centered_receptor_coords, final_transition_matrix)


    # check if all of the atoms are in the box, if not set the ligand to 0, but do not raise an error
    def set_elements_coords_zero(): return tf.constant([0], dtype=tf.int32), tf.constant([[0, 0, 0]], dtype=tf.float32)

    def keep_elements_coords(): return ligand_elements, rotatated_ligand_coords

    not_all = tf.cast(
        tf.reduce_max(tf.cast(tf.square(box_size * 0.5) - tf.square(rotatated_ligand_coords) < 0, tf.int32)), tf.bool)
    ligand_elements, rotatated_ligand_coords = tf.case({tf.equal(not_all, tf.constant(True)): set_elements_coords_zero},
                                                       keep_elements_coords,exclusive=True)

    indicate = tf.case({tf.equal(not_all,tf.constant(True)):lambda :tf.constant(True)},lambda :tf.constant(False),exclusive=True)

    # move coordinates of a complex to an integer number so as to put every atom on a grid
    # ceiled coords is an integer number out of real coordinates that corresponds to the index on the cell
    # epsilon - potentially, there might be very small rounding errors leading to additional indexes
    epsilon = tf.constant(0.999, dtype=tf.float32)
    ceiled_ligand_coords = tf.cast(
        tf.round((-0.5 + (tf.cast(side_pixels, tf.float32) * 0.5) + (rotatated_ligand_coords / pixel_size)) * epsilon),
        tf.int64)
    ceiled_receptor_coords = tf.cast(
        tf.round((-0.5 + (tf.cast(side_pixels, tf.float32) * 0.5) + (rotated_receptor_coords / pixel_size)) * epsilon),
        tf.int64)

    # crop atoms of the protein that do not fit inside the box
    top_filter = tf.reduce_max(ceiled_receptor_coords, reduction_indices=1) < side_pixels
    bottom_filter = tf.reduce_min(ceiled_receptor_coords, reduction_indices=1) > 0
    retain_atoms = tf.logical_and(top_filter, bottom_filter)
    cropped_receptor_coords = tf.boolean_mask(ceiled_receptor_coords, retain_atoms)
    cropped_receptor_elements = tf.boolean_mask(receptor_elements, retain_atoms)

    # merge protein and ligand together. In this case an arbitrary value of 10 is added to the ligand
    complex_coords = tf.concat(0, [ceiled_ligand_coords, cropped_receptor_coords])
    complex_elements = tf.concat(0, [ligand_elements + 7, cropped_receptor_elements])

    # in coordinates of a protein rounded to the nearest integer can be represented as indices of a sparse 3D tensor
    # values from the atom dictionary can be represented as values of a sparse tensor
    # in this case TF's sparse_tensor_to_dense can be used to generate an image out of rounded coordinates


    # move elemets to the dimension of depth
    complex_coords_4d = tf.concat(1,
                                  [complex_coords, tf.reshape(tf.ones(tf.shape(complex_elements),dtype=tf.int64)*index, [-1, 1])])
    sparse_image_4d = tf.SparseTensor(indices=complex_coords_4d, values=complex_elements,
                                      shape=[side_pixels, side_pixels, side_pixels,multiframe_num])

    # FIXME: try to save an image and see how it looks like
    return sparse_image_4d, ligand_center_of_mass, final_transition_matrix,indicate


def image_and_label_queue(batch_size, pixel_size, side_pixels, num_threads, filename_queue, epoch_counter):

    ligand_file, current_epoch, labels, ligand_elements, multiple_ligand_coords, receptor_elements, receptor_coords = read_receptor_and_multiframe_ligand(
        filename_queue,epoch_counter=epoch_counter)

    frames = []
    masks = []

    for i in range(multiframe_num):
        sparse_image,_,_,indicate = convert_protein_and_ligand_to_image(ligand_elements, multiple_ligand_coords, receptor_elements,
                                                                receptor_coords, side_pixels, pixel_size,i)
        frames.append(sparse_image)
        masks.append(indicate)


    #multiframe_batch = tf.boolean_mask(tensor=frames,mask=masks)
    #image_4d = tf.sparse_concat(-1,multiframe_batch)

    label = tf.cast(tf.greater(tf.reduce_sum(labels),0),tf.int32)

    #return ligand_file,current_epoch,label,image_4d
    return ligand_file,current_epoch,label,frames,masks
