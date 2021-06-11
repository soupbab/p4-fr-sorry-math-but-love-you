import os
import argparse
import random
from tqdm import tqdm
import csv
import torch
from torch.utils.data import DataLoader
import albumentations as A
from albumentations.pytorch import ToTensorV2

import numpy as np
import pandas as pd

from augmentations import get_valid_transforms
from metrics import word_error_rate, sentence_acc
from checkpoint import load_checkpoint
from dataset import LoadEvalDataset, collate_eval_batch, START, PAD
from flags import Flags
from utils import id_to_string, get_network, get_optimizer, set_seed


def main(parser):
    set_seed(21)
    output_fname = "mysatrn-output-managerv0.csv"

    is_cuda = torch.cuda.is_available()
    hardware = "cuda" if is_cuda else "cpu"
    device = torch.device(hardware)

    # Compose dataset to inference
    dummy_gt = "\sin " * parser.max_sequence
    token_to_id_ = load_checkpoint(parser.checkpoint[0], cuda=is_cuda)["token_to_id"]
    id_to_token_ = load_checkpoint(parser.checkpoint[0], cuda=is_cuda)["id_to_token"]
    root = os.path.join(os.path.dirname(parser.file_path), "images")
    df = pd.read_csv("./configs/data_info.txt")
    test_image_names = set(df[df["fold"] == 0]["image_name"].values)
    with open(os.path.join(os.path.dirname(parser.file_path), "gt.txt"), "r") as fd:
        data = []
        for line in fd:
            data.append(line.strip().split("\t"))
        dataset_len = round(len(data) * 1.0)
        data = data[:dataset_len]
    test_data = [
        [os.path.join(root, x[0]), x[0], dummy_gt]
        for x in data
        if x[0] in test_image_names
    ]

    # 모델 불러오기 & 모델별 데이터로더 할당
    num_models = len(parser.models)
    models = []
    dataloaders = []
    for idx in range(num_models):
        h, w = parser.heights[idx], parser.widths[idx]
        transforms = get_valid_transforms(h, w)
        dataset = LoadEvalDataset(
            test_data,
            token_to_id_,
            id_to_token_,
            crop=False,
            transform=transforms,
            rgb=3,
        )
        dataloader = DataLoader(
            dataset,
            batch_size=parser.batch_size,
            shuffle=False,
            num_workers=8,
            collate_fn=collate_eval_batch,
        )
        checkpoint = load_checkpoint(parser.models[idx], cuda=is_cuda)
        options = Flags(checkpoint["configs"]).get()
        model_checkpoint = checkpoint["model"]
        model = get_network(options.network, options, model_checkpoint, device, dataset)
        model.eval()
        models.append(model)
        dataloaders.append(iter(dataloader))

    print("--------------------------------")
    print("Running {} on device {}\n".format(options.network, device))

    num_iters = len(dataloaders[0])

    results = []
    with torch.no_grad():
        for _ in range(num_iters):
            batches = [next(loader) for loader in dataloaders]

            decoded_values = None
            for b, model in zip(batches, models):
                input = b['image'].to(device).float()
                expected = b['truth']['encoded'].to(device)
                output = model(input, expected, False, 0.0)
                if decoded_values is None:
                    decoded_values = output.transpose(1, 2)
                else:
                    decoded_values += output.transpose(1, 2)

            decoded_values /= len(models)

            _, sequence = torch.topk(decoded_values, 1, dim=1)
            sequence = sequence.squeeze(1)
            sequence_str = id_to_string(sequence, dataloaders[0], do_eval=1)

            for path, predicted in zip(b["file_path"], sequence_str):
                results.append((path, predicted))

        os.makedirs(parser.output_dir, exist_ok=True)
        with open(os.path.join(parser.output_dir, output_fname), "w") as w:
            for path, predicted in results:
                w.write(path + "\t" + predicted + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models",
        default=[
            "./log/my_satrn/checkpoints/0.7907 F0 dual opt MySATRN_best_model.pth"
        ],
        nargs="*",
        help="Path of checkpoint file",
    )
    parser.add_argument(
        "--heights",
        default=[256],
        nargs="*",
        help="Height of input. SATRN(256), SWIN(384), ASTER(256)",
    )
    parser.add_argument(
        "--width",
        default=[512],
        nargs="*",
        help="Height of input. SATRN(512), SWIN(384), ASTER(1024)",
    )
    parser.add_argument(
        "--max_sequence",
        dest="max_sequence",
        default=230,
        type=int,
        help="maximun sequence when doing inference",
    )
    parser.add_argument(
        "--batch_size",
        dest="batch_size",
        default=128,
        type=int,
        help="batch size when doing inference",
    )
    parser.add_argument("--seed", default=21, type=int, help="seed number")

    eval_dir = os.environ.get("SM_CHANNEL_EVAL", "/opt/ml/input/data/")
    file_path = os.path.join(eval_dir, "train_dataset/gt.txt")
    parser.add_argument(
        "--file_path",
        dest="file_path",
        default=file_path,
        type=str,
        help="file path when doing inference",
    )

    output_dir = os.environ.get("SM_OUTPUT_DATA_DIR", "submit")
    parser.add_argument(
        "--output_dir",
        dest="output_dir",
        default=output_dir,
        type=str,
        help="output directory",
    )

    parser = parser.parse_args()
    main(parser)
