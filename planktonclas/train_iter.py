"""
Training runfile

Date: September 20123
Author: Wout Decrop (based on code from Ignacio Heredia)
Email: wout.decrop@VLIZ.be
Github: woutdecrop/lifewatch

Description:
This file contains the commands for training a convolutional net for image classification for phytoplankton.

Additional notes:
* On the training routine: Preliminary tests show that using a custom lr multiplier for the lower layers yield to better
results than freezing them at the beginning and unfreezing them after a few epochs like it is suggested in the Keras
tutorials.
"""

#TODO List:

#TODO: Implement resuming training
#TODO: Try that everything works out with validation data
#TODO: Try several regularization parameters
#TODO: Add additional metrics for test time in addition to accuracy
#TODO: Implement additional techniques to deal with class imbalance (not only class_weigths)

import os
import time
import json
from datetime import datetime

import numpy as np
import tensorflow as tf
import tensorflow.keras.backend as K
from planktonclas.data_utils import k_crop_data_sequence
from planktonclas.data_utils import load_data_splits, compute_meanRGB, compute_classweights, load_class_names, data_sequence, \
    json_friendly
from planktonclas import paths, config, model_utils, utils
from planktonclas.optimizers import customAdam
# from planktonclas.api import load_previous_inference_model_update

import logging
# from planktonclas.api import load_inference_model

# Set Tensorflow verbosity logs
tf.get_logger().setLevel(logging.ERROR)


# Allow GPU memory growth
gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
    for gpu in gpus:
        tf.config.experimental.set_memory_growth(gpu, True)


def train_fn(TIMESTAMP, CONF):

    paths.timestamp = TIMESTAMP
    paths.CONF = CONF

    utils.create_dir_tree()
    utils.backup_splits()

    # Load the training data
    X_train, y_train = load_data_splits(splits_dir=paths.get_ts_splits_dir(),
                                        im_dir=paths.get_images_dir(),
                                        split_name='train')

    # Load the validation data
    if (CONF['training']['use_validation']) and ('val.txt' in os.listdir(paths.get_ts_splits_dir())):
        X_val, y_val = load_data_splits(splits_dir=paths.get_ts_splits_dir(),
                                        im_dir=paths.get_images_dir(),
                                        split_name='val')
    else:
        print('No validation data.')
        X_val, y_val = None, None
        CONF['training']['use_validation'] = False

        
    # Load the class names
    class_names = load_class_names(splits_dir=paths.get_ts_splits_dir())

    # Update the configuration
    CONF['model']['preprocess_mode'] = model_utils.model_modes[CONF['model']['modelname']]
    CONF['training']['batch_size'] = min(CONF['training']['batch_size'], len(X_train))

    if CONF['model']['num_classes'] is None:
        CONF['model']['num_classes'] = len(class_names)

    assert CONF['model']['num_classes'] >= np.amax(y_train), "Your train.txt file has more categories than those defined in classes.txt"
    if CONF['training']['use_validation']:
        assert CONF['model']['num_classes'] >= np.amax(y_val), "Your val.txt file has more categories than those defined in classes.txt"

    # Compute the class weights
    if CONF['training']['use_class_weights']:
        class_weights = compute_classweights(y_train,
                                             max_dim=CONF['model']['num_classes']).tolist()
        class_weights = {i: weight for i, weight in enumerate(class_weights)}
    else:
        class_weights = None

    # Compute the mean and std RGB values
    if CONF['dataset']['mean_RGB'] is None:
        CONF['dataset']['mean_RGB'], CONF['dataset']['std_RGB'] = compute_meanRGB(X_train)
    
    # print("mean RGB: ", compute_meanRGB(X_train))
    
    #Create data generator for train and val sets
    train_gen = data_sequence(X_train, y_train,
                              batch_size=CONF['training']['batch_size'],
                              num_classes=CONF['model']['num_classes'],
                              im_size=CONF['model']['image_size'],
                              mean_RGB=CONF['dataset']['mean_RGB'],
                              std_RGB=CONF['dataset']['std_RGB'],
                              preprocess_mode=CONF['model']['preprocess_mode'],
                              aug_params=CONF['augmentation']['train_mode'])
    train_steps = int(np.ceil(len(X_train)/CONF['training']['batch_size']))

    if CONF['training']['use_validation']:
        val_gen = data_sequence(X_val, y_val,
                                batch_size=CONF['training']['batch_size'],
                                num_classes=CONF['model']['num_classes'],
                                im_size=CONF['model']['image_size'],
                                mean_RGB=CONF['dataset']['mean_RGB'],
                                std_RGB=CONF['dataset']['std_RGB'],
                                preprocess_mode=CONF['model']['preprocess_mode'],
                                aug_params=CONF['augmentation']['val_mode'])
        val_steps = int(np.ceil(len(X_val)/CONF['training']['batch_size']))
    else:
        val_gen = None
        val_steps = None

    # Launch the training
    t0 = time.time()

    # Create the model and compile it
    model, base_model = model_utils.create_model(CONF)

    # Get a list of the top layer variables that should not be applied a lr_multiplier
    base_vars = [var.name for var in base_model.trainable_variables]
    all_vars = [var.name for var in model.trainable_variables]
    top_vars = set(all_vars) - set(base_vars)
    top_vars = list(top_vars)

    # Set trainable layers
    if CONF['training']['mode'] == 'fast':
        for layer in base_model.layers:
            layer.trainable = False
    
    # model=load_previous_inference_model_update(timestamp="2023-12-21_152322", ckpt_name="update_this.h5",old_timestamp=TIMESTAMP)
    
    model.compile(optimizer=customAdam(lr=CONF['training']['initial_lr'],
                                        amsgrad=True,
                                        lr_mult=0.1,
                                        excluded_vars=top_vars
                                        ),
                    loss='categorical_crossentropy',
                    metrics=['accuracy'])

    history = model.fit_generator(generator=train_gen,
                              steps_per_epoch=train_steps,
                              epochs=CONF['training']['epochs'],
                              class_weight=class_weights,
                              validation_data=val_gen,
                              validation_steps=val_steps,
                              callbacks=utils.get_callbacks(CONF),
                              verbose=1, max_queue_size=5, workers=4,
                              use_multiprocessing=CONF['training']['use_multiprocessing'],
                              initial_epoch=0)

    # Saving everything
    print('Saving data to {} folder.'.format(paths.get_timestamped_dir()))
    print('Saving training stats ...')
    stats = {'epoch': history.epoch,
             'training time (s)': round(time.time()-t0, 2),
             'timestamp': TIMESTAMP}
    stats.update(history.history)
    stats = json_friendly(stats)
    stats_dir = paths.get_stats_dir()
    with open(os.path.join(stats_dir, 'stats.json'), 'w') as outfile:
        json.dump(stats, outfile, sort_keys=True, indent=4)
            

    print('Saving the configuration ...')
    model_utils.save_conf(CONF)

    print('Saving the model to h5...')
    fpath = os.path.join(paths.get_checkpoints_dir(), 'final_model.h5')
    model.save(fpath,
               include_optimizer=False)

    # print('Saving the model to protobuf...')
    # fpath = os.path.join(paths.get_checkpoints_dir(), 'final_model.proto')
    # model_utils.save_to_pb(model, fpath)

    print('Finished')

    if CONF['testing']['use_test']:

        X_test, y_test = load_data_splits(splits_dir=paths.get_ts_splits_dir(),
                                        im_dir=paths.get_images_dir(),
                                        split_name='test')
        crop_num=10
        filemode='local'
        test_gen = k_crop_data_sequence(inputs=X_test,
                                    im_size=CONF['model']['image_size'],
                                    mean_RGB=CONF['dataset']['mean_RGB'],
                                    std_RGB=CONF['dataset']['std_RGB'],
                                    preprocess_mode=CONF['model']['preprocess_mode'],
                                    aug_params=CONF['augmentation']['val_mode'],
                                    crop_mode='random',
                                    crop_number=crop_num,
                                    filemode=filemode)
        top_K=5

        output = model.predict(test_gen,
                                  verbose=1, max_queue_size=10, workers=4,
                                  use_multiprocessing=CONF['training']['use_multiprocessing'])

        output = output.reshape(len(X_test), -1, output.shape[-1])  # reshape to (N, crop_number, num_classes)
        output = np.mean(output, axis=1)  # take the mean across the crops

        lab = np.argsort(output, axis=1)[:, ::-1]  # sort labels in descending prob
        lab = lab[:, :top_K]  # keep only top_K labels
        prob = output[np.repeat(np.arange(len(lab)), lab.shape[1]),
        lab.flatten()].reshape(lab.shape)  # retrieve corresponding probabilities

        pred_lab, pred_prob = lab, prob
        # Save the predictions
        pred_dict = {'filenames': list(X_test),
                     'pred_lab': pred_lab.tolist(),
                     'pred_prob': pred_prob.tolist()}
        if y is not None:
            pred_dict['true_lab'] = y_test.tolist()

        pred_path = os.path.join(paths.get_predictions_dir(), '{}+{}+top{}.json'.format('final_model.h5', 'premade_split', top_K))
        with open(pred_path, 'w') as outfile:
            json.dump(pred_dict, outfile, sort_keys=True)
    print("finished")
    
if __name__ == '__main__':

    CONF = config.get_conf_dict()
    timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')

    train_fn(TIMESTAMP=timestamp, CONF=CONF)
