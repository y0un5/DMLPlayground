from __future__ import division

import logging

import mxnet as mx
import numpy as np
from mxnet import autograd as ag
from tqdm import tqdm

from common.evaluate import evaluate
from common.parser import TrainingParser
from common.utils import average_results, get_lr, get_context, format_results, parse_steps, append_postfix
from dataset import get_npairs_iterators, get_dataset
from dataset.dataloader import DatasetIterator
from models import get_feature_model, EmbeddingNet
from models.loss import NPairsLoss


def parse_args():
    parser = TrainingParser(description='Distance metric learning using n-pairs',
                            default_logfile='train_npairs.log',
                            default_model_prefix='npairs_model')
    parser.add_argument('--batch-size', type=int, default=128,
                        help='Number of samples in a batch, this equals to 2N. Default is 128')
    parser.add_argument('--epochs', type=int, default=25,
                        help='number of training epochs. default is 25.')
    parser.add_argument('--epoch-length', type=int, default=200,
                        help='Number of iterations per epoch. Default is 200.')
    parser.add_argument('--lr', type=float, default=0.0001,
                        help='learning rate. default is 0.0001.')
    parser.add_argument('--factor', type=float, default=0.5,
                        help='learning rate schedule factor. default is 0.5.')
    parser.add_argument('--wd', type=float, default=0.00001,
                        help='weight decay rate. default is 0.00001.')
    parser.add_argument('--l2reg-weight', type=float, default=0.0005,
                        help='Weight of L2 regularization for feature vectors. Default is 0.25 * 0.002')
    parser.add_argument('--steps', type=str, default='12,14,16,18',
                        help='epochs to update learning rate. default is 12,14,16,18.')
    parser.add_argument('--same-image-sampling', type=float, default=0.1,
                        help='Chance to sample both items from the same image. Default is 0.9')
    parser.add_argument('--symmetric-loss', action="store_true",
                        help='Use symmetric loss.')
    parser.add_argument('--decrease-cnn-lr', action="store_true",
                        help='Use a lower LR on the feature extractor')

    opt = parser.parse_args()

    if opt.logfile.lower() != 'none':
        logging.basicConfig(filename=append_postfix(opt.logfile, opt.log_postfix), level=logging.INFO)
        logging.getLogger().addHandler(logging.StreamHandler())

    return opt


def validate(net, val_data, ctx, use_threads=True):
    """Test a model."""
    outputs = []
    labels = []
    ctx_cpu = mx.cpu()

    for batch in tqdm(val_data, desc='Computing test embeddings'):
        data = mx.gluon.utils.split_and_load(batch[0], ctx_list=ctx, batch_axis=0, even_split=False)
        label = mx.gluon.utils.split_and_load(batch[1], ctx_list=ctx, batch_axis=0, even_split=False)

        for x in data:
            outputs.append(net(x).as_in_context(ctx_cpu))
        labels += [l.as_in_context(ctx_cpu) for l in label]

    outputs = mx.nd.concatenate(outputs, axis=0)
    labels = mx.nd.concatenate(labels, axis=0)
    return evaluate(outputs, labels, val_data._dataset.num_classes(), use_threads=use_threads)


def train(net, opt, train_dataloader, val_dataloader, context, run_id):
    """Training function."""

    if not opt.skip_pretrain_validation:
        validation_results = validate(net, val_dataloader, context, use_threads=opt.num_workers > 0)
        for name, val_acc in validation_results:
            logging.info('Pre-train validation: %s=%f' % (name, val_acc))

    steps = parse_steps(opt.steps, opt.epochs, logging)

    opt_options = {'learning_rate': opt.lr, 'wd': opt.wd}
    if opt.optimizer == 'sgd':
        opt_options['momentum'] = 0.9
    if opt.optimizer == 'adam':
        opt_options['epsilon'] = 1e-7

    if opt.decrease_cnn_lr:
        logging.info('Setting embedding LR to %f' % (10.0 * opt.lr))
        for p, v in net.encoder.collect_params().items():
            v.lr_mult = 10.0

    trainer = mx.gluon.Trainer(net.collect_params(), opt.optimizer, opt_options, kvstore=opt.kvstore)

    L = NPairsLoss(l2_reg=opt.l2reg_weight, symmetric=opt.symmetric_loss)
    if not opt.disable_hybridize:
        L.hybridize()

    smoothing_constant = .01  # for tracking moving losses
    moving_loss = 0
    best_results = []  # R@1, NMI

    for epoch in range(1, opt.epochs + 1):
        p_bar = tqdm(enumerate(train_dataloader), total=len(train_dataloader), desc=('[Run %d/%d] Epoch %d' % (run_id, opt.number_of_runs, epoch)))
        trainer.set_learning_rate(get_lr(opt.lr, epoch, steps, opt.factor))

        for i, batch in p_bar:
            anchors_batch = batch[0][0]  # <N x I>
            positives_batch = batch[1][0]  # <N x I>

            anchors = mx.gluon.utils.split_and_load(anchors_batch, ctx_list=context, batch_axis=0)
            positives = mx.gluon.utils.split_and_load(positives_batch, ctx_list=context, batch_axis=0)
            labels_batch = mx.gluon.utils.split_and_load(batch[2][0], ctx_list=context, batch_axis=0)
            anchor_embs = []
            positives_embs = []

            with ag.record():
                for a, p in zip(anchors, positives):
                    a_emb = net(a)
                    p_emb = net(p)
                    anchor_embs.append(a_emb)
                    positives_embs.append(p_emb)
                anchors = mx.nd.concat(*anchor_embs, dim=0)
                positives = mx.nd.concat(*positives_embs, dim=0)

                loss = L(anchors, positives, labels_batch[0])

            loss.backward()
            trainer.step(opt.batch_size / 2)

            curr_loss = mx.nd.mean(loss).asscalar()
            moving_loss = (curr_loss if ((i == 0) and (epoch == 1))  # starting value
                           else (1 - smoothing_constant) * moving_loss + smoothing_constant * curr_loss)  # add current
            p_bar.set_postfix_str('Moving loss: %.4f' % moving_loss)

        validation_results = validate(net, val_dataloader, context, use_threads=opt.num_workers > 0)
        for name, val_acc in validation_results:
            logging.info('[Epoch %d] validation: %s=%f' % (epoch, name, val_acc))

        if (len(best_results) == 0) or (validation_results[0][1] > best_results[0][1]):
            best_results = validation_results
            if opt.save_model_prefix.lower() != 'none':
                filename = '%s.params' % opt.save_model_prefix
                logging.info('Saving %s.' % filename)
                net.save_parameters(filename)
            logging.info('New best validation: R@1: %f NMI: %f' % (best_results[0][1], best_results[-1][1]))

    return best_results


def train_npairs(opt):
    logging.info(opt)

    # Settings.
    mx.random.seed(opt.seed)
    np.random.seed(opt.seed)

    # Setup computation context
    context = get_context(opt.gpus, logging)

    run_results = []

    # Get model
    if opt.model == 'inception-bn':
        feature_net, feature_params = get_feature_model(opt.model, ctx=context)
        feature_net.collect_params().load(feature_params, ctx=context, ignore_extra=True)
        data_shape = 224
        scale_image_data = False
    elif opt.model == 'resnet50_v2':
        feature_net = mx.gluon.model_zoo.vision.resnet50_v2(pretrained=True, ctx=context).features
        data_shape = 224
        scale_image_data = True
    else:
        raise RuntimeError('Unsupported model: %s' % opt.model)

    net = EmbeddingNet(feature_net, opt.embed_dim, normalize=False)

    if opt.model == 'resnet50_v2':
        # Use a smaller learning rate for pre-trained convolutional layers.
        for v in net.base_net.collect_params().values():
            if 'conv' in v.name:
                setattr(v, 'lr_mult', 0.01)

    # Get iterators
    train_data, _ = get_npairs_iterators(opt.dataset, opt.data_path, batch_size=opt.batch_size,
                                         data_shape=data_shape,
                                         test_batch_size=len(context) * 32, use_crops=opt.use_crops,
                                         scale_image_data=scale_image_data,
                                         same_image_sampling=opt.same_image_sampling)

    train_it_dataloader = mx.gluon.data.DataLoader(DatasetIterator(train_data, opt.epoch_length), batch_size=1,
                                                   shuffle=False, num_workers=opt.num_workers,
                                                   last_batch='rollover')

    _, val_dataset = get_dataset(opt.dataset, opt.data_path, data_shape=data_shape, use_crops=opt.use_crops,
                                 use_aug=True, scale_image_data=scale_image_data)
    val_data = mx.gluon.data.DataLoader(val_dataset, batch_size=opt.batch_size, shuffle=False,
                                        num_workers=opt.num_workers, last_batch='keep')

    for run in range(1, opt.number_of_runs + 1):
        logging.info('Starting run %d/%d' % (run, opt.number_of_runs))

        net.init(mx.init.Xavier(magnitude=0.2), ctx=context, init_basenet=False)

        if opt.model == 'inception-bn':
            net.base_net.collect_params().load(feature_params, ctx=context, ignore_extra=True)
        elif opt.model == 'resnet50_v2':
            net.base_net = mx.gluon.model_zoo.vision.resnet50_v2(pretrained=True, ctx=context).features
        else:
            raise RuntimeError('Unsupported model: %s' % opt.model)
        if not opt.disable_hybridize:
            net.hybridize()

        run_result = train(net, opt, train_it_dataloader, val_data, context, run)
        run_results.append(run_result)
        logging.info('Run %d finished with %f' % (run, run_result[0][1]))

    logging.info(
        'Average validation of %d runs:\n%s' % (opt.number_of_runs, format_results(average_results(run_results))))


if __name__ == '__main__':
    train_npairs(parse_args())
