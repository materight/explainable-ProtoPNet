import os
import re
from argparse import Namespace
import numpy as np
import torch
from torch.autograd import Variable
import torchvision.transforms as transforms
import torchvision.datasets as datasets
from PIL import Image

from .helpers import makedir
from .log import create_logger
from .preprocess import mean, std
from .local_analysis import save_preprocessed_img

def run_analysis(args: Namespace):
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpus

    # Compute params
    img_path = os.path.abspath(args.img)  # ./datasets/celeb_a/gender/test/Male/1.jpg
    img_class, img_name = re.split(r'\\|/', img_path)[-2:]

    model_path = os.path.abspath(args.model)  # ./saved_models/vgg19/003/checkpoints/10_18push0.7822.pth
    model_base_architecture, experiment_run, _, model_name = re.split(r'\\|/', model_path)[-4:]
    start_epoch_number = int(re.search(r'\d+', model_name).group(0))

    save_analysis_path = os.path.join(args.out, model_base_architecture, experiment_run, model_name, 'alignment', img_class, img_name)
    makedir(save_analysis_path)
    log, logclose = create_logger(log_filename=os.path.join(save_analysis_path, 'local_analysis.log'))

    log(f'\nLoad model from: {args.model}')
    log(f'Model epoch: {start_epoch_number}')
    log(f'Model base architecture: {model_base_architecture}')
    log(f'Experiment run: {experiment_run}\n')

    ppnet = torch.load(args.model)
    ppnet = ppnet.cuda()
    ppnet_multi = torch.nn.DataParallel(ppnet)

    img_size = ppnet_multi.module.img_size
    prototype_shape = ppnet.prototype_shape
    max_dist = prototype_shape[1] * prototype_shape[2] * prototype_shape[3]
    normalize = transforms.Normalize(mean=mean, std=std)
    dataset = datasets.ImageFolder(os.path.join(os.path.dirname(img_path), '..'))

    # SANITY CHECK
    # confirm prototype class identity
    load_img_dir = os.path.join(os.path.dirname(args.model), '..', 'img')
    assert os.path.exists(load_img_dir), f'Folder "{load_img_dir}" does not exist'
    prototype_info = np.load(os.path.join(load_img_dir, f'epoch-{start_epoch_number}', 'bb.npy'))  # For each prototype: (prototype_original_image_id, height_start, height_end, width_start, width_end, prototype_class)
    prototype_img_identity = prototype_info[:, -1]
    log('Prototypes are chosen from ' + str(len(set(prototype_img_identity))) + ' classes')

    # confirm prototype connects most strongly to its own class
    prototype_max_connection = torch.argmax(ppnet.last_layer.weight, dim=0)
    prototype_max_connection = prototype_max_connection.cpu().numpy()
    if np.sum(prototype_max_connection == prototype_img_identity) == ppnet.num_prototypes:
        log('All prototypes connect strongly to their respective classes\n')
    else:
        log('WARNING: Not all prototypes connect most strongly to their respective classes\n')

    # load the test image and forward it through the network
    preprocess = transforms.Compose([
        transforms.Resize((img_size, img_size)),
        transforms.ToTensor(),
        normalize
    ])

    img_pil = Image.open(args.img)
    img_tensor = preprocess(img_pil)
    img_variable = Variable(img_tensor.unsqueeze(0))

    images_test = img_variable.cuda()
    labels_test = torch.tensor([ dataset.class_to_idx[img_class] ])

    logits, min_distances = ppnet_multi(images_test)
    _, distances = ppnet.push_forward(images_test)
    prototype_activations = ppnet.distance_2_similarity(min_distances)
    prototype_activation_patterns = ppnet.distance_2_similarity(distances)
    if ppnet.prototype_activation_function == 'linear':
        prototype_activations = prototype_activations + max_dist
        prototype_activation_patterns = prototype_activation_patterns + max_dist

    tables = []
    for i in range(logits.size(0)):
        tables.append((torch.argmax(logits, dim=1)[i].item(), labels_test[i].item()))

    idx = 0
    predicted_cls = tables[idx][0]
    correct_cls = tables[idx][1]
    log('Predicted class: ' + str(predicted_cls))
    log('Correct class: ' + str(correct_cls) + '\n')
    original_img = save_preprocessed_img(os.path.join(save_analysis_path, 'original_img.png'), images_test, idx)

    # MOST ACTIVATED (NEAREST) 10 PROTOTYPES OF THIS IMAGE
    array_act, sorted_indices_act = torch.sort(prototype_activations[idx])
    for i in tqdm(range(1, args.top_prototypes + 1), desc='Computing most activated prototypes'):
        out_dir = os.path.join(save_analysis_path, 'most_activated_prototypes', f'top-{i}')
        makedir(out_dir)
        save_prototype(load_img_dir, os.path.join(out_dir, 'prototype_patch.png'), start_epoch_number, sorted_indices_act[-i].item())
        save_prototype_original_img_with_bbox(
            load_img_dir=load_img_dir,
            fname=os.path.join(out_dir, 'prototype_bbox.png'),
            epoch=start_epoch_number,
            index=sorted_indices_act[-i].item(),
            bbox_height_start=prototype_info[sorted_indices_act[-i].item()][1],
            bbox_height_end=prototype_info[sorted_indices_act[-i].item()][2],
            bbox_width_start=prototype_info[sorted_indices_act[-i].item()][3],
            bbox_width_end=prototype_info[sorted_indices_act[-i].item()][4],
            color=(0, 255, 255)
        )
        save_prototype_self_activation(load_img_dir, os.path.join(out_dir, 'prototype_activation.png'), start_epoch_number, sorted_indices_act[-i].item())
        with open(os.path.join(out_dir, 'info.txt'), 'w') as f:
            f.write('prototype index: {0}\n'.format(sorted_indices_act[-i].item()))
            f.write('prototype class: {0}\n'.format(prototype_img_identity[sorted_indices_act[-i].item()]))
            if prototype_max_connection[sorted_indices_act[-i].item()] != prototype_img_identity[sorted_indices_act[-i].item()]:
                f.write('prototype connection: {0}\n'.format(prototype_max_connection[sorted_indices_act[-i].item()]))
            f.write('activation value (similarity score): {0:.4f}\n'.format(array_act[-i]))
            f.write('last layer connection with predicted class: {0:.4f}\n'.format(ppnet.last_layer.weight[predicted_cls][sorted_indices_act[-i].item()]))
        activation_pattern = prototype_activation_patterns[idx][sorted_indices_act[-i].item()].detach().cpu().numpy()
        upsampled_activation_pattern = cv2.resize(activation_pattern, dsize=(img_size, img_size), interpolation=cv2.INTER_CUBIC)

        # show the most highly activated patch of the image by this prototype
        high_act_patch_indices = find_high_activation_crop(upsampled_activation_pattern)
        high_act_patch = original_img[high_act_patch_indices[0]:high_act_patch_indices[1], high_act_patch_indices[2]:high_act_patch_indices[3], :]
        plt.axis('off')
        plt.imsave(os.path.join(out_dir, 'target_patch.png'), high_act_patch)
        imsave_with_bbox(fname=os.path.join(out_dir, 'target_bbox.png'),
                         img_rgb=original_img,
                         bbox_height_start=high_act_patch_indices[0],
                         bbox_height_end=high_act_patch_indices[1],
                         bbox_width_start=high_act_patch_indices[2],
                         bbox_width_end=high_act_patch_indices[3], color=(0, 255, 255))
        # show the image overlayed with prototype activation map
        rescaled_activation_pattern = upsampled_activation_pattern - np.amin(upsampled_activation_pattern)
        rescaled_activation_pattern = rescaled_activation_pattern / np.amax(rescaled_activation_pattern)
        heatmap = cv2.applyColorMap(np.uint8(255*rescaled_activation_pattern), cv2.COLORMAP_JET)
        heatmap = np.float32(heatmap) / 255
        heatmap = heatmap[..., ::-1]
        overlayed_img = 0.5 * original_img + 0.3 * heatmap
        plt.axis('off')
        plt.imsave(os.path.join(out_dir, 'target_activations.png'), overlayed_img)

    # PROTOTYPES FROM TOP-k CLASSES
    k = args.top_classes
    assert k < len(dataset.classes), 'k must be less than the number of available classes'
    log('Prototypes from top-%d classes:' % k)
    topk_logits, topk_classes = torch.topk(logits[idx], k=k)
    for i, c in enumerate(topk_classes.detach().cpu().numpy()):
        class_dir = os.path.join(save_analysis_path, 'class_prototypes', f'top-{i+1}_class')
        makedir(class_dir)
        class_prototype_indices = np.nonzero(ppnet.prototype_class_identity.detach().cpu().numpy()[:, c])[0]
        class_prototype_activations = prototype_activations[idx][class_prototype_indices]
        _, sorted_indices_cls_act = torch.sort(class_prototype_activations)
        prototype_cnt = 1
        reversed_indices = list(reversed(sorted_indices_cls_act.detach().cpu().numpy()))
        for j in tqdm(reversed_indices, desc=f'Computing prototypes of top-{i+1} class'):
            prototype_dir = os.path.join(class_dir, f'top-{prototype_cnt}_prototype')
            makedir(prototype_dir)
            prototype_index = class_prototype_indices[j]
            save_prototype(load_img_dir, os.path.join(prototype_dir, 'prototype_patch.png'), start_epoch_number, prototype_index)
            save_prototype_original_img_with_bbox(
                load_img_dir=load_img_dir,
                fname=os.path.join(prototype_dir, 'prototype_bbox.png'),
                epoch=start_epoch_number,
                index=prototype_index,
                bbox_height_start=prototype_info[prototype_index][1],
                bbox_height_end=prototype_info[prototype_index][2],
                bbox_width_start=prototype_info[prototype_index][3],
                bbox_width_end=prototype_info[prototype_index][4],
                color=(0, 255, 255)
            )
            save_prototype_self_activation(load_img_dir, os.path.join(prototype_dir, 'prototype_activation.png'), start_epoch_number, prototype_index)
            with open(os.path.join(prototype_dir, 'info.txt'), 'w') as f:
                f.write('prototype index: {0}\n'.format(prototype_index))
                f.write('prototype class: {0}\n'.format(prototype_img_identity[prototype_index]))
                f.write('prototype class logits: {0:.4f}\n'.format(topk_logits[i]))
                if prototype_max_connection[prototype_index] != prototype_img_identity[prototype_index]:
                    f.write('prototype connection: {0}\n'.format(prototype_max_connection[prototype_index]))
                f.write('activation value (similarity score): {0:.4f}\n'.format(prototype_activations[idx][prototype_index]))
                f.write('last layer connection: {0:.4f}\n'.format(ppnet.last_layer.weight[c][prototype_index]))

            activation_pattern = prototype_activation_patterns[idx][prototype_index].detach().cpu().numpy()
            upsampled_activation_pattern = cv2.resize(activation_pattern, dsize=(img_size, img_size), interpolation=cv2.INTER_CUBIC)

            # show the most highly activated patch of the image by this prototype
            high_act_patch_indices = find_high_activation_crop(upsampled_activation_pattern)
            high_act_patch = original_img[high_act_patch_indices[0]:high_act_patch_indices[1], high_act_patch_indices[2]:high_act_patch_indices[3], :]
            plt.axis('off')
            plt.imsave(os.path.join(prototype_dir, 'target_patch.png'), high_act_patch)
            imsave_with_bbox(fname=os.path.join(prototype_dir, 'target_bbox.png'),
                             img_rgb=original_img,
                             bbox_height_start=high_act_patch_indices[0],
                             bbox_height_end=high_act_patch_indices[1],
                             bbox_width_start=high_act_patch_indices[2],
                             bbox_width_end=high_act_patch_indices[3], color=(0, 255, 255))
            # show the image overlayed with prototype activation map
            rescaled_activation_pattern = upsampled_activation_pattern - np.amin(upsampled_activation_pattern)
            rescaled_activation_pattern = rescaled_activation_pattern / np.amax(rescaled_activation_pattern)
            heatmap = cv2.applyColorMap(np.uint8(255*rescaled_activation_pattern), cv2.COLORMAP_JET)
            heatmap = np.float32(heatmap) / 255
            heatmap = heatmap[..., ::-1]
            overlayed_img = 0.5 * original_img + 0.3 * heatmap
            plt.axis('off')
            plt.imsave(os.path.join(prototype_dir, 'target_activation.png'), overlayed_img)
            prototype_cnt += 1

    if predicted_cls == correct_cls:
        log('Prediction is correct.')
    else:
        log('Prediction is wrong.')

    logclose()